#!/usr/bin/env bash
# systemd-setup.sh — build the appliance's layout inside the harness container.
#
# Run once by verify-systemd-in-docker.sh before systemd-cases.sh. It installs
# the units, scripts and shared libraries from the working tree exactly where
# appliance/image/build.sh puts them, creates the /data and /srv/appliance
# skeletons with the ownership build.sh gives them, and adds the stand-ins the
# harness needs:
#
#   fake-core         auditorium-core's ExecStart: notifies systemd, writes its
#                     boot-state markers through the shared module, and fails
#                     when the version directory it runs from says to.
#   harness-request   writes a request as the application user would.
#   harness-json      reads one value out of a JSON file, so no assertion in
#                     the cases file parses JSON in bash.
#   harness-apply     runs the application's own update module as the
#                     application user, optionally killing itself at a named
#                     step so the next start can be asked what survived.
#   packages.py       a verifier standing in for the root image's, for the
#                     helper's side of the seam only: it accepts a package
#                     whose signature file holds a known marker. The update
#                     cases below use real signed packages and the real
#                     verifier instead.
#
# It also generates a signing key, installs its anchor on the read-only root,
# and builds the signed application packages the update cases apply.
#
# Ownership matters here as much as anything: boot-state.json is written by the
# application as uid 900 and by two root scripts, and the atomic rename every
# one of them uses needs write permission on /srv/appliance itself.

set -euo pipefail

SRC=${1:-/src}
PROJECT=${2:-/project}
APP_UID=900

echo "--- install the appliance's own files"
install -d /etc/systemd/system /usr/local/bin /usr/local/lib/auditorium \
    /usr/local/share/auditorium
