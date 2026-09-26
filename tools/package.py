#!/usr/bin/env python3
"""
package.py — build, sign and verify Proskenion packages (§14.1, §6.11, Q8).

One format serves application updates, OS upgrades and system images, and one
verification path admits or refuses all three. This tool is the other half of
that: it produces packages and checks them, using the same verifier the
appliance runs — :mod:`proskenion.core.packages` — so a package that passes
here passes there, and a package that fails here would have failed there.

    uv run python tools/package.py keygen  --name release-2026 --out-dir ~/keys
    uv run python tools/package.py build   --type app --version v1.3.0 \
                                           --source build/app --output dist/app-v1.3.0.tar
    uv run python tools/package.py sign    --package dist/app-v1.3.0.tar \
                                           --key ~/keys/release-2026.key
    uv run python tools/package.py verify  --package dist/app-v1.3.0.tar --type app \
                                           --anchors appliance/share/auditorium/trusted-keys

**build and sign are separate steps, and sign never runs in CI** (§22.8).
Continuous integration produces the unsigned artefact; the private key stays
on the developer's machine and in the password manager, because a hosted
secret store is somewhere a repository compromise can reach, and losing or
leaking this key is worse than the convenience is worth (§14.1).

The passphrase for a protected key is never a command-line argument — it
would sit in shell history and in every process listing on the machine. Pass
``--passphrase-env`` naming an environment variable, or let the tool prompt
with the echo off.

Key rollover (§14.1, §6.11)
    Anchors live in a directory precisely so two can be trusted at once:

      1. Generate the new key with ``keygen``. Add its ``.pub`` to
         ``appliance/share/auditorium/trusted-keys/`` **beside** the current
         one, and ship an OS package signed with the **current** key. Both
         keys are now trusted.
      2. Once that upgrade is installed everywhere, remove the old ``.pub``
         and ship a later OS package signed with the **new** key.

    Between the two steps either key verifies, so there is no window in which
    the appliance cannot be updated. Doing it the other way round — removing
    the old key in the same package that adds the new one — is fine in theory
    and unrecoverable if that package is ever rolled back.

Exit codes: 0 verified or written, 1 refused or failed, 2 bad usage.
"""

from __future__ import annotations

import argparse
import getpass
import io
import json
import os
import sys
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

_REPO_ROOT: Final = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:  # running the file directly, not as a module
    sys.path.insert(0, str(_REPO_ROOT))

from proskenion.core.packages import (  # noqa: E402  — after the sys.path line above
    MANIFEST_NAME,
    MAX_MANIFEST_BYTES,
    PACKAGE_TYPES,
    SIGNATURE_NAME,
    Manifest,
    Member,
    PackageError,
    PackageType,
    SigningKeyError,
    build_manifest_document,
    canonical_manifest_bytes,
    collect_members,
    default_anchors_dir,
    format_anchor,
    generate_signing_key,
    hash_file,
    is_version,
    key_id_for,
    load_anchors,
    load_private_key,
    make_tarinfo,
    parse_manifest,
    sign_manifest,
    verify_package,
    write_package,
    write_private_key,
)

AUCKLAND: Final = ZoneInfo("Pacific/Auckland")
EXIT_OK: Final = 0
EXIT_REFUSED: Final = 1
EXIT_USAGE: Final = 2


def fail(message: str, code: int = EXIT_REFUSED) -> int:
    print(f"package.py: {message}", file=sys.stderr)
    return code


def passphrase_from_args(args: argparse.Namespace, *, prompt: str) -> str | None:
    """A passphrase from the named environment variable, or a silent prompt."""
    if args.passphrase_env:
        value = os.environ.get(args.passphrase_env)
        if value:
            return value
        print(f"(no value in ${args.passphrase_env}; falling through to a prompt)")
    if args.no_passphrase:
        return None
    entered = getpass.getpass(prompt)
    return entered or None


def add_passphrase_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--passphrase-env",
        help="environment variable holding the key's passphrase "
        "(never pass the passphrase itself on the command line)",
    )
    parser.add_argument(
        "--no-passphrase",
        action="store_true",
        help="the key has no passphrase; do not prompt",
    )


# --------------------------------------------------------------------------
# keygen
# --------------------------------------------------------------------------


