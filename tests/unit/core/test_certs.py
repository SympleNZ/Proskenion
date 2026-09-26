"""Self-signed certificates and the nginx reload hook (spec §6.16, §10.4, §21.24)."""

import datetime as dt
import ipaddress
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.x509.oid import NameOID

from proskenion.core import certs
from proskenion.core.secrets import DeviceSecret
from proskenion.db.crud.base import AUCKLAND

HOSTNAME = "av.school.nz"
ADDRESS = "192.168.1.20"

NGINX_CONF = Path(__file__).resolve().parents[3] / "appliance" / "nginx" / "auditorium.conf"


def _load(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


@pytest.fixture
def issued(tmp_path: Path) -> certs.CertificatePaths:
    return certs.generate_self_signed(
        HOSTNAME,
        directory=certs.certificate_paths(tmp_path, HOSTNAME).directory,
        addresses=[ADDRESS],
        key_size=2048,
    )


# -- the path nginx reads -------------------------------------------------------------


def test_paths_match_what_nginx_is_configured_to_read() -> None:
    """The layout is not a convention we may change: nginx has it hard-coded."""
    conf = NGINX_CONF.read_text(encoding="utf-8")
    certificate = re.search(r"ssl_certificate\s+(\S+);", conf)
    key = re.search(r"ssl_certificate_key\s+(\S+);", conf)
    assert certificate and key
    fqdn = certificate.group(1).split("/")[-2]
    paths = certs.certificate_paths(Path("/data"), fqdn)
    assert paths.certificate.as_posix() == certificate.group(1)
    assert paths.key.as_posix() == key.group(1)


def test_the_name_nginx_serves_is_read_from_its_site() -> None:
    """§4.14's bootstrap file has no hostname; the site file is what nginx loads from."""
    assert certs.served_hostnames(NGINX_CONF) == [HOSTNAME]


def test_emergency_mode_is_owed_no_certificate_on_data() -> None:
    """emergency.conf serves /srv/appliance's pair and must never depend on /data (§4.6)."""
    emergency = NGINX_CONF.with_name("emergency.conf")
    assert certs.served_hostnames(emergency) == []


def test_a_commented_or_malformed_directive_names_nothing(tmp_path: Path) -> None:
    site = tmp_path / "site.conf"
    site.write_text(
        "    # ssl_certificate /data/certs/live/old.school.nz/fullchain.pem;\n"
        "    ssl_certificate /data/certs/live/-bad-/fullchain.pem;\n"
        "    ssl_certificate     /data/certs/live/av.school.nz/fullchain.pem;\n"
        "    ssl_certificate_key /data/certs/live/av.school.nz/privkey.pem;\n"
        "    ssl_certificate /data/certs/live/av.school.nz/fullchain.pem;\n",
        encoding="utf-8",
    )
    assert certs.served_hostnames(site) == [HOSTNAME]


def test_no_site_file_names_nothing(tmp_path: Path) -> None:
    """A development machine has no nginx site, and nothing is seeded there."""
    assert certs.served_hostnames(tmp_path / "absent.conf") == []


def test_certificate_installed_needs_a_certificate_and_a_key(
    tmp_path: Path, issued: certs.CertificatePaths
) -> None:
    assert certs.certificate_installed(tmp_path, HOSTNAME)
    issued.key.unlink()
    assert not certs.certificate_installed(tmp_path, HOSTNAME)
    assert not certs.certificate_installed(tmp_path, "other.school.nz")
    issued.certificate.write_text("not a certificate", encoding="utf-8")
    issued.key.write_text("a key", encoding="utf-8")
    assert not certs.certificate_installed(tmp_path, HOSTNAME)


def test_certificate_is_written_where_nginx_reads_it(
    tmp_path: Path, issued: certs.CertificatePaths
) -> None:
    expected = tmp_path / "certs" / "live" / HOSTNAME
    assert issued.directory == expected
    assert issued.certificate == expected / "fullchain.pem"
    assert issued.key == expected / "privkey.pem"
    assert issued.exists
    assert issued.certificate.read_text().startswith("-----BEGIN CERTIFICATE-----")
    assert "PRIVATE KEY" in issued.key.read_text()
    assert not list(expected.glob("*.tmp"))  # written atomically, nothing left behind


# -- content --------------------------------------------------------------------------


def test_certificate_parses_back_with_the_hostname_and_alt_names(
    issued: certs.CertificatePaths,
) -> None:
    certificate = _load(issued.certificate)
    common = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    assert common == HOSTNAME
    alt = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert alt.get_values_for_type(x509.DNSName) == [HOSTNAME]
    assert alt.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address(ADDRESS)]
    assert certificate.issuer == certificate.subject  # self-signed


