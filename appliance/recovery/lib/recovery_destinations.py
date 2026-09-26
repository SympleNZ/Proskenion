"""Fetching a backup archive from the network, standalone (§13.3, §13.7).

"Restore data from a backup archive found on ... the network" means reaching
the same SMB or SFTP destination
`proskenion.core.backup_destinations` writes nightly backups to. This module
talks the same two protocols — `smbprotocol`'s `smbclient` module for SMB,
`asyncssh` for SFTP — with the same calling conventions, but stands alone
rather than importing that module, for the same reason `recovery_archive.py`
does not import `backup_restore`: this file is built into a minimal recovery
image, and pulling in `proskenion.core.backup_destinations` would pull in the
device-secret-encrypted credential store and the async destination protocol
that exist to serve a running appliance, not a bare-metal recovery tool.

Both clients are installed into the recovery image's own virtual environment
at build time (`appliance/recovery/build.sh`), the same two vendored
dependencies Q19 already approved for the main application.

Both functions are synchronous from the caller's point of view — the recovery
web app is a small Flask application, not an asyncio one, and a one-shot
blocking fetch during a guided recovery is not a place that needs
concurrency. `asyncssh` is awaited through one `asyncio.run` per call.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path


class DestinationError(Exception):
    """The network destination could not be reached, listed or read."""


@dataclass(frozen=True, slots=True)
class SmbConfig:
    host: str
    share_path: str
    username: str
    password: str
    port: int = 445


@dataclass(frozen=True, slots=True)
class SftpConfig:
    host: str
    remote_dir: str
    username: str
    private_key_path: Path
    port: int = 22


#: What counts as a backup archive on either destination — the archive itself
#: and its checksum sidecar, contracts §8's naming.
_ARCHIVE_PREFIX = "auditorium-"


def is_archive_name(name: str) -> bool:
    """Whether `name` is a backup archive or its sidecar (not a system image)."""
    return name.startswith(_ARCHIVE_PREFIX) and (
        name.endswith(".tar.zst") or name.endswith(".tar.zst.sha256")
    )


def latest_archive_name(names: list[str]) -> str | None:
    """The most recent archive's base name (without `.sha256`) from a directory listing.

    Archive filenames are `auditorium-YYYYMMDD-HHMM.tar.zst` (contracts §8) —
    lexical order is chronological order, so the greatest name is the most
    recent capture. Pure and separately testable from the two protocols that
    produce the listing.
    """
    archives = sorted(name for name in names if name.endswith(".tar.zst") and is_archive_name(name))
    return archives[-1] if archives else None


# --------------------------------------------------------------------------
# SMB
# --------------------------------------------------------------------------


def _smb_unc(config: SmbConfig, filename: str = "") -> str:
    share = config.share_path.strip("\\/").replace("/", "\\")
    base = f"\\\\{config.host}\\{share}"
    return f"{base}\\{filename}" if filename else base


def list_smb(config: SmbConfig) -> list[str]:
    import smbclient
    from smbprotocol.exceptions import SMBException

    try:
        smbclient.register_session(
            config.host, username=config.username, password=config.password, port=config.port
        )
        return [entry.name for entry in smbclient.scandir(_smb_unc(config)) if entry.is_file()]
    except (SMBException, OSError) as exc:
        raise DestinationError(f"could not list the SMB share: {exc}") from exc


def fetch_smb(config: SmbConfig, filename: str, destination: Path) -> Path:
    import smbclient
    import smbclient.shutil as smbclient_shutil
    from smbprotocol.exceptions import SMBException

    try:
        smbclient.register_session(
            config.host, username=config.username, password=config.password, port=config.port
        )
        smbclient_shutil.copyfile(_smb_unc(config, filename), str(destination))
    except (SMBException, OSError) as exc:
        raise DestinationError(f"could not fetch {filename} over SMB: {exc}") from exc
    return destination


# --------------------------------------------------------------------------
# SFTP
# --------------------------------------------------------------------------


async def _list_sftp_async(config: SftpConfig) -> list[str]:
    import asyncssh

    async with asyncssh.connect(
        config.host,
        port=config.port,
        username=config.username,
        client_keys=[str(config.private_key_path)],
        known_hosts=None,  # school-network-only, as backup_destinations.py's SftpDestination
    ) as conn, conn.start_sftp_client() as sftp:
        entries = await sftp.readdir(config.remote_dir)
        return [entry.filename for entry in entries if entry.filename not in (".", "..")]


async def _fetch_sftp_async(config: SftpConfig, filename: str, destination: Path) -> None:
    import asyncssh

    async with asyncssh.connect(
        config.host,
        port=config.port,
        username=config.username,
        client_keys=[str(config.private_key_path)],
        known_hosts=None,
    ) as conn, conn.start_sftp_client() as sftp:
        remote = f"{config.remote_dir.rstrip('/')}/{filename}"
        await sftp.get(remote, str(destination))


def list_sftp(config: SftpConfig) -> list[str]:
    try:
        result: list[str] = asyncio.run(_list_sftp_async(config))
    except Exception as exc:  # asyncssh.Error, OSError, and anything asyncio.run surfaces
        raise DestinationError(f"could not list the SFTP destination: {exc}") from exc
    return result


def fetch_sftp(config: SftpConfig, filename: str, destination: Path) -> Path:
    try:
        asyncio.run(_fetch_sftp_async(config, filename, destination))
    except Exception as exc:
        raise DestinationError(f"could not fetch {filename} over SFTP: {exc}") from exc
    return destination


# --------------------------------------------------------------------------
# Protocol-agnostic entry point
# --------------------------------------------------------------------------


def fetch_latest_archive(
    config: SmbConfig | SftpConfig, work_dir: Path
) -> tuple[Path, Path]:
    """Fetch the most recent archive and its checksum sidecar into `work_dir`.

    Returns `(archive_path, sidecar_path)`, ready for
    `recovery_archive.check_checksum` and `recovery_archive.extract_checked`
    — this function only moves bytes; it does not itself trust anything it
    fetched (an archive over the network is exactly as unsigned as one found
    on a USB stick).
    """
    names = list_smb(config) if isinstance(config, SmbConfig) else list_sftp(config)
    latest = latest_archive_name(names)
    if latest is None:
        raise DestinationError("no backup archive found at the network destination")
    sidecar_name = f"{latest}.sha256"
    if sidecar_name not in names:
        raise DestinationError(f"{latest} has no {sidecar_name} sidecar at the destination")
    work_dir.mkdir(parents=True, exist_ok=True)
    archive_path = work_dir / latest
    sidecar_path = work_dir / sidecar_name
    if isinstance(config, SmbConfig):
        fetch_smb(config, latest, archive_path)
        fetch_smb(config, sidecar_name, sidecar_path)
    else:
        fetch_sftp(config, latest, archive_path)
        fetch_sftp(config, sidecar_name, sidecar_path)
    return archive_path, sidecar_path
