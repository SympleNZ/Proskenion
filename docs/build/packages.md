# Packages: the format, signing, verification and key rollover

How an application update, an OS upgrade and a system image are built, signed
and admitted (§14.1, §6.11, §13.6). One format serves all three and one
verification path admits or refuses all three, so there is one place to get
this right rather than three.

The verifier is `proskenion/core/packages.py`. The tool is `tools/package.py`.
The appliance's unprivileged application verifies an upload, and the
privileged helper runs its own copy of the same verifier from the read-only
root and verifies it again before anything is written outside `/data`
(phase-6 contracts §2). **The application's verdict is never trusted.**

Status: **written and tested off-device.** The bench items are in
`docs/plans/phase-6.md`.

---

## 1. The layout

A package is an **uncompressed tar** with three parts, in this order:

```
manifest.json          UTF-8 JSON, canonical bytes
manifest.json.sig      64 raw bytes — Ed25519 over the exact bytes of manifest.json
payload/…              every file the manifest lists, and nothing else
```

The order is fixed and enforced. The manifest is first and the signature
second so that a verifier reading the file as a stream knows the signature
before it reads anything it has not yet checked. An OS package carries a root
filesystem image and runs to gigabytes on a machine with 4 GB of RAM
(contracts §9), so nothing is buffered and nothing is extracted to be
inspected.

**The outer tar is uncompressed deliberately.** A compressed container has to
be expanded before its members can be seen, which is where decompression bombs
live. The payload members carry their own compression — `root.img.zst`,
`app.tar.zst` — and are opaque blobs to the verifier. A gzip, bzip2, xz, zstd
or zip container is refused by its magic bytes, with a message saying so.

Distributed application packages are named `auditorium_<version>.aupkg`
(§14.1); the extension is a convention and the verifier does not read it. An
upload lands at `/data/tmp/upload-<uuid>.tar` (contracts §2) and is verified
there.

### What each type carries

| Type | Payload | Anchors it is verified against |
|---|---|---|
| `app` | `app/`, `wheels/`, `migrations/`, `web/` (§14.1) | `/usr/local/share/auditorium/trusted-keys/` |
| `os` | `root.img.zst` plus a `boot/` tree — kernel, initramfs, `cmdline.txt`, device trees, overlays (Q10) | `/usr/local/share/auditorium/trusted-keys/` |
| `image` | a captured root slot plus its record (§13.6, Q13) | `/srv/appliance/image-keys/` |

**The two anchor directories are never interchangeable.** An `image` verified
against the vendor anchors would let any developer-signed image restore onto
this machine, which Q13 refuses; an `app` verified against the machine's own
anchors would let the appliance sign its own updates with the key it generated
at first boot. `default_anchors_dir()` pairs each type with its own, and a
caller that passes `anchors_dir` explicitly is choosing the pairing itself.

An OS package's `cmdline.txt` and `/etc/fstab` are **not** this machine's. The
helper writes this machine's `root=PARTUUID` from `boot-state.json`'s `slots`
and adds `panic=10` and a bounded `rootwait` (Q10, contracts §3).

---

## 2. The manifest

```json
{
  "type": "app",
  "version": "v1.3.0",
  "created_at": "2026-09-20T12:00:00+12:00",
  "min_app_version": "v1.2.0",
  "description": "Improved CQ-20B state synchronisation",
  "changes": ["Recovery interface keyboard input", "Fix: WebSocket reconnection"],
  "members": [
    {"path": "payload/app/main.py", "sha256": "…64 lowercase hex…", "size": 12345}
  ]
}
```

| Key | Required | Rule |
|---|---|---|
| `type` | yes | `app`, `os` or `image` |
| `version` | yes | `vX.Y.Z` exactly — no suffix, no build metadata, no leading zeros |
| `created_at` | yes | ISO 8601 **with** a UTC offset (§4.9). A naive timestamp is refused |
| `min_app_version` | no | `vX.Y.Z` |
| `description` | no | one line, shown on the review panel (§21.24) |
| `changes` | no | list of strings, shown on the review panel |
| `members` | yes | at least one, at most 20 000; sorted by path; no duplicates |

