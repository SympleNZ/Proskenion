"""TLS certificates — self-signed and Let's Encrypt (§3.2, §6.16, §10.4 step 6, §21.24, Q6, Q7).

The appliance serves its interface over HTTPS through nginx, which reads the
certificate and key from ``/data/certs/live/<fqdn>/`` (see
``appliance/nginx/auditorium.conf``). :func:`generate_self_signed` produces a
one-year self-signed certificate for the configured hostname, with the
appliance's current address added as a subject alternative name so a browser
reaching it by address is no worse off than one using the name.
:class:`CertificateManager` issues and renews a real one from Let's Encrypt
over DNS-01, through :mod:`proskenion.core.acme_client` and
:mod:`proskenion.core.cloudflare` (Q7).

Self-signed has a cost worth stating where the code is, not only in the UI
(§6.16): iOS Safari refuses to install a PWA to the home screen over an
untrusted certificate and fails WebSocket connections outright rather than
warning. The wizard shows :data:`IOS_TRUST_GUIDANCE` alongside this path.

The atomic pair — the bug this module used to have
----------------------------------------------------
Earlier, ``_write_exact`` replaced the key and the certificate one file at a
time in place. A power cut between the two writes left a mismatched pair on
disk, and nginx refuses to start on a certificate and key that do not match —
on an appliance with a read-only root and no console, that is an outage that
needs a screwdriver to fix.

:func:`write_certificate_pair` fixes this by never writing into the path
nginx reads at all: each issuance lands in its own version directory under
``.versions/<hostname>/<n>/``, complete, before anything changes; only then is
the ``live/<hostname>`` entry — a symlink, not a real directory — swapped to
point at it, with a single ``os.replace`` of a freshly-created temporary
symlink over the old one. ``rename(2)`` (and Windows' equivalent, used the
same way) is atomic: after a crash at any point, ``live/<hostname>`` either
still resolves to the previous, complete version, or already resolves to the
new, complete one — never to a directory with one file from each. The
previous version is kept as that fallback; anything older is pruned. nginx's
own configuration is unchanged — it reads ``live/<hostname>/fullchain.pem``
exactly as before, and does not know the entry is now a symlink.

Reloading nginx
    The application runs unprivileged with ``NoNewPrivileges=yes``, so it
    cannot restart a system service itself, and by default it does not try:
    ``appliance/systemd/auditorium-cert-reload.path`` watches
    ``/data/certs/live/`` on the appliance and reloads nginx itself whenever a
    certificate is written there, whether by this module or by Let's Encrypt
    renewal. :func:`reload_nginx` runs an injectable hook — by default
    :func:`no_reload_needed`, which performs no subprocess call and simply
    reports that the path unit owns the reload — and **returns**
    :class:`ReloadOutcome` rather than raising: a certificate that is on disk
    but not yet loaded is a warning to show, not a reason to unwind the write.
    A development machine or a test with no path unit can still pass its own
    hook, for instance :func:`systemctl_reload_nginx` where the appliance has
    granted that command directly (a ``sudoers.d`` line for exactly
    ``systemctl reload nginx``); a hook that fails is reported, not raised.

Degraded time (§4.9, Q6)
    §4.9's clock-unverified fallback used to mean "serve self-signed". That
    breaks iOS outright (§6.16) for no reason: a certificate that was valid
    yesterday does not stop being valid because the RTC battery died this
    morning. :class:`CertificateManager` keeps serving whatever Let's Encrypt
    certificate is already on disk while the clock is unverified and skips
    only the renewal attempt (which needs a trustworthy clock, since Let's
    Encrypt itself checks certificate validity against real time); it falls
    back to self-signed only when no valid Let's Encrypt certificate exists
    at all — none issued yet, or the one on disk has actually expired.

The Cloudflare token (§3.2, §6.10-style encryption at rest)
    Stored under ``<data_dir>/certs/cloudflare-token.enc``, encrypted with the
    device secret (:mod:`proskenion.core.secrets`) the same way a device
    password is. It is never returned by any endpoint, never written to a
    log, and — living inside ``certs/`` under a name the archiver excludes by
    name (contracts §8) — never included in a backup archive: restoring to
    replacement hardware means re-entering it, the same story as a device
    password after a restore.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from dataclasses import replace as _dataclass_replace
from pathlib import Path
from typing import Any, Final, Literal

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from proskenion.core import acme_client, auth
from proskenion.core.broadcast import Broadcaster, progress_message
from proskenion.core.cloudflare import API_BASE as CLOUDFLARE_API_BASE
from proskenion.core.cloudflare import CloudflareClient, CloudflareError
from proskenion.core.secrets import DeviceSecret, SecretMismatch
from proskenion.core.state import StateStore, SystemWriter
from proskenion.db.connection import Database
from proskenion.db.crud.base import AUCKLAND, now_iso

log = logging.getLogger(__name__)

CERTS_SUBDIR: Final = "certs"
LIVE_SUBDIR: Final = "live"
CERTIFICATE_FILENAME: Final = "fullchain.pem"
KEY_FILENAME: Final = "privkey.pem"
KEY_MODE: Final = 0o600
#: Touched after a certificate pair is written, at a path that never varies.
#: ``auditorium-cert-reload.path`` watches this rather than the certificate
#: directory, because inotify on a directory reports only its immediate
#: entries — a renewal rewriting files inside ``live/<hostname>/`` would not
#: be seen. Signalling explicitly also states the intent rather than
#: inferring it from a modification time.
RELOAD_SENTINEL_FILENAME: Final = "reload-requested"
CERTIFICATE_MODE: Final = 0o644

DEFAULT_VALID_DAYS: Final = 365
DEFAULT_KEY_SIZE: Final = 2048
ORGANISATION: Final = "Proskenion"

NGINX_RELOAD_ARGV: Final[tuple[str, ...]] = ("systemctl", "reload", "nginx")
RELOAD_TIMEOUT_S: Final = 10.0

#: The site nginx serves when the appliance is not in emergency mode, as
#: build.sh installs it from ``appliance/nginx/auditorium.conf`` with its
#: ``--fqdn`` substituted in. It is read for one thing: which names nginx
#: expects a certificate for (:func:`served_hostnames`). Absent on a
#: development machine, where nothing is seeded.
NGINX_SITE_CONFIG: Final = Path("/etc/nginx/sites-available/auditorium.conf")
#: ``ssl_certificate /data/certs/live/<name>/fullchain.pem;`` — the name is the
#: directory nginx reads the pair from, and so the one a certificate is owed to.
_SERVED_CERTIFICATE = re.compile(
    rf"^\s*ssl_certificate\s+\S*/{CERTS_SUBDIR}/{LIVE_SUBDIR}/([^/\s;]+)/"
    rf"{re.escape(CERTIFICATE_FILENAME)}\s*;",
    re.MULTILINE,
)

IOS_TRUST_GUIDANCE: Final[tuple[str, ...]] = (
    "Download the certificate from this controller on the iPad.",
    "Open Settings and install the downloaded profile.",
    "Settings → General → VPN & Device Management → trust the certificate.",
    "Until it is trusted, Safari will not install the app to the home screen "
    "and WebSocket connections fail without a warning (§6.16).",
)

# -- the atomic pair -----------------------------------------------------------------

#: ``live/.versions/<hostname>/<n>/`` holds one issuance each; ``live/<hostname>``
#: is a symlink to whichever is current, swapped by rename (see the module
#: docstring).
VERSIONS_DIRNAME: Final = ".versions"
#: The current version plus one fallback — enough to survive a crash mid-swap
#: without keeping every certificate this appliance has ever held.
KEEP_VERSIONS: Final = 2
_SWAP_SUFFIX: Final = ".swap"
#: Computed once, rather than read from ``os.name`` at each swap, so a test
#: can pin it to prove the production (POSIX) behaviour without monkeypatching
#: ``os.name`` itself — ``pathlib`` reads that directly to choose
#: ``PosixPath``/``WindowsPath`` and breaks if it is patched.
_WINDOWS_NON_ATOMIC_FALLBACK = os.name == "nt"

# -- the Cloudflare token (§3.2, §6.10-style encryption at rest) ---------------------

TOKEN_FILENAME: Final = "cloudflare-token.enc"
_TOKEN_FIELD_KEY: Final = "cloudflare_token"

# -- ACME account key (Q7 — "store the account key under /data/certs/") --------------

ACCOUNT_KEY_SUBDIR: Final = "acme"
ACCOUNT_KEY_FILENAME: Final = "account.key"

# -- renewal history -------------------------------------------------------------------

HISTORY_SUBDIR: Final = "renewal-history"
MAX_HISTORY_ENTRIES: Final = 50

# -- the six §21.24 progress steps (cert_issue / cert_renew) -------------------------

#: Verbatim from §21.24: "Renewal opens a ProgressPanel streaming live output
#: over the WebSocket — requesting, creating the TXT record, waiting for
#: propagation, verifying, downloading, reloading nginx." The first five are
#: :mod:`proskenion.core.acme_client`'s phases; the sixth is this module's own
#: write-and-reload.
PROGRESS_STEPS: Final[tuple[str, ...]] = (
    "Requesting",
    "Creating the TXT record",
    "Waiting for propagation",
    "Verifying",
    "Downloading",
    "Reloading nginx",
)
_PHASE_STEP: Final[Mapping[acme_client.Phase, int]] = {
    "requesting": 1,
    "dns_record": 2,
    "propagation": 3,
    "verifying": 4,
    "downloaded": 5,
}

#: How long §21.24's "Waiting for propagation" step sleeps before asking the
#: ACME server to validate. Cloudflare's own API confirms the record was
#: created; this is purely give DNS edges time to pick it up (certbot's own
#: Cloudflare plugin defaults to the same order of magnitude).
DNS_PROPAGATION_WAIT_S: Final = 10.0

#: Renew inside this many days of expiry (Q7, WORKLOG "renewing inside 30
#: days of expiry").
RENEWAL_WINDOW_DAYS: Final = 30
#: Amber banner threshold (Q6, overriding §21.26's illustrative "7 days" —
#: the plan is explicit this one is 14).
EXPIRING_BANNER_DAYS: Final = 14

CERT_EXPIRING_BANNER_KEY: Final = "cert_expiring"
CERT_SELF_SIGNED_BANNER_KEY: Final = "cert_self_signed"
#: §21.26's row for "Certificate expiring"; the day count is filled in.
_EXPIRING_BANNER_TEXT: Final = "TLS certificate expires in {days} day{plural}"
#: §21.26's row for "Certificate expired".
_SELF_SIGNED_BANNER_TEXT: Final = "TLS certificate expired — using self-signed"

#: ``request_renewal_check`` / the renewal watch loop (mirrors
#: ``request_reload``'s sentinel — see ``core/hirer_access.py``'s
#: ``ACCESS_SIGNAL_FILENAME`` for the same pattern). Touched by
#: ``auditorium-certbot-renew``, consumed by :meth:`CertificateManager.watch`.
RENEW_SIGNAL_FILENAME: Final = "renew-requested"
DEFAULT_RENEW_WATCH_INTERVAL_S: Final = 60.0

CERTS_OWNER: Final = "certs"


class CertificateError(Exception):
    """A certificate could not be generated or read."""


class TokenError(Exception):
    """The Cloudflare token is missing, unreadable, or fails Cloudflare's own test."""


