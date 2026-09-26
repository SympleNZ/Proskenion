"""SMB and SFTP against real servers, in Docker (Q19, contracts §2-§3).

Everything else proves the destinations with fakes
(``tests/unit/core/test_backup_destinations.py`` and
``tests/unit/core/test_backup.py``); this is the one place the real wire
protocols — ``smbprotocol`` talking to Samba, ``asyncssh`` talking to a real
SFTP server with the key pair this device generates — are exercised end to
end, the same role ``tests/integration/certs/test_acme_pebble.py`` plays for
ACME. Skipped, not failed, when Docker is not available.

SFTP is driven directly from this process: ``asyncssh`` is pure Python and
behaves the same on every platform this suite runs on. **SMB is driven from
inside a throwaway Linux container instead** — a real difference from the
Pebble test's shape, and worth recording why: ``smbprotocol``'s DFS-referral
handling (it probes ``IPC$`` before the real tree connect) misbehaves on a
Windows development machine's socket stack in a way it does not on Linux —
confirmed by hand with Samba's own ``smbclient`` CLI (works from both) and
the ``smbprotocol`` Python library (works from a Linux container against the
same server, fails from Windows). The appliance is Debian; running the SMB
half of this check inside a Linux container proves the code path that
actually matters and sidesteps a Windows-only library quirk this task did
not introduce and cannot fix from here.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from proskenion.core.backup_destinations import (
    SftpConfig,
    SftpDestination,
    generate_sftp_keypair_if_missing,
)

COMPOSE_FILE = Path(__file__).with_name("docker-compose.destinations.yml")
PROJECT = "proskenion-backup-destinations-test"
NETWORK = f"{PROJECT}_default"
SMB_PORT = 1445
SFTP_PORT = 2222
READY_TIMEOUT_S = 60.0
POLL_INTERVAL_S = 1.0


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "compose", "version"], capture_output=True, timeout=10, check=False
        )
    except OSError:
        return False
    return result.returncode == 0


skip_without_docker = pytest.mark.skipif(
    not _docker_available(), reason="docker (with the compose plugin) is not available"
)
pytestmark = [pytest.mark.integration, skip_without_docker]


def _tcp_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _smb_ready() -> bool:
    """True once smbd will actually authenticate and list the share — not
    merely that its own log said ``daemon_ready``.

    That log line used to be treated as the whole answer, but it says only
    that the daemon started, not that it will already answer a real SMB
    login — under load (several agents sharing one Docker host, 25
    September 2026) the gap between the two was wide enough for the write
    tests below to hit it. ``smbclient`` ships inside the samba image
    itself (confirmed: ``/usr/bin/smbclient``), so this is a real protocol
    round trip through ``docker exec`` — the same container already
    running, not one more to start — matching the real authentication
    check :func:`_sftp_authenticates` already does for SFTP.
    """
    result = subprocess.run(
        [
            "docker",
            "exec",
            f"{PROJECT}-samba-1",
            "smbclient",
            "-L",
            "localhost",
            "-U",
            "backupuser%backuppass",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.returncode == 0 and "backups" in result.stdout


async def _sftp_authenticates(key_dir: Path) -> bool:
    """True once the SFTP server will actually authenticate this device's key.

    A bare open TCP port is not enough: atmoz/sftp's entrypoint still has to
    create the user and install ``authorized_keys`` from the mounted
    directory after sshd starts listening, so a check that stops at "the
    port answers" is a race the container regularly wins.
    """
    destination = SftpDestination(
        SftpConfig(
            host="127.0.0.1",
            port=SFTP_PORT,
            remote_dir="/backups",
            username="backupuser",
            private_key_path=key_dir / "backup-sftp-key",
        )
    )
    return await destination.available()


def _wait_until_ready(deadline: float, key_dir: Path) -> None:
    while time.monotonic() < deadline:
        if (
            _tcp_open("127.0.0.1", SMB_PORT)
            and _smb_ready()
            and asyncio.run(_sftp_authenticates(key_dir))
        ):
            return
        time.sleep(POLL_INTERVAL_S)
    raise TimeoutError("the Samba and SFTP containers did not become ready in time")


@pytest.fixture(scope="module")
def servers(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Brings up Samba and SFTP, with this device's SFTP public key installed
    on the SFTP server before it starts. Yields the generated private key's
    directory."""
    key_dir = tmp_path_factory.mktemp("backup-destinations-keys")
    generate_sftp_keypair_if_missing(key_dir)
    authorized_keys_dir = COMPOSE_FILE.parent / "sftp-authorized-keys"
    authorized_keys_dir.mkdir(exist_ok=True)
    public_key = (key_dir / "backup-sftp-key.pub").read_text(encoding="ascii")
    (authorized_keys_dir / "device.pub").write_text(public_key, encoding="ascii")

    compose = ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", PROJECT]
    subprocess.run([*compose, "up", "-d"], check=True, capture_output=True)
    try:
        _wait_until_ready(time.monotonic() + READY_TIMEOUT_S, key_dir)
        yield key_dir
    finally:
        subprocess.run([*compose, "down", "-v"], check=False, capture_output=True)
        shutil.rmtree(authorized_keys_dir, ignore_errors=True)


