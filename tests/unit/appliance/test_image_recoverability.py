"""A failed boot must be both survivable and investigable.

The first boot of the golden image, 22 September 2026, failed in a way that
also took the network down. The appliance could then be reached by no route at
all: SSH was the only door and it was shut, the ``admin`` account had no
password so the console would not take a login either, and the journal was
volatile, so every reboot erased the one record of what had gone wrong. The
machine was re-imaged from scratch without anyone learning why it failed.

Each test here pins one of the changes made in response, and each names the
failure it prevents. Like ``test_build_step_order.py`` they read the files as
text: the image build needs root, loop devices and an arm64 chroot, which a
unit test cannot honestly provide.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

APPLIANCE: Final = Path(__file__).resolve().parents[3] / "appliance"
BUILD_SH: Final = APPLIANCE / "image" / "build.sh"
SSHD_CONF: Final = APPLIANCE / "etc" / "ssh" / "sshd_config.d" / "auditorium.conf"
JOURNALD_CONF: Final = APPLIANCE / "etc" / "systemd" / "journald.conf.d" / "auditorium.conf"


def _directives(path: Path) -> list[str]:
    """Uncommented, non-blank lines: what the daemon actually reads."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


@pytest.fixture(scope="module")
def build_sh() -> str:
    return BUILD_SH.read_text(encoding="utf-8")


def _function(script: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}", script, re.MULTILINE | re.DOTALL)
    assert match, f"build.sh has no {name}()"
    return match.group(1)


# -- SSH ------------------------------------------------------------------------


def test_sshd_is_not_pinned_to_an_address() -> None:
    """ListenAddress fails at boot, and survives an address change it should not.

    ssh.service starts after network.target, which does not wait for
    NetworkManager to assign the address; sshd then cannot bind, exits 255,
    and is not restarted. And the address is runtime configuration since
    Phase 6, while ListenAddress was baked in at build time.
    """
    pinned = [d for d in _directives(SSHD_CONF) if d.split()[0].lower() == "listenaddress"]
    assert not pinned, (
        f"sshd is pinned to an address again ({pinned}). It races NetworkManager at "
        "boot and outlives a change of address; nftables already restricts who can "
        "reach port 22 (§3.3)."
    )


def test_ssh_still_refuses_passwords() -> None:
    """The console password must never become an SSH password.

    The admin account has a password now, for the console. That is safe only
    while sshd refuses password and keyboard-interactive authentication.
    """
    directives = {d.split()[0].lower(): " ".join(d.split()[1:]) for d in _directives(SSHD_CONF)}
    assert directives.get("passwordauthentication") == "no"
    assert directives.get("kbdinteractiveauthentication") == "no"
    assert directives.get("pubkeyauthentication") == "yes"


# -- the console ----------------------------------------------------------------


def test_the_admin_account_gets_a_console_password(build_sh: str) -> None:
    """An account with no password cannot log in at the console either."""
    users = _function(build_sh, "step_users_and_layout")
    assert "set_admin_console_password" in users, (
        "the admin account is created without a console password again: if the "
        "network fails, nobody standing at the machine can get a shell"
    )


def test_the_console_password_never_appears_on_a_command_line(build_sh: str) -> None:
    """chpasswd reads it from stdin; nothing puts it in argv or the build log.

    A password on a command line is in every process listing and, through
    in_chroot's DECISION line, in the build log that sits on the build host.
    """
    body = _function(build_sh, "set_admin_console_password")
    assert re.search(r"printf '%s:%s\\n' \"\$ADMIN_USER\" \"\$pw\" \| chroot", body), (
        "the password is no longer piped to chpasswd on stdin"
    )
    assert "in_chroot" not in body, (
        "in_chroot prints its whole command line as a DECISION: anything passed "
        "through it lands in the build log"
    )
    for line in body.splitlines():
        if "decide" in line or "warn" in line or "log " in line:
            leaked = "$pw" in line or "${pw}" in line
            assert not leaked, f"the password is logged: {line.strip()}"


# -- the journal ----------------------------------------------------------------


def test_the_journal_survives_a_reboot() -> None:
    """A volatile journal erased the only record of the first boot's failure."""
    directives = dict(
        d.split("=", 1) for d in _directives(JOURNALD_CONF) if "=" in d and not d.startswith("[")
    )
    assert directives.get("Storage") == "persistent", (
        "the journal is volatile again: a boot that fails will erase its own account "
        "of why at the next reboot"
    )
    assert "SystemMaxUse" in directives, "a persistent journal with no size bound can fill /data"