def command_keygen(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    private_path = out_dir / f"{args.name}.key"
    public_path = out_dir / f"{args.name}.pub"
    if public_path.exists():
        return fail(f"{public_path} exists")

    passphrase = passphrase_from_args(args, prompt="Passphrase for the new signing key: ")
    if passphrase is not None:
        again = getpass.getpass("Repeat the passphrase: ") if not args.passphrase_env else passphrase
        if again != passphrase:
            return fail("the passphrases do not match", EXIT_USAGE)

    key = generate_signing_key()
    try:
        write_private_key(key, private_path, passphrase=passphrase)
    except SigningKeyError as exc:
        return fail(str(exc))
    public_path.write_text(format_anchor(key.public_key(), comment=args.comment), encoding="utf-8")
    public_path.chmod(0o644)

    print(f"private key  {private_path}  (mode 0600 — never commit this, never put it in CI)")
    print(f"anchor       {public_path}")
    print(f"key id       {key_id_for(key.public_key())}")
    print()
    print("Next: copy the anchor into appliance/share/auditorium/trusted-keys/ and rebuild")
    print("the image (appliance/image/build.sh). Back the private key up in the password")
    print("manager and one offline copy — losing it turns an update into a rebuild (§14.1).")
    return EXIT_OK


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------


def command_build(args: argparse.Namespace) -> int:
    source = Path(args.source)
    output = Path(args.output)
    if not is_version(args.version):
        return fail(f"--version {args.version!r} is not vX.Y.Z", EXIT_USAGE)
    if args.min_app_version is not None and not is_version(args.min_app_version):
        return fail(f"--min-app-version {args.min_app_version!r} is not vX.Y.Z", EXIT_USAGE)
    if output.exists() and not args.force:
        return fail(f"{output} exists; pass --force to replace it", EXIT_USAGE)

    try:
        found = collect_members(source)
    except (OSError, ValueError, PackageError) as exc:
        return fail(str(exc))

    members: list[Member] = []
    for member_path, file_path in found:
        digest, size = hash_file(file_path)
        members.append(Member(path=member_path, sha256=digest, size=size))

    created_at = args.created_at or datetime.now(AUCKLAND).replace(microsecond=0).isoformat()
    document = build_manifest_document(
        package_type=args.type,
        version=args.version,
        created_at=created_at,
        members=members,
        min_app_version=args.min_app_version,
        description=args.description,
        changes=args.change or [],
    )
    manifest_bytes = canonical_manifest_bytes(document)
    # Parse what we just wrote, with the appliance's own parser, so a manifest
    # this tool cannot produce legally is caught here and not on the bench.
    try:
        parse_manifest(manifest_bytes)
    except PackageError as exc:
        return fail(f"the manifest this build produced is not valid: {exc}")

    mtime = int(datetime.fromisoformat(created_at).timestamp())
    output.parent.mkdir(parents=True, exist_ok=True)
    write_package(output, manifest_bytes=manifest_bytes, signature=None, members=found, mtime=mtime)

    total = sum(member.size for member in members)
    print(f"built  {output}")
    print(f"  type {args.type}   version {args.version}   members {len(members)}   bytes {total}")
    print(f"  unsigned — run 'package.py sign --package {output} --key <key>' locally (§22.8)")
    return EXIT_OK


# --------------------------------------------------------------------------
# sign
# --------------------------------------------------------------------------


def _read_manifest_bytes(package: Path) -> bytes:
    """The manifest of an unsigned package, refusing one that is already signed."""
    manifest_bytes: bytes | None = None
    with tarfile.open(package, mode="r:") as tar:
        for info in tar:
            if info.name == SIGNATURE_NAME:
                raise ValueError("this package is already signed")
            if info.name != MANIFEST_NAME:
                continue
            extracted = tar.extractfile(info)
            if extracted is None:
                raise ValueError(f"{MANIFEST_NAME} has no content; is this a package?")
            manifest_bytes = extracted.read(MAX_MANIFEST_BYTES + 1)
    if manifest_bytes is None:
        raise ValueError(f"no {MANIFEST_NAME}; is this a package?")
    return manifest_bytes


def command_sign(args: argparse.Namespace) -> int:
    package = Path(args.package)
    output = Path(args.output) if args.output else package
    try:
        manifest_bytes = _read_manifest_bytes(package)
    except (OSError, tarfile.TarError, ValueError) as exc:
        return fail(str(exc))
    try:
        manifest = parse_manifest(manifest_bytes)
    except PackageError as exc:
        return fail(f"the manifest will not verify, so it will not be signed: {exc}")

    passphrase = passphrase_from_args(args, prompt=f"Passphrase for {args.key}: ")
    try:
        key = load_private_key(Path(args.key), passphrase=passphrase)
    except SigningKeyError as exc:
        return fail(str(exc))
    signature = sign_manifest(key, manifest_bytes)

    mtime = int(datetime.fromisoformat(manifest.created_at).timestamp())
    staging = output.parent / f".{output.name}.signing"
    try:
        _write_signed(package, staging, manifest_bytes, signature, mtime)
    except (OSError, tarfile.TarError) as exc:
        staging.unlink(missing_ok=True)
        return fail(str(exc))
    os.replace(staging, output)

    print(f"signed {output}")
    print(f"  key id {key_id_for(key.public_key())}")
    print(f"  type {manifest.type}   version {manifest.version}")
    return EXIT_OK


def _write_signed(
    source: Path, target: Path, manifest_bytes: bytes, signature: bytes, mtime: int
) -> None:
    """Rewrite the package with the signature second, streaming the payload.

    The signature has to be the second member, because that is where the
    appliance's streaming verifier looks for it before reading anything else.
    Members are copied through their file objects rather than read into
    memory: an OS package carries a root filesystem image.
    """
    with (
        tarfile.open(source, mode="r:") as reader,
        # pax, as write_package does: a wheel's name can outgrow ustar's 100 bytes.
        tarfile.open(target, mode="w", format=tarfile.PAX_FORMAT) as writer,
    ):
        writer.addfile(
            make_tarinfo(MANIFEST_NAME, len(manifest_bytes), mtime), io.BytesIO(manifest_bytes)
        )
        writer.addfile(make_tarinfo(SIGNATURE_NAME, len(signature), mtime), io.BytesIO(signature))
        for info in reader:
            if info.name == MANIFEST_NAME:
                continue
            extracted = reader.extractfile(info)
            if extracted is None:
                raise ValueError(f"{info.name} has no content; is this a package?")
            with extracted:
                writer.addfile(make_tarinfo(info.name, info.size, mtime), extracted)


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


def command_verify(args: argparse.Namespace) -> int:
    anchors_dir = Path(args.anchors) if args.anchors else default_anchors_dir(args.type)
    anchors = load_anchors(anchors_dir)
    print(f"anchors {anchors_dir}")
    for anchor in anchors:
        print(f"  {anchor.key_id}  {anchor.path.name}  {anchor.comment}")
    if not anchors:
        print("  (none)")

    try:
        manifest = verify_package(
            Path(args.package),
            expect_type=args.type,
            anchors_dir=anchors_dir,
            installed_version=args.installed_version,
            app_version=args.app_version,
        )
    except PackageError as exc:
        print()
        print(f"REFUSED  rule={exc.rule}")
        print(f"  {exc.summary}")
        print(f"  {exc}")
        return EXIT_REFUSED

    print()
    print("VERIFIED")
    _print_manifest(manifest)
    return EXIT_OK


def _print_manifest(manifest: Manifest) -> None:
    print(f"  type             {manifest.type}")
    print(f"  version          {manifest.version}")
    print(f"  created          {manifest.created_at}")
    print(f"  min app version  {manifest.min_app_version or '—'}")
    print(f"  signed by        {manifest.key_id}")
    print(f"  members          {len(manifest.members)}  ({manifest.total_size} bytes)")
    if manifest.description:
        print(f"  description      {manifest.description}")
    for change in manifest.changes:
        print(f"    · {change}")


# --------------------------------------------------------------------------
# show
# --------------------------------------------------------------------------


def command_show(args: argparse.Namespace) -> int:
    """Print an unverified manifest, for looking at a package that was refused."""
    try:
        manifest_bytes, _ = _read_unsigned_or_signed(Path(args.package))
    except (OSError, tarfile.TarError, ValueError) as exc:
        return fail(str(exc))
    print("*** UNVERIFIED — this is what the package claims, not what has been checked ***")
    print(json.dumps(json.loads(manifest_bytes.decode("utf-8", "replace")), indent=2))
    return EXIT_OK


def _read_unsigned_or_signed(package: Path) -> tuple[bytes, bytes | None]:
    manifest_bytes: bytes | None = None
    signature: bytes | None = None
    with tarfile.open(package, mode="r:") as tar:
        for info in tar:
            if info.name not in (MANIFEST_NAME, SIGNATURE_NAME):
                continue
            extracted = tar.extractfile(info)
            if extracted is None:
                continue
            if info.name == MANIFEST_NAME:
                manifest_bytes = extracted.read()
            else:
                signature = extracted.read()
    if manifest_bytes is None:
        raise ValueError(f"no {MANIFEST_NAME}; is this a package?")
    return manifest_bytes, signature


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="package.py",
        description="Build, sign and verify Proskenion packages (§14.1, §6.11)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Signing is a local step and never runs in CI (§22.8).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    keygen = subparsers.add_parser("keygen", help="generate an Ed25519 signing key and its anchor")
    keygen.add_argument("--name", required=True, help="base name for <name>.key and <name>.pub")
    keygen.add_argument("--out-dir", required=True, type=Path, help="where to write the pair")
    keygen.add_argument("--comment", help="comment line for the anchor, e.g. who holds the key")
    add_passphrase_arguments(keygen)
    keygen.set_defaults(handler=command_keygen)

    build = subparsers.add_parser("build", help="build an unsigned package from a directory")
    build.add_argument("--type", required=True, choices=sorted(PACKAGE_TYPES))
    build.add_argument("--version", required=True, help="vX.Y.Z")
    build.add_argument("--source", required=True, type=Path, help="directory to become payload/")
    build.add_argument("--output", required=True, type=Path, help="the .tar to write")
    build.add_argument("--min-app-version", help="vX.Y.Z the appliance must already run")
    build.add_argument("--description", help="one line shown on the Updates screen (§21.24)")
    build.add_argument(
        "--change", action="append", help="a change line; repeat for each (§21.24)"
    )
    build.add_argument(
        "--created-at",
        help="ISO 8601 with offset; defaults to now in Pacific/Auckland. "
        "Fixing it makes the build reproducible byte for byte.",
    )
    build.add_argument("--force", action="store_true", help="replace an existing output file")
    build.set_defaults(handler=command_build)

    sign = subparsers.add_parser("sign", help="add the Ed25519 signature — LOCAL ONLY, never CI")
    sign.add_argument("--package", required=True, type=Path, help="the unsigned package")
    sign.add_argument("--key", required=True, type=Path, help="the PKCS#8 PEM private key")
    sign.add_argument("--output", type=Path, help="write here instead of replacing the input")
    add_passphrase_arguments(sign)
    sign.set_defaults(handler=command_sign)

    verify = subparsers.add_parser("verify", help="run the appliance's verifier over a package")
    verify.add_argument("--package", required=True, type=Path)
    verify.add_argument("--type", required=True, choices=sorted(PACKAGE_TYPES))
    verify.add_argument(
        "--anchors",
        type=Path,
        help="anchors directory; defaults to the installed one for this type",
    )
    verify.add_argument(
        "--installed-version", help="vX.Y.Z this package would replace, for the downgrade rule"
    )
    verify.add_argument(
        "--app-version", help="vX.Y.Z of the installed application, for min_app_version"
    )
    verify.set_defaults(handler=command_verify)

    show = subparsers.add_parser("show", help="print a package's manifest WITHOUT verifying it")
    show.add_argument("--package", required=True, type=Path)
    show.set_defaults(handler=command_show)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "type", None) is not None:
        args.type = cast_package_type(args.type)
    handler = args.handler
    result: int = handler(args)
    return result


def cast_package_type(value: str) -> PackageType:
    if value not in PACKAGE_TYPES:  # argparse has already checked; this satisfies the type
        raise SystemExit(f"unknown package type {value!r}")
    return value  # type: ignore[return-value]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_USAGE)
