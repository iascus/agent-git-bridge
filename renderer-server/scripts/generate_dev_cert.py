from __future__ import annotations

import argparse
import socket
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


CERT_DIR = Path("certs")


def detect_lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("192.0.2.1", 80))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def write_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a CA and LAN HTTPS certificate.")
    parser.add_argument("--lan-ip", default=detect_lan_ip(), help="LAN IPv4 address for the certificate SAN")
    parser.add_argument("--cert-dir", type=Path, default=CERT_DIR, help="Directory to write certificate files")
    args = parser.parse_args()
    lan_ip = str(ip_address(args.lan_ip))
    cert_dir = args.cert_dir
    cert_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    ca_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "file-viewer local dev CA")]
    )
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "file-viewer LAN renderer")]
    )
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=825))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.IPAddress(ip_address(lan_ip)),
                    x509.IPAddress(ip_address("127.0.0.1")),
                    x509.DNSName("localhost"),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    write_key(cert_dir / "local-ca.key", ca_key)
    write_key(cert_dir / "server.key", server_key)
    ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM)
    server_pem = server_cert.public_bytes(serialization.Encoding.PEM)
    (cert_dir / "local-ca.crt").write_bytes(ca_pem)
    (cert_dir / "server.crt").write_bytes(server_pem)
    (cert_dir / "server-chain.crt").write_bytes(server_pem + ca_pem)

    print(f"Generated certificate for LAN IP {lan_ip}")
    print(f"Wrote {cert_dir / 'local-ca.crt'}")
    print(f"Wrote {cert_dir / 'local-ca.key'}")
    print(f"Wrote {cert_dir / 'server.crt'}")
    print(f"Wrote {cert_dir / 'server-chain.crt'}")
    print(f"Wrote {cert_dir / 'server.key'}")


if __name__ == "__main__":
    main()