def test_the_journal_lives_on_data_not_the_overlay(build_sh: str) -> None:
    """Persistent in the RAM overlay would be volatile under another name.

    /var/log is on the read-only root's tmpfs overlay. The journal only
    survives if /var/log/journal is really /data/logs/journal, and systemd only
    waits for it if it is a mount point rather than a symlink.
    """
    fstab = _function(build_sh, "step_fstab")
    bind = re.search(r"^/data/logs/journal\s+/var/log/journal\s+none\s+(\S+)", fstab, re.MULTILINE)
    assert bind, "/var/log/journal is no longer bind-mounted from /data"
    options = bind.group(1).split(",")
    assert "bind" in options
    assert "nofail" in options, (
        "without nofail a broken /data takes the journal mount down with it and "
        "drops the boot to emergency.target"
    )
    assert "x-systemd.requires-mounts-for=/data" in options, (
        "the bind can be attempted before /data is mounted"
    )
    assert "var/log/journal" in fstab, "the mount point is not created in the root image"
    users = _function(build_sh, "step_users_and_layout")
    assert '"${DATA}/logs/journal" 2755' in users, (
        "/data/logs/journal is not created with journald's setgid layout"
    )


# -- the stock units --------------------------------------------------------------


def test_each_unwanted_package_is_purged_on_its_own(build_sh: str) -> None:
    """apt refuses a whole purge if any one name is unknown.

    A single `apt-get purge a b c` naming a package this base image lacks
    removes none of them — brltty included, which claims the serial adapters
    (§4.12). One purge per package keeps a wrong guess from costing the rest.
    """
    packages = _function(build_sh, "step_packages")
    for args in re.findall(r'in_chroot "\$ROOT_A" apt-get -y purge ([^\n]*)', packages):
        names = args.rstrip("\\").split()  # a trailing \ is a line continuation, not a name
        assert len(names) == 1, (
            f"several packages are purged in one apt transaction again: {names}"
        )
    loop = re.search(r"for unwanted in ([^;]+); do", packages)
    assert loop, "the per-package purge loop has gone"
    wanted_gone = set(loop.group(1).split())
    for name in ("brltty", "dphys-swapfile", "rpi-swap", "cloud-init"):
        assert name in wanted_gone, f"{name} is no longer purged"


# -- what the first WORKING boot found (24 September 2026) -------------------------
#
# The image booted, and its persistent journal on /data said why the previous
# attempts had not: no initramfs, so no overlay, so a plainly read-only root;
# sshd refusing keys stored under a group-writable directory; and a machine-id
# that made every boot a "first boot".


def _config_block(build_sh: str, heredoc: str) -> str:
    match = re.search(rf"<<{heredoc}\n(.*?)^{heredoc}$", build_sh, re.MULTILINE | re.DOTALL)
    assert match, f"build.sh no longer writes a <<{heredoc} block"
    return match.group(1)


@pytest.mark.parametrize("heredoc", ["CFG", "TRY"])
def test_the_initramfs_is_named_not_left_to_auto_initramfs(build_sh: str, heredoc: str) -> None:
    """auto_initramfs=1 does not look under os_prefix.

    The kernel then ran /sbin/init with no initramfs, boot=overlay never
    happened, and everything that writes at boot died on "Read-only file
    system": no console login, no address, no SSH. The name carries no
    directory, because tryboot.txt is derived from config.txt by changing only
    os_prefix=, and the firmware resolves the name inside the prefix.
    """
    block = _config_block(build_sh, heredoc)
    lines = [line.strip() for line in block.splitlines() if not line.lstrip().startswith("#")]
    initramfs = [line for line in lines if line.startswith("initramfs ")]
    assert initramfs == ["initramfs initramfs_2712 followkernel"], (
        f"{heredoc}: the initramfs is not named explicitly (found {initramfs})"
    )


