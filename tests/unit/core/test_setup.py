"""The first-run wizard state machine (spec §10.4, §16.4)."""

import os
from pathlib import Path
from typing import Any

import pytest

from proskenion.config import Config
from proskenion.core import auth, certs, setup
from proskenion.core.bus import EventBus
from proskenion.core.cloudflare import CloudflareError, TxtRecord
from proskenion.core.platform import DevelopmentPlatform
from proskenion.core.secrets import DeviceSecret
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import system_state, users

ADMIN_PASSWORD = "admin-password-long"
OPERATOR_PASSWORD = "operator-password-long"


@pytest.fixture(autouse=True)
def cheap_bcrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wizard hashes up to three passwords per test; production cost is 12."""
    monkeypatch.setattr(auth, "BCRYPT_ROUNDS", 4)


def _ok_reload() -> certs.ReloadOutcome:
    return certs.ReloadOutcome(True, "nginx reloaded")


class _FakeBroadcaster:
    """Duck-typed: only ``.publish()`` is used by ``CertificateManager``."""

    def publish(self, message: Any, *, domain: str | None = None, cls: str = "discrete") -> int:
        return 0


class _FakeCloudflareRejectsToken:
    """Stands in for :class:`~proskenion.core.cloudflare.CloudflareClient`: every
    request behaves as Cloudflare does for a token it will not authenticate
    (the real appliance, 24 September 2026, a token's id
    pasted in place of the token itself)."""

    def __init__(self, token: str, **_kwargs: Any) -> None:
        self.token = token

    def __enter__(self) -> "_FakeCloudflareRejectsToken":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def verify_token(self) -> bool:
        raise CloudflareError("6003 Invalid request headers")

    def zone_id_for(self, hostname: str) -> str:
        raise AssertionError("zone_id_for must not be reached: verify_token already refused")

    def create_txt_record(self, zone_id: str, hostname: str, value: str) -> TxtRecord:
        raise AssertionError("issuance must not be reached: verify_token already refused")


def _certificate_manager(
    db: Database, dev_config: Config, tmp_path: Path, *, hostname: str = "av.school.nz"
) -> certs.CertificateManager:
    bus = EventBus()
    state = StateStore(dev_config, bus)
    return certs.CertificateManager(
        state,
        db,
        _FakeBroadcaster(),  # type: ignore[arg-type]
        DeviceSecret(os.urandom(32)),
        data_dir=tmp_path,
        hostname=hostname,
    )


async def _steps_one_and_two(db: Database) -> None:
    await setup.submit_welcome(db, locale="en_NZ.UTF-8", timezone=setup.SUPPORTED_TIMEZONE)
    await setup.submit_admin_password(
        db, password=ADMIN_PASSWORD, confirmation=ADMIN_PASSWORD
    )


async def _through_step_six(db: Database, tmp_path: Path) -> None:
    await _steps_one_and_two(db)
    await setup.submit_network(db, address="192.168.1.20", hostname="av.school.nz", skipped=True)
    await setup.submit_devices(db, device_ids=[2, 1], skipped=False)
    await setup.submit_operator_password(
        db, password=OPERATOR_PASSWORD, confirmation=OPERATOR_PASSWORD
    )
    await setup.submit_certificate(
        db,
        option="self_signed",
        hostname="av.school.nz",
        data_dir=tmp_path,
        addresses=["192.168.1.20"],
        reload_hook=_ok_reload,
    )


# -- detection ----------------------------------------------------------------------


async def test_first_run_is_the_absence_of_the_flag(db: Database) -> None:
    assert await setup.is_first_run(db) is True
    assert await setup.first_run_completed(db) is False
    await system_state.set(db, setup.SYSTEM_DOMAIN, setup.FIRST_RUN_COMPLETED_KEY, "true")
    assert await setup.is_first_run(db) is False


async def test_flag_caches_and_invalidates(db: Database) -> None:
    flag = setup.FirstRunFlag()
    assert flag.cached is None
    assert await flag.is_first_run(db) is True
    assert flag.cached is False  # read once, remembered

    await system_state.set(db, setup.SYSTEM_DOMAIN, setup.FIRST_RUN_COMPLETED_KEY, "true")
    assert await flag.is_first_run(db) is True  # still the cached answer
    flag.invalidate()
    assert await flag.is_first_run(db) is False

    fresh = setup.FirstRunFlag()
    fresh.mark_complete()
    assert await fresh.is_first_run(db) is False


async def test_empty_state_resumes_at_step_one(db: Database) -> None:
    state = await setup.load_state(db)
    assert state.next_step is setup.Step.WELCOME
    assert [r.completed for r in state.records.values()] == [False] * 7
    assert state.records[setup.Step.WELCOME].label == "Welcome"


async def test_unreadable_record_is_treated_as_incomplete(db: Database) -> None:
    await system_state.set(db, setup.SETUP_DOMAIN, "step_1", "not json")
    state = await setup.load_state(db)
    assert state.records[setup.Step.WELCOME].completed is False
    assert state.next_step is setup.Step.WELCOME


# -- resumability and abort safety (§10.4) --------------------------------------------


async def test_resumes_at_the_next_incomplete_step_with_summaries(db: Database) -> None:
    await _steps_one_and_two(db)

    # The client is discarded; the state is read afresh, as it would be on reload.
    state = await setup.load_state(db)
    assert state.next_step is setup.Step.NETWORK
    assert state.is_complete(setup.Step.WELCOME)
    assert state.is_complete(setup.Step.ADMIN_PASSWORD)
    assert state.records[setup.Step.WELCOME].summary == {
        "locale": "en_NZ.UTF-8",
        "timezone": "Pacific/Auckland",
    }
    assert state.records[setup.Step.ADMIN_PASSWORD].summary == {
        "admin_password_set": True,
        "operator_seeded": True,
    }
    assert state.records[setup.Step.ADMIN_PASSWORD].completed_at is not None
    assert state.incomplete == [
        setup.Step.NETWORK,
        setup.Step.DEVICES,
        setup.Step.OPERATOR_PASSWORD,
        setup.Step.CERTIFICATE,
        setup.Step.SUMMARY,
    ]


async def test_no_summary_carries_a_password(db: Database) -> None:
    await _steps_one_and_two(db)
    state = await setup.load_state(db)
    blob = repr(state.to_json())
    assert ADMIN_PASSWORD not in blob
    assert "password" not in str(state.records[setup.Step.WELCOME].summary)


async def test_abort_after_step_two_keeps_the_password_and_first_run(db: Database) -> None:
    await _steps_one_and_two(db)
    # Abort: nothing else runs. The admin password is real and usable...
    admin = await users.get_by_tier(db, "admin")
    assert admin is not None
    assert not admin.has_placeholder_password
    assert auth.verify_secret(ADMIN_PASSWORD, admin.password)
    # ...and the appliance is still in first-run mode until step 7 commits.
    assert await setup.is_first_run(db) is True


async def test_step_two_replaces_both_placeholder_hashes(db: Database) -> None:
    assert sorted(await setup.placeholder_tiers(db)) == ["admin", "operator"]
    await _steps_one_and_two(db)
    assert await setup.placeholder_tiers(db) == []
    operator = await users.get_by_tier(db, "operator")
    assert operator is not None
    # The seed password is random and never leaves the function that made it.
    assert not auth.verify_secret(ADMIN_PASSWORD, operator.password)
    assert operator.token_version == 1


async def test_step_five_replaces_the_temporary_operator_password(db: Database) -> None:
    await _steps_one_and_two(db)
    seeded = await users.get_by_tier(db, "operator")
    assert seeded is not None
    await setup.submit_network(db, address=None, hostname=None, skipped=True)
    await setup.submit_devices(db, device_ids=[], skipped=True)
    await setup.submit_operator_password(
        db, password=OPERATOR_PASSWORD, confirmation=OPERATOR_PASSWORD
    )
    operator = await users.get_by_tier(db, "operator")
    assert operator is not None
    assert operator.password != seeded.password
    assert auth.verify_secret(OPERATOR_PASSWORD, operator.password)
    assert operator.token_version == seeded.token_version + 1


async def test_a_step_may_be_resubmitted_and_overwrites_its_record(db: Database) -> None:
    await _steps_one_and_two(db)
    await setup.submit_welcome(db, locale="en_GB.UTF-8", timezone=setup.SUPPORTED_TIMEZONE)
    state = await setup.load_state(db)
    assert state.records[setup.Step.WELCOME].summary["locale"] == "en_GB.UTF-8"
    assert state.next_step is setup.Step.NETWORK  # revisiting does not undo step 2


# -- ordering and validation ----------------------------------------------------------


async def test_a_step_out_of_order_is_refused(db: Database) -> None:
    with pytest.raises(setup.StepRejected) as exc:
        await setup.submit_devices(db, device_ids=[], skipped=True)
    assert exc.value.fields[0]["field"] == "step"
    assert exc.value.fields[0]["type"] == "out_of_order"
    assert (await setup.load_state(db)).next_step is setup.Step.WELCOME


async def test_short_password_and_mismatch_are_refused_per_field(db: Database) -> None:
    await setup.submit_welcome(db, locale="en_NZ.UTF-8", timezone=setup.SUPPORTED_TIMEZONE)
    with pytest.raises(setup.StepRejected) as short:
        await setup.submit_admin_password(
            db, password="01234567890", confirmation="01234567890"  # 11 characters
        )
    assert [f["field"] for f in short.value.fields] == ["password"]
    assert short.value.fields[0]["type"] == "too_short"

    with pytest.raises(setup.StepRejected) as mismatch:
        await setup.submit_admin_password(
            db, password=ADMIN_PASSWORD, confirmation=ADMIN_PASSWORD + "x"
        )
    assert [f["field"] for f in mismatch.value.fields] == ["password_confirm"]
    assert mismatch.value.fields[0]["type"] == "mismatch"

    # Neither attempt wrote anything.
    assert (await setup.load_state(db)).next_step is setup.Step.ADMIN_PASSWORD
    admin = await users.get_by_tier(db, "admin")
    assert admin is not None and admin.has_placeholder_password


async def test_welcome_refuses_another_timezone(db: Database) -> None:
    with pytest.raises(setup.StepRejected) as exc:
        await setup.submit_welcome(db, locale="en_NZ.UTF-8", timezone="Europe/London")
    assert exc.value.fields[0]["field"] == "timezone"


async def test_certificate_step_refuses_lets_encrypt_with_no_token(
    db: Database, tmp_path: Path
) -> None:
    """A missing token is the admin's mistake — rejected, not silently swapped for self-signed."""
    await _steps_one_and_two(db)
    await setup.submit_network(db, address=None, hostname=None, skipped=True)
    await setup.submit_devices(db, device_ids=[], skipped=True)
    await setup.submit_operator_password(
        db, password=OPERATOR_PASSWORD, confirmation=OPERATOR_PASSWORD
    )
    with pytest.raises(setup.StepRejected) as exc:
        await setup.submit_certificate(
            db,
            option="lets_encrypt",
            hostname="av.school.nz",
            data_dir=tmp_path,
            reload_hook=_ok_reload,
        )
    assert exc.value.fields[0]["type"] == "required"
    assert exc.value.fields[0]["field"] == "token"


