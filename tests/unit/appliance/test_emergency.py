"""``appliance/lib/auditorium_emergency.py`` — the emergency responder (§4.6,
§16.7, Q12).

Imported in isolation, with nothing from ``proskenion``: this is the
non-negotiable the brief states outright, and it is checked here by static
analysis of every root-side module this responder touches, not merely by the
fact that importing it in this test file happens not to fail.
"""

from __future__ import annotations

import ast
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

APPLIANCE = Path(__file__).resolve().parents[3] / "appliance"
LIB = APPLIANCE / "lib"
BIN = APPLIANCE / "bin"

ROOT_SIDE_FILES = [
    LIB / "auditorium_emergency.py",
    LIB / "auditorium_emergency_reason.py",
    LIB / "auditorium_device_secret.py",
    LIB / "auditorium_fallback_mail.py",
    BIN / "auditorium-emergency",
]


# -- the non-negotiable: no import from the application ----------------------


@pytest.mark.parametrize("path", ROOT_SIDE_FILES, ids=lambda p: p.name)
def test_nothing_here_imports_from_proskenion(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("proskenion"), f"{path.name}: import {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not module.startswith("proskenion"), f"{path.name}: from {module} import ..."


def test_the_responder_is_importable_on_its_own(emergency: ModuleType) -> None:
    """The fixture itself proves this: importing auditorium_emergency pulls in
    only its three sibling root-side modules, never proskenion or the venv
    packages it depends on (aiosmtplib, cryptography via the application's
    own secrets module)."""
    assert emergency.DEFAULT_PORT > 0


# -- §16.7's fixed payload, for every reason ----------------------------------


ALL_REASONS = [
    "data_unavailable",
    "data_readonly",
    "migration_failed",
    "disk_full",
    "not_installed",
]


@pytest.mark.parametrize("reason", ALL_REASONS)
def test_build_payload_matches_the_spec_shape(reason: str, emergency: ModuleType) -> None:
    payload = emergency.build_payload(
        reason=reason,
        version="v1.3.0",
        mount_state=emergency.MOUNT_STATE_BY_REASON[reason],
        storage={"detected": True, "smart": "passed", "media_errors": 0},
        last_backup="2026-09-07T03:00:00+12:00",
        backup_media_present=True,
    )
    assert payload == {
        "status": "emergency",
        "version": "v1.3.0",
        "reason": reason,
        "detail": {
            "mount_state": emergency.MOUNT_STATE_BY_REASON[reason],
            "storage": {"detected": True, "smart": "passed", "media_errors": 0},
            "last_backup": "2026-09-07T03:00:00+12:00",
            "backup_media_present": True,
        },
    }
    # §16.7: json.dumps must round-trip it with nothing lost or reordered away.
    assert json.loads(json.dumps(payload)) == payload


def test_build_payload_refuses_a_reason_outside_the_closed_set(emergency: ModuleType) -> None:
    with pytest.raises(ValueError):
        emergency.build_payload(
            reason="something_else",
            version="v1.3.0",
            mount_state="failed",
            storage=dict(emergency.STORAGE_UNKNOWN),
            last_backup=None,
            backup_media_present=False,
        )


def test_every_closed_set_reason_has_a_mount_state(emergency: ModuleType) -> None:
    import auditorium_emergency_reason as emergency_reason

    assert set(emergency.MOUNT_STATE_BY_REASON) == set(emergency_reason.REASONS)


# -- entry path 1: detecting /data live ---------------------------------------


def test_detect_data_state_when_not_a_mount(tmp_path: Path, emergency: ModuleType) -> None:
    not_a_mount = tmp_path / "data"  # a plain directory, never a real mount point
    not_a_mount.mkdir()
    reason, detail = emergency.detect_data_state(not_a_mount)
    assert reason == "data_unavailable"
    assert str(not_a_mount) in detail


def test_detect_data_state_when_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, emergency: ModuleType
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(emergency.os.path, "ismount", lambda path: True)
    original = Path.write_text

    def deny(self: Path, *args: object, **kwargs: object) -> int:
        if self.name.startswith(".auditorium-emergency-probe"):
            raise PermissionError("Read-only file system")
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", deny)
    reason, detail = emergency.detect_data_state(data)
    assert reason == "data_readonly"
    assert "Read-only" in detail


# -- the version comes from boot-state.json, never __version__ ---------------


def test_read_version_prefers_healthy_over_started(tmp_path: Path, emergency: ModuleType) -> None:
    path = tmp_path / "boot-state.json"
    path.write_text(
        json.dumps(
            {
                "started": {"version": "v1.3.1", "at": "2026-09-20T19:42:11+12:00"},
                "healthy": {"version": "v1.3.0", "at": "2026-09-20T03:10:02+12:00"},
            }
        ),
        encoding="utf-8",
    )
    assert emergency.read_version(path) == "v1.3.0"


def test_read_version_falls_back_to_started(tmp_path: Path, emergency: ModuleType) -> None:
    path = tmp_path / "boot-state.json"
    path.write_text(json.dumps({"started": {"version": "v1.3.1", "at": "x"}}), encoding="utf-8")
    assert emergency.read_version(path) == "v1.3.1"


def test_read_version_is_unknown_rather_than_a_guess(tmp_path: Path, emergency: ModuleType) -> None:
    assert emergency.read_version(tmp_path / "does-not-exist.json") == "unknown"


# -- the last backup, read from /mnt/backup, a different device --------------


def test_read_last_backup_absent_media(tmp_path: Path, emergency: ModuleType) -> None:
    last_backup, present = emergency.read_last_backup(tmp_path / "not-mounted")
    assert last_backup is None
    assert present is False


def test_read_last_backup_picks_the_newest_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, emergency: ModuleType
) -> None:
    mount = tmp_path / "mnt-backup"
    mount.mkdir()
    monkeypatch.setattr(emergency.os.path, "ismount", lambda path: True)
    names = ("auditorium-20260906-0300.tar.zst", "auditorium-20260907-0300.tar.zst", "ignored.txt")
    for name in names:
        (mount / name).write_bytes(b"")
    last_backup, present = emergency.read_last_backup(mount)
    assert present is True
    assert last_backup is not None
    assert last_backup.startswith("2026-09-07T03:00:00")