Each member has exactly `path`, `sha256` (64 lowercase hex) and `size` (a
whole number ≥ 0) — no more keys and no fewer.

**Versions are directory names, never `__version__`** (contracts §1). The
appliance compares two of these to decide whether to admit an update; every
extra form is another ordering to get wrong. `v1.9.0 < v1.10.0`, numerically.

**Unknown manifest keys are refused, not ignored.** A key this verifier does
not understand means a package built for a later application, and
`min_app_version` is the mechanism for saying so: the first package that uses
a new key sets a minimum that includes the verifier which knows it. Silently
ignoring a key would let a constraint be added to the format and be invisible
to an appliance that had not been updated.

### Canonical bytes

The signature covers the manifest's bytes literally, so the manifest is
serialised one way and one way only: `json.dumps(..., sort_keys=True,
ensure_ascii=False, separators=(",", ":"))`, UTF-8, with a single trailing
newline. Optional keys are omitted rather than written as `null`. Members are
sorted by path. A build is therefore reproducible byte for byte from the same
inputs and a fixed `--created-at`, which is what makes the artefact CI builds
the artefact that gets signed locally.

### Member paths

Every path, in the manifest and in the tar header alike:

- begins with `payload/` and has at least one component after it;
- is relative — no leading `/`, no `X:` drive prefix;
- contains no `.` or `..` component, and equals its own `posixpath.normpath`;
- contains no NUL, no control character, no backslash;
- is at most 1024 characters, with components at most 255;
- has components matching `[A-Za-z0-9._+-]+` only — ASCII, deliberately. A
  package carries application code, vendored wheels, built web assets and a
  boot tree, none of which needs a space or a non-ASCII name, and every one of
  those has been a path-handling bug somewhere.

Every member is a **plain file**. A symlink, hardlink, device node, FIFO or
directory member is refused. Directories are implied by their members' paths
and created at extraction with modes this code chooses, so a package cannot
dictate the mode of a directory it is about to be extracted into.

---

## 3. Verification order

`verify_package(path, expect_type=…, anchors_dir=…, installed_version=…,
app_version=…)` streams the file once and applies these rules in this order.
**Nothing is extracted, written or decompressed until every one has passed.**

1. **Container.** The file is not empty, is not a compressed archive, and is a
   readable tar. Its first member is `manifest.json` and its second is
   `manifest.json.sig`, which is exactly 64 bytes. A package with no signature
   member is refused as unsigned.
2. **Signature.** `manifest.json.sig` verifies against some `*.pub` in the
   anchors directory. **Anchors come only from the root image** — a key inside
   the package is payload, never an anchor. Every installed anchor is tried,
   because during a rollover either may be the one that signed this package.
3. **Manifest.** It parses, every key is known, and every declared member path
   passes the path rules above.
4. **Type.** `type` matches what the caller will apply. An `os` package
   offered to the application endpoint is refused, and the reverse.
5. **Downgrade.** `version` is strictly higher than the installed one. Equal
   is refused too. Roll back is the way back to a previous version (§14.3). A
   `type: "image"` is exempt: restoring an older image is the point of keeping
   them.
6. **Minimum application version.** `min_app_version` is satisfied by the
   installed application. A package that states a minimum when the installed
   version is unknown is **refused**, not admitted.
7. **Payload.** Every remaining member, streamed: it is named in the manifest,
   is a plain file, its tar header size equals the manifest's size, and its
   SHA-256 matches, hashed as it streams. Nothing in the tar is absent from the
   manifest and nothing in the manifest is absent from the tar. Nothing but
   zero padding follows the end of the archive.

### Why 4 to 6 come before the payload

