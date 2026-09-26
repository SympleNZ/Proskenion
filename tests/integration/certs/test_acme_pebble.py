"""Real issuance against Pebble and a Cloudflare stub, in Docker (Q7).

Everything else proves the orchestration with fakes (``tests/unit/core/
test_cert_manager.py``); this is the one place the real seams —
:mod:`proskenion.core.acme_client` talking ACME over the wire, and
:mod:`proskenion.core.cloudflare` talking the real Cloudflare HTTP shape —
are exercised end to end. Pebble is the ACME server letsencrypt/pebble ships
for exactly this purpose; it validates DNS-01 against
letsencrypt/pebble-challtestsrv's fake DNS rather than the real internet, so
nothing here reaches Let's Encrypt or Cloudflare.

The "Cloudflare stub" is a tiny in-process HTTP server implementing the three
calls :class:`~proskenion.core.cloudflare.CloudflareClient` makes — token
verify (a zone list), create a TXT record, delete it — and forwarding the
record itself to challtestsrv's management API. :class:`CertificateManager`
runs completely unmodified against it via its ``cloudflare_base_url``
override.

Skipped, not failed, when Docker is not available — the rest of the suite
must pass on a machine with no Docker (§22.4's default run), and this is the
one place §21.24's plan asks to be "proved off-device ... in Docker" rather
than run everywhere.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from proskenion.core import certs
from proskenion.core.broadcast import Message
from proskenion.core.bus import EventBus
from proskenion.core.secrets import DeviceSecret
from proskenion.core.state import StateStore
from proskenion.db.connection import MEMORY, Database
from proskenion.db.migrations import migrate

COMPOSE_FILE = Path(__file__).with_name("docker-compose.pebble.yml")
PEBBLE_DIRECTORY_URL = "https://localhost:14000/dir"
CHALLTESTSRV_URL = "http://localhost:8055"
HOSTNAME = "pebble-cert-test.example"
FAKE_TOKEN = "stub-cloudflare-token"
READY_TIMEOUT_S = 90.0
POLL_INTERVAL_S = 1.0

pytestmark = [
    pytest.mark.integration,
    # verify_ssl=False against Pebble's own test CA is deliberate (see the
    # module docstring); the production default stays verify_ssl=True.
    pytest.mark.filterwarnings("ignore::urllib3.exceptions.InsecureRequestWarning"),
]


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


# -- the Cloudflare stub ---------------------------------------------------------------


class _StubHandler(BaseHTTPRequestHandler):
    server: _StubServer

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003 - stdlib signature
        pass  # quiet: the pytest log is noisy enough without an HTTP access log

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw or b"{}")
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        # /zones or /zones?name=<x>&per_page=1 — both the token test and the
        # zone lookup use this one endpoint, exactly as Cloudflare's API does.
        if self.path.startswith("/zones"):
            self._reply(200, {"success": True, "result": [{"id": "zone-1", "name": HOSTNAME}]})
            return
        self._reply(404, {"success": False, "errors": [{"message": "not found"}]})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/zones/zone-1/dns_records":
            body = self._body()
            name = str(body.get("name"))
            value = str(body.get("content"))
            httpx.post(
                f"{CHALLTESTSRV_URL}/set-txt", json={"host": f"{name}.", "value": value}, timeout=10
            ).raise_for_status()
            # The record id doubles as the name: the stub keeps no state of
            # its own, and DELETE below reads it straight back out.
            self._reply(200, {"success": True, "result": {"id": name}})
            return
        self._reply(404, {"success": False, "errors": [{"message": "not found"}]})

    def do_DELETE(self) -> None:  # noqa: N802
        prefix = "/zones/zone-1/dns_records/"
        if self.path.startswith(prefix):
            name = self.path[len(prefix) :]
            httpx.post(
                f"{CHALLTESTSRV_URL}/clear-txt", json={"host": f"{name}."}, timeout=10
            ).raise_for_status()
            self._reply(200, {"success": True, "result": {"id": name}})
            return
        self._reply(404, {"success": False, "errors": [{"message": "not found"}]})


class _StubServer(ThreadingHTTPServer):
    daemon_threads = True


def _run_cloudflare_stub() -> Iterator[str]:
    server = _StubServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def cloudflare_stub() -> Iterator[str]:
    yield from _run_cloudflare_stub()


# -- Pebble, via docker compose ---------------------------------------------------------


def _tcp_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _wait_until_ready(deadline: float) -> None:
    while time.monotonic() < deadline:
        if _tcp_open("localhost", 14000) and _tcp_open("localhost", 8055):
            try:
                response = httpx.get(PEBBLE_DIRECTORY_URL, verify=False, timeout=2.0)  # noqa: S501
                if response.status_code == 200 and "newOrder" in response.json():
                    return
            except httpx.HTTPError:
                pass
        time.sleep(POLL_INTERVAL_S)
    raise TimeoutError("Pebble did not become ready in time")


@pytest.fixture(scope="module")
def pebble() -> Iterator[None]:
    compose = ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", "proskenion-pebble-test"]
    subprocess.run([*compose, "up", "-d"], check=True, capture_output=True)
    try:
        _wait_until_ready(time.monotonic() + READY_TIMEOUT_S)
        yield
    finally:
        subprocess.run([*compose, "down", "-v"], check=False, capture_output=True)


# -- the test itself ---------------------------------------------------------------------


class _RecordingBroadcaster:
    def __init__(self) -> None:
        self.sent: list[Message] = []

    def publish(self, message: Message, *, domain: str | None = None, cls: str = "discrete") -> int:
        self.sent.append(message)
        return 0


@skip_without_docker
async def test_real_issuance_against_pebble_and_the_cloudflare_stub(
    pebble: None, cloudflare_stub: str, tmp_path: Path
) -> None:
    db = Database()
    await db.open(MEMORY)
    await migrate(db)
    bus = EventBus()
    state = StateStore(_dev_config(tmp_path), bus)
    broadcaster = _RecordingBroadcaster()
    manager = certs.CertificateManager(
        state,
        db,
        broadcaster,  # type: ignore[arg-type]
        DeviceSecret(os.urandom(32)),
        data_dir=tmp_path,
        hostname=HOSTNAME,
        directory_url=PEBBLE_DIRECTORY_URL,
        verify_ssl=False,  # Pebble's own test CA; not the production default
        cloudflare_base_url=cloudflare_stub,
    )
    await manager.set_token(FAKE_TOKEN)

    info = await manager.issue(method="manual")

    assert info.self_signed is False
    assert "Pebble" in info.issuer
    installed = certs.certificate_paths(tmp_path, HOSTNAME)
    assert installed.exists
    assert installed.directory.is_symlink()  # the atomic pair, not a plain directory

    progress = [m for m in broadcaster.sent if m["type"] == "progress"]
    assert [p["step"] for p in progress] == [1, 2, 3, 4, 5, 6]

    history = await certs.renewal_history(tmp_path, HOSTNAME)
    assert history[-1].result == "success"
    assert history[-1].issuer and "Pebble" in history[-1].issuer

    await db.close()


def _dev_config(tmp_path: Path) -> Any:
    from proskenion.config import parse_config

    return parse_config(
        {
            "database": {"path": str(tmp_path / "db.sqlite")},
            "logging": {"path": str(tmp_path / "logs")},
            "app": {"environment": "development"},
        }
    )