# -- the page ------------------------------------------------------------------


def test_render_page_fills_every_placeholder(emergency: ModuleType) -> None:
    template = (APPLIANCE / "share" / "auditorium" / "emergency" / "index.html").read_text(
        encoding="utf-8"
    )
    rendered = emergency.render_page(
        template,
        hostname="auditorium",
        time_text="2026-09-20T19:42:11+12:00",
        reason="disk_full",
        ssd_status="passed (0 media errors)",
        last_backup="2026-09-07T03:00:00+12:00",
    )
    # The five tokens render_page() fills are gone; the HTML comment above
    # them, which mentions "{{PLACEHOLDERS}}" as prose, is expected to remain.
    for token in ("{{HOSTNAME}}", "{{TIME}}", "{{REASON}}", "{{SSD_STATUS}}", "{{LAST_BACKUP}}"):
        assert token not in rendered
    expected_strings = (
        "auditorium",
        "disk_full",
        "passed (0 media errors)",
        "2026-09-07T03:00:00+12:00",
    )
    for expected in expected_strings:
        assert expected in rendered
    # §4.6: no scripts, no fonts, no images.
    assert "<script" not in rendered
    assert "@font-face" not in rendered
    assert 'src="http' not in rendered


def test_render_page_escapes_its_values(emergency: ModuleType) -> None:
    rendered = emergency.render_page(
        "{{HOSTNAME}}",
        hostname="<script>alert(1)</script>",
        time_text="t",
        reason="disk_full",
        ssd_status="s",
        last_backup="b",
    )
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


# -- nginx: swapped to emergency.conf, idempotently ---------------------------