def test_sshd_reads_its_keys_through_a_path_strictmodes_accepts(build_sh: str) -> None:
    """/srv/appliance is group-writable by design; StrictModes refuses keys below it.

    "Authentication refused: bad ownership or modes for directory
    /srv/appliance". sshd reads them through a bind mount at /etc/ssh/appliance
    instead, and StrictModes stays on.
    """
    paths = [
        d.split(None, 1)[1]
        for d in _directives(SSHD_CONF)
        if d.split()[0] in ("HostKey", "AuthorizedKeysFile")
    ]
    assert paths, "sshd names no host keys or authorised keys"
    for path in paths:
        assert path.startswith("/etc/ssh/appliance/"), (
            f"sshd reads {path} — anything under /srv/appliance is refused by StrictModes"
        )
    fstab = _function(build_sh, "step_fstab")
    bind = re.search(
        r"^/srv/appliance/ssh\s+/etc/ssh/appliance\s+none\s+(\S+)", fstab, re.MULTILINE
    )
    assert bind, "/etc/ssh/appliance is not bind-mounted from /srv/appliance/ssh"
    options = bind.group(1).split(",")
    assert "x-systemd.before=ssh.service" in options, (
        "a nofail mount is not ordered before local-fs.target: without this, sshd "
        "can start before its keys are mounted"
    )
    assert "x-systemd.requires-mounts-for=/srv/appliance" in options
    assert "etc/ssh/appliance" in fstab, "the mount point is not created in the root image"


def test_the_image_has_a_machine_id(build_sh: str) -> None:
    """ "uninitialized" on a read-only root is a first boot on every boot."""
    finalise = _function(build_sh, "step_finalise_root")
    assert '"${ROOT_A}/etc/machine-id"' in finalise, "slot A is given no machine-id"
    assert "token_hex(16)" in finalise, "a machine-id is 32 hexadecimal digits"


def test_units_the_image_ships_are_enabled_by_it(build_sh: str) -> None:
    """Every appliance unit that can be enabled, is — by the build, not by luck.

    auditorium-config-apply.timer (the nightly SMTP-relay re-resolve) was never
    enabled by the build. It ran only because every boot was a first boot and
    Debian's presets switched it on — so fixing the machine-id would have
    quietly switched it off. A unit whose [Install] section is never acted on
    is a unit that is not running.
    """
    enable_units = " ".join(_function(build_sh, "step_enable_units").split())
    # A real section header, not the words in a comment: helper@ and
    # cert-reload say "no [Install] section" in theirs, and mean it.
    section = re.compile(r"^\[Install\]\s*$", re.MULTILINE)
    installable = sorted(
        path.name
        for path in (APPLIANCE / "systemd").iterdir()
        if path.is_file() and section.search(path.read_text(encoding="utf-8"))
    )
    assert installable, "no installable units found; has appliance/systemd moved?"
    missing = []
    for unit in installable:
        stem = unit.removesuffix(".service")
        if f" {unit} " not in f" {enable_units} " and f" {stem} " not in f" {enable_units} ":
            missing.append(unit)
    assert not missing, f"shipped but never enabled by the build: {missing}"


def test_the_console_login_is_enabled_explicitly(build_sh: str) -> None:
    """Pi OS disables getty@tty1 and leaves its wizard to re-enable it.

    The wizard is masked, so the build has to; until now only the accidental
    first-boot preset run was doing it.
    """
    assert "getty@tty1.service" in _function(build_sh, "step_enable_units")


def test_stock_units_that_broke_the_boot_are_masked(build_sh: str) -> None:
    array = re.search(r"^STOCK_UNITS_MASKED=\((.*?)^\)", build_sh, re.MULTILINE | re.DOTALL)
    assert array, "the STOCK_UNITS_MASKED list has gone"
    masked = {
        word
        for line in array.group(1).splitlines()
        if not line.strip().startswith("#")
        for word in line.split()
    }
    for unit in (
        "systemd-networkd.service",  # a second network stack
        "systemd-networkd-wait-online.service",  # two minutes of every boot
        "rpi-resize.service",  # failed every boot
        "ssh.socket",  # competes with ssh.service
    ):
        assert unit in masked, f"{unit} is no longer masked"
    assert '"${STOCK_UNITS_MASKED[@]}"' in _function(build_sh, "step_enable_units")


def test_what_apt_would_undo_is_done_after_apt(build_sh: str) -> None:
    """Removing Debian's nginx logrotate file before nginx is installed does nothing."""
    order = re.findall(
        r"^\s*(step_[a-z_]+)\s*$", _function(build_sh, "main"), re.MULTILINE
    )
    assert order.index("step_finalise_root") > order.index("step_packages")
    finalise = _function(build_sh, "step_finalise_root")
    removal = 'rm -f "${ROOT_A}/etc/logrotate.d/nginx"'
    assert removal in finalise
    assert removal not in _function(build_sh, "step_appliance_files"), (
        "the nginx logrotate file is removed before step_packages again, which reinstalls it"
    )
    assert "rename_user.conf" in finalise, "the first-boot wizard's SSH banner is back"