install -m 0644 "${SRC}"/systemd/*.service "${SRC}"/systemd/*.path "${SRC}"/systemd/*.timer \
    /etc/systemd/system/
for d in "${SRC}"/systemd/*.d; do
    install -d "/etc/systemd/system/$(basename "$d")"
    for f in "$d"/*.conf; do
        [ -e "$f" ] && install -m 0644 "$f" "/etc/systemd/system/$(basename "$d")/"
    done
done
install -m 0644 "${SRC}/systemd/auditorium-core.service.d/post-update.conf.example" \
    /usr/local/share/auditorium/post-update.conf.example
find "${SRC}/bin" -maxdepth 1 -type f -exec install -m 0755 {} /usr/local/bin/ \;
install -m 0644 "${SRC}"/lib/*.py /usr/local/lib/auditorium/

echo "--- nginx, as build.sh leaves it"
# Rendered the way build.sh renders it, with its default --fqdn. Debian's own
# site goes, as step_finalise_root removes it: nginx's install put it back.
FQDN=auditorium.obhs.school.nz
install -d /etc/nginx/sites-available /etc/nginx/sites-enabled /etc/nginx/snippets \
    /usr/local/share/auditorium/emergency /usr/local/share/auditorium/reconnect
for site in auditorium emergency; do
    sed -e "s/av\.school\.nz/${FQDN}/g" "${SRC}/nginx/${site}.conf" \
        > "/etc/nginx/sites-available/${site}.conf"
done
install -m 0644 "${SRC}"/nginx/snippets/*.conf /etc/nginx/snippets/
install -m 0644 "${SRC}/share/auditorium/emergency/index.html" /usr/local/share/auditorium/emergency/
install -m 0644 "${SRC}/share/auditorium/reconnect/index.html" /usr/local/share/auditorium/reconnect/
rm -f /etc/nginx/sites-enabled/default /etc/nginx/sites-available/default
ln -sfn /etc/nginx/sites-available/auditorium.conf /etc/nginx/sites-enabled/auditorium.conf

echo "--- /data and /srv/appliance, with build.sh's ownership"
install -d -m 0755 -o "$APP_UID" -g "$APP_UID" /data
for d in config certs logs backups/snapshots backups/daily tmp; do
    install -d -m 0750 -o "$APP_UID" -g "$APP_UID" "/data/${d}"
done
# 0755: nginx's workers (www-data) serve the web interface from under it.
install -d -m 0755 -o "$APP_UID" -g "$APP_UID" /data/app
install -d -m 0755 -o root -g root /data/run
install -d -m 0770 -o root -g "$APP_UID" /data/run/helper
# Sticky and group-writable: the application renames boot-state.json into
# place here, and must not be able to replace anything root owns beside it.
chown "root:${APP_UID}" /srv/appliance
chmod 1775 /srv/appliance

# What the application writes outside /data takes its mode and owner from
# build.sh itself, not a copy of them here: this harness had /srv/local
# application-owned when the image made it root's, and every case passed
# while the real nightly backup failed with EACCES (24 September 2026).
# image_attrs NAME prints "MODE OWNER" from build.sh's make_dir or
# write_file line for NAME, spelled as build.sh spells it.
image_attrs() {
    local line
    line=$(grep -F -e "make_dir \"$1\" " -e "write_file \"$1\" " "${SRC}/image/build.sh" | head -1)
    [ -n "$line" ] || { echo "build.sh no longer creates $1" >&2; exit 1; }
    awk '{ print $3, $4 }' <<<"$line" | tr -d '"' | sed "s/\${APP_UID}/${APP_UID}/g"
}
# The partition root is mkfs's, root 0755, and build.sh leaves it so. (The
# tmpfs standing in for it here starts 1777.)
install -d -m 0755 -o root -g root /srv/local
for d in backups images; do
    read -r mode owner <<<"$(image_attrs "\${LOCAL}/${d}")"
    install -d -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "/srv/local/${d}"
done
read -r mode owner <<<"$(image_attrs "\${APPLIANCE}/smtp-fallback.toml")"
printf '# as build.sh writes it: empty until SMTP has sent once\n' > /srv/appliance/smtp-fallback.toml
chown "$owner" /srv/appliance/smtp-fallback.toml
chmod "$mode" /srv/appliance/smtp-fallback.toml
install -d -m 0755 /mnt
# First boot's self-signed pair, as first-boot.sh makes it: emergency.conf
# serves it, and the emergency cases switch nginx to that site.
install -d -m 0750 -o root -g "$APP_UID" /srv/appliance/certs/self-signed
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
    -subj "/CN=${FQDN}" -addext "subjectAltName=DNS:${FQDN}" \
    -keyout /srv/appliance/certs/self-signed/privkey.pem \
    -out /srv/appliance/certs/self-signed/fullchain.pem 2>/dev/null
chown "root:${APP_UID}" /srv/appliance/certs/self-signed/privkey.pem \
    /srv/appliance/certs/self-signed/fullchain.pem
chmod 0640 /srv/appliance/certs/self-signed/privkey.pem

echo "--- two installed versions and a database"
for version in v1.3.0 v1.3.1; do
    install -d -m 0755 -o "$APP_UID" -g "$APP_UID" "/data/app/${version}"
    printf '%s\n' "$version" > "/data/app/${version}/VERSION"
    printf 'ok\n' > "/data/app/${version}/mode"
    chown -R "${APP_UID}:${APP_UID}" "/data/app/${version}"
done
rm -rf /data/app/v1.3.1  # installed by apply-update during the run
ln -sfn v1.3.0 /data/app/current
chown -h "${APP_UID}:${APP_UID}" /data/app/current
python3 -c '
import sqlite3
connection = sqlite3.connect("/data/auditorium.db")
connection.execute("CREATE TABLE IF NOT EXISTS scenes (id INTEGER PRIMARY KEY, name TEXT)")
connection.execute("INSERT INTO scenes (name) VALUES (?)", ("assembly",))
connection.commit()
connection.close()
'
chown "${APP_UID}:${APP_UID}" /data/auditorium.db

cat > /srv/appliance/boot-state.json <<'JSON'
{
  "active_slot": "a",
  "last_known_good": "a",
  "staged": null,
  "slots": {
    "a": "5a1b2c3d-02",
    "b": "5a1b2c3d-03"
  },
  "started": null,
  "healthy": null,
  "update": null,
  "rollback": null,
  "trial": null
}
JSON
# Owned by the application: in a sticky directory only the owner may rename
# over an entry, and writing this file is the application's job (contracts §1).
chown "${APP_UID}:${APP_UID}" /srv/appliance/boot-state.json
chmod 0664 /srv/appliance/boot-state.json

echo "--- the verifier the root image would carry (proskenion/core/packages.py, not this stand-in)"
cat > /usr/local/lib/auditorium/packages.py <<'PY'
"""A stand-in for proskenion.core.packages, for the harness only.

It has the seam's signature — verify_package(path, *, expect_type) — and the
only two behaviours the helper's side of the seam depends on: it returns the
manifest for a package it accepts, and raises for one it does not. What makes
a package acceptable here is a marker in manifest.json.sig rather than an
Ed25519 signature over the trust anchors; substituting the real verifier is
replacing this one file.
"""

import json
import tarfile


class VerificationError(Exception):
    pass


MARKER = b"HARNESS-TRUSTED"


def verify_package(path, *, expect_type):
    with tarfile.open(path) as archive:
        manifest_member = archive.extractfile("manifest.json")
        signature_member = archive.extractfile("manifest.json.sig")
        if manifest_member is None or signature_member is None:
            raise VerificationError("the package has no manifest or no signature")
        raw = manifest_member.read()
        if signature_member.read().strip() != MARKER:
            raise VerificationError("the signature does not verify against any trust anchor")
        manifest = json.loads(raw)
        names = {member.name for member in archive.getmembers() if member.isfile()}
    declared = {"manifest.json", "manifest.json.sig"} | {
        member["path"] for member in manifest.get("members", [])
    }
    extra = names - declared
    if extra:
        raise VerificationError(f"the package carries members outside the manifest: {extra}")
    if manifest.get("type") != expect_type:
        raise VerificationError(
            f"the package is a {manifest.get('type')} package, not {expect_type}"
        )
    return manifest
PY

echo "--- the packages the cases apply"
python3 - <<'PY'
import io
import json
import subprocess
import tarfile
from pathlib import Path

MARKER = b"HARNESS-TRUSTED"


def app_tree(version):
    """A tar.zst of an application tree, as an app package's payload carries."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as inner:
        for name, body in (
            ("VERSION", version + "\n"),
            # A version the application starts on. The rollback case breaks it
            # afterwards, so "was this update applied" and "does this version
            # start" stay separate questions.
            ("mode", "ok\n"),
            ("proskenion/__init__.py", "__version__ = '0.1.0'\n"),
        ):
            data = body.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            inner.addfile(info, io.BytesIO(data))
    return subprocess.run(
        ["zstd", "-q", "-c"], input=buffer.getvalue(), stdout=subprocess.PIPE, check=True
    ).stdout


def package(path, version, signature):
    payload = app_tree(version)
    manifest = json.dumps(
        {
            "type": "app",
            "version": version,
            "created_at": "2026-09-20T12:00:00+12:00",
            "min_app_version": "v1.0.0",
            "members": [{"path": "payload/app.tar.zst", "size": len(payload)}],
        }
    ).encode()
    with tarfile.open(path, "w") as archive:
        for name, data in (
            ("manifest.json", manifest),
            ("manifest.json.sig", signature),
            ("payload/app.tar.zst", payload),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    Path(path).chmod(0o600)


package("/data/tmp/good.tar", "v1.3.1", MARKER)
package("/data/tmp/tampered.tar", "v1.3.2", b"FORGED")
PY
chown "${APP_UID}:${APP_UID}" /data/tmp/good.tar /data/tmp/tampered.tar

echo "--- the harness's stand-ins"
cat > /usr/local/bin/fake-core <<'PY'
#!/usr/bin/python3
"""auditorium-core's ExecStart inside the harness.

It does the three things the units under test react to: it notifies systemd
(Type=notify, WatchdogSec), it writes its boot-state markers through the same
shared module the appliance uses, and it fails when the version directory it
is running from says to — which is how a failed update is modelled, rather
than a flag somewhere the rollback cannot see.
"""

import os
import socket
import sys
import time

sys.path.insert(0, "/usr/local/lib/auditorium")
import auditorium_bootstate as bootstate  # noqa: E402


def notify(message):
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(address)
        sock.sendall(message.encode())


#: How long a version marked "slow" takes to report ready. Long enough to be
#: observed from outside, short enough not to reach TimeoutStartSec.
SLOW_START_S = 12


def mode():
    try:
        with open("/data/app/current/mode", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return "ok"


bootstate.mark_started()
current = mode()
if current == "fail":
    print("fake-core: this version does not start", flush=True)
    sys.exit(1)
if current == "slow":
    # A first start that takes a while, which is what §4.7's widened watchdog
    # window exists for — and the only way to watch that window from outside
    # while it is still open.
    print(f"fake-core: slow start, {SLOW_START_S} s before ready", flush=True)
    time.sleep(SLOW_START_S)
notify("READY=1")
print("fake-core: serving", flush=True)
while True:
    notify("WATCHDOG=1")
    time.sleep(5)
PY
chmod 0755 /usr/local/bin/fake-core

cat > /usr/local/bin/harness-request <<'PY'
#!/usr/bin/python3
"""Write a helper request as the application would, and print its id.

  harness-request VERB [ARGS_JSON] [--age SECONDS]

--age backdates requested_at, which is how a stale request is tested without
waiting ten minutes for one to become stale.
"""

import argparse
import datetime as dt
import json
import os
import pwd
import uuid

parser = argparse.ArgumentParser()
parser.add_argument("verb")
parser.add_argument("args", nargs="?", default="{}")
parser.add_argument("--age", type=float, default=0.0)
parser.add_argument("--directory", default="/data/run/helper")
options = parser.parse_args()

request_id = str(uuid.uuid4())
when = dt.datetime.now().astimezone() - dt.timedelta(seconds=options.age)
body = {
    "verb": options.verb,
    "id": request_id,
    "requested_at": when.isoformat(timespec="seconds"),
    "args": json.loads(options.args),
}

application = pwd.getpwnam("auditorium")
temporary = os.path.join(options.directory, f".{request_id}.tmp")
final = os.path.join(options.directory, f"{request_id}.json")
descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
    json.dump(body, handle)
os.chown(temporary, application.pw_uid, application.pw_gid)
os.replace(temporary, final)
print(request_id)
PY
chmod 0755 /usr/local/bin/harness-request

cat > /usr/local/bin/harness-json <<'PY'
#!/usr/bin/python3
"""Print one dotted key out of a JSON file, or nothing when it is absent.

  harness-json /srv/appliance/boot-state.json update.to
"""

import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        value = json.load(handle)
except (OSError, ValueError):
    sys.exit(0)
for part in sys.argv[2].split("."):
    if not isinstance(value, dict) or part not in value:
        sys.exit(0)
    value = value[part]
if value is None:
    sys.exit(0)
print(value if isinstance(value, str) else json.dumps(value))
PY
chmod 0755 /usr/local/bin/harness-json

echo "--- drop-ins that stand things in"
install -d /etc/systemd/system/auditorium-core.service.d
cat > /etc/systemd/system/auditorium-core.service.d/harness.conf <<'CONF'
# The harness stands in for the application. Everything else in the unit —
# User=, ReadWritePaths=, Restart=on-failure, StartLimitBurst=3,
# OnFailure=, WatchdogSec= — is the real file, because those are what is
# under test.
[Unit]
After=
Wants=

[Service]
ExecStart=
ExecStart=/usr/local/bin/fake-core
WorkingDirectory=/
CONF

install -d "/etc/systemd/system/auditorium-helper@.service.d"
cat > "/etc/systemd/system/auditorium-helper@.service.d/harness.conf" <<'CONF'
# §4.7's window is 60 s on the appliance. The harness asserts that it is
# opened, that systemd picked it up, and that it is closed again — only how
# long it waits in between is shortened. Ten seconds, not one: the window has
# to be wide enough to observe without the observation itself deciding the
# result.
[Service]
ExecStart=
ExecStart=/usr/local/bin/auditorium-helper --dispatch %i --watchdog-suspend-s 10
CONF

echo "--- a real signing key, and the anchors the verifier reads"
# The application's verifier (§6.11) reads
# /usr/local/share/auditorium/trusted-keys and nowhere else, so the packages
# below are admitted here exactly as they would be on the appliance: Ed25519
# over the canonical manifest bytes, against an anchor on the read-only root.
install -d -m 0755 /usr/local/share/auditorium/trusted-keys
install -d -m 0700 /root/harness-keys
python3 "${PROJECT}/tools/package.py" keygen --name harness --out-dir /root/harness-keys \
    --no-passphrase --comment "the harness's key, generated per run" >/dev/null
install -m 0644 /root/harness-keys/harness.pub /usr/local/share/auditorium/trusted-keys/

echo "--- real application packages, built and signed with tools/package.py"
# An application package is a tree plus the wheels its environment is built
# from. The migration runner inside it is what an apply asks about the
# database, so a package carrying a failing one is how "a bad migration
# changes nothing" is tested for real rather than by patching something out.
build_package() { # build_package VERSION MIGRATION_EXIT [START_MODE]
    local version="$1" exit_code="$2"
    local start_mode="${3:-ok}"
    local tree="/root/pkg-${version}"
    rm -rf "$tree"
    mkdir -p "${tree}/proskenion/db" "${tree}/wheels"
    printf '%s\n' "$version" > "${tree}/VERSION"
    # fake-core reads this to decide whether, and how quickly, it starts.
    printf '%s\n' "$start_mode" > "${tree}/mode"
    printf "__version__ = '0.1.0'\n" > "${tree}/proskenion/__init__.py"
    : > "${tree}/proskenion/db/__init__.py"
    cat > "${tree}/proskenion/db/migrations.py" <<PY
"""The migration runner this version would run at startup (§15.2).

The apply hands it a copy of the live database and never the database itself,
so a runner that writes — or one that fails, as this version's may — cannot
reach production.
"""
import sqlite3, sys
connection = sqlite3.connect(sys.argv[-1])
connection.execute("CREATE TABLE IF NOT EXISTS schema_versions (migration TEXT PRIMARY KEY)")
connection.execute("DELETE FROM scenes")
connection.commit()
connection.close()
if ${exit_code}:
    print("004_scenes.sql failed: no such column: hirer_max_db", file=sys.stderr)
    raise SystemExit(${exit_code})
print('{"ok": true}')
PY
    python3 - "$tree" <<'PY'
"""A minimal but genuine wheel, so the offline pip install has something to do."""
import base64, hashlib, sys, zipfile
from pathlib import Path

tree = Path(sys.argv[1])
name, version = "proskenion_stub", "1.0.0"
distinfo = f"{name}-{version}.dist-info"
entries = {
    f"{name}/__init__.py": "__version__ = '1.0.0'\n",
    f"{distinfo}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
    f"{distinfo}/WHEEL": (
        "Wheel-Version: 1.0\nGenerator: harness\n"
        "Root-Is-Purelib: true\nTag: py3-none-any\n"
    ),
}
record = []
for member, body in entries.items():
    data = body.encode()
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    record.append(f"{member},sha256={digest},{len(data)}")
record.append(f"{distinfo}/RECORD,,")
entries[f"{distinfo}/RECORD"] = "\n".join(record) + "\n"
with zipfile.ZipFile(tree / "wheels" / f"{name}-{version}-py3-none-any.whl", "w") as archive:
    for member, body in entries.items():
        archive.writestr(member, body)
PY
    python3 "${PROJECT}/tools/package.py" build --type app --version "$version" \
        --source "$tree" --output "/data/tmp/app-${version}.tar" --force \
        --change "Harness build ${version}" >/dev/null
    python3 "${PROJECT}/tools/package.py" sign --package "/data/tmp/app-${version}.tar" \
        --key /root/harness-keys/harness.key --no-passphrase >/dev/null
    chown "${APP_UID}:${APP_UID}" "/data/tmp/app-${version}.tar"
}

# One version per step a kill case stops at, in ascending order so no apply is
# ever refused as a downgrade. v2.0.0 reports ready slowly, so §4.7's widened
# window can be watched while it is open; v3.0.0 carries the migration that
# fails; v3.1.0 is the never-started directory a killed apply leaves behind for
# the rollback cases.
build_package v2.0.0 0 slow
for version in v2.1.0 v2.2.0 v2.3.0 v2.4.0 v2.5.0 v2.6.0 v2.7.0 v3.1.0; do
    build_package "$version" 0
done
build_package v3.0.0 2

cat > /usr/local/bin/harness-apply <<'PY'
#!/usr/bin/python3
"""Apply a package the way the unprivileged application does.

The application's own module is what runs here, from the repository, because
the point of these cases is that the real ordering survives being
interrupted. Everything privileged still goes through auditorium-helper; this
process is the auditorium user and nothing more.

  harness-apply VERSION [--kill-after STEP]
"""

import os
import sys

sys.path.insert(0, "/project")

version = sys.argv[1]
os.environ["PYTHONPATH"] = "/project"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"  # /project is mounted read-only
os.execv(
    sys.executable,
    [
        sys.executable,
        "-m",
        "proskenion.core.update",
        "--package",
        f"/data/tmp/app-{version}.tar",
        "--version",
        version,
        "--data-dir",
        "/data",
        "--state-dir",
        "/srv/appliance",
        "--database",
        "/data/auditorium.db",
        *sys.argv[2:],
    ],
)
PY
chmod 0755 /usr/local/bin/harness-apply

echo "--- the A/B root slots: a FAT boot partition and a standby root (§14.4)"
# Everything the slot cases assert is a property of the real media: FAT32 has
# no journal, which is why every write to p1 is a new file and a rename, and a
# root slot is a filesystem written to a partition and then mounted to have
# one file rewritten in it. Both are loop devices here. /proc/cmdline cannot
# be faked in a container, so the helper's active-slot lookup falls through to
# boot-state.json's record — which is the fallback it has on the appliance too.
BOOT_PARTUUID=5a1b2c3d-01
ROOT_A_PARTUUID=5a1b2c3d-02
ROOT_B_PARTUUID=5a1b2c3d-03

install -d /root/slots /dev/disk/by-partuuid
if ! losetup -f >/dev/null 2>&1; then
    echo "no loop devices available: the container needs --privileged" >&2
    exit 1
fi

# attach_loop FILE -> the device it is attached to.
#
# `mount -o loop` and a bare `losetup -f --show` both assume the device node
# appears by itself, which it does on a machine with a udev listening to the
# kernel. A container's /dev has no udev, so the kernel allocates loop8 and
# nothing creates /dev/loop8. The node is made here instead, which is what
# udev would have done.
attach_loop() {
    local file="$1" number device
    # `losetup -f` says "/dev/loop9 (lost)" when the node is the very thing
    # that is missing, so the number is taken rather than the rest of the line.
    number=$(losetup -f | sed -n 's#.*/loop\([0-9][0-9]*\).*#\1#p')
    [ -n "$number" ] || { echo "no free loop device" >&2; return 1; }
    device="/dev/loop${number}"
    [ -b "$device" ] || mknod "$device" b 7 "$number"
    losetup "$device" "$file"
    printf '%s\n' "$device"
}

