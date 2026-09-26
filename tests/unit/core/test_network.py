"""Network configuration: validation, the pending-change marker, and applying
(spec §21.24, §10.8; contracts §4, §5).

The revert *firing* — the root-side half of confirm-or-revert — is tested
against ``auditorium-helper``'s ``check_network_revert`` in
``tests/unit/appliance/test_helper.py``; this file covers the application's
side: what gets validated before anything is written, and what
:func:`~proskenion.core.network.begin_change` leaves on disk for that root-side
check to read.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from proskenion.core import certs, network, system_config
from proskenion.core.cloudflare import CloudflareError
from proskenion.core.helper import HelperClient
from proskenion.core.secrets import DeviceSecret

AUCKLAND = ZoneInfo("Pacific/Auckland")
AUCKLAND_NOW = datetime.fromisoformat("2026-09-20T12:00:00+12:00")

# -- validate ---------------------------------------------------------------------------


def test_a_valid_submission_is_accepted_and_normalised() -> None:
    settings = network.validate(
        hostname="AUDITORIUM",
        address="10.2.30.45",
        prefix_length=24,
        gateway="10.2.30.1",
        dns=["10.2.30.1", "10.2.30.2"],
    )
    assert settings.hostname == "auditorium"
    assert settings.address == "10.2.30.45"
    assert settings.prefix_length == 24
    assert settings.gateway == "10.2.30.1"
    assert settings.dns == ["10.2.30.1", "10.2.30.2"]
    assert settings.cidr == "10.2.30.45/24"


def test_an_invalid_hostname_is_rejected() -> None:
    with pytest.raises(network.NetworkValidationError) as excinfo:
        network.validate(
            hostname="Not A Hostname!",
            address="10.2.30.45",
            prefix_length=24,
            gateway="10.2.30.1",
            dns=["10.2.30.1"],
        )
    assert "hostname" in excinfo.value.fields


def test_a_malformed_address_is_rejected() -> None:
    with pytest.raises(network.NetworkValidationError) as excinfo:
        network.validate(
            hostname="auditorium",
            address="not-an-address",
            prefix_length=24,
            gateway="10.2.30.1",
            dns=["10.2.30.1"],
        )
    assert "address" in excinfo.value.fields


def test_a_gateway_off_the_subnet_is_rejected() -> None:
    with pytest.raises(network.NetworkValidationError) as excinfo:
        network.validate(
            hostname="auditorium",
            address="10.2.30.45",
            prefix_length=24,
            gateway="10.9.9.1",
            dns=["10.2.30.1"],
        )
    assert "gateway" in excinfo.value.fields


def test_an_invalid_dns_server_is_rejected() -> None:
    with pytest.raises(network.NetworkValidationError) as excinfo:
        network.validate(
            hostname="auditorium",
            address="10.2.30.45",
            prefix_length=24,
            gateway="10.2.30.1",
            dns=["not-an-ip"],
        )
    assert "dns" in excinfo.value.fields


def test_no_dns_servers_at_all_is_rejected() -> None:
    with pytest.raises(network.NetworkValidationError) as excinfo:
        network.validate(
            hostname="auditorium",
            address="10.2.30.45",
            prefix_length=24,
            gateway="10.2.30.1",
            dns=[],
        )
    assert "dns" in excinfo.value.fields


def test_an_address_already_used_by_a_device_is_rejected() -> None:
    """§21.24: "no conflict with a device address from §3.1"."""
    with pytest.raises(network.NetworkValidationError) as excinfo:
        network.validate(
            hostname="auditorium",
            address="10.2.30.249",
            prefix_length=24,
            gateway="10.2.30.1",
            dns=["10.2.30.1"],
            device_addresses=["10.2.30.249"],
        )
    assert "address" in excinfo.value.fields


def test_every_failing_field_is_reported_together_not_just_the_first() -> None:
    with pytest.raises(network.NetworkValidationError) as excinfo:
        network.validate(
            hostname="Not Valid!",
            address="also not valid",
            prefix_length=24,
            gateway="also not valid",
            dns=["also not valid"],
        )
    assert set(excinfo.value.fields) == {"hostname", "address", "gateway", "dns"}


# -- the pending-change marker ------------------------------------------------------


def test_begin_change_writes_the_new_settings_to_system_json(tmp_path: Path) -> None:
    system_config.merge(
        tmp_path,
        {
            "hostname": "old-host",
            "network": {
                "vlan": "10.2.30.0/24",
                "address": "10.2.30.10/24",
                "gateway": "10.2.30.1",
                "dns": ["10.2.30.1"],
            },
        },
    )
    settings = network.validate(
        hostname="new-host",
        address="10.2.30.50",
        prefix_length=24,
        gateway="10.2.30.1",
        dns=["10.2.30.1"],
    )
    network.begin_change(tmp_path, settings, now=AUCKLAND_NOW)

    doc = system_config.read(tmp_path)
    assert doc["hostname"] == "new-host"
    assert doc["network"]["address"] == "10.2.30.50/24"
    # A key this call does not own survives (read-merge-write).
    assert doc["network"]["vlan"] == "10.2.30.0/24"


def test_read_pending_reports_the_previous_address(tmp_path: Path) -> None:
    system_config.merge(
        tmp_path,
        {"hostname": "old-host", "network": {"address": "10.2.30.10/24", "gateway": "10.2.30.1"}},
    )
    settings = network.validate(
        hostname="new-host",
        address="10.2.30.50",
        prefix_length=24,
        gateway="10.2.30.1",
        dns=["10.2.30.1"],
    )
    token = network.begin_change(tmp_path, settings, now=AUCKLAND_NOW)

    pending = network.read_pending(tmp_path)
    assert pending is not None
    assert pending.confirm_token == token
    assert pending.previous_address == "10.2.30.10/24"
    assert pending.applied_at == AUCKLAND_NOW.isoformat(timespec="seconds")
    assert pending.reverts_at == (AUCKLAND_NOW + timedelta(minutes=3)).isoformat(
        timespec="seconds"
    )


@pytest.mark.parametrize(
    "applied",
    [
        # A minute before clocks go forward (02:00 → 03:00, 27 September 2026).
        datetime(2026, 9, 27, 1, 59, tzinfo=AUCKLAND),
        # A minute before clocks go back (03:00 → 02:00, 4 April 2027), on the
        # first pass through the repeated hour.
        datetime(2027, 4, 4, 2, 59, tzinfo=AUCKLAND),
        # Half an hour before the end of the repeated hour, on its second pass.
        datetime(2027, 4, 4, 2, 30, fold=1, tzinfo=AUCKLAND),
    ],
    ids=["spring-forward", "fall-back-first-pass", "fall-back-second-pass"],
)
def test_the_confirm_window_is_three_real_minutes_across_a_daylight_saving_change(
    tmp_path: Path, applied: datetime
) -> None:
    settings = network.validate(
        hostname="new-host",
        address="10.2.30.50",
        prefix_length=24,
        gateway="10.2.30.1",
        dns=["10.2.30.1"],
    )
    network.begin_change(tmp_path, settings, now=applied)
    pending = network.read_pending(tmp_path)
    assert pending is not None
    reverts_at = datetime.fromisoformat(pending.reverts_at)
    assert reverts_at.timestamp() - applied.timestamp() == 180
    # And written as the wall clock will read then, not as a time that never
    # exists (02:02+12:00 on the morning the clocks skip that hour).
    assert reverts_at.astimezone(AUCKLAND).isoformat(timespec="seconds") == pending.reverts_at


def test_read_pending_is_none_with_nothing_pending(tmp_path: Path) -> None:
    assert network.read_pending(tmp_path) is None


def test_confirm_change_clears_a_matching_pending_change(tmp_path: Path) -> None:
    settings = network.validate(
        hostname="new-host",
        address="10.2.30.50",
        prefix_length=24,
        gateway="10.2.30.1",
        dns=["10.2.30.1"],
    )
    token = network.begin_change(tmp_path, settings, now=AUCKLAND_NOW)

    assert network.confirm_change(tmp_path, token) is True
    assert network.read_pending(tmp_path) is None


def test_confirm_change_refuses_a_stale_or_wrong_token(tmp_path: Path) -> None:
    settings = network.validate(
        hostname="new-host",
        address="10.2.30.50",
        prefix_length=24,
        gateway="10.2.30.1",
        dns=["10.2.30.1"],
    )
    network.begin_change(tmp_path, settings, now=AUCKLAND_NOW)

    assert network.confirm_change(tmp_path, "not-the-real-token") is False
    # And the real pending change is untouched — a wrong guess must not
    # cancel someone else's in-flight change.
    assert network.read_pending(tmp_path) is not None


def test_confirm_change_with_nothing_pending_is_false(tmp_path: Path) -> None:
    assert network.confirm_change(tmp_path, "anything") is False


# -- apply_change ---------------------------------------------------------------------


def _settings() -> network.NetworkSettings:
    return network.validate(
        hostname="auditorium",
        address="10.2.30.50",
        prefix_length=24,
        gateway="10.2.30.1",
        dns=["10.2.30.1"],
    )


async def test_apply_change_submits_apply_network_to_the_helper(tmp_path: Path) -> None:
    helper = HelperClient(tmp_path)
    change = await network.apply_change(tmp_path, helper, _settings())

    requests = list((tmp_path / "run" / "helper").glob("*.json"))
    assert len(requests) == 1
    assert '"verb": "apply-network"' in requests[0].read_text(encoding="utf-8")
    assert change.confirm_token
    assert change.dns_updated is False  # no secret given: DNS is not touched


async def test_apply_change_without_a_secret_never_touches_dns(tmp_path: Path) -> None:
    helper = HelperClient(tmp_path)
    change = await network.apply_change(tmp_path, helper, _settings(), secret=None)
    assert change.dns_updated is False


async def test_apply_change_with_a_secret_but_no_token_leaves_dns_updated_false(
    tmp_path: Path,
) -> None:
    helper = HelperClient(tmp_path)
    secret = DeviceSecret(b"0" * 32)
    change = await network.apply_change(tmp_path, helper, _settings(), secret=secret)
    assert change.dns_updated is False


class _FakeCloudflareClient:
    """Stands in for :class:`~proskenion.core.cloudflare.CloudflareClient`:
    proves ``apply_change`` calls exactly ``zone_id_for`` then
    ``upsert_a_record`` with the settings' hostname and address, without a
    real Cloudflare API."""

    calls: list[tuple[str, str, str]] = []
    fail = False

    def __init__(self, token: str, **_kwargs: Any) -> None:
        self.token = token

    def __enter__(self) -> _FakeCloudflareClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        return None

    def zone_id_for(self, hostname: str) -> str:
        return f"zone-for-{hostname}"

    def upsert_a_record(self, zone_id: str, hostname: str, address: str) -> None:
        if self.fail:
            raise CloudflareError("simulated failure")
        type(self).calls.append((zone_id, hostname, address))


@pytest.fixture(autouse=True)
def _reset_fake_cloudflare() -> None:
    _FakeCloudflareClient.calls = []
    _FakeCloudflareClient.fail = False


async def test_apply_change_updates_dns_when_a_token_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(network, "CloudflareClient", _FakeCloudflareClient)
    secret = DeviceSecret(b"1" * 32)
    await certs.store_token(tmp_path, "cf-token", secret)
    helper = HelperClient(tmp_path)

    change = await network.apply_change(tmp_path, helper, _settings(), secret=secret)

    assert change.dns_updated is True
    assert _FakeCloudflareClient.calls == [
        ("zone-for-auditorium", "auditorium", "10.2.30.50")
    ]


async def test_a_cloudflare_failure_does_not_block_the_address_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(network, "CloudflareClient", _FakeCloudflareClient)
    _FakeCloudflareClient.fail = True
    secret = DeviceSecret(b"2" * 32)
    await certs.store_token(tmp_path, "cf-token", secret)
    helper = HelperClient(tmp_path)

    change = await network.apply_change(tmp_path, helper, _settings(), secret=secret)

    assert change.dns_updated is False
    # The address change itself still went ahead.
    assert network.read_pending(tmp_path) is not None
    assert system_config.read(tmp_path)["network"]["address"] == "10.2.30.50/24"