The contract lists them after the size and digest checks. They read only the
manifest, which rule 2 has already made trustworthy, so applying them first
costs nothing and saves reading two gigabytes to discover that a package was
offered to the wrong screen. No rule is skipped and no untrusted value is read
early; only the order of two refusals changes, and the more useful one wins.

The rules that need a member's tar header or its bytes cannot run any earlier
than the pass that reads them. Within that pass, a member's header is checked
before it is looked up in the manifest — a symlink is a symlink whether or not
the manifest mentions it — and its declared size is checked against the
manifest before a single byte of it is read, which is the whole of the defence
against a member that claims to be small and is not.

### What a caller sees

Every refusal raises a `PackageError` subclass carrying `rule` (stable,
machine-readable) and `summary` (written for the operator). `str(e)` adds the
specifics and belongs in the log, not on screen.

| Exception | `rule` | What the operator is told |
|---|---|---|
| `PackageMalformed` | `container` | This file is not a package, or it did not arrive intact. |
| `LayoutInvalid` | `layout` | This package is not laid out as a package: the manifest is missing or misplaced. |
| `PackageUnsigned` | `signature` | This package is unsigned. Signing is a deliberate step and has not been done. |
| `NoTrustAnchors` | `anchors` | No package signing key is installed, so no package can be verified. |
| `SignatureInvalid` | `signature` | §21.24's text: *This package could not be verified. It may be corrupted, or it was not built with a trusted signing key.* |
| `ManifestInvalid` | `manifest` | This package's manifest could not be read. |
| `UnsafeMemberPath` | `member_path` | This package names a file outside its payload, and was refused. |
| `UnsafeMemberType` | `member_type` | This package contains something that is not a plain file, and was refused. |
| `MemberMismatch` | `member_digest` | §21.24's text, as above — a corrupted member and a tampered one are indistinguishable. |
| `UnexpectedMember` | `unlisted_member` | This package contains a file its manifest does not list, and was refused. |
| `MissingMember` | `missing_member` | This package is incomplete: a file its manifest lists is not present. |
| `PackageTypeMismatch` | `type` | This package is not the kind of package this screen applies. |
| `DowngradeRefused` | `downgrade` | This package is not newer than the installed version. Use Roll back to return to a previous version. |
| `MinAppVersionNotMet` | `min_app_version` | This package needs a newer application version than the one installed. |

The REST layer turns `rule` into the `detail` of a §16.1 `validation_failed`
envelope; the vocabulary of codes is closed and `rule` does not join it.

---

## 4. Extraction

`extract_verified(path, destination, …)` **verifies again** rather than
trusting an earlier `verify_package`, and recomputes every member's hash as it
writes it. Two passes over the same file are two chances for the file to have
changed between them — an upload sits in `/data/tmp` owned by the
unprivileged application while the helper runs as root — so the pass that
writes is the pass that checks.

- `payload/` is stripped: `payload/app/main.py` becomes
  `destination/app/main.py`.
- Files are written 0644 and directories 0755, from this code. The package's
  ownership, timestamps and modes are discarded entirely.
- Only what the manifest names is written. **Nested archives are not
  unpacked** — a payload member that is itself a tar is written as a file, and
  whatever it contains is the business of whoever opens it later.
- The destination must be absent or an empty directory. Extraction happens in
  a sibling staging directory and is renamed into place only after the last
  member has matched, so **a refused package leaves the destination as it was
  found**, and a caller never sees a partial tree.

---

## 5. Trust anchors

Public keys live in `/usr/local/share/auditorium/trusted-keys/` on the
read-only root, installed there by `appliance/image/build.sh` from
`appliance/share/auditorium/trusted-keys/*.pub` in the repository. Any `*.pub`
in the directory can verify a package.