# -- SFTP: driven directly (asyncssh is platform-independent) --------------------------


@skip_without_docker
async def test_sftp_write_list_read_delete(servers: Path) -> None:
    private_key = servers / "backup-sftp-key"
    destination = SftpDestination(
        SftpConfig(
            host="127.0.0.1",
            port=SFTP_PORT,
            remote_dir="/backups",
            username="backupuser",
            private_key_path=private_key,
        )
    )
    assert await destination.available() is True

    local = servers / "auditorium-20260920-0300.tar.zst"
    local.write_bytes(b"a real backup archive's bytes")
    await destination.write(local, "auditorium-20260920-0300.tar.zst")
    assert "auditorium-20260920-0300.tar.zst" in await destination.list_names()

    readback = servers / "readback.tar.zst"
    await destination.read("auditorium-20260920-0300.tar.zst", readback)
    assert readback.read_bytes() == local.read_bytes()

    await destination.delete("auditorium-20260920-0300.tar.zst")
    assert "auditorium-20260920-0300.tar.zst" not in await destination.list_names()


@skip_without_docker
async def test_sftp_available_is_false_for_a_wrong_key(servers: Path, tmp_path: Path) -> None:
    """A device whose key was never installed on the NAS must be told it
    cannot reach the destination, not raise."""
    generate_sftp_keypair_if_missing(tmp_path)  # a different key, never authorised
    destination = SftpDestination(
        SftpConfig(
            host="127.0.0.1",
            port=SFTP_PORT,
            remote_dir="/backups",
            username="backupuser",
            private_key_path=tmp_path / "backup-sftp-key",
        )
    )
    assert await destination.available() is False


# -- SMB: driven from inside a Linux container (see the module docstring) --------------

_SMB_CHECK_SCRIPT = """
import asyncio
from pathlib import Path
from proskenion.core.backup_destinations import SmbConfig, SmbDestination

async def main():
    dest = SmbDestination(SmbConfig(
        host="samba", port=445, share_path="backups",
        username="backupuser", password="backuppass",
    ))
    assert await dest.available() is True, "SMB destination reported unavailable"

    src = Path("/tmp/auditorium-20260920-0300.tar.zst")
    src.write_bytes(b"a real backup archive's bytes")
    await dest.write(src, "auditorium-20260920-0300.tar.zst")
    names = await dest.list_names()
    assert "auditorium-20260920-0300.tar.zst" in names, names

    out = Path("/tmp/readback.tar.zst")
    await dest.read("auditorium-20260920-0300.tar.zst", out)
    assert out.read_bytes() == src.read_bytes()

    await dest.delete("auditorium-20260920-0300.tar.zst")
    names = await dest.list_names()
    assert "auditorium-20260920-0300.tar.zst" not in names, names

    print("SMB_CHECK_OK")

asyncio.run(main())
"""


