"""Create a self-signed X.509 device certificate for IoT Hub thumbprint authentication.

    python tools/make_device_cert.py press-01 [--out certs] [--days 365] [--force]

Writes ``<out>/<id>-cert.pem`` and ``<out>/<id>-key.pem`` (owner-only permissions)
and prints the SHA-256 thumbprint. The certificate's CN is the device id.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9\-.%_*?!(),:=@$']{1,128}$")


def create_certificate(device_id: str, days: int) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    if not DEVICE_ID_PATTERN.match(device_id):
        raise ValueError(f"invalid IoT Hub device id: {device_id!r}")
    if not 1 <= days <= 825:
        raise ValueError("days must be between 1 and 825")
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, device_id)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def write_files(
    key: ec.EllipticCurvePrivateKey, cert: x509.Certificate, out: Path, device_id: str, force: bool
) -> tuple[Path, Path]:
    cert_path, key_path = out / f"{device_id}-cert.pem", out / f"{device_id}-key.pem"
    existing = [str(p) for p in (cert_path, key_path) if p.exists()]
    if existing and not force:
        raise FileExistsError(f"refusing to overwrite {', '.join(existing)} (use --force)")
    out.mkdir(parents=True, exist_ok=True)
    key_bytes = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key_bytes)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a self-signed IoT device certificate.")
    parser.add_argument("device_id")
    parser.add_argument("--out", type=Path, default=Path("certs"))
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    args = parser.parse_args()
    try:
        key, cert = create_certificate(args.device_id, args.days)
        cert_path, key_path = write_files(key, cert, args.out, args.device_id, args.force)
    except (ValueError, FileExistsError) as exc:
        raise SystemExit(f"error: {exc}") from exc
    print(f"certificate: {cert_path}\nprivate key: {key_path}")
    print(f"SHA-256 thumbprint: {cert.fingerprint(hashes.SHA256()).hex().upper()}")


if __name__ == "__main__":
    main()