def test_certificate_is_valid_for_a_year(issued: certs.CertificatePaths) -> None:
    certificate = _load(issued.certificate)
    span = certificate.not_valid_after_utc - certificate.not_valid_before_utc
    assert dt.timedelta(days=364) < span <= dt.timedelta(days=366)


def test_a_name_alt_name_is_kept_as_a_dns_name(tmp_path: Path) -> None:
    paths = certs.generate_self_signed(
        HOSTNAME,
        directory=tmp_path / "certs",
        addresses=["auditorium.local", "not an address"],
    )
    alt = _load(paths.certificate).extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    assert alt.get_values_for_type(x509.DNSName) == [HOSTNAME, "auditorium.local"]


def test_a_blank_hostname_is_refused(tmp_path: Path) -> None:
    with pytest.raises(certs.CertificateError):
        certs.generate_self_signed("  ", directory=tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_the_key_is_not_world_readable(issued: certs.CertificatePaths) -> None:
    mode = stat.S_IMODE(os.stat(issued.key).st_mode)
    assert mode == certs.KEY_MODE
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO)


# -- description (§21.24) -------------------------------------------------------------


async def test_describe_reports_self_signed_and_a_sensible_expiry(
    issued: certs.CertificatePaths,
) -> None:
    info = await certs.describe(issued)
    assert info is not None
    assert info.domain == HOSTNAME
    assert info.issuer == HOSTNAME
    assert info.self_signed is True
    assert info.renewal_history == []
    assert 362 <= info.days_remaining <= 365
    assert info.expired is False
    # ISO 8601 with the Pacific/Auckland offset (§4.9).
    assert dt.datetime.fromisoformat(info.issued).utcoffset() is not None
    assert dt.datetime.fromisoformat(info.expires) > dt.datetime.fromisoformat(info.issued)


async def test_describe_of_an_expired_certificate(issued: certs.CertificatePaths) -> None:
    later = dt.datetime.now(tz=AUCKLAND) + dt.timedelta(days=400)
    info = await certs.describe(issued, now=later)
    assert info is not None
    assert info.days_remaining < 0
    assert info.expired is True


async def test_describe_without_a_certificate_is_none(tmp_path: Path) -> None:
    assert await certs.describe(certs.certificate_paths(tmp_path, HOSTNAME)) is None


def test_load_certificate_rejects_rubbish(tmp_path: Path) -> None:
    path = tmp_path / "fullchain.pem"
    path.write_text("not a certificate")
    with pytest.raises(certs.CertificateError):
        certs.load_certificate(path)


# -- reloading nginx ------------------------------------------------------------------


async def test_a_failing_reload_is_reported_not_raised(issued: certs.CertificatePaths) -> None:
    def refuses() -> certs.ReloadOutcome:
        raise PermissionError("systemctl: access denied")

    outcome = await certs.reload_nginx(refuses)
    assert outcome.ok is False
    assert "access denied" in outcome.detail
    # The certificate written before the reload is untouched.
    assert issued.exists
    assert _load(issued.certificate).subject.rfc4514_string()


async def test_a_successful_reload_is_reported() -> None:
    outcome = await certs.reload_nginx(lambda: certs.ReloadOutcome(True, "nginx reloaded"))
    assert outcome.ok is True


async def test_default_reload_performs_no_subprocess_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """auditorium-cert-reload.path owns the reload now; the app must not shell out."""

    def explodes(*args: object, **kwargs: object) -> object:
        raise AssertionError("the default reload hook must not run a subprocess")

    monkeypatch.setattr(subprocess, "run", explodes)
    outcome = await certs.reload_nginx()
    assert outcome.ok is True
    assert "auditorium-cert-reload.path" in outcome.detail