def test_switch_nginx_to_emergency_swaps_the_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, emergency: ModuleType
) -> None:
    available = tmp_path / "sites-available"
    enabled = tmp_path / "sites-enabled"
    available.mkdir()
    enabled.mkdir()
    (available / "emergency.conf").write_text("emergency", encoding="utf-8")
    (enabled / "auditorium.conf").symlink_to(available / "auditorium.conf")

    calls: list[list[str]] = []
    monkeypatch.setattr(emergency.subprocess, "run", lambda cmd, **kw: calls.append(cmd))

    emergency.switch_nginx_to_emergency(sites_enabled=enabled, sites_available=available)

    assert not (enabled / "auditorium.conf").exists()
    assert (enabled / "emergency.conf").resolve() == (available / "emergency.conf").resolve()
    assert calls and "nginx.service" in calls[0]

    # Idempotent: calling it again with the swap already done changes nothing
    # and does not raise.
    emergency.switch_nginx_to_emergency(sites_enabled=enabled, sites_available=available)
    assert (enabled / "emergency.conf").resolve() == (available / "emergency.conf").resolve()


# -- the fallback alert, once per entry ---------------------------------------


def test_send_alert_once_attempts_exactly_once_per_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, emergency: ModuleType
) -> None:
    sent: list[tuple[str, str]] = []

    class FakeSettings:
        recipient = "ict@obhs.school.nz"

    def fake_read(path: Path) -> object:
        return FakeSettings()

    def fake_send(settings: object, *, subject: str, body: str) -> None:
        sent.append((subject, body))

    monkeypatch.setattr(emergency.fallback_mail, "read", fake_read)
    monkeypatch.setattr(emergency.fallback_mail, "send", fake_send)

    marker = tmp_path / "emergency-alert-sent"
    fallback_path = tmp_path / "smtp-fallback.toml"
    reason_doc = {
        "reason": "disk_full",
        "detail": "42 bytes free",
        "at": "2026-09-20T19:00:00+12:00",
    }

    first = emergency.send_alert_once(
        reason_doc,
        hostname="auditorium",
        version="v1.3.0",
        storage={"detected": True, "smart": "passed", "media_errors": 0},
        last_backup=None,
        backup_media_present=False,
        marker_path=marker,
        fallback_path=fallback_path,
    )
    assert first is True
    assert len(sent) == 1
    assert "disk_full" in sent[0][0]

    second = emergency.send_alert_once(
        reason_doc,
        hostname="auditorium",
        version="v1.3.0",
        storage={"detected": True, "smart": "passed", "media_errors": 0},
        last_backup=None,
        backup_media_present=False,
        marker_path=marker,
        fallback_path=fallback_path,
    )
    assert second is False
    assert len(sent) == 1  # not sent again


def test_send_alert_once_is_attempted_again_for_a_new_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, emergency: ModuleType
) -> None:
    sent: list[str] = []

    class FakeSettings:
        recipient = "ict@obhs.school.nz"

    monkeypatch.setattr(emergency.fallback_mail, "read", lambda path: FakeSettings())
    monkeypatch.setattr(
        emergency.fallback_mail, "send", lambda settings, *, subject, body: sent.append(subject)
    )

    marker = tmp_path / "emergency-alert-sent"
    fallback_path = tmp_path / "smtp-fallback.toml"

    emergency.send_alert_once(
        {"reason": "migration_failed", "detail": "d", "at": "2026-09-20T19:00:00+12:00"},
        hostname="h",
        version="v1",
        storage={},
        last_backup=None,
        backup_media_present=False,
        marker_path=marker,
        fallback_path=fallback_path,
    )
    new_entry = {"reason": "migration_failed", "detail": "d", "at": "2026-09-21T03:00:00+12:00"}
    emergency.send_alert_once(
        new_entry,
        hostname="h",
        version="v1",
        storage={},
        last_backup=None,
        backup_media_present=False,
        marker_path=marker,
        fallback_path=fallback_path,
    )
    assert len(sent) == 2