They are **never** read through a path under `/opt/auditorium`, which resolves
into `/data/app/current`: an update package could then ship its own key and
self-authorise every later update (§6.11). A `*.pub` that is not a regular
file — a symlink above all — is ignored, because an anchor must be the file
the root image placed there and not a pointer to somewhere writable.

### The on-disk format

One key per file:

```
# Proskenion package signing anchor (§6.11)
# key-id: 3f7a1c9e2b4d6085
# release key, held by Simon
ed25519 tVJ8QkrN0X5…base64 of the 32 raw public key bytes…=
```

- Lines beginning `#` are comments; blank lines are ignored; there is exactly
  one key line, and a file with two is rejected.
- The key line is `ed25519 ` followed by base64 of the **32 raw public key
  bytes**. Bare base64 rather than PEM or OpenSSH: the verifier must not be
  talked into parsing a certificate, a chain, or an algorithm it did not ask
  for.
- **The key id is derived, not read.** It is the first eight bytes of the
  SHA-256 of the raw key, as hex. The `# key-id:` comment is documentation; a
  file whose comment disagrees with its key is loaded anyway, under the real
  id, and logs a warning. A comment cannot claim an identity the key does not
  have.
- Mode 0644, owned by root, directory 0755.

Until at least one `*.pub` is present, `build.sh` warns and no package of any
kind can be verified — `NoTrustAnchors`, which says exactly that.

`/srv/appliance/image-keys/` holds this machine's own image-signing anchor,
generated on first boot alongside the device secret, in the same format. It is
on partition 4 rather than the read-only root because it is per-machine and
has to survive an OS upgrade.

---

## 6. Key rollover

The anchors live in a directory rather than a file precisely so that two can
be trusted at once. Replacing the anchor needs an OS upgrade, which needs a
valid signature from the key being replaced; holding two keys breaks that
circularity (§6.11, §14.1).

**Step 1 — add the new key, signed with the old one.**

```sh
uv run python tools/package.py keygen --name release-2027 --out-dir ~/keys \
    --comment "release key 2027, held by Simon"
cp ~/keys/release-2027.pub appliance/share/auditorium/trusted-keys/
# release-2026.pub stays where it is. Build and ship an OS package with BOTH.
uv run python tools/package.py build --type os --version v2.0.0 \
    --source build/os --output dist/auditorium_v2.0.0.aupkg
uv run python tools/package.py sign --package dist/auditorium_v2.0.0.aupkg \
    --key ~/keys/release-2026.key          # the OLD key
```

Both keys are now trusted on every appliance that has taken this upgrade, and
either verifies a package.

**Step 2 — remove the old key, signed with the new one.**

```sh
rm appliance/share/auditorium/trusted-keys/release-2026.pub
uv run python tools/package.py build --type os --version v2.1.0 \
    --source build/os --output dist/auditorium_v2.1.0.aupkg
uv run python tools/package.py sign --package dist/auditorium_v2.1.0.aupkg \
    --key ~/keys/release-2027.key          # the NEW key
```

Between the two steps there is no window in which the appliance cannot be
updated. **Do not combine them.** A single package that adds the new key and
removes the old one is fine until it is rolled back, at which point the
machine trusts a key you no longer sign with and refuses the package that
would fix it.

Keep the old private key until step 2 has been applied everywhere and
confirmed. Retire it afterwards; do not delete the record of its key id, which
appears in the audit log of every update it admitted.

### If the signing key is lost

No further packages of either type apply. There is no bypass, by design
(§14.1). Recovery is to reimage from the golden image with a fresh key pair
and restore `/data` from backup — the recovery USB (§13.7) does exactly this.

This is why the private key is backed up in **two independent places**: the
password manager and one offline copy (§20.1). Losing it turns a ten-minute
update into a rebuild.

---

## 7. Building and signing