def test_default_hook_reports_a_missing_systemctl() -> None:
    outcome = certs.systemctl_reload_nginx(argv=("definitely-not-a-command-9f2b",))
    assert outcome.ok is False
    assert "not available" in outcome.detail


def test_default_hook_reports_a_non_zero_exit() -> None:
    outcome = certs.systemctl_reload_nginx(
        argv=(sys.executable, "-c", "import sys; sys.stderr.write('nope\\n'); sys.exit(3)")
    )
    assert outcome.ok is False
    assert "exited 3" in outcome.detail and "nope" in outcome.detail


def test_default_hook_reports_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def hangs(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="systemctl", timeout=10.0)

    monkeypatch.setattr(subprocess, "run", hangs)
    outcome = certs.systemctl_reload_nginx()
    assert outcome.ok is False and "timed out" in outcome.detail


def test_default_hook_succeeds_on_exit_zero() -> None:
    outcome = certs.systemctl_reload_nginx(argv=(sys.executable, "-c", "pass"))
    assert outcome.ok is True


class TestReloadSentinel:
    """The path unit watches a fixed file, not the certificate directory (§6.16)."""

    async def test_issuing_a_certificate_touches_the_sentinel(self, tmp_path: Path) -> None:
        await certs.issue_self_signed("av.school.nz", data_dir=tmp_path)
        sentinel = certs.reload_sentinel_path(tmp_path)
        assert sentinel.is_file()
        assert sentinel.read_text(encoding="utf-8").strip()

    async def test_the_sentinel_path_never_varies_with_the_hostname(
        self, tmp_path: Path
    ) -> None:
        # inotify on a directory reports only its immediate entries, so a
        # renewal rewriting files inside live/<hostname>/ would go unnoticed.
        # The sentinel sits above that directory and its path is fixed.
        first = certs.reload_sentinel_path(tmp_path)
        await certs.issue_self_signed("av.school.nz", data_dir=tmp_path)
        await certs.issue_self_signed("other.school.nz", data_dir=tmp_path)
        assert certs.reload_sentinel_path(tmp_path) == first
        assert first.parent == tmp_path / certs.CERTS_SUBDIR

    async def test_a_second_issue_rewrites_the_sentinel(self, tmp_path: Path) -> None:
        await certs.issue_self_signed("av.school.nz", data_dir=tmp_path)
        sentinel = certs.reload_sentinel_path(tmp_path)
        sentinel.write_text("stale", encoding="utf-8")
        await certs.issue_self_signed("av.school.nz", data_dir=tmp_path)
        assert sentinel.read_text(encoding="utf-8").strip() != "stale"


# -- the atomic pair: proving the bug is fixed -----------------------------------------