def test_the_console_keyboard_is_us_and_the_cached_keymap_is_rebuilt(build_sh: str) -> None:
    """/etc/default/keyboard alone changes nothing: keyboard-setup loads a cached keymap."""
    finalise = _function(build_sh, "step_finalise_root")
    assert 'XKBLAYOUT="us"' in finalise
    assert "setupcon --force --save-only" in finalise, (
        "the UK keymap cached in /etc/console-setup would still be the one loaded"
    )


# -- what the first real INSTALL found (24 September 2026) ---------------------------
#
# The application installed and answered /health in 9 s, and the web
# interface still could not be reached, for three independent reasons. Two of
# them were in the image.

FIRST_BOOT_SH: Final = APPLIANCE / "image" / "first-boot.sh"
EMERGENCY_PY: Final = APPLIANCE / "lib" / "auditorium_emergency.py"


def test_debians_default_nginx_site_is_removed_after_apt(build_sh: str) -> None:
    """`listen 80 default_server` answered everything that did not name the FQDN.

    By address, / was "Welcome to nginx!" and /health a 404, where §3.3 says
    port 80 serves /health and redirects the rest. The build removed it before
    step_packages, whose install of nginx put it straight back.
    """
    order = re.findall(r"^\s*(step_[a-z_]+)\s*$", _function(build_sh, "main"), re.MULTILINE)
    assert order.index("step_finalise_root") > order.index("step_packages")
    finalise = _function(build_sh, "step_finalise_root")
    assert '"${ROOT_A}/etc/nginx/sites-enabled/default"' in finalise, (
        "Debian's default site is enabled in the finished image again"
    )
    assert '"${ROOT_A}/etc/nginx/sites-available/default"' in finalise, (
        "the default site is left one `ln -s` from coming back"
    )
    assert "sites-enabled/default" not in _function(build_sh, "step_appliance_files"), (
        "the default site is removed before step_packages again, which recreates it"
    )


def test_emergency_mode_never_brings_the_default_site_back() -> None:
    """auditorium-emergency swaps auditorium.conf for emergency.conf, and nothing else.

    With the default site gone, whichever of our two is enabled is the default
    server for port 80 — in emergency mode too, which is when a request by
    address matters most.
    """
    source = EMERGENCY_PY.read_text(encoding="utf-8")
    body = re.search(
        r"^def switch_nginx_to_emergency\(.*?(?=^def |\Z)", source, re.MULTILINE | re.DOTALL
    )
    assert body, "switch_nginx_to_emergency has moved"
    names = set(re.findall(r'"([a-z]+\.conf|default)"', body.group(0)))
    assert names == {"auditorium.conf", "emergency.conf"}, names


def test_nginx_can_traverse_to_the_web_interface(build_sh: str) -> None:
    """/data/app at 0750 made every page a 500: nginx's workers run as www-data.

    Only /data/app opens up. Its siblings hold the database, the
    configuration and the certificates' keys, and stay 0750 — which is also
    why www-data is not simply added to the auditorium group.
    """
    users = _function(build_sh, "step_users_and_layout")
    assert re.search(r'make_dir "\$\{DATA\}/app" 0755 "\$\{APP_UID\}:\$\{APP_UID\}"', users), (
        "/data/app is not created 0755"
    )
    loop = re.search(r'for d in ([^;]+); do\n\s*make_dir "\$\{DATA\}/\$\{d\}" 0750', users)
    assert loop, "the 0750 /data skeleton loop has moved"
    closed = loop.group(1).split()
    assert "app" not in closed, "/data/app is back in the 0750 loop"
    for private in ("config", "certs", "backups/snapshots", "tmp"):
        assert private in closed, f"/data/{private} is no longer 0750"
    first_boot = FIRST_BOOT_SH.read_text(encoding="utf-8")
    assert re.search(r"^\s*chmod 0755 /data/app\s*$", first_boot, re.MULTILINE), (
        "first-boot.sh no longer sets /data/app's mode on a restored or replaced partition"
    )
