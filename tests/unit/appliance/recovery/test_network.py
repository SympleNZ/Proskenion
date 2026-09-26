"""Validating the recovery environment's own network settings."""

from __future__ import annotations

from types import ModuleType

import pytest


class TestCheckCidr:
    @pytest.mark.parametrize("address", ["10.2.30.45/24", "192.168.1.1/32", "0.0.0.0/0"])
    def test_valid(self, network: ModuleType, address: str) -> None:
        assert network.check_cidr(address) == address

    @pytest.mark.parametrize(
        "address", ["10.2.30.45", "10.2.30.45/33", "256.1.1.1/24", "10.2.30/24", "not-an-ip/24"]
    )
    def test_invalid(self, network: ModuleType, address: str) -> None:
        with pytest.raises(network.NetworkConfigError):
            network.check_cidr(address)


class TestCheckIp:
    def test_valid(self, network: ModuleType) -> None:
        assert network.check_ip("10.2.30.1", what="gateway") == "10.2.30.1"

    @pytest.mark.parametrize("address", ["10.2.30.1/24", "999.1.1.1", "", "not-an-ip"])
    def test_invalid(self, network: ModuleType, address: str) -> None:
        with pytest.raises(network.NetworkConfigError):
            network.check_ip(address, what="gateway")


class TestStaticSettings:
    def test_builds_from_valid_fields(self, network: ModuleType) -> None:
        settings = network.static_settings(
            device="eth0", address="10.2.30.50/24", gateway="10.2.30.1", dns=("10.2.30.1",)
        )
        assert settings.device == "eth0"
        assert settings.dns == ("10.2.30.1",)

    def test_no_device_is_refused(self, network: ModuleType) -> None:
        with pytest.raises(network.NetworkConfigError):
            network.static_settings(device="", address="10.2.30.50/24", gateway="10.2.30.1", dns=())

    def test_a_bad_dns_entry_is_refused(self, network: ModuleType) -> None:
        with pytest.raises(network.NetworkConfigError):
            network.static_settings(
                device="eth0", address="10.2.30.50/24", gateway="10.2.30.1", dns=("nope",)
            )


class TestStaticArgs:
    def test_builds_an_nmcli_profile(self, network: ModuleType) -> None:
        settings = network.static_settings(
            device="eth0",
            address="10.2.30.50/24",
            gateway="10.2.30.1",
            dns=("10.2.30.1", "8.8.8.8"),
        )
        args = network.static_args(settings)
        assert args[:3] == ["nmcli", "con", "add"]
        assert "eth0" in args
        assert "10.2.30.50/24" in args
        assert "10.2.30.1,8.8.8.8" in args


class TestDhcpArgs:
    def test_builds_a_dhcp_profile(self, network: ModuleType) -> None:
        args = network.dhcp_args("eth0")
        assert "ipv4.auto" not in args  # sanity: no stray key
        assert "auto" in args

    def test_no_device_is_refused(self, network: ModuleType) -> None:
        with pytest.raises(network.NetworkConfigError):
            network.dhcp_args("")