truncate -s 64M /root/slots/boot.img
mkfs.vfat -n BOOT -F 32 /root/slots/boot.img >/dev/null
install -d /boot/firmware
attach_loop /root/slots/boot.img > /root/slots/boot.loop
mount "$(cat /root/slots/boot.loop)" /boot/firmware
# mkdir, not install -d: install chmods what it creates, and FAT refuses a
# chmod that does not match the mount's own umask.
mkdir -p /boot/firmware/slot-a /boot/firmware/slot-b
cat > /boot/firmware/config.txt <<'CFG'
# stock config.txt
[all]
dtparam=watchdog=on
os_prefix=slot-a/
CFG
cp /boot/firmware/config.txt /boot/firmware/tryboot.txt
printf 'console=tty1 root=PARTUUID=%s rootfstype=ext4 rootwait=30 panic=10 ro boot=overlay\n' \
    "$ROOT_A_PARTUUID" > /boot/firmware/slot-a/cmdline.txt
printf 'golden-image\n' > /boot/firmware/slot-a/os-version.txt
sync
mount -o remount,ro /boot/firmware

# One loop device per root slot, reachable by PARTUUID the way §4.4 names
# every partition. Slot A's exists so the helper's second check — the two
# letters must not resolve to one device — has something to compare.
for slot in a b; do
    truncate -s 48M "/root/slots/root-${slot}.img"
    attach_loop "/root/slots/root-${slot}.img" > "/root/slots/root-${slot}.loop"
