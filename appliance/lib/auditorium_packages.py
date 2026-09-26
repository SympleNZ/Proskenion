"""The helper's seam onto package verification (contracts §2, §3; §6.11).

``auditorium-helper`` re-verifies every package and image against the trust
anchors before anything is written outside ``/data``. The application's verdict
is never trusted: it runs unprivileged, it is the thing being replaced, and an
update that could vouch for itself would authorise every update after it.

The verification itself is one implementation, ``verify_package(path, *,
expect_type)``. This module only decides **which copy of it the helper is
allowed to run**, and that decision is the security property:

* ``/usr/local/lib/auditorium/packages.py``, carried by the read-only root
  image and replaceable only by an OS upgrade that was itself verified, is the
  copy the appliance uses.
* An importable ``proskenion.core.packages`` is accepted only when it does not
  resolve under an untrusted prefix. On a development machine the package sits
  in a checkout and is fine; on the appliance it resolves through
  ``/opt/auditorium`` into ``/data/app/current``, which the unprivileged
  application owns, so it is refused. A package that shipped its own verifier
  must never be able to verify itself (§6.11).

There is no path through this module that reports a package as verified
without an implementation having said so. When none is available,
:func:`verify_package` raises :class:`VerifierUnavailable`, and the helper
refuses the request.

Trust anchors are read only from :data:`TRUSTED_KEYS_DIR`, which is on the
read-only root for the same reason.

``capture-image`` (§13.6, Q13) needs more of the implementation than
``verify_package`` alone — generating the image signing key pair and signing
the manifest it builds — so :func:`load_packages_module` exposes the whole
trusted module under the same two loaders and the same prefix rules.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Protocol

TRUSTED_KEYS_DIR = Path("/usr/local/share/auditorium/trusted-keys")

#: The implementation the root image carries, installed by
#: ``appliance/image/build.sh`` from ``proskenion/core/packages.py``.
ROOT_IMAGE_VERIFIER = Path("/usr/local/lib/auditorium/packages.py")

#: Prefixes an implementation must not come from. ``/data/app/current`` is the
#: application tree an update replaces; ``/opt/auditorium`` is the symlink into
#: it (§2.3); ``/srv/local`` and ``/data`` hold uploads and archives.
UNTRUSTED_PREFIXES = ("/data", "/opt/auditorium", "/srv/local", "/mnt")

#: Prefixes an installed implementation may come from. This is an allow list,
#: not the inverse of the one above: anywhere unlisted — a developer's
#: checkout, a home directory, a temporary path — is refused, so the helper
#: runs an implementation only from a place a root filesystem puts it. On the
#: appliance the root image's own copy is found first in any case.
TRUSTED_MODULE_PREFIXES = ("/usr/local/lib", "/usr/lib", "/usr/share")

#: The package types the verifier distinguishes (contracts §3).
PACKAGE_TYPES = ("app", "os", "image")


class VerificationError(Exception):
    """The package was not accepted. Nothing may be written on this path."""


class VerifierUnavailable(VerificationError):
    """No trusted implementation of the verifier could be loaded."""


class Verifier(Protocol):
    def __call__(self, path: Path, *, expect_type: str) -> Any: ...


def _is_untrusted(path: Path) -> bool:
    text = path.as_posix()
    return any(text == p or text.startswith(p + "/") for p in UNTRUSTED_PREFIXES)


def _is_trusted_module(path: Path) -> bool:
    text = path.as_posix()
    return any(text.startswith(p + "/") for p in TRUSTED_MODULE_PREFIXES)


def _attribute(module: object) -> Verifier | None:
    function = getattr(module, "verify_package", None)
    return function if callable(function) else None


def _from_root_image_module(path: Path) -> Any | None:
    if not path.is_file() or _is_untrusted(path):
        return None
    name = "auditorium_verifier_impl"
    existing = sys.modules.get(name)
    if existing is None:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            del sys.modules[name]
            raise
    else:
        module = existing
    return module


def _from_installed_module() -> Any | None:
    try:
        module = importlib.import_module("proskenion.core.packages")
    except ImportError:
        return None
    origin = getattr(module, "__file__", None)
    if origin is None:
        return None
    resolved = Path(os.path.realpath(origin))
    if _is_untrusted(resolved) or not _is_trusted_module(resolved):
        return None
    return module


def load_packages_module(*, root_image_verifier: Path = ROOT_IMAGE_VERIFIER) -> Any:
    """The trusted implementation module itself, not just ``verify_package``.

    ``capture-image`` (§13.6, Q13) needs more than verification: it
    generates the image signing key at first boot and signs the manifest it
    builds at every capture. Those are the same trust rules as
    :func:`load_verifier` — the code must come from the read-only root, never
    from a path an update can write — so this shares its two loaders rather
    than duplicating the prefix checks. Raises :class:`VerifierUnavailable`
    when neither loader finds one, exactly as :func:`load_verifier` does.
    """
    for candidate in (lambda: _from_root_image_module(root_image_verifier), _from_installed_module):
        try:
            found = candidate()
        except Exception as exc:  # a broken implementation is an unavailable one
            raise VerifierUnavailable(f"the package verifier could not be loaded: {exc}") from exc
        if found is not None:
            return found
    raise VerifierUnavailable(
        "no package verifier is available: expected verify_package() in "
        f"{root_image_verifier} or in an installed proskenion.core.packages outside "
        f"{', '.join(UNTRUSTED_PREFIXES)}"
    )


def load_verifier(*, root_image_verifier: Path = ROOT_IMAGE_VERIFIER) -> Verifier:
    """The verifier the helper may run, or raise :class:`VerifierUnavailable`."""
    module = load_packages_module(root_image_verifier=root_image_verifier)
    function = _attribute(module)
    if function is None:
        raise VerifierUnavailable(
            f"{module} carries no callable verify_package(); the package implementation "
            "is not the one this helper expects"
        )
    return function


def verify_package(
    path: Path, *, expect_type: str, root_image_verifier: Path = ROOT_IMAGE_VERIFIER
) -> Any:
    """Verify ``path`` as a package of ``expect_type``, or raise.

    Returns whatever the implementation returns — the verified manifest — so
    the caller can read the member list it just checked instead of parsing the
    package a second time.
    """
    if expect_type not in PACKAGE_TYPES:
        raise VerificationError(f"unknown package type {expect_type!r}")
    verifier = load_verifier(root_image_verifier=root_image_verifier)
    try:
        return verifier(path, expect_type=expect_type)
    except VerificationError:
        raise
    except Exception as exc:
        raise VerificationError(f"{path} failed verification: {exc}") from exc


def peek_type(path: Path, *, root_image_verifier: Path = ROOT_IMAGE_VERIFIER) -> str | None:
    """The ``type`` an unverified package *claims*, before anything is checked.

    Used only to choose which anchors directory a later, real verification
    should apply — ``write-slot`` (contracts §2) writes a slot from either an
    OS package or a system image (Q13), and those verify against
    different anchors. A lie here only gets the package refused by
    :func:`verify_package`'s own rule 5 (type matches expected), so nothing
    here needs to be trusted; see :func:`proskenion.core.packages.peek_manifest_type`.
    """
    try:
        module = load_packages_module(root_image_verifier=root_image_verifier)
    except VerifierUnavailable:
        # No implementation to peek with is not a reason to fail here: the
        # real verification the caller runs next needs the same
        # implementation and reports that absence properly. This is only
        # ever a hint for choosing anchors.
        return None
    function = getattr(module, "peek_manifest_type", None)
    if not callable(function):
        return None
    try:
        result = function(path)
    except Exception:  # a peek that fails is simply "no hint" (unverified anyway)
        return None
    return result if isinstance(result, str) else None
