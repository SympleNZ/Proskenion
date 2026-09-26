"""Backup destinations: local, USB and the network (SMB/SFTP) (spec §13.3, contracts §2-§3).

Three destinations, one small interface (:class:`BackupDestination`):
``available()``, ``write()``, ``delete()``, ``list_names()``. Retention days
differ per destination (§13.3, Q4) — 14 local, 7 on the USB, 30 on the
network — and that policy lives in :mod:`proskenion.core.backup`, which is
the only caller that knows the archive index; this module only moves bytes.

**Local** (``/srv/local``) and **USB** (``/mnt/backup``) are both plain
filesystem copies — :class:`FilesystemDestination` — but the USB adds two
things the local disk does not need: a mount check (removable media is
routinely absent, §4.5) and capacity eviction (Q4: *"the USB stick checks
capacity first and evicts images, never archives"*). :func:`evict_images_for_space`
only ever removes files matching :data:`IMAGE_GLOB` — the system-image
capture owns these — and never touches anything matching
:data:`ARCHIVE_GLOB`; an archive is pruned only by age
(:mod:`proskenion.core.backup`), never to make room.

**The network destination** is SMB (``smbprotocol``) or SFTP (``asyncssh``),
in userland with no root mount (Q19, §13.3) — the appliance never runs
``mount.cifs`` or an autofs unit. Both clients are synchronous or
callback-driven at the wire level, so every call here runs in a worker
thread or is awaited through the library's own asyncio integration and nothing
blocks the event loop (§5.3).

**Credentials.** SMB authenticates with a username and password, encrypted
with the device secret the way every other §6.10 credential is
(:mod:`proskenion.db.crud.backup`). SFTP authenticates with a key pair
*generated on this device* — :func:`generate_sftp_keypair_if_missing` — whose
private half never leaves ``<state_dir>/backup-sftp-key`` (mode 0600, the
same protection as the device secret and the JWT signing key) and whose
public half is served at ``GET /system/backup/sftp-key`` for Simon to
install on the NAS's ``authorized_keys``. There is no password to store for
SFTP, and nothing here ever logs or returns the private key.

Host key verification for the SFTP destination is deliberately not
implemented: the contract gives no host-key pinning UI, and the destination
is reachable only on the school network (the same trust boundary as the SMTP
relay, phase-6 plan Q1/Q22). This is a real gap for a future phase, not a
silent one — it is called out in this module's tests and in the task report.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol

import asyncssh
import smbclient
import smbclient.path as smbclient_path
import smbclient.shutil as smbclient_shutil
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from smbprotocol.exceptions import SMBException

log = logging.getLogger(__name__)

DestinationName = Literal["local", "usb", "network"]
Protocol_ = Literal["smb", "sftp"]

#: §13.3, Q4: 14 days local, 7 on the USB, 30 on the network.
RETENTION_DAYS: Final[dict[DestinationName, int]] = {"local": 14, "usb": 7, "network": 30}

#: §2.3's p6, "local backup copies, system images". The partition root is
#: root's; appliance/image/build.sh makes the two directories below it
#: application-owned, and those are the only places the application (and
#: auditorium-backup.service, which runs as it) can write. On 24 September
#: 2026 the nightly job wrote to the root itself and failed with EACCES.
LOCAL_PARTITION: Final = Path("/srv/local")
#: The "local" archive destination (§13.1, §13.3: 14 days).
DEFAULT_LOCAL_BACKUPS_DIR: Final = LOCAL_PARTITION / "backups"
#: Captured system images and their image-keys/ sidecar (§13.6: retain 3).
DEFAULT_LOCAL_IMAGES_DIR: Final = LOCAL_PARTITION / "images"
DEFAULT_USB_MOUNT: Final = Path("/mnt/backup")

#: What :func:`evict_images_for_space` may remove, and what it must never touch.
IMAGE_GLOB: Final = "*.img.gz"
ARCHIVE_GLOB: Final = "auditorium-*.tar.zst*"  # the archive and its .sha256 sidecar

#: A margin above the archive's own size, so a write does not leave the USB
#: at exactly zero free bytes.
CAPACITY_HEADROOM_BYTES: Final = 64 * 1024 * 1024  # 64 MiB

SFTP_KEY_FILENAME: Final = "backup-sftp-key"
SFTP_PUBLIC_KEY_FILENAME: Final = "backup-sftp-key.pub"
SFTP_KEY_COMMENT: Final = "proskenion-backup"


class DestinationError(Exception):
    """A destination could not be written, read or listed. Safe to log; never a secret."""


class DestinationUnavailable(DestinationError):
    """The destination is not reachable right now — absent media, no network route, a
    refused connection. §4.5/§13.4: this is *skipped*, not a hard failure."""


class BackupDestination(Protocol):
    """What :mod:`proskenion.core.backup` needs from every destination."""

    name: DestinationName

    async def available(self) -> bool: ...
    async def write(self, local_path: Path, filename: str) -> None: ...
    async def delete(self, filename: str) -> None: ...
    async def list_names(self) -> list[str]: ...
    async def read(self, filename: str, local_path: Path) -> None: ...


# -- local and USB: plain filesystem copies -------------------------------------------


class FilesystemDestination:
    """A destination that is just a directory — local storage or a mounted USB stick."""

    def __init__(self, name: DestinationName, root: Path) -> None:
        self.name = name
        self.root = root

    async def available(self) -> bool:
        return await asyncio.to_thread(self.root.is_dir)

    async def write(self, local_path: Path, filename: str) -> None:
        await asyncio.to_thread(self._write_sync, local_path, filename)

    def _write_sync(self, local_path: Path, filename: str) -> None:
        destination = self.root / filename
        tmp = destination.with_suffix(destination.suffix + ".tmp")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            # filename is a flat basename for every existing caller (an
            # archive, an image); the image-keys/*.pub sidecar
            # (contracts §"what a captured image carries") is the first to
            # nest one, so the destination's own parent is made too rather
            # than assuming it is always self.root.
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(local_path, tmp)
            tmp.replace(destination)
        except PermissionError as exc:
            # A freshly formatted USB stick's root is owned root:root, which
            # the application cannot write into — the CM5, 24-25 September
            # 2026. The raw OSError's str() is "[Errno 13] Permission denied:
            # '/mnt/backup/....tmp'", which reads like a traceback and names
            # nothing the operator can act on; say what is actually wrong.
            tmp.unlink(missing_ok=True)
            raise DestinationError(
                f"{self.root} is not writable by the application (permission denied); "
                "check that it is owned by the application user"
            ) from exc
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise DestinationError(f"could not write {filename} to {self.root}: {exc}") from exc

    async def delete(self, filename: str) -> None:
        await asyncio.to_thread((self.root / filename).unlink, True)  # missing_ok

    async def list_names(self) -> list[str]:
        def _list() -> list[str]:
            if not self.root.is_dir():
                return []
            return [p.name for p in self.root.iterdir() if p.is_file()]

        return await asyncio.to_thread(_list)

    async def read(self, filename: str, local_path: Path) -> None:
        source = self.root / filename
        if not source.is_file():
            raise DestinationError(f"{filename} is not present at {self.root}")
        await asyncio.to_thread(shutil.copyfile, source, local_path)


def is_mounted(path: Path) -> bool:
    """Whether ``path`` is a mount point. Absent or unreadable counts as absent (§4.5)."""
    try:
        return path.is_mount()
    except (OSError, ValueError):
        return False


class UsbDestination(FilesystemDestination):
    """The removable backup stick: absent is normal, and it evicts images before
    archives ever compete for its space (Q4)."""

    def __init__(self, root: Path = DEFAULT_USB_MOUNT) -> None:
        super().__init__("usb", root)

    async def available(self) -> bool:
        return await asyncio.to_thread(is_mounted, self.root)

    async def make_room(self, needed_bytes: int) -> int:
        """Evict images until ``needed_bytes`` plus headroom is free. Never touches
        an archive. Returns the number of files removed."""
        return await asyncio.to_thread(
            evict_images_for_space, self.root, needed_bytes + CAPACITY_HEADROOM_BYTES
        )

    async def write(self, local_path: Path, filename: str) -> None:
        size = await asyncio.to_thread(lambda: local_path.stat().st_size)
        await self.make_room(size)
        await super().write(local_path, filename)


def evict_images_for_space(root: Path, needed_free_bytes: int) -> int:
    """Delete the oldest ``*.img.gz`` files under ``root`` until ``needed_free_bytes``
    is free, or there are none left to remove. Blocking; call through a thread.

    Never removes anything matching :data:`ARCHIVE_GLOB` — the USB "checks
    capacity first and evicts images, never archives" (Q4).
    """
    if not root.is_dir():
        return 0
    try:
        free = shutil.disk_usage(root).free
    except OSError:
        return 0
    if free >= needed_free_bytes:
        return 0
    images = sorted(root.glob(IMAGE_GLOB), key=lambda p: p.stat().st_mtime)
    removed = 0
    for image in images:
        if free >= needed_free_bytes:
            break
        try:
            size = image.stat().st_size
            image.unlink()
        except OSError as exc:
            log.warning("could not evict %s for space: %s", image, exc)
            continue
        free += size
        removed += 1
        log.info("evicted %s to make room on the backup USB", image.name)
    return removed


# -- the network destination: SMB or SFTP ----------------------------------------------


@dataclass(frozen=True, slots=True)
class SmbConfig:
    host: str
    port: int
    share_path: str  # "SHARE" or "SHARE\\subdir"
    username: str
    password: str  # plain — decrypted by the caller before this is built


@dataclass(frozen=True, slots=True)
class SftpConfig:
    host: str
    port: int
    remote_dir: str
    username: str
    private_key_path: Path


class SmbDestination:
    """SMB, via ``smbprotocol``'s ``smbclient`` module (Q19: no root mount)."""

    name: DestinationName = "network"

    def __init__(self, config: SmbConfig) -> None:
        self._config = config

    def _unc(self, filename: str = "") -> str:
        share = self._config.share_path.strip("\\/").replace("/", "\\")
        base = f"\\\\{self._config.host}\\{share}"
        return f"{base}\\{filename}" if filename else base

    def _session_kwargs(self) -> dict[str, object]:
        return {
            "username": self._config.username,
            "password": self._config.password,
            "port": self._config.port,
        }

    async def available(self) -> bool:
        try:
            await asyncio.to_thread(self._probe_sync)
        except (SMBException, OSError) as exc:
            log.info("SMB destination unavailable: %s", exc)
            return False
        return True

    def _probe_sync(self) -> None:
        smbclient.register_session(self._config.host, **self._session_kwargs())
        smbclient_path.isdir(self._unc())

    async def write(self, local_path: Path, filename: str) -> None:
        try:
            await asyncio.to_thread(self._write_sync, local_path, filename)
        except (SMBException, OSError) as exc:
            raise DestinationError(f"could not write {filename} over SMB: {exc}") from exc

    def _write_sync(self, local_path: Path, filename: str) -> None:
        smbclient.register_session(self._config.host, **self._session_kwargs())
        smbclient_shutil.copyfile(str(local_path), self._unc(filename))

    async def delete(self, filename: str) -> None:
        try:
            await asyncio.to_thread(self._delete_sync, filename)
        except (SMBException, OSError) as exc:
            raise DestinationError(f"could not delete {filename} over SMB: {exc}") from exc

    def _delete_sync(self, filename: str) -> None:
        smbclient.register_session(self._config.host, **self._session_kwargs())
        path = self._unc(filename)
        if smbclient_path.isfile(path):
            smbclient.remove(path)

    async def list_names(self) -> list[str]:
        try:
            return await asyncio.to_thread(self._list_sync)
        except (SMBException, OSError) as exc:
            raise DestinationError(f"could not list the SMB destination: {exc}") from exc

    def _list_sync(self) -> list[str]:
        smbclient.register_session(self._config.host, **self._session_kwargs())
        return [
            entry.name
            for entry in smbclient.scandir(self._unc())
            if entry.is_file()
        ]

    async def read(self, filename: str, local_path: Path) -> None:
        try:
            await asyncio.to_thread(self._read_sync, filename, local_path)
        except (SMBException, OSError) as exc:
            raise DestinationError(f"could not read {filename} over SMB: {exc}") from exc

    def _read_sync(self, filename: str, local_path: Path) -> None:
        smbclient.register_session(self._config.host, **self._session_kwargs())
        smbclient_shutil.copyfile(self._unc(filename), str(local_path))