def test_send_alert_marks_attempted_even_when_no_relay_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, emergency: ModuleType
) -> None:
    monkeypatch.setattr(emergency.fallback_mail, "read", lambda path: None)
    marker = tmp_path / "emergency-alert-sent"
    reason_doc = {"reason": "data_unavailable", "detail": "d", "at": "2026-09-20T19:00:00+12:00"}

    sent = emergency.send_alert_once(
        reason_doc,
        hostname="h",
        version="v1",
        storage={},
        last_backup=None,
        backup_media_present=False,
        marker_path=marker,
        fallback_path=tmp_path / "smtp-fallback.toml",
    )
    assert sent is False
    assert marker.read_text(encoding="utf-8") == reason_doc["at"]


def test_send_alert_marks_attempted_even_when_the_send_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, emergency: ModuleType
) -> None:
    class FakeSettings:
        recipient = "ict@obhs.school.nz"

    def boom(settings: object, *, subject: str, body: str) -> None:
        raise OSError("relay unreachable")

    monkeypatch.setattr(emergency.fallback_mail, "read", lambda path: FakeSettings())
    monkeypatch.setattr(emergency.fallback_mail, "send", boom)

    marker = tmp_path / "emergency-alert-sent"
    reason_doc = {"reason": "disk_full", "detail": "d", "at": "2026-09-20T19:00:00+12:00"}
    sent = emergency.send_alert_once(
        reason_doc,
        hostname="h",
        version="v1",
        storage={},
        last_backup=None,
        backup_media_present=False,
        marker_path=marker,
        fallback_path=tmp_path / "smtp-fallback.toml",
    )
    assert sent is False  # attempted, not delivered — never crashes the responder
    assert marker.exists()


# -- the HTTP server: real sockets, every reason ------------------------------


PAGE_TEMPLATE = "<html>{{HOSTNAME}} {{TIME}} {{REASON}} {{SSD_STATUS}} {{LAST_BACKUP}}</html>"


@pytest.fixture
def running_server(emergency: ModuleType) -> Iterator[object]:
    servers = []

    def factory(reason: str):
        context = emergency.Context(
            reason=reason, version="v1.3.0", hostname="auditorium", template=PAGE_TEMPLATE
        )
        handler = emergency.make_handler(context)
        server = emergency.http.server.HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        return server

    yield factory
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("reason", ALL_REASONS)
def test_health_endpoint_serves_503_with_the_reason(
    reason: str, running_server, monkeypatch: pytest.MonkeyPatch, emergency: ModuleType
) -> None:
    monkeypatch.setattr(
        emergency, "read_storage_state", lambda device=None: dict(emergency.STORAGE_UNKNOWN)
    )
    monkeypatch.setattr(emergency, "read_last_backup", lambda mount=None: (None, False))
    server = running_server(reason)
    port = server.server_address[1]
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5)
        raise AssertionError("expected a 503")
    except urllib.error.HTTPError as exc:
        assert exc.code == 503
        payload = json.loads(exc.read())
    assert payload["status"] == "emergency"
    assert payload["reason"] == reason
    assert payload["version"] == "v1.3.0"
    assert payload["detail"]["mount_state"] == emergency.MOUNT_STATE_BY_REASON[reason]


def test_everything_else_serves_the_page(
    running_server, monkeypatch: pytest.MonkeyPatch, emergency: ModuleType
) -> None:
    monkeypatch.setattr(
        emergency, "read_storage_state", lambda device=None: dict(emergency.STORAGE_UNKNOWN)
    )
    monkeypatch.setattr(emergency, "read_last_backup", lambda mount=None: (None, False))
    server = running_server("disk_full")
    port = server.server_address[1]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/html")
        body = response.read().decode("utf-8")
    assert "auditorium" in body
    assert "disk_full" in body

    # Every other path gets the same page (nginx's try_files fallback shape).
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/anything/else", timeout=5) as response:
        assert response.status == 200