An application package is built by `./build_package.sh` at the repository
root (§19.2), which assembles the `app/`, `wheels/`, `migrations/` and `web/`
tree — every locked dependency as a binary wheel for CPython 3.13 on aarch64
Debian 13 — and runs the `build` below on it. It never signs. The first
install of one is `docs/hardware/setup.md` §8. Members are written as pax
tar, which reads as plain ustar for every name under 100 bytes; wheel names
are often longer.

```sh
# Generate a key pair. Writes <name>.key (PKCS#8 PEM, mode 0600) and <name>.pub.
uv run python tools/package.py keygen --name release-2026 --out-dir ~/keys \
    --comment "release key, held by Simon"

# Build an unsigned package from a directory. The directory becomes payload/.
uv run python tools/package.py build --type app --version v1.3.0 \
    --source build/app --output dist/auditorium_v1.3.0.aupkg \
    --min-app-version v1.2.0 \
    --description "Improved CQ-20B state synchronisation" \
    --change "Recovery interface keyboard input" \
    --change "Fix: WebSocket reconnection"

# Sign it. LOCAL ONLY.
uv run python tools/package.py sign --package dist/auditorium_v1.3.0.aupkg \
    --key ~/keys/release-2026.key

# Check it with the appliance's own verifier before it leaves the machine.
uv run python tools/package.py verify --package dist/auditorium_v1.3.0.aupkg \
    --type app --anchors appliance/share/auditorium/trusted-keys \
    --installed-version v1.2.0 --app-version v1.2.0

# Look at a manifest without verifying it — for a package that was refused.
uv run python tools/package.py show --package dist/auditorium_v1.3.0.aupkg
```

Exit codes: `0` verified or written, `1` refused or failed, `2` bad usage.

**`build` and `sign` are separate steps, and `sign` never runs in CI**
(§22.8). Continuous integration produces the unsigned artefact on a `v*` tag;
signing is a deliberate local step with the private key. Putting the key into a
hosted secret store would place it somewhere a repository compromise can
reach, and its loss or theft is more consequential than the convenience is
worth.

`build` refuses a source tree it could not produce a verifiable package from:
a symlink, a device, a name with a space, an empty directory. A package this
tool cannot build is a package the appliance would refuse, and finding that
out at build time rather than on the bench is the point.

### Passphrases

A passphrase is **never** a command-line argument — it would sit in shell
history and in every process listing on the machine. Use `--passphrase-env`
naming an environment variable, or let the tool prompt with the echo off.
`--no-passphrase` says the key has none and suppresses the prompt.

### Reproducible builds

Pass `--created-at` to fix the timestamp and the same inputs produce the same
bytes: modes, ownership and member order are fixed by the writer, and the
manifest is canonical. Without it, `created_at` is now, in Pacific/Auckland
(§4.9).

---

## 8. What the tests cover

`tests/unit/core/test_packages.py` builds a corpus of packages that are wrong
in exactly one way and asserts both which rule refuses each one and that the
destination directory is still empty afterwards. It covers: a good package of
each type; a flipped signature byte; a signature by an untrusted key; an
anchor shipped inside the package; a manifest altered after signing; an
altered member of the same length; an extra, missing, duplicated or
size-mismatched member; `..` traversal, absolute paths, drive letters,
backslashes, un-normalised paths and names escaping `payload/`; names carrying
a NUL or a newline delivered through pax records; over-long names and
components; symlink, hardlink, device, FIFO and directory members; a header
claiming four gigabytes; a manifest claiming four gigabytes; a nested archive,
which is written as a file and never unpacked; a swapped `type`; downgrades
and equal versions; an unsatisfied and an unknowable `min_app_version`; an
empty file; five compressed containers and a really gzipped package; a
truncated tar; a member cut short; and a member appended past the end of the
archive. Two seeded property tests then mutate a good package bit by bit and
generate structurally hostile tars, asserting that nothing is ever accepted
with different content and that no input escapes as an unhandled exception.

`tests/unit/test_package_tool.py` covers the tool end to end, including the
two-step rollover with two keys and that the repository ships no private key.