class SftpDestination:
    """SFTP, via ``asyncssh``, authenticating with the key this device generated."""

    name: DestinationName = "network"

    def __init__(self, config: SftpConfig) -> None:
        self._config = config

    def _connect(self) -> AbstractAsyncContextManager[asyncssh.SSHClientConnection]:
        # asyncssh.connect() is both an awaitable and an async context manager
        # (its @async_context_manager decorator); only the latter shape is
        # used here, so that is what this method's return type promises.
        return asyncssh.connect(
            self._config.host,
            port=self._config.port,
            username=self._config.username,
            client_keys=[str(self._config.private_key_path)],
            # The NAS is reachable only on the school network (Q1/Q22); the
            # contract gives no host-key pinning UI, so verification is
            # deliberately not implemented here — see the module docstring.
            known_hosts=None,
        )

    async def available(self) -> bool:
        try:
            async with self._connect() as conn:
                async with conn.start_sftp_client() as sftp:
                    return await sftp.isdir(self._config.remote_dir)
        except (asyncssh.Error, OSError) as exc:
            log.info("SFTP destination unavailable: %s", exc)
            return False

    async def write(self, local_path: Path, filename: str) -> None:
        try:
            async with self._connect() as conn:
                async with conn.start_sftp_client() as sftp:
                    if not await sftp.isdir(self._config.remote_dir):
                        await sftp.makedirs(self._config.remote_dir, exist_ok=True)
                    remote = f"{self._config.remote_dir.rstrip('/')}/{filename}"
                    tmp = f"{remote}.tmp"
                    await sftp.put(str(local_path), tmp)
                    await sftp.rename(tmp, remote)
        except (asyncssh.Error, OSError) as exc:
            raise DestinationError(f"could not write {filename} over SFTP: {exc}") from exc

    async def delete(self, filename: str) -> None:
        try:
            async with self._connect() as conn:
                async with conn.start_sftp_client() as sftp:
                    remote = f"{self._config.remote_dir.rstrip('/')}/{filename}"
                    if await sftp.exists(remote):
                        await sftp.remove(remote)
        except (asyncssh.Error, OSError) as exc:
            raise DestinationError(f"could not delete {filename} over SFTP: {exc}") from exc

    async def list_names(self) -> list[str]:
        try:
            async with self._connect() as conn:
                async with conn.start_sftp_client() as sftp:
                    names = await sftp.listdir(self._config.remote_dir)
                    return [n for n in names if n not in (".", "..")]
        except (asyncssh.Error, OSError) as exc:
            raise DestinationError(f"could not list the SFTP destination: {exc}") from exc

    async def read(self, filename: str, local_path: Path) -> None:
        try:
            async with self._connect() as conn:
                async with conn.start_sftp_client() as sftp:
                    remote = f"{self._config.remote_dir.rstrip('/')}/{filename}"
                    await sftp.get(remote, str(local_path))
        except (asyncssh.Error, OSError) as exc:
            raise DestinationError(f"could not read {filename} over SFTP: {exc}") from exc