async def test_certificate_step_falls_back_to_self_signed_with_no_manager(
    db: Database, tmp_path: Path
) -> None:
    """First-run must never be blocked by a failed issuance (Q7) — even with a token supplied."""
    await _steps_one_and_two(db)
    await setup.submit_network(db, address=None, hostname=None, skipped=True)
    await setup.submit_devices(db, device_ids=[], skipped=True)
    await setup.submit_operator_password(
        db, password=OPERATOR_PASSWORD, confirmation=OPERATOR_PASSWORD
    )
    record = await setup.submit_certificate(
        db,
        option="lets_encrypt",
        hostname="av.school.nz",
        data_dir=tmp_path,
        reload_hook=_ok_reload,
        token="cf-token",
        manager=None,  # nothing wired: the expected shape of a bench without ACME
    )
    assert record.summary["option"] == "self_signed"
    assert record.summary["requested_option"] == "lets_encrypt"
    assert record.summary["fallback_reason"]
    assert certs.certificate_paths(tmp_path, "av.school.nz").exists


async def test_certificate_step_refuses_a_token_cloudflare_rejects(
    db: Database, dev_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the fix, a rejected token went straight to
    issuance — it failed halfway with an ACME-flavoured message and silently
    wrote a self-signed fallback, exactly what happened on the real appliance
    (24 September 2026) when the token pasted was actually its id. The
    wizard must refuse up front, with Cloudflare's own reason, and write
    nothing at all — not even the self-signed fallback ``manager=None``
    legitimately gets above.
    """
    monkeypatch.setattr(certs, "CloudflareClient", _FakeCloudflareRejectsToken)
    manager = _certificate_manager(db, dev_config, tmp_path)

    await _steps_one_and_two(db)
    await setup.submit_network(db, address=None, hostname=None, skipped=True)
    await setup.submit_devices(db, device_ids=[], skipped=True)
    await setup.submit_operator_password(
        db, password=OPERATOR_PASSWORD, confirmation=OPERATOR_PASSWORD
    )

    with pytest.raises(setup.StepRejected) as exc:
        await setup.submit_certificate(
            db,
            option="lets_encrypt",
            hostname="av.school.nz",
            data_dir=tmp_path,
            reload_hook=_ok_reload,
            token="cf-token-id-not-the-secret",
            manager=manager,
        )
    assert "6003 Invalid request headers" in exc.value.message
    assert exc.value.fields[0]["field"] == "token"
    # No fallback certificate was written — unlike the "no manager" case above.
    assert not certs.certificate_paths(tmp_path, "av.school.nz").exists
    state = await setup.load_state(db)
    assert state.records[setup.Step.CERTIFICATE].completed is False


# -- a certificate for a name nginx does not serve is refused --


async def test_certificate_step_refuses_a_hostname_nginx_does_not_serve(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checked for both options, before either is attempted — the self-signed
    option never reaches a :class:`~proskenion.core.certs.CertificateManager`
    at all, so the guard has to live here too, not only inside it (see
    ``tests/unit/core/test_cert_manager.py`` for the manager's own guard,
    which the admin Certificates screen relies on)."""
    site = tmp_path / "auditorium.conf"
    site.write_text(
        "    ssl_certificate     /data/certs/live/auditorium.obhs.school.nz/fullchain.pem;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(certs, "NGINX_SITE_CONFIG", site)

    await _steps_one_and_two(db)
    await setup.submit_network(db, address=None, hostname=None, skipped=True)
    await setup.submit_devices(db, device_ids=[], skipped=True)
    await setup.submit_operator_password(
        db, password=OPERATOR_PASSWORD, confirmation=OPERATOR_PASSWORD
    )

    with pytest.raises(setup.StepRejected) as exc:
        await setup.submit_certificate(
            db,
            option="self_signed",
            hostname="auditorium",
            data_dir=tmp_path / "data",
            reload_hook=_ok_reload,
        )
    assert exc.value.fields[0]["field"] == "hostname"
    assert "auditorium.obhs.school.nz" in exc.value.message
    assert not certs.certificate_paths(tmp_path / "data", "auditorium").exists


def test_lets_encrypt_option_is_reported_available() -> None:
    options = {option["id"]: option for option in setup.certificate_options()}
    assert options["self_signed"]["available"] is True
    assert options["lets_encrypt"]["available"] is True
    assert options["lets_encrypt"]["reason"] is None
    assert options["self_signed"]["guidance"]  # the §6.16 iOS trust steps


def test_lets_encrypt_option_reports_the_unavailable_reason_when_asked() -> None:
    options = {
        option["id"]: option
        for option in setup.certificate_options(lets_encrypt_available=False)
    }
    assert options["lets_encrypt"]["available"] is False
    assert options["lets_encrypt"]["reason"]


# -- the certificate step -------------------------------------------------------------


async def test_certificate_step_records_a_failed_reload(db: Database, tmp_path: Path) -> None:
    await _steps_one_and_two(db)
    await setup.submit_network(db, address=None, hostname=None, skipped=True)
    await setup.submit_devices(db, device_ids=[], skipped=True)
    await setup.submit_operator_password(
        db, password=OPERATOR_PASSWORD, confirmation=OPERATOR_PASSWORD
    )

    def refuses() -> certs.ReloadOutcome:
        raise PermissionError("systemctl: access denied")

    record = await setup.submit_certificate(
        db,
        option="self_signed",
        hostname="av.school.nz",
        data_dir=tmp_path,
        reload_hook=refuses,
    )
    assert record.completed is True
    assert record.summary["nginx_reloaded"] is False
    assert "access denied" in record.summary["nginx_detail"]
    # The certificate survived the failed reload.
    assert certs.certificate_paths(tmp_path, "av.school.nz").exists
    assert record.summary["self_signed"] is True


# -- commit ---------------------------------------------------------------------------


async def test_commit_is_refused_while_a_step_is_incomplete(db: Database) -> None:
    await _steps_one_and_two(db)
    with pytest.raises(setup.StepRejected) as exc:
        await setup.commit(db)
    fields = {f["field"] for f in exc.value.fields}
    assert {"step_3", "step_4", "step_5", "step_6"} <= fields
    assert await setup.is_first_run(db) is True


async def test_commit_is_refused_while_a_placeholder_remains(
    db: Database, tmp_path: Path
) -> None:
    await _through_step_six(db, tmp_path)
    # Put the operator back to the seed placeholder: an appliance nobody can
    # sign in to must not be committable.
    await users.set_password_hash(db, "operator", users.PLACEHOLDER_HASH_PREFIX + ".x")
    with pytest.raises(setup.StepRejected) as exc:
        await setup.commit(db)
    assert any(f["type"] == "placeholder" for f in exc.value.fields)
    assert await setup.is_first_run(db) is True


async def test_commit_sets_the_flag_and_records_the_event(
    db: Database, tmp_path: Path
) -> None:
    await _through_step_six(db, tmp_path)
    state = await setup.commit(db, ip_address="192.168.1.50")
    assert state.next_step is None
    assert state.records[setup.Step.SUMMARY].summary["committed"] is True
    assert await setup.first_run_completed(db) is True

    async with db.read() as conn:
        cursor = await conn.execute(
            "SELECT event_type, detail FROM security_events WHERE event_type = 'config_changed'"
        )
        rows = await cursor.fetchall()
    assert len(rows) == 1
    assert "first_run_completed" in rows[0]["detail"]


async def test_everything_refuses_once_committed(db: Database, tmp_path: Path) -> None:
    await _through_step_six(db, tmp_path)
    await setup.commit(db)
    with pytest.raises(setup.SetupAlreadyComplete):
        await setup.submit_welcome(db, locale="en_NZ.UTF-8", timezone=setup.SUPPORTED_TIMEZONE)
    with pytest.raises(setup.SetupAlreadyComplete) as exc:
        await setup.commit(db)
    assert "database reset" in exc.value.message


async def test_review_step_records_progress(db: Database, tmp_path: Path) -> None:
    await _through_step_six(db, tmp_path)
    record = await setup.submit_review(db)
    assert record.summary["reviewed"] is True
    assert record.summary["steps_complete"] == [1, 2, 3, 4, 5, 6]


# -- environment ----------------------------------------------------------------------


async def test_detect_environment_reports_the_platform_and_timezone() -> None:
    environment = await setup.detect_environment(
        DevelopmentPlatform(), configured_hostname="av.school.nz"
    )
    assert environment.platform == "development"
    assert environment.hostname == "av.school.nz"
    assert environment.locale
    assert environment.to_json()["timezone"] == environment.timezone
    # The address is whatever the routing table offers; it may be absent.
    assert environment.address is None or environment.address.count(".") == 3


# -- the detected timezone (§10.4) ---------------------------------------------


def _link_or_skip(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("this machine cannot create symbolic links")


def test_localtime_wins_over_a_stale_etc_timezone(tmp_path: Path) -> None:
    """``timedatectl set-timezone`` re-points /etc/localtime and leaves the
    older /etc/timezone as it was, so the link is the truth."""
    localtime = tmp_path / "localtime"
    _link_or_skip(localtime, Path("/usr/share/zoneinfo/Pacific/Auckland"))
    stale = tmp_path / "timezone"
    stale.write_text("Etc/UTC\n", encoding="utf-8")
    assert setup._detected_timezone(localtime=localtime, etc_timezone=stale) == "Pacific/Auckland"


def test_etc_timezone_is_the_fallback_when_localtime_is_not_a_link(tmp_path: Path) -> None:
    copy = tmp_path / "localtime"
    copy.write_bytes(b"TZif")
    named = tmp_path / "timezone"
    named.write_text("Pacific/Auckland\n", encoding="utf-8")
    assert setup._detected_timezone(localtime=copy, etc_timezone=named) == "Pacific/Auckland"


def test_nothing_readable_means_the_supported_zone(tmp_path: Path) -> None:
    assert (
        setup._detected_timezone(localtime=tmp_path / "absent", etc_timezone=tmp_path / "absent2")
        == setup.SUPPORTED_TIMEZONE
    )
