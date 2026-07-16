"""TLS/SSL context builder + self-signed certificate generation."""
from __future__ import annotations

import ssl
import os
from pathlib import Path


def build_ssl_context(tls_cfg) -> ssl.SSLContext | None:
    """Build an SSLContext for the proxy server from TLSConfig.

    Returns None if TLS is disabled, so aiohttp falls back to plain HTTP.
    """
    if not tls_cfg.enabled:
        return None
    if not (tls_cfg.cert_file and tls_cfg.key_file):
        raise RuntimeError("tls.enabled=true but cert_file/key_file not configured")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tls_cfg.cert_file, tls_cfg.key_file)
    return ctx


def selfsign(
    out_cert: str,
    out_key: str,
    out_ca: str = "",
    cn: str = "zhongzhuan",
    san_dns: list[str] | None = None,
    san_ip: list[str] | None = None,
    days: int = 3650,
) -> None:
    """Generate a self-signed CA + leaf certificate with SANs.

    Uses the `cryptography` library. Falls back to openssl CLI if unavailable.
    """
    san_dns = san_dns or []
    san_ip = san_ip or []

    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime
        import ipaddress

        now = datetime.datetime.now(datetime.timezone.utc)

        # 1. Generate CA key + self-signed CA cert
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{cn}-CA")])
        # SubjectKeyIdentifier on the CA — required so the leaf's
        # AuthorityKeyIdentifier can reference it; modern TLS verifiers
        # (Python 3.14+, Node undici) reject CA-signed leaves missing AKI.
        ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_subject)
            .issuer_name(ca_subject)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False,
            ), critical=True)
            .add_extension(ca_ski, critical=False)
            .sign(ca_key, hashes.SHA256())
        )

        # 2. Generate leaf key + CSR, signed by CA
        leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        leaf_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])

        san_entries: list[x509.GeneralName] = []
        for dns_name in san_dns:
            san_entries.append(x509.DNSName(dns_name))
        for ip_addr in san_ip:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(ip_addr)))
        # Always include localhost
        if not san_entries:
            san_entries.append(x509.DNSName("localhost"))
            san_entries.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))

        leaf_cert = (
            x509.CertificateBuilder()
            .subject_name(leaf_subject)
            .issuer_name(ca_subject)
            .public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=min(days, 365)))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectAlternativeName(san_entries), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )

        # 3. Write files
        _write_key(out_key, leaf_key)
        _write_cert(out_cert, leaf_cert)
        if out_ca:
            _write_cert(out_ca, ca_cert)

    except ImportError:
        _selfsign_openssl(out_cert, out_key, out_ca, cn, san_dns, san_ip, days)


def _write_key(path: str, key) -> None:
    from cryptography.hazmat.primitives import serialization
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    Path(path).write_bytes(pem)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _write_cert(path: str, cert) -> None:
    from cryptography.hazmat.primitives import serialization
    pem = cert.public_bytes(serialization.Encoding.PEM)
    Path(path).write_bytes(pem)


def _selfsign_openssl(
    out_cert: str, out_key: str, out_ca: str,
    cn: str, san_dns: list[str], san_ip: list[str], days: int,
) -> None:
    """Fallback: use openssl CLI to generate self-signed cert."""
    import subprocess
    import sys

    san_parts = [f"DNS:{d}" for d in san_dns] + [f"IP:{i}" for i in san_ip]
    if not san_parts:
        san_parts = ["DNS:localhost", "IP:127.0.0.1"]
    san_str = ",".join(san_parts)

    # Generate key + self-signed cert with SANs (single cert, no separate CA)
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", out_key, "-out", out_cert,
        "-days", str(min(days, 365)), "-nodes",
        "-subj", f"/CN={cn}",
        "-addext", f"subjectAltName={san_str}",
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    try:
        os.chmod(out_key, 0o600)
    except OSError:
        pass

    # If CA output requested, copy the cert as CA (single-cert mode)
    if out_ca:
        import shutil
        shutil.copy2(out_cert, out_ca)
