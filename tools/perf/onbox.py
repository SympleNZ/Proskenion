"""Running the harness on the appliance itself (``--mint-admin-session``).

Measured from the owner's laptop on 1 Oct 2026 the HTTP row read 61/s (the
laptop's network path, p50 99 ms) where the same server did 426/s through
nginx and 500/s direct when measured on the CM5. A report that does not say
*where it was measured from* records the wrong thing, so every run states its
vantage (``on-box`` / ``off-box``) and path (``direct`` / ``nginx`` /
``remote``) in the terminal and in the JSON.

On the box there is no password to type: the session is minted with the
installation's own secret, exactly as ``tools/provision/stage_lighting.py``
does (its ``mint_admin_token`` is reused, not re-implemented). That needs the
application's user (or root) - the secret file is not world-readable.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from typing import Any

DIRECT_URL = "http://127.0.0.1:8000"
NGINX_URL = "https://127.0.0.1"
DEFAULT_HOSTNAME = "auditorium.obhs.school.nz"


@dataclass(frozen=True, slots=True)
class RunTarget:
    """Where and how the harness talks to the server."""

    base_url: str
    vantage: str  # "on-box" | "off-box"
    path: str  # "direct" | "nginx" | "remote"
    host_header: str | None = None
    origin: str | None = None
    insecure: bool = False
    session: str = "password"  # "password" | "minted"


def resolve_target(
    *,
    base_url: str | None,
    mint_admin_session: bool,
    via_nginx: bool,
    onbox_hostname: str,
    origin: str | None,
    insecure: bool,
) -> RunTarget:
    """Work out the target from the flags. Raises :class:`ValueError` (the
    message is shown to the operator) for combinations that make no sense."""
    if via_nginx and not mint_admin_session:
        raise ValueError(
            "--via-nginx is for on-box runs: it dials 127.0.0.1 with the public Host header, "
            "and needs --mint-admin-session (off the box, give --base-url instead)"
        )
    if not mint_admin_session:
        if base_url is None:
            raise ValueError("--base-url is required (or --mint-admin-session, on the appliance)")
        return RunTarget(
            base_url=base_url,
            vantage="off-box",
            path="remote",
            origin=origin,
            insecure=insecure,
            session="password",
        )
    if via_nginx:
        return RunTarget(
            base_url=base_url or NGINX_URL,
            vantage="on-box",
            path="nginx",
            host_header=onbox_hostname,
            # The server compares Origin to [server] hostname (§16.2, §6.12);
            # WebSocket upgrades are refused on a mismatch.
            origin=origin or f"https://{onbox_hostname}",
            insecure=True,  # a certificate for the hostname is not valid for 127.0.0.1
            session="minted",
        )
    return RunTarget(
        base_url=base_url or DIRECT_URL,
        vantage="on-box",
        path="direct",
        origin=origin or f"https://{onbox_hostname}",
        insecure=insecure,
        session="minted",
    )


def mint_session(config_path: str | None) -> str:
    """An admin session token from this installation's own secret. Reuses
    ``tools.provision.stage_lighting.mint_admin_token`` (imported lazily:
    copy ``tools/provision`` to the box beside ``tools/perf``). Raises
    :class:`MintFailed` with a readable message."""
    try:
        from tools.provision.stage_lighting import CannotStart, mint_admin_token
    except ImportError as exc:  # pragma: no cover - depends on how it was copied
        raise MintFailed(
            f"cannot import tools.provision.stage_lighting ({exc}); copy tools/provision "
            "to the appliance beside tools/perf (see tools/perf/README.md)"
        ) from exc
    try:
        token, _expires = mint_admin_token(config_path)
    except CannotStart as exc:
        raise MintFailed(str(exc)) from exc
    return token


class MintFailed(Exception):
    """The session could not be minted; the message says why."""


def run_context(target: RunTarget, *, capture_iface: str | None = None) -> dict[str, Any]:
    """The 'where was this measured' block recorded in the JSON report."""
    geteuid = getattr(os, "geteuid", None)
    context: dict[str, Any] = {
        "vantage": target.vantage,
        "path": target.path,
        "base_url": target.base_url,
        "host_header": target.host_header,
        "tls_verified": not target.insecure if target.base_url.startswith("https") else None,
        "session": target.session,
        "machine": socket.gethostname(),
        "euid": geteuid() if geteuid else None,
    }
    if capture_iface is not None:
        context["dmx_capture_iface"] = capture_iface
    return context


def describe(target: RunTarget) -> str:
    """One line for the terminal and the notes: ``on-box, via nginx ...``."""
    where = {
        "direct": "on the appliance, direct to the application (no nginx)",
        "nginx": "on the appliance, through nginx",
        "remote": "from another machine, over the network",
    }[target.path]
    return f"{where}: {target.base_url}" + (
        f" (Host: {target.host_header})" if target.host_header else ""
    )
