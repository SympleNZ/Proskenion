"""``auditorium-wait-for-knx-gateway`` — knxd.service's ExecStartPre= (§4.11).

Commissioned on the CM5 on 24-25 September 2026: knxd started 35 ms after
eth0's carrier came up, tried its KNXnet/IP tunnel CONNECT before the switch
port was forwarding, and failed its first start on every boot. Two things had
to be true together for the fix to actually help, and neither side proves it
alone:

- the real ``knxd.conf.default`` — ``-e 0.0.1 -E 0.0.2:8 -b ipt:10.2.30.252``,
  the format ``EnvironmentFile=/etc/knxd.conf`` hands knxd — has to parse to
  the right host and port;
- a real UDP exchange against a real socket has to detect a gateway that
  answers quickly, and give up (without hanging the boot) when nothing does.

``appliance/tests/verify-in-docker.sh`` runs the same two things together
against the installed script and the installed config file inside a
container; this is the fast, no-Docker version of the same hand-off.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from types import ModuleType

APPLIANCE = Path(__file__).resolve().parents[3] / "appliance"
KNXD_CONF_DEFAULT = APPLIANCE / "etc" / "knxd.conf.default"


def test_the_real_knxd_conf_default_names_the_tunnelling_gateway(knx_wait: ModuleType) -> None:
    """``-b ipt:HOST`` from the file that ``/etc/knxd.conf`` symlinks to (§4.3)."""
    text = KNXD_CONF_DEFAULT.read_text(encoding="utf-8")
    opts_line = next(line for line in text.splitlines() if line.startswith("KNXD_OPTS="))
    opts = opts_line.split("=", 1)[1].strip('"')
    assert "-b ipt:10.2.30.252" in opts, "knxd.conf.default drifted from what this test expects"

    target = knx_wait.gateway_host_and_port(opts)
    assert target == ("10.2.30.252", knx_wait.KNXNET_IP_PORT)


def test_multicast_backend_has_no_single_gateway_to_wait_for(knx_wait: ModuleType) -> None:
    """Debian's own default, ``-b ip:`` (multicast), names no single host."""
    assert knx_wait.gateway_host_and_port("-e 0.0.1 -u /tmp/eib -b ip:") is None


def test_environment_wins_over_the_file(knx_wait: ModuleType, tmp_path: Path, monkeypatch) -> None:
    """ExecStartPre= inherits EnvironmentFile=/etc/knxd.conf as $KNXD_OPTS.

    The direct file read is only the fallback for running this by hand — the
    one place it actually runs, systemd has already loaded the file.
    """
    stale = tmp_path / "knxd.conf"
    stale.write_text('KNXD_OPTS="-b ipt:should-not-be-read"\n', encoding="utf-8", newline="\n")
    monkeypatch.setenv("KNXD_OPTS", "-b ipt:10.2.30.252")
    assert knx_wait.read_knxd_opts(str(stale)) == "-b ipt:10.2.30.252"


def _fake_gateway(*, answer: bool) -> tuple[threading.Thread, int, threading.Event]:
    """A UDP socket on loopback that answers (or does not) like a real gateway."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(5.0)
    port = sock.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        try:
            data, addr = sock.recvfrom(2048)
        except OSError:
            return
        if answer:
            sock.sendto(b"\x06\x10\x02\x04\x00\x08", addr)  # KNXnet/IP-shaped reply
        stop.set()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread, port, stop


def test_a_responding_gateway_is_found_well_inside_the_timeout(knx_wait: ModuleType) -> None:
    thread, port, _ = _fake_gateway(answer=True)
    try:
        started = time.monotonic()
        found = knx_wait.wait_for_gateway("127.0.0.1", port, timeout_s=5.0, poll_interval_s=0.1)
        elapsed = time.monotonic() - started
    finally:
        thread.join(timeout=5.0)
    assert found is True
    assert elapsed < 2.0, "a live gateway should answer almost immediately, not after retries"


def test_a_silent_gateway_gives_up_without_blocking_forever(knx_wait: ModuleType) -> None:
    # A real UDP port that never answers, not a mock standing in for one. The
    # socket stays bound for the whole wait: closing it to "free" the port
    # let a concurrently running test's fake gateway take the same port and
    # answer (a flaky failure seen in the Linux run, 29 Sep 2026).
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as silent:
        silent.bind(("127.0.0.1", 0))
        silent_port = silent.getsockname()[1]
        started = time.monotonic()
        found = knx_wait.wait_for_gateway(
            "127.0.0.1", silent_port, timeout_s=0.6, poll_interval_s=0.1
        )
        elapsed = time.monotonic() - started
    assert found is False
    assert elapsed < 5.0, "the wait must respect its own timeout, not knxd's 30s default"


def test_main_always_exits_zero_even_when_nothing_answers(
    knx_wait: ModuleType, monkeypatch, capsys
) -> None:
    """A dead gateway is Restart='s job; ExecStartPre= must never fail the start."""
    monkeypatch.setenv("KNXD_OPTS", "-e 0.0.1 -E 0.0.2:8 -b ipt:127.0.0.1")
    # Held bound and silent for the whole wait (see the test above).
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as silent:
        silent.bind(("127.0.0.1", 0))
        monkeypatch.setattr(knx_wait, "KNXNET_IP_PORT", silent.getsockname()[1])
        rc = knx_wait.main(["--timeout-s", "0.4", "--poll-interval-s", "0.1"])
    assert rc == 0
    assert "did not answer within" in capsys.readouterr().out