done
ln -sfn "$(cat /root/slots/root-a.loop)" "/dev/disk/by-partuuid/${ROOT_A_PARTUUID}"
ln -sfn "$(cat /root/slots/root-b.loop)" "/dev/disk/by-partuuid/${ROOT_B_PARTUUID}"

cat > /srv/appliance/partitions.env <<ENV
# partitions.env — as appliance/image/build.sh writes it. This machine's IDs.
BOOT_PARTUUID=${BOOT_PARTUUID}
ROOT_A_PARTUUID=${ROOT_A_PARTUUID}
ROOT_B_PARTUUID=${ROOT_B_PARTUUID}
APPLIANCE_PARTUUID=5a1b2c3d-04
DATA_PARTUUID=5a1b2c3d-05
LOCAL_PARTUUID=5a1b2c3d-06
ENV

echo "--- the OS package the slot cases write"
# A real ext4 filesystem, carrying the build host's /etc/fstab — every
# PARTUUID in it is wrong for this machine, which is the point (§14.1, Q10).
truncate -s 32M /root/slots/new-root.img
mkfs.ext4 -q -F -L newroot /root/slots/new-root.img
install -d /root/slots/mnt
attach_loop /root/slots/new-root.img > /root/slots/new-root.loop
mount "$(cat /root/slots/new-root.loop)" /root/slots/mnt
install -d /root/slots/mnt/etc
cat > /root/slots/mnt/etc/fstab <<'FSTAB'
# /etc/fstab from the build host. Every PARTUUID below is the build host's.
PARTUUID=0badf00d-01  /boot/firmware  vfat  ro,noatime,nofail  0 2
PARTUUID=0badf00d-04  /srv/appliance  ext4  defaults,nofail  0 2
PARTUUID=0badf00d-05  /data  ext4  defaults,nofail  0 2
PARTUUID=0badf00d-06  /srv/local  ext4  defaults,nofail  0 2
LABEL=AVC-BACKUP  /mnt/backup  ext4  defaults,nofail  0 2
FSTAB
printf 'v2.0.0\n' > /root/slots/mnt/etc/slot-marker
sync
umount /root/slots/mnt
losetup -d "$(cat /root/slots/new-root.loop)"
rm -f /root/slots/new-root.loop
zstd -q -f /root/slots/new-root.img -o /root/slots/root.img.zst