class IssuanceError(Exception):
    """A Let's Encrypt issuance or renewal attempt failed. ``detail`` is safe to show."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.detail = message


@dataclass(frozen=True, slots=True)
class CertificatePaths:
    """Where nginx expects one certificate pair to live."""

    directory: Path
    certificate: Path
    key: Path

    @property
    def exists(self) -> bool:
        return self.certificate.exists() and self.key.exists()


@dataclass(frozen=True, slots=True)
class CertificateInfo:
    """What the §21.24 certificate card displays.

    ``renewal_history`` is always empty from :func:`describe` — a pure reader
    of one certificate file, with no ``data_dir``/``hostname`` to find a
    history file with — and never an absent field the client has to
    special-case. :func:`certificate_card` is the version that attaches the
    real history (Q7).
    """

    domain: str
    issuer: str
    issued: str  # ISO 8601 with offset, Pacific/Auckland (§4.9)
    expires: str
    days_remaining: int
    self_signed: bool
    renewal_history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def expired(self) -> bool:
        return self.days_remaining < 0


@dataclass(frozen=True, slots=True)
class ReloadOutcome:
    """The result of asking nginx to reload. Never raised — always reported."""

    ok: bool
    detail: str


ReloadHook = Callable[[], ReloadOutcome]
"""A blocking callable that reloads nginx; run in a worker thread (§5.3)."""


# -- paths -------------------------------------------------------------------------


def certificate_paths(data_dir: Path, hostname: str) -> CertificatePaths:
    """``<data_dir>/certs/live/<hostname>/{fullchain.pem,privkey.pem}``.

    The layout matches ``appliance/nginx/auditorium.conf``, which reads
    ``/data/certs/live/<fqdn>/``; ``data_dir`` comes from the platform layer
    so a development machine writes under its own tree (§5.4).
    """
    directory = Path(data_dir) / CERTS_SUBDIR / LIVE_SUBDIR / hostname
    return CertificatePaths(
        directory=directory,
        certificate=directory / CERTIFICATE_FILENAME,
        key=directory / KEY_FILENAME,
    )


# -- generation --------------------------------------------------------------------


_HOSTNAME = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$")


def _alt_names(hostname: str, addresses: Sequence[str]) -> list[x509.GeneralName]:
    """The subject alternative names: the hostname, then any address or extra name.

    A browser reaching the appliance by address rather than by name is no
    worse off, which matters before DNS is configured (§6.16). Anything that
    is neither an address nor a plausible hostname is dropped rather than
    embedded: x509 would happily carry it and no client would ever match it.
    """
    names: list[x509.GeneralName] = [x509.DNSName(hostname)]
    for address in addresses:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(address)))
        except ValueError:
            if address and address != hostname and _HOSTNAME.match(address):
                names.append(x509.DNSName(address))
            else:
                log.debug("ignoring alternative name %r: not an address or hostname", address)
    return names


def _write_exact(path: Path, payload: bytes, mode: int) -> None:
    """Write ``payload`` to ``path`` with an exact mode, replacing atomically.

    nginx may be reading the previous file: writing beside it and renaming
    means a reload never sees a half-written certificate.
    """
    temporary = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(temporary, flags, mode)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    os.chmod(temporary, mode)  # O_CREAT's mode is masked by umask; make it exact
    os.replace(temporary, path)
    os.chmod(path, mode)


def _build_self_signed_pair(
    hostname: str,
    *,
    addresses: Sequence[str],
    valid_days: int,
    key_size: int,
    now: dt.datetime | None,
) -> tuple[bytes, bytes]:
    """The x509 building common to :func:`generate_self_signed` and the atomic path.

    Returns ``(key_pem, certificate_pem)`` without writing anything — the two
    callers differ only in where those bytes end up.
    """
    if not hostname.strip():
        raise CertificateError("a hostname is required to generate a certificate")
    hostname = hostname.strip()
    moment = (now or dt.datetime.now(tz=AUCKLAND)).astimezone(dt.UTC)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, hostname),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORGANISATION),
        ]
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)  # self-signed: issuer is the subject
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        # A minute of leeway: a clock a little behind ours must not see a
        # certificate from the future (§4.9 degraded time mode).
        .not_valid_before(moment - dt.timedelta(minutes=1))
        .not_valid_after(moment + dt.timedelta(days=valid_days))
        .add_extension(x509.SubjectAlternativeName(_alt_names(hostname, addresses)), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
    return key_pem, certificate_pem


def generate_self_signed(
    hostname: str,
    *,
    directory: Path,
    addresses: Sequence[str] = (),
    valid_days: int = DEFAULT_VALID_DAYS,
    key_size: int = DEFAULT_KEY_SIZE,
    now: dt.datetime | None = None,
) -> CertificatePaths:
    """Generate a self-signed certificate and key, written directly into ``directory``.

    Blocking — see :func:`issue_self_signed`. A low-level primitive: it
    writes wherever it is told, with no versioning or atomic swap, which is
    exactly what makes it useful for a test that wants to plant a certificate
    at a known path. Production issuance goes through :func:`issue_self_signed`
    (self-signed) or :class:`CertificateManager` (Let's Encrypt), both of
    which route the same bytes through :func:`write_certificate_pair`.
    """
    key_pem, certificate_pem = _build_self_signed_pair(
        hostname, addresses=addresses, valid_days=valid_days, key_size=key_size, now=now
    )
    paths = CertificatePaths(
        directory=directory,
        certificate=directory / CERTIFICATE_FILENAME,
        key=directory / KEY_FILENAME,
    )
    directory.mkdir(parents=True, exist_ok=True)
    _write_exact(paths.key, key_pem, KEY_MODE)
    _write_exact(paths.certificate, certificate_pem, CERTIFICATE_MODE)
    log.info("wrote a self-signed certificate for %s to %s", hostname, paths.directory)
    return paths


# -- the atomic pair (see the module docstring) ---------------------------------------


def _versions_root(data_dir: Path, hostname: str) -> Path:
    return Path(data_dir) / CERTS_SUBDIR / LIVE_SUBDIR / VERSIONS_DIRNAME / hostname


def _read_current_version(current_link: Path) -> int | None:
    """The version number ``current_link`` resolves to, or ``None`` if it is not our symlink."""
    try:
        target = os.readlink(current_link)
    except OSError:
        return None
    name = Path(target).name
    return int(name) if name.isdigit() else None


def _remove_path(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        pass


def _swap_current(current_link: Path, version_dir: Path) -> None:
    """Point ``current_link`` at ``version_dir`` with one atomic rename.

    A fresh symlink is built beside ``current_link`` under a temporary name
    and then renamed over it. ``os.replace`` is ``rename(2)`` under the hood
    on POSIX (and the equivalent atomic replace on Windows): the destination
    either has its old content or its new content at every instant — this is
    the operation the module docstring's crash-safety proof rests on.
    """
    current_link.parent.mkdir(parents=True, exist_ok=True)
    relative_target = os.path.relpath(version_dir, start=current_link.parent)
    temporary = current_link.with_name(current_link.name + _SWAP_SUFFIX)
    _remove_path(temporary)  # a crash after a previous symlink() but before replace()
    os.symlink(relative_target, temporary, target_is_directory=True)
    try:
        os.replace(temporary, current_link)
    except OSError:
        # Windows' MoveFileEx refuses MOVEFILE_REPLACE_EXISTING when either
        # side names a directory, which a directory symlink counts as — so
        # os.replace() cannot atomically swap one directory-symlink for
        # another there. Production is Debian, where the os.replace above
        # already succeeds and this branch never runs; this fallback exists
        # only so the same code develops and tests on Windows. It is not
        # atomic, which is why it is not the primary path — see
        # _WINDOWS_NON_ATOMIC_FALLBACK, which a test pins to False to prove
        # the production (POSIX) behaviour without touching the real
        # os.name (pathlib reads that directly and breaks if it is patched).
        if not _WINDOWS_NON_ATOMIC_FALLBACK:  # pragma: no cover - exercised only on Windows
            raise
        _remove_path(current_link)
        os.rename(temporary, current_link)


def _prune_versions(versions_root: Path, keep: set[int]) -> None:
    if not versions_root.is_dir():
        return
    for child in versions_root.iterdir():
        if child.is_dir() and child.name.isdigit() and int(child.name) not in keep:
            shutil.rmtree(child, ignore_errors=True)


def write_certificate_pair(
    data_dir: Path, hostname: str, key_pem: bytes, cert_pem: bytes
) -> CertificatePaths:
    """Write a new certificate version and atomically swap it in. Blocking.

    Used by :func:`issue_self_signed` and :class:`CertificateManager` alike —
    every certificate this application writes, self-signed or Let's Encrypt,
    goes through here. See the module docstring for the crash-safety this
    provides; ``tests/unit/core/test_certs.py``'s
    ``TestAtomicSwap`` proves it by interrupting the write.
    """
    hostname = hostname.strip()
    live_root = Path(data_dir) / CERTS_SUBDIR / LIVE_SUBDIR
    current_link = live_root / hostname
    if current_link.exists() and not current_link.is_symlink():
        # A pre-atomic-pair layout (or a stray directory): one-time migration,
        # never the steady state from here on.
        log.warning(
            "%s is a real directory, not the version symlink; replacing it", current_link
        )
        _remove_path(current_link)

    versions_root = _versions_root(data_dir, hostname)
    versions_root.mkdir(parents=True, exist_ok=True)
    previous_version = _read_current_version(current_link)
    existing = {
        int(child.name)
        for child in versions_root.iterdir()
        if child.is_dir() and child.name.isdigit()
    }
    next_version = (max(existing) if existing else 0) + 1
    version_dir = versions_root / str(next_version)
    version_dir.mkdir()
    # Nothing serves this directory yet, so writing the two files one at a
    # time (as _write_exact already does, for nginx's benefit) cannot produce
    # a mismatched *served* pair — a crash here just leaves an orphaned,
    # half-written version that the swap below never points at.
    _write_exact(version_dir / KEY_FILENAME, key_pem, KEY_MODE)
    _write_exact(version_dir / CERTIFICATE_FILENAME, cert_pem, CERTIFICATE_MODE)

    _swap_current(current_link, version_dir)

    # KEEP_VERSIONS (2): the one just swapped in, plus whichever was current
    # before it — the fallback an interrupted future write leaves served.
    keep = set(sorted(existing | {next_version}, reverse=True)[:KEEP_VERSIONS])
    if previous_version is not None:
        keep.add(previous_version)  # belt and braces: always keep what was live
    _prune_versions(versions_root, keep)
    log.info(
        "wrote certificate version %d for %s and swapped it in (kept %s)",
        next_version,
        hostname,
        sorted(keep),
    )
    return CertificatePaths(
        directory=current_link,
        certificate=current_link / CERTIFICATE_FILENAME,
        key=current_link / KEY_FILENAME,
    )


async def issue_self_signed(
    hostname: str,
    *,
    data_dir: Path,
    addresses: Sequence[str] = (),
    valid_days: int = DEFAULT_VALID_DAYS,
    key_size: int = DEFAULT_KEY_SIZE,
    now: dt.datetime | None = None,
) -> CertificatePaths:
    """Generate the pair for ``hostname`` and swap it in atomically, off the event loop (§5.3).

    Routes through :func:`write_certificate_pair` rather than writing
    directly into the served path — the same fix Let's Encrypt issuance uses
    (see the module docstring).
    """
    hostname = hostname.strip()
    key_pem, certificate_pem = await asyncio.to_thread(
        _build_self_signed_pair,
        hostname,
        addresses=addresses,
        valid_days=valid_days,
        key_size=key_size,
        now=now,
    )
    written = await asyncio.to_thread(
        write_certificate_pair, data_dir, hostname, key_pem, certificate_pem
    )
    # Signal the reload here rather than in the generator: this is the layer
    # that knows data_dir, and the sentinel sits above <hostname>/ (§6.16).
    await asyncio.to_thread(request_reload, data_dir)
    return written


# -- description (§21.24) -----------------------------------------------------------


def _common_name(name: x509.Name) -> str | None:
    values = name.get_attributes_for_oid(NameOID.COMMON_NAME)
    if not values:
        return None
    value = values[0].value
    return value if isinstance(value, str) else value.decode("utf-8", "replace")


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(AUCKLAND).isoformat(timespec="seconds")


def load_certificate(path: Path) -> x509.Certificate:
    """Parse a PEM certificate. Blocking."""
    try:
        return x509.load_pem_x509_certificate(path.read_bytes())
    except FileNotFoundError as exc:
        raise CertificateError(f"{path}: no certificate installed") from exc
    except (OSError, ValueError) as exc:
        raise CertificateError(f"{path}: cannot read the certificate: {exc}") from exc


def describe_certificate(
    certificate: x509.Certificate, *, now: dt.datetime | None = None
) -> CertificateInfo:
    """Summarise a parsed certificate for the §21.24 card."""
    moment = now or dt.datetime.now(tz=AUCKLAND)
    issued = certificate.not_valid_before_utc
    expires = certificate.not_valid_after_utc
    remaining = expires - moment.astimezone(dt.UTC)
    return CertificateInfo(
        domain=_common_name(certificate.subject) or certificate.subject.rfc4514_string(),
        issuer=_common_name(certificate.issuer) or certificate.issuer.rfc4514_string(),
        issued=_iso(issued),
        expires=_iso(expires),
        # Whole days, rounded towards the past: 23 hours left is "0 days".
        days_remaining=int(remaining.total_seconds() // 86400),
        self_signed=certificate.issuer == certificate.subject,
        renewal_history=[],
    )


def describe_sync(paths: CertificatePaths, *, now: dt.datetime | None = None) -> CertificateInfo:
    return describe_certificate(load_certificate(paths.certificate), now=now)


async def describe(
    paths: CertificatePaths, *, now: dt.datetime | None = None
) -> CertificateInfo | None:
    """The installed certificate's card, or ``None`` when none is installed."""
    try:
        return await asyncio.to_thread(describe_sync, paths, now=now)
    except CertificateError as exc:
        log.info("no certificate to describe: %s", exc)
        return None


# -- what browsers see (§6.16, §21.8) -------------------------------------------------

CertificateTrust = Literal["trusted", "self_signed"]


async def served_trust(
    data_dir: Path, hostname: str | None, *, development: bool
) -> CertificateTrust:
    """Whether the certificate nginx serves is self-signed, for the install prompt.

    nginx serves ``<data_dir>/certs/live/<hostname>/fullchain.pem`` (see
    :func:`certificate_paths`); a certificate whose issuer is its own subject
    is ``self_signed``, as on the §21.24 card. With no hostname configured, or
    no certificate installed there, the appliance can only be serving the
    emergency self-signed pair, so the answer is ``self_signed`` — except on a
    development machine, where there is no nginx and the page is served over
    ``localhost``, which browsers treat as secure.
    """
    if hostname:
        info = await describe(certificate_paths(data_dir, hostname))
        if info is not None:
            return "self_signed" if info.self_signed else "trusted"
    return "trusted" if development else "self_signed"


# -- reloading nginx ----------------------------------------------------------------


def systemctl_reload_nginx(
    argv: Sequence[str] = NGINX_RELOAD_ARGV, timeout_s: float = RELOAD_TIMEOUT_S
) -> ReloadOutcome:
    """Run ``systemctl reload nginx``. Blocking; never raises.

    Expected to fail until the appliance grants the application user exactly
    this command (see the module docstring). The failure is reported so the
    wizard can say "the certificate is written; nginx has not picked it up
    yet" instead of losing the certificate.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError:
        return ReloadOutcome(False, f"{argv[0]} is not available on this system")
    except subprocess.TimeoutExpired:
        return ReloadOutcome(False, f"{' '.join(argv)} timed out after {timeout_s:.0f} s")
    except OSError as exc:
        return ReloadOutcome(False, f"{' '.join(argv)} could not be run: {exc}")
    if completed.returncode == 0:
        return ReloadOutcome(True, "nginx reloaded")
    detail = (completed.stderr or completed.stdout or "").strip().splitlines()
    return ReloadOutcome(
        False,
        f"{' '.join(argv)} exited {completed.returncode}"
        + (f": {detail[0]}" if detail else ""),
    )


def reload_sentinel_path(data_dir: Path) -> Path:
    """``<data_dir>/certs/reload-requested`` — the file the path unit watches."""
    return Path(data_dir) / CERTS_SUBDIR / RELOAD_SENTINEL_FILENAME


def request_reload(data_dir: Path) -> Path:
    """Ask for an nginx reload by touching the sentinel (§6.16).

    Whatever writes a certificate calls this: the first-run wizard now, a
    Let's Encrypt renewal from Phase 6. The application needs no privilege —
    it writes a file in a directory it already owns — and
    ``auditorium-cert-reload.path`` does the reload.
    """
    sentinel = reload_sentinel_path(data_dir)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(tz=AUCKLAND).isoformat(timespec="seconds")
    sentinel.write_text(stamp + chr(10), encoding="utf-8")
    return sentinel


def no_reload_needed() -> ReloadOutcome:
    """The default reload hook: performs no subprocess call.

    ``auditorium-cert-reload.path`` (appliance/systemd/) watches
    ``/data/certs/live/`` and reloads nginx itself once the certificate this
    module just wrote lands there — the application is granted no privilege
    to do it itself (§6.16), on the appliance or in development. This is
    reported as a success, not a failure: nothing has gone wrong, the reload
    simply happens outside the application.
    """
    log.info(
        "certificate written and the reload sentinel touched; "
        "auditorium-cert-reload.path owns the nginx reload"
    )
    return ReloadOutcome(True, "auditorium-cert-reload.path will reload nginx")


async def reload_nginx(hook: ReloadHook | None = None) -> ReloadOutcome:
    """Ask nginx to reload through ``hook``, reporting failure rather than raising.

    With no ``hook`` the default is :func:`no_reload_needed`, which shells out
    to nothing — the path unit does the reload. A caller that wants the old
    direct behaviour (a development machine or a test with no path unit, or
    an appliance that has granted the command some other way) passes its own
    hook, for instance :func:`systemctl_reload_nginx`. Every hook runs in a
    worker thread (§5.3), and one that raises is caught here: the certificate
    is already on disk and losing it because a reload failed would be the
    worse outcome.
    """
    chosen: ReloadHook = hook if hook is not None else no_reload_needed
    try:
        return await asyncio.to_thread(chosen)
    except Exception as exc:  # the hook is injectable; it must not sink the request
        log.warning("nginx reload hook failed: %s", exc)
        return ReloadOutcome(False, f"the nginx reload failed: {exc}")


# -- the Cloudflare token (§3.2, §6.10-style encryption at rest) ---------------------


def token_path(data_dir: Path) -> Path:
    """``<data_dir>/certs/cloudflare-token.enc`` — excluded from archives by name (contracts §8)."""
    return Path(data_dir) / CERTS_SUBDIR / TOKEN_FILENAME


def _store_token_sync(data_dir: Path, token: str, secret: DeviceSecret) -> None:
    encrypted = secret.encrypt_value(_TOKEN_FIELD_KEY, token)
    path = token_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_exact(path, json.dumps(encrypted).encode("utf-8"), KEY_MODE)


def _load_token_sync(data_dir: Path, secret: DeviceSecret) -> str | None:
    path = token_path(data_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TokenError(f"cannot read the stored Cloudflare token: {exc}") from exc
    try:
        stored = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TokenError("the stored Cloudflare token is unreadable") from exc
    try:
        return secret.decrypt_value(_TOKEN_FIELD_KEY, stored)
    except SecretMismatch as exc:
        raise TokenError(str(exc)) from exc


async def store_token(data_dir: Path, token: str, secret: DeviceSecret) -> None:
    """Encrypt and store the Cloudflare API token. Never logged, never returned (§3.2)."""
    token = token.strip()
    if not token:
        raise TokenError("a token is required")
    await asyncio.to_thread(_store_token_sync, data_dir, token, secret)


async def load_token(data_dir: Path, secret: DeviceSecret) -> str | None:
    """The plain token, or ``None`` if none is stored. Raises :class:`TokenError` on mismatch."""
    return await asyncio.to_thread(_load_token_sync, data_dir, secret)


async def token_configured(data_dir: Path) -> bool:
    """Whether a token is stored — for ``GET /system/certs/token``, which never reveals it."""
    return await asyncio.to_thread(lambda: token_path(data_dir).exists())


def clear_token(data_dir: Path) -> None:
    """Remove the stored token, e.g. when Let's Encrypt is abandoned for self-signed."""
    _remove_path(token_path(data_dir))


async def test_token(
    data_dir: Path,
    secret: DeviceSecret,
    hostname: str | None = None,
    *,
    base_url: str = CLOUDFLARE_API_BASE,
) -> bool:
    """§21.24's "Test": a real zone read against Cloudflare with the stored token.

    Raises :class:`TokenError` if no token is stored, and
    :class:`~proskenion.core.cloudflare.CloudflareError` if Cloudflare could
    not be reached or refused it — both are safe to show (§16.1). ``base_url``
    defaults to the real Cloudflare API, exactly as :class:`CloudflareClient`
    itself does; :meth:`CertificateManager.test_token` passes its own
    ``cloudflare_base_url`` override through here, the same one
    :meth:`CertificateManager.issue` already used — this function was the one
    place still reaching the real API regardless of that override.
    """
    token = await load_token(data_dir, secret)
    if token is None:
        raise TokenError("no Cloudflare token is stored")

    def _test() -> bool:
        with CloudflareClient(token, base_url=base_url) as client:
            if not client.verify_token():
                return False
            if hostname:
                client.zone_id_for(hostname)  # raises CloudflareError if not found
            return True

    return await asyncio.to_thread(_test)


# -- renewal history ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RenewalRecord:
    """One row of §21.24's "Renewal history" card."""

    attempted_at: str  # ISO 8601 with offset (§4.9)
    method: Literal["manual", "automatic"]
    result: Literal["success", "failed", "skipped"]
    issuer: str | None = None
    serial: str | None = None
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "attempted_at": self.attempted_at,
            "method": self.method,
            "result": self.result,
            "issuer": self.issuer,
            "serial": self.serial,
            "detail": self.detail,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RenewalRecord:
        method = data.get("method")
        result = data.get("result")
        return cls(
            attempted_at=str(data.get("attempted_at", "")),
            method=method if method in ("manual", "automatic") else "automatic",
            result=result if result in ("success", "failed", "skipped") else "failed",
            issuer=data.get("issuer") if isinstance(data.get("issuer"), str) else None,
            serial=data.get("serial") if isinstance(data.get("serial"), str) else None,
            detail=data.get("detail") if isinstance(data.get("detail"), str) else None,
        )


def history_path(data_dir: Path, hostname: str) -> Path:
    return Path(data_dir) / CERTS_SUBDIR / HISTORY_SUBDIR / f"{hostname}.json"


def _read_history_sync(data_dir: Path, hostname: str) -> list[RenewalRecord]:
    path = history_path(data_dir, hostname)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("renewal history at %s is unreadable; treating it as empty", path)
        return []
    if not isinstance(data, list):
        return []
    return [RenewalRecord.from_json(item) for item in data if isinstance(item, dict)]


def _append_history_sync(data_dir: Path, hostname: str, record: RenewalRecord) -> None:
    path = history_path(data_dir, hostname)
    records = _read_history_sync(data_dir, hostname)
    records.append(record)
    if len(records) > MAX_HISTORY_ENTRIES:
        records = records[-MAX_HISTORY_ENTRIES:]
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([r.to_json() for r in records], indent=2).encode("utf-8")
    _write_exact(path, payload, CERTIFICATE_MODE)


async def renewal_history(data_dir: Path, hostname: str) -> list[RenewalRecord]:
    """Every attempt recorded for ``hostname``, oldest first (§21.24's card shows newest first)."""
    return await asyncio.to_thread(_read_history_sync, data_dir, hostname)


async def record_renewal(data_dir: Path, hostname: str, record: RenewalRecord) -> None:
    await asyncio.to_thread(_append_history_sync, data_dir, hostname, record)


async def certificate_card(
    data_dir: Path, hostname: str, *, now: dt.datetime | None = None
) -> CertificateInfo | None:
    """The §21.24 card: the installed certificate, with its real renewal history attached.

    :func:`describe` alone always answers an empty ``renewal_history`` — it is
    a pure reader of one certificate file and does not know where the history
    lives. This is the version the API serves.
    """
    paths = certificate_paths(data_dir, hostname)
    info = await describe(paths, now=now)
    if info is None:
        return None
    history = await renewal_history(data_dir, hostname)
    return _dataclass_replace(info, renewal_history=[r.to_json() for r in history])


# -- the weekly renewal trigger (mirrors request_reload / core/hirer_access.py) -------


def renew_signal_path(data_dir: Path) -> Path:
    """``<data_dir>/certs/renew-requested`` — touched by ``auditorium-certbot-renew``."""
    return Path(data_dir) / CERTS_SUBDIR / RENEW_SIGNAL_FILENAME


def request_renewal_check(data_dir: Path) -> Path:
    """Ask for a renewal check by touching the signal file.

    ``auditorium-certbot-renew`` (a plain shell script, like
    ``auditorium-cert-reload``) touches this weekly; only the running
    application can act on it — it needs the database, the device secret and
    a WebSocket broadcaster to report progress on, none of which a one-shot
    script run by a systemd timer has. :meth:`CertificateManager.watch`
    consumes it the same way ``HirerAccess.watch`` consumes the reset tool's
    signal.
    """
    sentinel = renew_signal_path(data_dir)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(tz=AUCKLAND).isoformat(timespec="seconds")
    sentinel.write_text(stamp + chr(10), encoding="utf-8")
    return sentinel


# -- the fallback nginx needs before it can start at all (§3.2) ------------------------


def served_hostnames(site_config: Path = NGINX_SITE_CONFIG) -> list[str]:
    """The names nginx's site reads a certificate for, in order; ``[]`` without one.

    Blocking. The site file is the authority rather than the configuration:
    §4.14's bootstrap file carries no hostname on the appliance, and what
    matters here is exactly the path nginx will try to load — build.sh wrote
    the FQDN into it. Commented-out directives are not directives.
    """
    try:
        text = site_config.read_text(encoding="utf-8")
    except OSError:
        return []
    names: list[str] = []
    for name in _SERVED_CERTIFICATE.findall(text):
        if _HOSTNAME.match(name) and name not in names:
            names.append(name)
    return names


def certificate_installed(data_dir: Path, hostname: str) -> bool:
    """Whether nginx would find a pair for ``hostname``: a parsable certificate and a key.

    Blocking.
    """
    paths = certificate_paths(data_dir, hostname)
    try:
        load_certificate(paths.certificate)
        return paths.key.stat().st_size > 0
    except (CertificateError, OSError):
        return False


def certificate_names(certificate: x509.Certificate) -> list[str]:
    """Every name and address a browser would accept ``certificate`` for.

    The subject alternative names, in order, with the common name first when
    the certificate carries no SAN at all (browsers ignore the CN when a SAN
    is present, so it is not added then). This is what a client compares
    ``window.location.hostname`` with to say whether the page it is on is one
    the certificate covers.
    """
    names: list[str] = []
    try:
        extension = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        common = _common_name(certificate.subject)
        return [common] if common else []
    for dns_name in extension.value.get_values_for_type(x509.DNSName):
        if dns_name not in names:
            names.append(dns_name)
    for address in extension.value.get_values_for_type(x509.IPAddress):
        text = str(address)
        if text not in names:
            names.append(text)
    return names


def served_certificate_names(data_dir: Path, hostname: str) -> list[str]:
    """:func:`certificate_names` of the pair nginx serves for ``hostname``; ``[]`` without one.

    Blocking.
    """
    path = certificate_paths(data_dir, hostname).certificate
    try:
        return certificate_names(load_certificate(path))
    except CertificateError:
        return []


def served_certificate_bytes(data_dir: Path, hostname: str) -> bytes | None:
    """The certificate nginx serves for ``hostname``, byte for byte, or ``None``. Blocking."""
    try:
        return certificate_paths(data_dir, hostname).certificate.read_bytes()
    except OSError:
        return None


#: A self-signed certificate closer than this to expiry is replaced rather
#: than reused by the wizard's certificate step.
REUSE_MIN_DAYS: Final = 30


def reusable_self_signed(
    data_dir: Path,
    hostname: str,
    addresses: Sequence[str] = (),
    *,
    min_days: int = REUSE_MIN_DAYS,
    now: dt.datetime | None = None,
) -> CertificateInfo | None:
    """The installed self-signed certificate for ``hostname``, if it already
    does everything a new one would; ``None`` when a new one is needed.

    Blocking. Reusable means: a complete pair is installed, it is self-signed,
    it names ``hostname`` and every address in ``addresses``, and it has at
    least ``min_days`` left. That is exactly what :func:`issue_self_signed`
    would produce, so replacing it would change nothing but the key — and a
    browser that has accepted the old self-signed certificate refuses the new
    one on its very next request (the first-run wizard on the real appliance,
    25 September 2026: the certificate step replaced the fallback issued at
    first start moments earlier, and the wizard's next request failed at TLS).
    """
    paths = certificate_paths(data_dir, hostname)
    if not certificate_installed(data_dir, hostname):
        return None
    try:
        certificate = load_certificate(paths.certificate)
    except CertificateError:
        return None
    info = describe_certificate(certificate, now=now)
    if not info.self_signed or info.days_remaining < min_days:
        return None
    names = certificate_names(certificate)
    wanted = [hostname.strip()]
    for address in addresses:
        try:
            wanted.append(str(ipaddress.ip_address(address)))
        except ValueError:
            if address and _HOSTNAME.match(address):
                wanted.append(address)
    if any(name not in names for name in wanted):
        return None
    return info


# -- issuance and renewal orchestration (Q6, Q7) --------------------------------------


AddressProvider = Callable[[], Sequence[str]]
DegradedProbe = Callable[[], bool]


class CertificateManager:
    """Issues and renews the Let's Encrypt certificate, and keeps its banners honest.

    Everything §21.24's Certificates card needs lives here: issuance and
    renewal both call :meth:`issue`, which reports the six progress steps
    over ``broadcaster`` as ``cert_issue`` or ``cert_renew``, writes the
    atomic pair, records history and audits ``certificate_renewed``.
    :meth:`check_and_renew` is the weekly-check entry point (Q7) and applies
    Q6's degraded-time rule; :meth:`watch` is what the application spawns as
    a background task to react to :func:`request_renewal_check`.
    """

    def __init__(
        self,
        state: StateStore,
        db: Database,
        broadcaster: Broadcaster,
        secret: DeviceSecret,
        *,
        data_dir: Path,
        hostname: str | None,
        contact_email: str | None = None,
        directory_url: str = acme_client.LETS_ENCRYPT_DIRECTORY_URL,
        verify_ssl: bool = True,
        cloudflare_base_url: str = CLOUDFLARE_API_BASE,
        addresses: AddressProvider = lambda: (),
        degraded: DegradedProbe = lambda: False,
        reload_hook: ReloadHook | None = None,
        owner: str = CERTS_OWNER,
        site_config: Path = NGINX_SITE_CONFIG,
    ) -> None:
        state.register_owner("system", owner, allow_multiple=True)
        self._writer: SystemWriter = state.system.writer(owner)
        self._db = db
        self._broadcaster = broadcaster
        self._secret = secret
        self._data_dir = Path(data_dir)
        self._hostname = hostname.strip() if hostname else None
        self._contact_email = contact_email
        self._directory_url = directory_url
        self._verify_ssl = verify_ssl
        self._cloudflare_base_url = cloudflare_base_url
        self._addresses = addresses
        self._degraded = degraded
        self._reload_hook = reload_hook
        self._site_config = site_config

    @property
    def hostname(self) -> str | None:
        return self._hostname

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def account_key_path(self) -> Path:
        """Under ``/data/certs/`` per Q7 — see :func:`acme_client.load_or_create_account_key`."""
        return self._data_dir / CERTS_SUBDIR / ACCOUNT_KEY_SUBDIR / ACCOUNT_KEY_FILENAME

    # -- token ---------------------------------------------------------------

    async def set_token(self, token: str) -> None:
        await store_token(self._data_dir, token, self._secret)

    async def clear_token(self) -> None:
        await asyncio.to_thread(clear_token, self._data_dir)

    async def token_is_configured(self) -> bool:
        return await token_configured(self._data_dir)

    async def test_token(self, hostname: str | None = None) -> bool:
        """§21.24's "Test": authenticates, and — for ``hostname`` or the
        configured one — proves the token can manage that zone.

        ``hostname`` lets a caller test against the name it is *about* to
        issue for rather than whatever this manager happens to be configured
        with (:meth:`issue`'s own default) — the first-run wizard uses this
        to pre-flight its target hostname before ever
        reaching Cloudflare's DNS-01 or an ACME server.
        """
        return await test_token(
            self._data_dir,
            self._secret,
            hostname or self._hostname,
            base_url=self._cloudflare_base_url,
        )

    async def verify_token(self) -> bool:
        """Whether the stored token authenticates with Cloudflare at all — no zone lookup.

        Deliberately narrower than :meth:`test_token`: it proves only that
        Cloudflare accepts the token, not that it can manage any particular
        zone. The first-run wizard calls this before issuance
        to catch a token Cloudflare rejects outright — pasted as the
        token's id rather than the token itself, expired, revoked — and
        refuse the submission with Cloudflare's own reason, rather than
        attempting issuance, failing with an ACME-flavoured error, and
        silently writing a self-signed fallback the administrator has no
        reason to expect. A token that authenticates but cannot manage the
        target zone (wrong domain, zone not yet on this Cloudflare account)
        is not caught here — that is still an infrastructure problem "the
        venue has to fix later" (Q7), and :meth:`issue` still falls back to
        self-signed for it, exactly as before.

        Raises :class:`TokenError` if no token is stored, and
        :class:`~proskenion.core.cloudflare.CloudflareError` — carrying
        Cloudflare's own code and message — if the token does not
        authenticate. Both are safe to show (§16.1).
        """
        token = await load_token(self._data_dir, self._secret)
        if token is None:
            raise TokenError("no Cloudflare token is stored")

        def _verify() -> bool:
            with CloudflareClient(token, base_url=self._cloudflare_base_url) as client:
                return client.verify_token()

        return await asyncio.to_thread(_verify)

    # -- issuance and renewal (§21.24's six steps) ----------------------------

    def _report_progress(self, operation: str, step: int, message: str) -> None:
        self._broadcaster.publish(
            progress_message(operation, step, len(PROGRESS_STEPS), message)
        )

    async def issue(
        self,
        hostname: str | None = None,
        *,
        method: Literal["manual", "automatic"] = "manual",
    ) -> CertificateInfo:
        """Issue or renew ``hostname`` (default: the configured one) via Let's Encrypt.

        ``operation`` in the progress frames is ``cert_issue`` for a manual
        request and ``cert_renew`` for the weekly check (contracts §6) — the
        same six steps either way, since renewal is "the same code path"
        (WORKLOG).
        """
        target = (hostname or self._hostname or "").strip()
        if not target:
            raise IssuanceError("no hostname is configured for this certificate")
        await self._ensure_target_is_served(target)
        operation = "cert_issue" if method == "manual" else "cert_renew"
        token = await load_token(self._data_dir, self._secret)
        if token is None:
            error = "no Cloudflare token is stored"
            await self._record_attempt(target, method, "failed", detail=error)
            raise TokenError(error)

        loop = asyncio.get_running_loop()

        def report(phase: acme_client.Phase) -> None:
            step = _PHASE_STEP[phase]
            loop.call_soon_threadsafe(
                self._report_progress, operation, step, PROGRESS_STEPS[step - 1]
            )

        account_key_pem = acme_client.load_or_create_account_key(self.account_key_path())

        def run() -> acme_client.IssuedCertificate:
            with CloudflareClient(token, base_url=self._cloudflare_base_url) as cf:
                zone_id = cf.zone_id_for(target)

                def publish(host: str, value: str) -> object:
                    return cf.create_txt_record(zone_id, host, value)

                def remove(record: object) -> None:
                    cf.delete_txt_record(record)  # type: ignore[arg-type]

                def wait() -> None:
                    time.sleep(DNS_PROPAGATION_WAIT_S)

                return acme_client.request_certificate(
                    target,
                    account_key_pem=account_key_pem,
                    directory_url=self._directory_url,
                    contact_email=self._contact_email,
                    publish_challenge=publish,
                    remove_challenge=remove,
                    wait_for_propagation=wait,
                    on_phase=report,
                    verify_ssl=self._verify_ssl,
                )

        try:
            issued = await asyncio.to_thread(run)
        except (acme_client.AcmeError, CloudflareError) as exc:
            await self._record_attempt(target, method, "failed", detail=str(exc))
            raise IssuanceError(str(exc)) from exc

        self._report_progress(operation, len(PROGRESS_STEPS), PROGRESS_STEPS[-1])
        await asyncio.to_thread(
            write_certificate_pair, self._data_dir, target, issued.key_pem, issued.fullchain_pem
        )
        await asyncio.to_thread(request_reload, self._data_dir)
        outcome = await reload_nginx(self._reload_hook)
        if not outcome.ok:
            log.warning("certificate written but nginx was not reloaded: %s", outcome.detail)

        info = await certificate_card(self._data_dir, target)
        await self._record_attempt(
            target, method, "success", issuer=issued.issuer, serial=issued.serial
        )
        await auth.record_event(
            self._db,
            "certificate_renewed",
            user_ident=None,
            ip_address=None,
            detail={
                "hostname": target,
                "issuer": issued.issuer,
                "serial": issued.serial,
                "method": method,
            },
        )
        await self._update_banners(info)
        if info is None:  # pragma: no cover - defensive; we just wrote it
            raise IssuanceError("the certificate was written but could not be read back")
        return info

    async def use_self_signed(
        self, hostname: str | None = None, *, reason: str = "requested by an admin"
    ) -> CertificateInfo:
        """§21.24's "Use self-signed" (contracts §5, wave 3 additions): the way
        back when issuance cannot work — an expired token, no DNS, a site off
        the internet — so, unlike :meth:`issue`, this reaches neither
        Cloudflare nor an ACME server. Shares :func:`issue_self_signed`'s
        atomic swap with the Q6 automatic fallback
        (:meth:`_fallback_to_self_signed`), and this class's own
        renewal-history and banner bookkeeping, so §21.24's card and history
        read the same regardless of how a self-signed certificate got here.
        """
        target = (hostname or self._hostname or "").strip()
        if not target:
            raise IssuanceError("no hostname is configured for this certificate")
        await self._ensure_target_is_served(target)
        await issue_self_signed(target, data_dir=self._data_dir, addresses=self._addresses())
        info = await certificate_card(self._data_dir, target)
        await self._record_attempt(target, "manual", "success", detail=reason)
        await self._update_banners(info)
        if info is None:  # pragma: no cover - defensive; we just wrote it
            raise IssuanceError(
                "the self-signed certificate was written but could not be read back"
            )
        return info

    async def _ensure_target_is_served(self, target: str) -> None:
        """Refuse a name nginx's site does not read a certificate for.

        nginx reads only ``ssl_certificate``'s name(s) from ``auditorium.conf``
        (see :func:`served_hostnames`); a certificate issued for any other
        name is written under ``.versions/<name>/`` and ``live/<name>/`` same
        as any other, but nginx never looks there, so it is never served —
        and worse, the issuing screen (wizard or admin) reports it as though
        it were the appliance's certificate. That is exactly what happened
        twice on the real appliance on 24 September 2026: a stray self-signed
        ``auditorium`` pair sat next to the real, served
        ``auditorium.obhs.school.nz`` one, doing nothing.

        There is no legitimate reason to issue for a name nginx does not
        serve: this appliance's nginx site is generated once, by
        ``build.sh --fqdn``, for exactly one served name (see
        ``appliance/nginx/auditorium.conf``) — there is no second site block,
        no multi-domain configuration, that a certificate for another name
        could ever be for. Refusing spends neither a Let's Encrypt request
        nor an admin's attention on a certificate that can only confuse.

        Enforced only when nginx's site is actually readable —
        :func:`served_hostnames` answers ``[]`` on a development machine
        with no nginx, where nothing is enforced, exactly as
        :meth:`ensure_served` already treats an absent site.
        """
        served = await asyncio.to_thread(served_hostnames, self._site_config)
        if served and target not in served:
            names = " or ".join(repr(name) for name in served)
            raise IssuanceError(
                f"nginx serves {names}, not {target!r}; a certificate for {target!r} "
                "would never be served"
            )

    async def ensure_served(self) -> list[str]:
        """Give every name nginx serves a certificate if it has none; return the names seeded.

        §3.2: "a self-signed certificate is the fallback during initial
        setup". Without it, the first boot after the first install is a
        deadlock: auditorium.conf names ``/data/certs/live/<fqdn>/``, nothing
        is there yet, so nginx will not start — and the Certificates screen
        and first-run wizard that would issue one are behind nginx. Called
        once at startup.

        A fresh pair from :func:`issue_self_signed`, not a copy of the one
        first boot made in ``/srv/appliance/certs/self-signed``: that one
        belongs to emergency mode, which must work with ``/data`` gone, and a
        copy on ``/data`` would travel in every backup archive (contracts §8)
        and be shared by two roles; and its name came from ``/etc/hosts`` at
        first boot, where this one's is the name nginx asks for. Either way
        it goes through :func:`write_certificate_pair` and asks for the reload
        like every other certificate, which is what starts a stopped nginx.

        Only an absent or unreadable pair is replaced. An expired one still
        lets nginx start, and replacing it is :meth:`check_and_renew`'s call
        (Q6), not this. Nothing here marks the wizard's certificate step done:
        the wizard still offers both options, and whichever is chosen is
        written over this as the next version.
        """
        names = await asyncio.to_thread(served_hostnames, self._site_config)
        seeded: list[str] = []
        for name in names:
            if await asyncio.to_thread(certificate_installed, self._data_dir, name):
                continue
            log.warning(
                "nginx serves %s and there is no certificate for it; "
                "installing a self-signed fallback (§3.2)",
                name,
            )
            await issue_self_signed(name, data_dir=self._data_dir, addresses=self._addresses())
            await self._record_attempt(
                name,
                "automatic",
                "success",
                detail="no certificate at startup: installed a self-signed fallback so "
                "nginx can start (§3.2)",
            )
            seeded.append(name)
        if self._hostname is not None and self._hostname in seeded:
            await self._update_banners(await certificate_card(self._data_dir, self._hostname))
        return seeded

    async def _record_attempt(
        self,
        hostname: str,
        method: Literal["manual", "automatic"],
        result: Literal["success", "failed", "skipped"],
        *,
        issuer: str | None = None,
        serial: str | None = None,
        detail: str | None = None,
    ) -> None:
        await record_renewal(
            self._data_dir,
            hostname,
            RenewalRecord(
                attempted_at=now_iso(),
                method=method,
                result=result,
                issuer=issuer,
                serial=serial,
                detail=detail,
            ),
        )

    # -- the weekly check, and Q6's degraded-time rule ------------------------

    async def check_and_renew(self) -> None:
        """The weekly check (Q7): renew inside 30 days of expiry, skip while degraded (Q6)."""
        hostname = self._hostname
        if not hostname:
            return
        if self._degraded():
            await self._handle_degraded(hostname)
            return
        if not await self.token_is_configured():
            await self._update_banners(await certificate_card(self._data_dir, hostname))
            return
        info = await certificate_card(self._data_dir, hostname)
        needs_issuance = info is None or info.self_signed or info.expired
        due_for_renewal = info is not None and info.days_remaining <= RENEWAL_WINDOW_DAYS
        if not (needs_issuance or due_for_renewal):
            await self._update_banners(info)
            return
        try:
            await self.issue(hostname, method="automatic")
        except (IssuanceError, TokenError) as exc:
            log.warning("certificate renewal failed for %s: %s", hostname, exc)
            info = await certificate_card(self._data_dir, hostname)
            if info is None or info.expired:
                await self._fallback_to_self_signed(hostname)
            else:
                await self._update_banners(info)

    async def _handle_degraded(self, hostname: str) -> None:
        """Q6: keep serving whatever is on disk; fall back only if nothing valid remains."""
        info = await certificate_card(self._data_dir, hostname)
        if info is None or info.expired:
            log.warning(
                "time is unverified and no valid certificate is on disk for %s; "
                "falling back to self-signed (§4.9)",
                hostname,
            )
            await self._fallback_to_self_signed(hostname)
        else:
            await self._update_banners(info)

    async def _fallback_to_self_signed(self, hostname: str) -> None:
        await issue_self_signed(hostname, data_dir=self._data_dir, addresses=self._addresses())
        await self._record_attempt(
            hostname,
            "automatic",
            "skipped",
            detail="fell back to a self-signed certificate: no valid Let's Encrypt "
            "certificate was available",
        )
        await self._update_banners(await certificate_card(self._data_dir, hostname))

    async def _update_banners(self, info: CertificateInfo | None) -> None:
        """§21.26's ``cert_expiring`` (amber, 14 days — Q6) and ``cert_self_signed``."""
        if info is None:
            self._writer.set("cert_expiry", None)
            self._writer.clear_banner(CERT_EXPIRING_BANNER_KEY)
            self._writer.clear_banner(CERT_SELF_SIGNED_BANNER_KEY)
            return
        self._writer.set("cert_expiry", info.expires)
        # Self-signed is only a fault worth a red banner when Let's Encrypt
        # was meant to be serving instead (a token is configured) — a site
        # that has deliberately not set up Let's Encrypt is not in an error
        # state (§21.26 is about the *expired-and-fell-back* condition).
        token_set = await self.token_is_configured()
        if info.self_signed and token_set:
            self._writer.set_banner(
                CERT_SELF_SIGNED_BANNER_KEY, "red", _SELF_SIGNED_BANNER_TEXT
            )
        else:
            self._writer.clear_banner(CERT_SELF_SIGNED_BANNER_KEY)
        if not info.self_signed and 0 <= info.days_remaining <= EXPIRING_BANNER_DAYS:
            days = info.days_remaining
            text = _EXPIRING_BANNER_TEXT.format(days=days, plural="" if days == 1 else "s")
            self._writer.set_banner(CERT_EXPIRING_BANNER_KEY, "amber", text)
        else:
            self._writer.clear_banner(CERT_EXPIRING_BANNER_KEY)

    # -- the renewal signal (see request_renewal_check) -----------------------

    def _consume_signal(self) -> bool:
        path = renew_signal_path(self._data_dir)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            log.warning("cannot remove %s: %s", path, exc)
            return False
        return True

    async def poll_signal(self) -> bool:
        """Check for and consume the renewal signal, running a check if it was set."""
        if not self._consume_signal():
            return False
        await self.check_and_renew()
        return True

    async def watch(self, interval_s: float = DEFAULT_RENEW_WATCH_INTERVAL_S) -> None:
        """Poll for ``auditorium-certbot-renew``'s signal until cancelled."""
        while True:
            try:
                await self.poll_signal()
            except Exception:  # pragma: no cover - defensive
                log.exception("certificate renewal check failed")
            await asyncio.sleep(interval_s)
