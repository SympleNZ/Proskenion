"""Whether the served certificate is self-signed (spec §6.16, §21.8; phase-5 Q10).

``GET /auth/session`` reports ``certificate: "trusted" | "self_signed"`` so the
install prompt can suppress itself where iOS would refuse the install.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from proskenion.core import certs

HOSTNAME = "av.school.nz"


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _write(
    data_dir: Path,
    hostname: str,
    issuer: x509.Name,
    signer: ec.EllipticCurvePrivateKey,
    key: ec.EllipticCurvePrivateKey,
) -> Path:
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(_name(hostname))
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=90))
        .sign(signer, hashes.SHA256())
    )
    paths = certs.certificate_paths(data_dir, hostname)
    paths.directory.mkdir(parents=True, exist_ok=True)
    paths.certificate.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return paths.certificate


def write_self_signed(data_dir: Path, hostname: str = HOSTNAME) -> Path:
    """A certificate whose issuer is its own subject, where nginx reads it."""
    key = ec.generate_private_key(ec.SECP256R1())
    return _write(data_dir, hostname, _name(hostname), key, key)


def write_ca_issued(data_dir: Path, hostname: str = HOSTNAME) -> Path:
    """A certificate issued by a separate authority, as Let's Encrypt's would be."""
    authority = ec.generate_private_key(ec.SECP256R1())
    key = ec.generate_private_key(ec.SECP256R1())
    return _write(data_dir, hostname, _name("Test Issuing Authority"), authority, key)


async def test_a_self_signed_certificate_is_reported_as_such(tmp_path: Path) -> None:
    write_self_signed(tmp_path)
    assert await certs.served_trust(tmp_path, HOSTNAME, development=False) == "self_signed"
    assert await certs.served_trust(tmp_path, HOSTNAME, development=True) == "self_signed"


async def test_a_certificate_from_an_authority_is_trusted(tmp_path: Path) -> None:
    write_ca_issued(tmp_path)
    assert await certs.served_trust(tmp_path, HOSTNAME, development=False) == "trusted"


async def test_the_generated_self_signed_certificate_is_self_signed(tmp_path: Path) -> None:
    certs.generate_self_signed(
        HOSTNAME, directory=certs.certificate_paths(tmp_path, HOSTNAME).directory, key_size=2048
    )
    assert await certs.served_trust(tmp_path, HOSTNAME, development=False) == "self_signed"


async def test_nothing_installed_means_the_emergency_self_signed_pair(tmp_path: Path) -> None:
    assert await certs.served_trust(tmp_path, HOSTNAME, development=False) == "self_signed"
    assert await certs.served_trust(tmp_path, None, development=False) == "self_signed"


async def test_a_development_machine_without_a_certificate_is_trusted(tmp_path: Path) -> None:
    """No nginx, served from localhost, which browsers treat as secure."""
    assert await certs.served_trust(tmp_path, None, development=True) == "trusted"
    assert await certs.served_trust(tmp_path, HOSTNAME, development=True) == "trusted"


async def test_another_hostnames_certificate_is_not_read(tmp_path: Path) -> None:
    write_ca_issued(tmp_path, "other.school.nz")
    assert await certs.served_trust(tmp_path, HOSTNAME, development=False) == "self_signed"