python3 - <<'PY'
"""The OS package (contracts §3, Q10), signed the way this harness signs.

The signature is the harness verifier's marker rather than Ed25519, for the
same reason good.tar's is: what these cases are about is what the helper
*writes*, and the real verification is proved against real anchors in
tests/unit/api/test_os_upgrade.py and by
tests/unit/appliance/test_image_keys.py. The manifest carries every member's
SHA-256 and size, because the helper re-hashes the bytes it writes.
"""

import hashlib
import io
import json
import tarfile
from pathlib import Path

MARKER = b"HARNESS-TRUSTED"

CMDLINE = (
    "console=serial0,115200 console=tty1 root=PARTUUID=0badf00d-02 "
    "rootfstype=ext4 fsck.repair=yes rootwait ro boot=overlay\n"
).encode()

payload = {
    "payload/root.img.zst": Path("/root/slots/root.img.zst").read_bytes(),
    "payload/boot/cmdline.txt": CMDLINE,
    "payload/boot/kernel8.img": b"not a kernel, but it is in the manifest\n",
    "payload/boot/overlays/disable-bt.dtbo": b"not an overlay either\n",
}


def write(path, package_type, version, signature=MARKER, members=payload):
    manifest = json.dumps(
        {
            "type": package_type,
            "version": version,
            "created_at": "2026-09-20T12:00:00+12:00",
            "members": [
                {
                    "path": name,
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "size": len(body),
                }
                for name, body in members.items()
            ],
        },
        indent=2,
    ).encode()
    with tarfile.open(path, "w") as archive:
        for name, body in (
            ("manifest.json", manifest),
            ("manifest.json.sig", signature),
            *members.items(),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(body))
    Path(path).chmod(0o600)
    return manifest


manifest = write("/data/tmp/os-v2.0.0.tar", "os", "v2.0.0")
Path("/data/tmp/os-v2.0.0.manifest.json").write_bytes(manifest)
# The manifest the application would have to have swapped the package for.
Path("/data/tmp/os-v2.0.0.other.json").write_bytes(
    manifest.replace(b'"v2.0.0"', b'"v9.9.9"')
)
PY
chown "${APP_UID}:${APP_UID}" /data/tmp/os-v2.0.0.tar /data/tmp/os-v2.0.0.manifest.json \
    /data/tmp/os-v2.0.0.other.json

systemctl daemon-reload
echo "setup: complete"