@skip_without_docker
def test_smb_write_list_read_delete(servers: Path) -> None:
    proskenion_src = Path(__file__).resolve().parents[3] / "proskenion"
    assert (proskenion_src / "core" / "backup_destinations.py").is_file()
    # Written to a real file and mounted in, rather than passed as a `-c`
    # string: a script this size round-tripped through two layers of shell
    # quoting (this call's argv, then bash -c's own parsing inside the
    # container) is exactly how the first version of this test broke.
    script_path = servers / "smb_check.py"
    script_path.write_text(_SMB_CHECK_SCRIPT, encoding="utf-8")
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            NETWORK,
            "-v",
            f"{proskenion_src}:/src/proskenion:ro",
            "-v",
            f"{script_path}:/check.py:ro",
            "python:3.13-slim",
            "bash",
            "-c",
            "pip install -q smbprotocol asyncssh cryptography >/dev/null 2>&1 && "
            "PYTHONPATH=/src python3 /check.py",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SMB_CHECK_OK" in result.stdout, result.stdout + result.stderr


# -- a restore over the real wire ----------------------------------------------


@skip_without_docker
async def test_a_backup_is_restored_from_the_real_network_destination(
    servers: Path, tmp_path: Path
) -> None:
    """The restore path exercised here against a real SFTP server rather than a stub.

    Everything else about a restore is proved with fakes in
    ``tests/unit/core/test_backup_restore.py``. What only a real server can
    prove is the part this test exists for: that the archive and the
    ``.sha256`` sidecar beside it come back over the wire byte for byte, so
    the checksum the restore insists on still matches once the file has made
    the round trip (§13.2, §13.4).
    """
    from proskenion.core.backup_archive import (
        archive_filename,
        build_archive,
        checksum_filename,
    )
    from proskenion.core.backup_restore import NamedSource, RestorePaths, RestoreService
    from proskenion.core.secrets import DeviceSecret
    from proskenion.db.connection import Database
    from proskenion.db.crud import backup as backup_crud
    from proskenion.db.crud import system_state
    from proskenion.db.migrations import migrate

    data_dir = tmp_path / "data"
    state_dir = tmp_path / "appliance"
    database_path = data_dir / "db" / "auditorium.db"
    database_path.parent.mkdir(parents=True)
    state_dir.mkdir()
    (data_dir / "config").mkdir(parents=True)
    (data_dir / "config" / "system.json").write_text("{}", encoding="utf-8")

    db = Database()
    await db.open(database_path)
    await migrate(db)
    await system_state.set(db, "venue", "name", "archived")

    built = await build_archive(
        db_path=database_path,
        data_dir=data_dir,
        state_dir=state_dir,
        staging_dir=tmp_path / "staging",
        schema_version=2,
        app_version="v1.2.0",
    )
    destination = SftpDestination(
        SftpConfig(
            host="127.0.0.1",
            port=SFTP_PORT,
            remote_dir="/backups",
            username="backupuser",
            private_key_path=servers / "backup-sftp-key",
        )
    )
    await destination.write(built.path, archive_filename(built.id))
    await destination.write(built.checksum_path, checksum_filename(built.id))
    await backup_crud.record_archive(
        db,
        archive_id=built.id,
        created_at=built.manifest.created_at,
        source="scheduled",
        size_bytes=built.size_bytes,
        sha256=built.sha256,
        schema_version=built.manifest.schema_version,
        app_version=built.manifest.app_version,
        local_present=False,
        usb_present=False,
        network_present=True,
    )
    await system_state.set(db, "venue", "name", "running")

    async def network() -> SftpDestination:
        return destination

    service = RestoreService(
        db,
        RestorePaths.for_appliance(
            database=database_path,
            data_dir=data_dir,
            state_dir=state_dir,
            local_dir=tmp_path / "srv-local",
            usb_dir=tmp_path / "mnt-backup",
        ),
        secret=DeviceSecret(b"\x33" * 32),
        network_destination=network,
    )
    try:
        result = await service.restore(NamedSource(archive_id=built.id, destination="network"))
    finally:
        await destination.delete(archive_filename(built.id))
        await destination.delete(checksum_filename(built.id))

    assert result.source == "network"
    assert result.checksum_verified is True

    restored = Database()
    await restored.open(database_path)
    try:
        assert await system_state.get_value(restored, "venue", "name") == "archived"
    finally:
        await restored.close()
