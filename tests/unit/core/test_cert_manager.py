"""``CertificateManager`` — issuance, renewal, degraded time and banners (Q6, Q7, §21.24).

The ACME and Cloudflare seams (:mod:`proskenion.core.acme_client` and
:mod:`proskenion.core.cloudflare`) are faked here rather than exercised for
real — that proof lives in ``tests/integration/certs/`` against Pebble and a
Cloudflare stub, in Docker. What matters at this level is the orchestration:
the six progress steps, renewal history, the audit row, and Q6's degraded-time
rule — all things a real ACME server would only make slower to prove.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from proskenion.config import Config
from proskenion.core import acme_client, certs
from proskenion.core.broadcast import Message
from proskenion.core.bus import EventBus
from proskenion.core.cloudflare import CloudflareError, TxtRecord
from proskenion.core.secrets import DeviceSecret
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import security_events

HOSTNAME = "av.school.nz"
TOKEN = "cf-scoped-token"


# -- fakes ------------------------------------------------------------------------------


class FakeBroadcaster:
    """Records every ``publish`` call; no connections, no event loop plumbing."""

    def __init__(self) -> None:
        self.sent: list[Message] = []

    def publish(self, message: Message, *, domain: str | None = None, cls: str = "discrete") -> int:
        self.sent.append(message)
        return 0


def _issued_certificate(
    hostname: str = HOSTNAME,
    *,
    issuer_cn: str = "Pebble Intermediate CA",
    serial: str = "deadbeef",
    days: int = 90,
    now: dt.datetime | None = None,
) -> acme_client.IssuedCertificate:
    """A real, parseable certificate standing in for one Let's Encrypt would issue.

    Issuer and subject differ, so ``self_signed`` reads ``False`` — the same
    signal :func:`proskenion.core.certs.describe_certificate` uses.
    """
    # 1024 bits: not for security, only to keep the suite fast — this key
    # never leaves the test process.
    key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    now = now or dt.datetime.now(dt.UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(int(serial, 16))
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return acme_client.IssuedCertificate(
        fullchain_pem=certificate.public_bytes(serialization.Encoding.PEM),
        key_pem=key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        issuer=issuer_cn,
        serial=serial,
        not_before=certificate.not_valid_before_utc,
        not_after=certificate.not_valid_after_utc,
    )


class FakeAcme:
    """Stands in for :func:`acme_client.request_certificate`.

    Walks through the same phase callbacks and DNS publish/remove calls the
    real function does, so the orchestration around it — progress frames,
    Cloudflare record cleanup — is exercised the same way.
    """

    def __init__(
        self,
        *,
        fails_with: Exception | None = None,
        issued: acme_client.IssuedCertificate | None = None,
    ) -> None:
        self.fails_with = fails_with
        self.issued = issued or _issued_certificate()
        self.calls = 0
        self.phases: list[str] = []

    def __call__(self, hostname: str, **kwargs: Any) -> acme_client.IssuedCertificate:
        self.calls += 1
        on_phase = kwargs["on_phase"]
        publish = kwargs["publish_challenge"]
        remove = kwargs["remove_challenge"]
        wait = kwargs["wait_for_propagation"]
        on_phase("requesting")
        token = publish(hostname, "validation-value")
        on_phase("dns_record")
        wait()
        on_phase("propagation")
        if self.fails_with is not None:
            remove(token)
            raise self.fails_with
        on_phase("verifying")
        remove(token)
        on_phase("downloaded")
        self.phases = list(acme_client.PHASES)
        return self.issued


class FakeCloudflareClient:
    """Stands in for :class:`~proskenion.core.cloudflare.CloudflareClient`."""

    instances: list[FakeCloudflareClient] = []
    verify_result = True
    verify_error: Exception | None = None
    zone_error: Exception | None = None
    zone_lookups: list[str] = []

    def __init__(self, token: str, **_kwargs: Any) -> None:
        self.token = token
        self.created: list[tuple[str, str, str]] = []
        self.deleted: list[TxtRecord] = []
        FakeCloudflareClient.instances.append(self)

    def __enter__(self) -> FakeCloudflareClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def verify_token(self) -> bool:
        if FakeCloudflareClient.verify_error is not None:
            raise FakeCloudflareClient.verify_error
        return FakeCloudflareClient.verify_result

    def zone_id_for(self, hostname: str) -> str:
        FakeCloudflareClient.zone_lookups.append(hostname)
        if FakeCloudflareClient.zone_error is not None:
            raise FakeCloudflareClient.zone_error
        return "zone-1"

    def create_txt_record(self, zone_id: str, hostname: str, value: str) -> TxtRecord:
        self.created.append((zone_id, hostname, value))
        return TxtRecord(record_id="rec-1", zone_id=zone_id, name=f"_acme-challenge.{hostname}")

    def delete_txt_record(self, record: TxtRecord) -> None:
        self.deleted.append(record)


@pytest.fixture(autouse=True)
def _reset_fake_cloudflare() -> None:
    FakeCloudflareClient.instances = []
    FakeCloudflareClient.verify_result = True
    FakeCloudflareClient.verify_error = None
    FakeCloudflareClient.zone_error = None
    FakeCloudflareClient.zone_lookups = []


@pytest.fixture
def secret() -> DeviceSecret:
    import os

    return DeviceSecret(os.urandom(32))


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def state(dev_config: Config, bus: EventBus) -> StateStore:
    return StateStore(dev_config, bus)


@pytest.fixture
def broadcaster() -> FakeBroadcaster:
    return FakeBroadcaster()


def make_manager(
    db: Database,
    state: StateStore,
    broadcaster: FakeBroadcaster,
    secret: DeviceSecret,
    tmp_path: Path,
    *,
    degraded: bool = False,
    hostname: str | None = HOSTNAME,
    addresses: Sequence[str] = (),
) -> certs.CertificateManager:
    return certs.CertificateManager(
        state,
        db,
        broadcaster,  # type: ignore[arg-type]  # duck-typed: only .publish() is used
        secret,
        data_dir=tmp_path,
        hostname=hostname,
        degraded=lambda: degraded,
        addresses=lambda: list(addresses),
    )


@pytest.fixture(autouse=True)
def _patch_acme(monkeypatch: pytest.MonkeyPatch) -> FakeAcme:
    fake = FakeAcme()
    monkeypatch.setattr(certs.acme_client, "request_certificate", fake)
    monkeypatch.setattr(certs, "CloudflareClient", FakeCloudflareClient)
    return fake


# -- issuance ---------------------------------------------------------------------------


async def test_issue_requires_a_token(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    manager = make_manager(db, state, broadcaster, secret, tmp_path)
    with pytest.raises(certs.TokenError):
        await manager.issue()
    history = await certs.renewal_history(tmp_path, HOSTNAME)
    assert history[-1].result == "failed"


async def test_issue_writes_the_certificate_and_reports_six_steps(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    manager = make_manager(db, state, broadcaster, secret, tmp_path)
    await manager.set_token(TOKEN)

    info = await manager.issue(method="manual")

    assert info.self_signed is False
    assert info.issuer == "Pebble Intermediate CA"
    installed = certs.certificate_paths(tmp_path, HOSTNAME)
    assert installed.exists

    progress = [m for m in broadcaster.sent if m["type"] == "progress"]
    assert [p["step"] for p in progress] == [1, 2, 3, 4, 5, 6]
    assert [p["of"] for p in progress] == [6] * 6
    assert progress[0]["operation"] == "cert_issue"
    assert progress[-1]["message"] == "Reloading nginx"

    # The TXT record was created and removed — never left behind.
    [cf] = FakeCloudflareClient.instances
    assert cf.created and cf.deleted
    assert cf.token == TOKEN  # the plain token, decrypted — never logged (see docstring)


async def test_issue_renew_uses_cert_renew_as_the_operation_name(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    manager = make_manager(db, state, broadcaster, secret, tmp_path)
    await manager.set_token(TOKEN)
    await manager.issue(method="automatic")
    progress = [m for m in broadcaster.sent if m["type"] == "progress"]
    assert {p["operation"] for p in progress} == {"cert_renew"}


async def test_certificate_renewed_is_audited(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    manager = make_manager(db, state, broadcaster, secret, tmp_path)
    await manager.set_token(TOKEN)
    info = await manager.issue(method="automatic")

    events = await security_events.query(db, event_type="certificate_renewed")
    assert len(events) == 1
    assert events[0].detail is not None
    assert HOSTNAME in events[0].detail
    assert info.issuer in events[0].detail


async def test_a_failed_issuance_is_recorded_and_raises(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path, _patch_acme: FakeAcme,
) -> None:
    _patch_acme.fails_with = acme_client.AcmeError("Pebble refused the order")
    manager = make_manager(db, state, broadcaster, secret, tmp_path)
    await manager.set_token(TOKEN)

    with pytest.raises(certs.IssuanceError, match="Pebble refused the order"):
        await manager.issue()

    history = await certs.renewal_history(tmp_path, HOSTNAME)
    assert history[-1].result == "failed"
    assert "Pebble refused the order" in (history[-1].detail or "")
    # A failure never leaves a half-written certificate served.
    assert certs.certificate_paths(tmp_path, HOSTNAME).exists is False


async def test_a_cloudflare_zone_lookup_failure_is_wrapped(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    FakeCloudflareClient.zone_error = CloudflareError("no zone owns this hostname")
    manager = make_manager(db, state, broadcaster, secret, tmp_path)
    await manager.set_token(TOKEN)
    with pytest.raises(certs.IssuanceError, match="no zone owns this hostname"):
        await manager.issue()


# -- use_self_signed (§21.24's "Use self-signed", contracts §5, wave 3 additions) ------------


async def test_use_self_signed_writes_the_pair_and_reaches_neither_cloudflare_nor_acme(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path, _patch_acme: FakeAcme,
) -> None:
    # Primed to fail if either were ever reached — the whole point of "Use
    # self-signed" is that neither has to work.
    _patch_acme.fails_with = acme_client.AcmeError("would fail if this were ever called")
    FakeCloudflareClient.zone_error = CloudflareError("would fail if this were ever called")
    manager = make_manager(db, state, broadcaster, secret, tmp_path)

    info = await manager.use_self_signed()

    assert info.self_signed is True
    assert info.domain == HOSTNAME
    installed = certs.certificate_paths(tmp_path, HOSTNAME)
    assert installed.exists
    assert installed.directory.is_symlink()  # the same atomic pair an issued certificate gets
    assert FakeCloudflareClient.instances == []
    assert broadcaster.sent == []  # no progress frames: there are no steps to report


async def test_use_self_signed_is_recorded_with_its_reason_and_raises_the_banner(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    manager = make_manager(db, state, broadcaster, secret, tmp_path)
    await manager.set_token(TOKEN)  # a token configured is what makes self-signed a fault (§21.26)

    await manager.use_self_signed(reason="the Cloudflare token had expired")

    history = await certs.renewal_history(tmp_path, HOSTNAME)
    assert history[-1].method == "manual"
    assert history[-1].result == "success"
    assert history[-1].detail == "the Cloudflare token had expired"

    banner = state.system.banner(certs.CERT_SELF_SIGNED_BANNER_KEY)
    assert banner is not None and banner.level == "red"


async def test_use_self_signed_with_no_hostname_configured_raises(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    manager = make_manager(db, state, broadcaster, secret, tmp_path, hostname=None)

    with pytest.raises(certs.IssuanceError, match="no hostname is configured"):
        await manager.use_self_signed()

    # Nothing was written for want of somewhere to write it.
    assert not any((tmp_path / "certs").glob("**/*"))


# -- the token ----------------------------------------------------------------------------


async def test_token_test_is_a_real_zone_read(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    manager = make_manager(db, state, broadcaster, secret, tmp_path)
    await manager.set_token(TOKEN)
    assert await manager.test_token() is True
    FakeCloudflareClient.verify_result = False
    assert await manager.test_token() is False


async def test_token_test_without_a_token_raises(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    manager = make_manager(db, state, broadcaster, secret, tmp_path)
    with pytest.raises(certs.TokenError):
        await manager.test_token()


async def test_test_token_with_a_hostname_checks_that_zone_not_the_managers(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    """The wizard tests the hostname it is about to issue
    for, which need not be the manager's own configured one."""
    manager = make_manager(db, state, broadcaster, secret, tmp_path, hostname=HOSTNAME)
    await manager.set_token(TOKEN)

    assert await manager.test_token("other.school.nz") is True

    assert FakeCloudflareClient.zone_lookups == ["other.school.nz"]


