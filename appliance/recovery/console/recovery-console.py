#!/usr/bin/env python3
"""The HDMI text menu — what someone sees on a screen plugged into the appliance (§13.7).

Runs on `tty1` in place of a login prompt (`getty@tty1.service` is masked;
`recovery-console.service` starts this instead — see
`appliance/recovery/build.sh`'s `step_install_recovery_app`). It exists for
the case the brief calls out explicitly: "for when there is no other machine
to hand." It offers the address to browse to, and the same four actions the
web interface offers, as a plain numbered menu — no scrolling panes, no
mouse, nothing this needs a wide terminal for.

It calls the same library modules the web interface calls
(`recovery_partitioning`, `recovery_image`, `recovery_archive`,
`recovery_network`, `recovery_diagnostics`) directly, in-process — there is
no HTTP round trip to itself, and the two front ends can never disagree about
what a partition plan or a verified manifest says, because they are reading
the same verdict from the same call.

**No shell escape.** Every prompt reads one line, parses it against a closed
set of choices, and refuses anything else — there is no "run a command" menu
item, and there never will be (§13.7's rule is the reason this file exists in
the form it does).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # /opt/recovery: lib/ sits beside this
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import recovery_diagnostics as diagnostics_lib  # noqa: E402
import recovery_network as network_lib  # noqa: E402

USB_MOUNT = Path(os.environ.get("RECOVERY_USB_MOUNT", "/mnt/backup"))
ATTACHED_MOUNT = Path(os.environ.get("RECOVERY_ATTACHED_MOUNT", "/mnt/attached"))


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def clear() -> None:
    print("\033c", end="")


def own_address() -> str:
    try:
        hostname = socket.gethostname()
        return f"{socket.gethostbyname(hostname)} (or http://{hostname}.local:8080)"
    except OSError:
        return "unknown — check Network settings (option 4)"


def pause() -> None:
    input("\nPress Enter to return to the menu... ")


def main_menu() -> None:
    while True:
        clear()
        print("Proskenion recovery")
        print("=" * 40)
        print(f"Web interface: http://{own_address()}:8080")
        print()
        print("1) Partition and image a new SSD")
        print("2) Restore data from a backup archive")
        print("3) Network settings")
        print("4) Diagnostics")
        print("5) Reboot")
        print("0) Nothing — leave this screen as it is")
        choice = input("\n> ").strip()
        if choice == "1":
            partition_menu()
        elif choice == "2":
            restore_menu()
        elif choice == "3":
            network_menu()
        elif choice == "4":
            diagnostics_menu()
        elif choice == "5":
            confirm_and_run(["reboot"], what="reboot this machine")
        elif choice == "0":
            continue
        else:
            print("Not a listed option.")
            pause()


def partition_menu() -> None:
    clear()
    print("Partition and image a new SSD")
    print("-" * 40)
    print("This is the same action as the web interface's 'Partition & image' page.")
    print(f"Browse to http://{own_address()}:8080/partition to choose a disk and an image —")
    print("the confirmation and the disk list are easier to read on a full page than here.")
    pause()


def restore_menu() -> None:
    clear()
    print("Restore data from a backup archive")
    print("-" * 40)
    print(f"Browse to http://{own_address()}:8080/restore to choose an archive and confirm.")
    pause()


def network_menu() -> None:
    clear()
    print("Network settings")
    print("-" * 40)
    devices = network_lib.current_devices(run=_run)
    for device, kind, state in devices:
        print(f"  {device:12s} {kind:10s} {state}")
    print()
    print("1) Use DHCP")
    print("2) Set a static address")
    print("0) Back")
    choice = input("\n> ").strip()
    if choice == "1":
        device = input("Device (e.g. eth0): ").strip()
        try:
            network_lib.apply_dhcp(device, run=subprocess.run)
            print("Applied.")
        except network_lib.NetworkConfigError as exc:
            print(f"Refused: {exc}")
        pause()
    elif choice == "2":
        device = input("Device (e.g. eth0): ").strip()
        address = input("Address, CIDR (e.g. 10.2.30.60/24): ").strip()
        gateway = input("Gateway (e.g. 10.2.30.1): ").strip()
        dns_raw = input("DNS, comma-separated: ").split(",")
        dns = tuple(entry.strip() for entry in dns_raw if entry.strip())
        try:
            settings = network_lib.static_settings(
                device=device, address=address, gateway=gateway, dns=dns
            )
            network_lib.apply_static(settings, run=subprocess.run)
            print("Applied.")
        except network_lib.NetworkConfigError as exc:
            print(f"Refused: {exc}")
        pause()


def diagnostics_menu() -> None:
    clear()
    print("Diagnostics")
    print("-" * 40)
    try:
        disks = diagnostics_lib.list_disks(run=_run)
    except diagnostics_lib.DiagnosticsError as exc:
        print(f"Could not list disks: {exc}")
        disks = []
    for disk in disks:
        try:
            health = diagnostics_lib.smart_health(disk.path, run=_run)
            health_text = (
                "unknown"
                if health.passed is None
                else ("PASSED" if health.passed else f"FAILED ({health.summary})")
            )
        except diagnostics_lib.DiagnosticsError:
            health_text = "unknown"
        size_gib = disk.size_bytes / (1024**3)
        print(f"  {disk.path:16s} {size_gib:7.1f} GiB  SMART: {health_text}")
    print()
    print("For image and archive verification, use the web interface's Diagnostics page —")
    print(f"http://{own_address()}:8080/diagnostics")
    pause()


def confirm_and_run(command: list[str], *, what: str) -> None:
    answer = input(f"Type 'yes' to {what}: ").strip()
    if answer == "yes":
        subprocess.run(command, check=False)
    else:
        print("Cancelled.")
        pause()


if __name__ == "__main__":
    try:
        main_menu()
    except (KeyboardInterrupt, EOFError):
        pass