class TestAtomicSwap:
    """``write_certificate_pair`` — the real bug §21.24's card used to hide.

    ``_write_exact`` used to replace the key and the certificate one file at
    a time *in the path nginx reads*. A crash between the two writes left a
    mismatched pair there, and nginx refuses to start on one. These tests
    interrupt a second issuance partway through and show the served pair —
    ``certificate_paths(...)`` — still matches, because nothing about
    ``live/<hostname>`` changes until the whole new version is written and a
    single rename swaps it in.
    """

    async def test_current_is_a_symlink_to_a_version_directory(self, tmp_path: Path) -> None:
        paths = await certs.issue_self_signed(HOSTNAME, data_dir=tmp_path)
        assert paths.directory.is_symlink()
        target = paths.directory.resolve()
        assert target.name == "1"
        assert target.parent.name == HOSTNAME
        assert target.parent.parent.name == certs.VERSIONS_DIRNAME

    async def test_a_crash_between_the_key_and_the_certificate_leaves_the_old_pair_served(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # v1: a complete, valid pair — what "the old pair" means below.
        first = await certs.issue_self_signed(HOSTNAME, data_dir=tmp_path)
        served_before = _load(first.certificate).serial_number

        real_write_exact = certs._write_exact
        calls = 0

        def crashes_on_the_second_file(path: Path, payload: bytes, mode: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:  # the key (1st) succeeded; the certificate (2nd) does not
                raise OSError("simulated power loss")
            real_write_exact(path, payload, mode)

        monkeypatch.setattr(certs, "_write_exact", crashes_on_the_second_file)
        with pytest.raises(OSError, match="simulated power loss"):
            await certs.issue_self_signed(HOSTNAME, data_dir=tmp_path)
        monkeypatch.undo()

        # current still resolves to v1 — the crash never touched it.
        served_paths = certs.certificate_paths(tmp_path, HOSTNAME)
        assert served_paths.directory.resolve().name == "1"
        assert served_paths.exists
        assert _load(served_paths.certificate).serial_number == served_before
        # The half-written v2 is an orphan nothing points at; not a served,
        # mismatched pair.
        orphan = certs._versions_root(tmp_path, HOSTNAME) / "2"
        assert orphan.is_dir()
        assert (orphan / certs.KEY_FILENAME).exists()
        assert not (orphan / certs.CERTIFICATE_FILENAME).exists()

    async def test_a_crash_during_the_symlink_swap_leaves_the_old_pair_served(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The production (POSIX) path: ``os.replace`` either lands or it doesn't — no in-between.

        ``os.name`` is pinned to ``"posix"`` so the Windows-only development
        fallback (see :func:`certs._swap_current`) does not mask what is
        under test: on the real appliance there is no fallback, because
        ``rename(2)`` never partially completes, so a raise here is the
        whole story, not a special case to recover from.
        """
        first = await certs.issue_self_signed(HOSTNAME, data_dir=tmp_path)
        served_before = _load(first.certificate).serial_number
        served_link = certs.certificate_paths(tmp_path, HOSTNAME).directory

        real_replace = os.replace

        def crashes_only_on_the_swap(src: object, dst: object) -> None:
            # os.replace also lands the key and certificate files inside the
            # new version directory (_write_exact); only the final rename —
            # onto the served symlink itself — is the step under test here.
            if Path(dst) == served_link:
                raise OSError("simulated power loss mid-rename")
            real_replace(src, dst)

        monkeypatch.setattr(certs.os, "replace", crashes_only_on_the_swap)
        monkeypatch.setattr(certs, "_WINDOWS_NON_ATOMIC_FALLBACK", False)
        with pytest.raises(OSError, match="simulated power loss mid-rename"):
            await certs.issue_self_signed(HOSTNAME, data_dir=tmp_path)
        monkeypatch.undo()

        served_paths = certs.certificate_paths(tmp_path, HOSTNAME)
        assert served_paths.directory.resolve().name == "1"
        assert _load(served_paths.certificate).serial_number == served_before

        # A retry after the "reboot" succeeds and cleans up the stray temp link
        # (the crashed attempt's version directory is left as another orphan;
        # version numbers only ever move forward).
        second = await certs.issue_self_signed(HOSTNAME, data_dir=tmp_path)
        assert second.directory.resolve().name == "3"
        assert second.exists

    async def test_only_the_last_two_versions_are_kept(self, tmp_path: Path) -> None:
        for _ in range(4):
            await certs.issue_self_signed(HOSTNAME, data_dir=tmp_path)
        versions = sorted(
            int(p.name) for p in certs._versions_root(tmp_path, HOSTNAME).iterdir()
        )
        assert versions == [3, 4]
        current = certs.certificate_paths(tmp_path, HOSTNAME)
        assert current.directory.resolve().name == "4"

    async def test_a_pre_atomic_pair_directory_is_migrated_on_next_write(
        self, tmp_path: Path
    ) -> None:
        """A real directory at live/<hostname> (the old layout) is replaced, not fought."""
        legacy_dir = certs.certificate_paths(tmp_path, HOSTNAME).directory
        certs.generate_self_signed(HOSTNAME, directory=legacy_dir)
        assert legacy_dir.is_dir() and not legacy_dir.is_symlink()

        written = await certs.issue_self_signed(HOSTNAME, data_dir=tmp_path)
        assert written.directory.is_symlink()
        assert written.exists


# -- the Cloudflare token: encrypted at rest, never returned (§3.2) -------------------


def _secret() -> DeviceSecret:
    return DeviceSecret(os.urandom(32))


class TestToken:
    async def test_a_stored_token_round_trips(self, tmp_path: Path) -> None:
        secret = _secret()
        await certs.store_token(tmp_path, "cf-token-value", secret)
        assert await certs.load_token(tmp_path, secret) == "cf-token-value"
        assert await certs.token_configured(tmp_path) is True

    async def test_the_token_file_never_contains_the_plain_value(self, tmp_path: Path) -> None:
        await certs.store_token(tmp_path, "super-secret-cf-token", _secret())
        raw = certs.token_path(tmp_path).read_text(encoding="utf-8")
        assert "super-secret-cf-token" not in raw

    async def test_no_token_reports_not_configured(self, tmp_path: Path) -> None:
        assert await certs.token_configured(tmp_path) is False
        assert await certs.load_token(tmp_path, _secret()) is None

    async def test_a_blank_token_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(certs.TokenError):
            await certs.store_token(tmp_path, "   ", _secret())

    async def test_a_token_encrypted_with_a_different_secret_is_a_mismatch(
        self, tmp_path: Path
    ) -> None:
        await certs.store_token(tmp_path, "cf-token", _secret())
        with pytest.raises(certs.TokenError):
            await certs.load_token(tmp_path, _secret())  # a fresh, different key

    def test_clear_token_removes_the_file(self, tmp_path: Path) -> None:
        certs.token_path(tmp_path).parent.mkdir(parents=True)
        certs.token_path(tmp_path).write_text("{}", encoding="utf-8")
        certs.clear_token(tmp_path)
        assert not certs.token_path(tmp_path).exists()


# -- renewal history (§21.24 "Renewal history") ---------------------------------------


class TestRenewalHistory:
    async def test_empty_before_anything_is_recorded(self, tmp_path: Path) -> None:
        assert await certs.renewal_history(tmp_path, HOSTNAME) == []

    async def test_a_recorded_attempt_round_trips(self, tmp_path: Path) -> None:
        record = certs.RenewalRecord(
            attempted_at="2026-09-20T12:00:00+12:00",
            method="automatic",
            result="success",
            issuer="Let's Encrypt",
            serial="abc123",
        )
        await certs.record_renewal(tmp_path, HOSTNAME, record)
        history = await certs.renewal_history(tmp_path, HOSTNAME)
        assert history == [record]

    async def test_history_is_bounded(self, tmp_path: Path) -> None:
        for i in range(certs.MAX_HISTORY_ENTRIES + 5):
            await certs.record_renewal(
                tmp_path,
                HOSTNAME,
                certs.RenewalRecord(
                    attempted_at=f"2026-09-{(i % 28) + 1:02d}T00:00:00+12:00",
                    method="automatic",
                    result="success",
                ),
            )
        history = await certs.renewal_history(tmp_path, HOSTNAME)
        assert len(history) == certs.MAX_HISTORY_ENTRIES

    async def test_certificate_card_attaches_history_that_describe_does_not(
        self, tmp_path: Path
    ) -> None:
        await certs.issue_self_signed(HOSTNAME, data_dir=tmp_path)
        await certs.record_renewal(
            tmp_path,
            HOSTNAME,
            certs.RenewalRecord(
                attempted_at="2026-09-20T12:00:00+12:00", method="manual", result="success"
            ),
        )
        card = await certs.certificate_card(tmp_path, HOSTNAME)
        assert card is not None
        assert card.renewal_history == [
            certs.RenewalRecord(
                attempted_at="2026-09-20T12:00:00+12:00", method="manual", result="success"
            ).to_json()
        ]
        # describe() alone stays pure — it has no data_dir/hostname to find history with.
        plain = await certs.describe(certs.certificate_paths(tmp_path, HOSTNAME))
        assert plain is not None and plain.renewal_history == []

    async def test_certificate_card_is_none_with_nothing_installed(
        self, tmp_path: Path
    ) -> None:
        assert await certs.certificate_card(tmp_path, HOSTNAME) is None