# -- verify_token: authentication only, no zone lookup -----------------


async def test_verify_token_true_and_never_reaches_the_zone_lookup(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    manager = make_manager(db, state, broadcaster, secret, tmp_path)
    await manager.set_token(TOKEN)
    FakeCloudflareClient.zone_error = CloudflareError("would fail if this were ever called")

    assert await manager.verify_token() is True
    assert FakeCloudflareClient.zone_lookups == []


async def test_verify_token_raises_cloudflares_own_reason(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    """The real defect: a rejected token used to reach ``manager.issue()`` and
    fail with an ACME-flavoured message. Cloudflare's own code and message
    must come straight through."""
    manager = make_manager(db, state, broadcaster, secret, tmp_path)
    await manager.set_token(TOKEN)
    FakeCloudflareClient.verify_error = CloudflareError(
        "Cloudflare rejected the request: 6003 Invalid request headers"
    )

    with pytest.raises(CloudflareError, match="6003 Invalid request headers"):
        await manager.verify_token()


async def test_verify_token_without_a_token_raises(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    manager = make_manager(db, state, broadcaster, secret, tmp_path)
    with pytest.raises(certs.TokenError):
        await manager.verify_token()


# -- the weekly check: renewal window, and Q6's degraded-time rule ------------------------


async def test_check_and_renew_does_nothing_when_not_due(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path, _patch_acme: FakeAcme,
) -> None:
    manager = make_manager(db, state, broadcaster, secret, tmp_path)
    await manager.set_token(TOKEN)
    await manager.issue(method="manual")  # a fresh 90-day certificate
    _patch_acme.calls = 0

    await manager.check_and_renew()

    assert _patch_acme.calls == 0


async def test_check_and_renew_renews_inside_the_30_day_window(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path, _patch_acme: FakeAcme,
) -> None:
    manager = make_manager(db, state, broadcaster, secret, tmp_path)
    await manager.set_token(TOKEN)
    near_expiry = _issued_certificate(days=10)
    certs.write_certificate_pair(
        tmp_path, HOSTNAME, near_expiry.key_pem, near_expiry.fullchain_pem
    )

    await manager.check_and_renew()

    assert _patch_acme.calls == 1
    info = await certs.certificate_card(tmp_path, HOSTNAME)
    assert info is not None and info.days_remaining > 30  # replaced with a fresh one


async def test_check_and_renew_skips_while_degraded_but_keeps_serving(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path, _patch_acme: FakeAcme,
) -> None:
    """Q6: a valid Let's Encrypt certificate is kept as-is; only renewal is skipped."""
    near_expiry = _issued_certificate(days=10)
    certs.write_certificate_pair(
        tmp_path, HOSTNAME, near_expiry.key_pem, near_expiry.fullchain_pem
    )
    manager = make_manager(db, state, broadcaster, secret, tmp_path, degraded=True)
    await manager.set_token(TOKEN)

    await manager.check_and_renew()

    assert _patch_acme.calls == 0
    info = await certs.certificate_card(tmp_path, HOSTNAME)
    assert info is not None
    assert info.issuer == near_expiry.issuer  # the same certificate, untouched
    assert info.self_signed is False


async def test_check_and_renew_degraded_with_no_valid_certificate_falls_back(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path, _patch_acme: FakeAcme,
) -> None:
    """Q6: fall back to self-signed only when nothing valid is being served."""
    manager = make_manager(db, state, broadcaster, secret, tmp_path, degraded=True)
    await manager.set_token(TOKEN)

    await manager.check_and_renew()

    assert _patch_acme.calls == 0  # never attempted issuance while degraded
    info = await certs.certificate_card(tmp_path, HOSTNAME)
    assert info is not None and info.self_signed is True
    history = await certs.renewal_history(tmp_path, HOSTNAME)
    assert history[-1].result == "skipped"


async def test_check_and_renew_falls_back_to_self_signed_on_issuance_failure_with_nothing_valid(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path, _patch_acme: FakeAcme,
) -> None:
    _patch_acme.fails_with = acme_client.AcmeError("Pebble unreachable")
    manager = make_manager(db, state, broadcaster, secret, tmp_path)
    await manager.set_token(TOKEN)

    await manager.check_and_renew()

    info = await certs.certificate_card(tmp_path, HOSTNAME)
    assert info is not None and info.self_signed is True


async def test_check_and_renew_keeps_a_still_valid_certificate_after_a_renewal_failure(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path, _patch_acme: FakeAcme,
) -> None:
    near_expiry = _issued_certificate(days=10)
    certs.write_certificate_pair(
        tmp_path, HOSTNAME, near_expiry.key_pem, near_expiry.fullchain_pem
    )
    manager = make_manager(db, state, broadcaster, secret, tmp_path)
    await manager.set_token(TOKEN)
    _patch_acme.fails_with = acme_client.AcmeError("Pebble unreachable")

    await manager.check_and_renew()

    info = await certs.certificate_card(tmp_path, HOSTNAME)
    assert info is not None
    assert info.self_signed is False  # the still-good certificate was not replaced
    assert info.issuer == near_expiry.issuer


async def test_check_and_renew_with_no_hostname_configured_does_nothing(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path, _patch_acme: FakeAcme,
) -> None:
    manager = make_manager(db, state, broadcaster, secret, tmp_path, hostname=None)
    await manager.check_and_renew()
    assert _patch_acme.calls == 0


# -- banners (§21.26, Q6's 14-day amber threshold) -----------------------------------------


async def test_expiring_banner_at_fourteen_days(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    manager = make_manager(db, state, broadcaster, secret, tmp_path)
    near = _issued_certificate(days=10)
    certs.write_certificate_pair(tmp_path, HOSTNAME, near.key_pem, near.fullchain_pem)

    await manager._update_banners(await certs.certificate_card(tmp_path, HOSTNAME))

    banner = state.system.banner(certs.CERT_EXPIRING_BANNER_KEY)
    assert banner is not None
    assert banner.level == "amber"
    # 9 or 10 days remaining depending on how much of the "10 days" has
    # elapsed since _issued_certificate built the certificate a moment ago.
    assert re.search(r"expires in (9|10) days", banner.text)


async def test_no_expiring_banner_comfortably_inside_the_window(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    manager = make_manager(db, state, broadcaster, secret, tmp_path)
    fresh = _issued_certificate(days=90)
    certs.write_certificate_pair(tmp_path, HOSTNAME, fresh.key_pem, fresh.fullchain_pem)

    await manager._update_banners(await certs.certificate_card(tmp_path, HOSTNAME))

    assert state.system.banner(certs.CERT_EXPIRING_BANNER_KEY) is None


async def test_self_signed_banner_only_when_a_token_is_configured(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    """Self-signed by deliberate choice (no token set) is not an error state."""
    manager = make_manager(db, state, broadcaster, secret, tmp_path)
    self_signed = await certs.issue_self_signed(HOSTNAME, data_dir=tmp_path)
    info = await certs.certificate_card(tmp_path, HOSTNAME)

    await manager._update_banners(info)
    assert state.system.banner(certs.CERT_SELF_SIGNED_BANNER_KEY) is None

    await manager.set_token(TOKEN)
    await manager._update_banners(info)
    banner = state.system.banner(certs.CERT_SELF_SIGNED_BANNER_KEY)
    assert banner is not None and banner.level == "red"
    assert self_signed.exists


async def test_cert_expiry_scalar_is_set_from_the_installed_certificate(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    manager = make_manager(db, state, broadcaster, secret, tmp_path)
    await manager.set_token(TOKEN)
    info = await manager.issue()
    assert state.system.cert_expiry == info.expires


# -- the renewal signal (auditorium-certbot-renew) -----------------------------------------


async def test_watch_consumes_the_signal_once(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path, _patch_acme: FakeAcme,
) -> None:
    manager = make_manager(db, state, broadcaster, secret, tmp_path)
    await manager.set_token(TOKEN)
    near = _issued_certificate(days=5)
    certs.write_certificate_pair(tmp_path, HOSTNAME, near.key_pem, near.fullchain_pem)

    certs.request_renewal_check(tmp_path)
    assert await manager.poll_signal() is True
    assert _patch_acme.calls == 1
    assert await manager.poll_signal() is False  # already consumed
    assert _patch_acme.calls == 1


# -- the startup fallback nginx needs to start at all (§3.2) ------------------------------
#
# The first boot after the first install, 24 September 2026: auditorium.conf
# names /data/certs/live/<fqdn>/, nothing had put a certificate there, nginx
# would not start, and the wizard and Certificates screen that could issue one
# were behind it.


def _site(tmp_path: Path, *names: str) -> Path:
    site = tmp_path / "auditorium.conf"
    site.write_text(
        "".join(
            f"    ssl_certificate     /data/certs/live/{name}/fullchain.pem;\n"
            f"    ssl_certificate_key /data/certs/live/{name}/privkey.pem;\n"
            for name in names
        ),
        encoding="utf-8",
    )
    return site


def _seeding_manager(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    data_dir: Path, site: Path, *, hostname: str | None = HOSTNAME,
) -> certs.CertificateManager:
    return certs.CertificateManager(
        state,
        db,
        broadcaster,  # type: ignore[arg-type]  # duck-typed: only .publish() is used
        secret,
        data_dir=data_dir,
        hostname=hostname,
        addresses=lambda: ["10.2.30.251"],
        site_config=site,
    )


async def test_a_name_nginx_serves_with_no_certificate_gets_a_self_signed_one(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    written: list[str] = []
    original = certs.write_certificate_pair

    def spy(data: Path, hostname: str, key_pem: bytes, cert_pem: bytes) -> certs.CertificatePaths:
        written.append(hostname)
        return original(data, hostname, key_pem, cert_pem)

    monkeypatch.setattr(certs, "write_certificate_pair", spy)
    manager = _seeding_manager(
        db, state, broadcaster, secret, data_dir, _site(tmp_path, HOSTNAME), hostname=None
    )

    assert await manager.ensure_served() == [HOSTNAME]

    # The one blessed path: a version directory and the live/ symlink swapped to it.
    assert written == [HOSTNAME]
    installed = certs.certificate_paths(data_dir, HOSTNAME)
    assert installed.directory.is_symlink()
    assert certs.certificate_installed(data_dir, HOSTNAME)
    info = await certs.describe(installed)
    assert info is not None and info.self_signed and info.domain == HOSTNAME
    # The reload request is what auditorium-cert-reload acts on: it starts nginx.
    assert certs.reload_sentinel_path(data_dir).is_file()
    history = await certs.renewal_history(data_dir, HOSTNAME)
    assert [(r.method, r.result) for r in history] == [("automatic", "success")]
    assert history[0].detail is not None and "§3.2" in history[0].detail


async def test_a_certificate_already_there_is_left_alone(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    """Every start calls this; only the first, on a fresh machine, may write anything."""
    data_dir = tmp_path / "data"
    issued = _issued_certificate()
    certs.write_certificate_pair(data_dir, HOSTNAME, issued.key_pem, issued.fullchain_pem)
    before = (certs.certificate_paths(data_dir, HOSTNAME).certificate).read_bytes()
    manager = _seeding_manager(db, state, broadcaster, secret, data_dir, _site(tmp_path, HOSTNAME))

    assert await manager.ensure_served() == []

    assert certs.certificate_paths(data_dir, HOSTNAME).certificate.read_bytes() == before
    assert not certs.reload_sentinel_path(data_dir).exists()


async def test_an_expired_certificate_is_not_the_fallback_s_to_replace(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    """nginx starts on an expired pair; replacing it is Q6's call, in check_and_renew."""
    data_dir = tmp_path / "data"
    expired = _issued_certificate(days=30, now=dt.datetime.now(dt.UTC) - dt.timedelta(days=60))
    certs.write_certificate_pair(data_dir, HOSTNAME, expired.key_pem, expired.fullchain_pem)
    manager = _seeding_manager(db, state, broadcaster, secret, data_dir, _site(tmp_path, HOSTNAME))
    assert await manager.ensure_served() == []


async def test_a_second_start_seeds_nothing(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    manager = _seeding_manager(db, state, broadcaster, secret, data_dir, _site(tmp_path, HOSTNAME))
    assert await manager.ensure_served() == [HOSTNAME]
    assert await manager.ensure_served() == []


async def test_without_an_nginx_site_nothing_is_seeded(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    """A development machine: no nginx, so no certificate appears unasked."""
    data_dir = tmp_path / "data"
    manager = _seeding_manager(
        db, state, broadcaster, secret, data_dir, tmp_path / "no-such-site.conf"
    )
    assert await manager.ensure_served() == []
    assert not (data_dir / "certs").exists()


async def test_the_seeded_certificate_is_what_the_card_reports(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    """With no Cloudflare token, a self-signed certificate is not a fault (§21.26)."""
    data_dir = tmp_path / "data"
    manager = _seeding_manager(db, state, broadcaster, secret, data_dir, _site(tmp_path, HOSTNAME))
    await manager.ensure_served()
    info = await certs.certificate_card(data_dir, HOSTNAME)
    assert info is not None
    assert state.system.cert_expiry == info.expires
    assert state.system.banner(certs.CERT_SELF_SIGNED_BANNER_KEY) is None


async def test_issue_refuses_a_hostname_nginx_does_not_serve(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    """``POST /system/certs/issue`` (proskenion/api/certs.py)
    goes through :meth:`CertificateManager.issue` — a certificate for a name
    nginx does not read one for would sit in ``.versions/`` unused, exactly
    the stray ``auditorium`` pair left on the real appliance (24 September
    2026) beside the real, served ``auditorium.obhs.school.nz`` one."""
    data_dir = tmp_path / "data"
    site = _site(tmp_path, "auditorium.obhs.school.nz")
    manager = _seeding_manager(
        db, state, broadcaster, secret, data_dir, site, hostname="auditorium"
    )

    with pytest.raises(certs.IssuanceError, match="auditorium.obhs.school.nz"):
        await manager.issue()

    assert not certs.certificate_paths(data_dir, "auditorium").exists


async def test_use_self_signed_refuses_a_hostname_nginx_does_not_serve(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    """Also proskenion/api/certs.py's ``use_self_signed`` (the Certificates
    screen's "Use self-signed") and the wizard's own self-signed option,
    which route the same way."""
    data_dir = tmp_path / "data"
    site = _site(tmp_path, "auditorium.obhs.school.nz")
    manager = _seeding_manager(
        db, state, broadcaster, secret, data_dir, site, hostname="auditorium"
    )

    with pytest.raises(certs.IssuanceError, match="auditorium.obhs.school.nz"):
        await manager.use_self_signed()

    assert not certs.certificate_paths(data_dir, "auditorium").exists


async def test_issue_permits_the_served_name(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    """The guard is not a blanket refusal — the name nginx actually serves
    still issues normally."""
    data_dir = tmp_path / "data"
    site = _site(tmp_path, HOSTNAME)
    manager = _seeding_manager(db, state, broadcaster, secret, data_dir, site, hostname=HOSTNAME)
    await manager.set_token(TOKEN)

    info = await manager.issue()

    assert info.domain == HOSTNAME


async def test_the_wizard_still_writes_its_own_certificate_over_the_fallback(
    db: Database, state: StateStore, broadcaster: FakeBroadcaster, secret: DeviceSecret,
    tmp_path: Path,
) -> None:
    """Seeding is not the wizard's step 6: its choice lands as the next version."""
    data_dir = tmp_path / "data"
    manager = _seeding_manager(db, state, broadcaster, secret, data_dir, _site(tmp_path, HOSTNAME))
    await manager.ensure_served()
    seeded = certs.certificate_paths(data_dir, HOSTNAME).certificate.read_bytes()

    await certs.issue_self_signed(HOSTNAME, data_dir=data_dir)

    live = certs.certificate_paths(data_dir, HOSTNAME)
    assert live.certificate.read_bytes() != seeded
    assert Path(os.readlink(live.directory)).name == "2"