# -- the SFTP key pair, generated on the device -----------------------------------------


def sftp_key_paths(state_dir: Path) -> tuple[Path, Path]:
    return state_dir / SFTP_KEY_FILENAME, state_dir / SFTP_PUBLIC_KEY_FILENAME


def generate_sftp_keypair_if_missing(state_dir: Path) -> Path:
    """Create the backup destination's Ed25519 key pair unless it exists.

    Mirrors :func:`proskenion.core.secrets.generate_secret_if_missing`: the
    private half is written with mode 0600 and never leaves this file.
    Returns the private key path.
    """
    private_path, public_path = sftp_key_paths(state_dir)
    if private_path.exists() and public_path.exists():
        return private_path
    state_dir.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    private_bytes = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    public_bytes = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    )
    _write_private(private_path, private_bytes)
    public_path.write_bytes(public_bytes + f" {SFTP_KEY_COMMENT}\n".encode("ascii"))
    return private_path


def _write_private(path: Path, payload: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def sftp_public_key_text(state_dir: Path) -> str:
    """The public half only — for ``GET /system/backup/sftp-key``. Generates the
    pair first if it does not exist yet, so the endpoint always has an answer."""
    generate_sftp_keypair_if_missing(state_dir)
    _, public_path = sftp_key_paths(state_dir)
    return public_path.read_text(encoding="ascii")


__all__ = [
    "ARCHIVE_GLOB",
    "DEFAULT_LOCAL_BACKUPS_DIR",
    "DEFAULT_LOCAL_IMAGES_DIR",
    "DEFAULT_USB_MOUNT",
    "IMAGE_GLOB",
    "RETENTION_DAYS",
    "BackupDestination",
    "DestinationError",
    "DestinationName",
    "DestinationUnavailable",
    "FilesystemDestination",
    "SftpConfig",
    "SftpDestination",
    "SmbConfig",
    "SmbDestination",
    "UsbDestination",
    "evict_images_for_space",
    "generate_sftp_keypair_if_missing",
    "is_mounted",
    "sftp_key_paths",
    "sftp_public_key_text",
]
