"""Unit tests for the TLS module (build_ssl_context + selfsign)."""

import os
import ssl
import tempfile
from pathlib import Path

import pytest

from zhongzhuan.config.config import TLSConfig
from zhongzhuan.proxy.tls import build_ssl_context, selfsign


class TestBuildSSLContext:
    def test_disabled_returns_none(self):
        cfg = TLSConfig(enabled=False)
        assert build_ssl_context(cfg) is None

    def test_enabled_without_files_raises(self):
        cfg = TLSConfig(enabled=True, cert_file="", key_file="")
        with pytest.raises(RuntimeError):
            build_ssl_context(cfg)

    def test_enabled_with_files_returns_context(self, tmp_path):
        # Generate a cert pair using selfsign first.
        cert = str(tmp_path / "cert.pem")
        key = str(tmp_path / "key.pem")
        ca = str(tmp_path / "ca.pem")
        selfsign(out_cert=cert, out_key=key, out_ca=ca, cn="test", san_dns=["localhost"])

        cfg = TLSConfig(enabled=True, cert_file=cert, key_file=key)
        ctx = build_ssl_context(cfg)
        assert isinstance(ctx, ssl.SSLContext)
        # Should be a server-side context.
        assert ctx.protocol == ssl.PROTOCOL_TLS_SERVER


class TestSelfsign:
    def test_generates_files(self, tmp_path):
        cert = str(tmp_path / "cert.pem")
        key = str(tmp_path / "key.pem")
        ca = str(tmp_path / "ca.pem")
        selfsign(
            out_cert=cert,
            out_key=key,
            out_ca=ca,
            cn="myproxy",
            san_dns=["example.com"],
            san_ip=["127.0.0.1"],
        )
        assert Path(cert).exists()
        assert Path(key).exists()
        assert Path(ca).exists()
        # Files should be non-empty PEM.
        cert_pem = Path(cert).read_bytes()
        key_pem = Path(key).read_bytes()
        ca_pem = Path(ca).read_bytes()
        assert b"BEGIN CERTIFICATE" in cert_pem
        assert b"BEGIN CERTIFICATE" in ca_pem
        assert b"PRIVATE KEY" in key_pem
        # Key permissions should be 0600 (or close — best-effort).
        if os.name == "posix":
            mode = os.stat(key).st_mode & 0o777
            assert mode == 0o600

    def test_default_san_when_none_provided(self, tmp_path):
        cert = str(tmp_path / "cert.pem")
        key = str(tmp_path / "key.pem")
        selfsign(out_cert=cert, out_key=key, cn="default")
        # Should still produce valid cert files with default localhost SAN.
        assert Path(cert).exists()
        assert Path(key).exists()

    def test_cert_loadable_by_sslcontext(self, tmp_path):
        """The generated cert/key pair must load into an SSLContext."""
        cert = str(tmp_path / "cert.pem")
        key = str(tmp_path / "key.pem")
        selfsign(out_cert=cert, out_key=key, cn="loader", san_dns=["localhost"])
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)  # should not raise

    def test_cert_has_san_entries(self, tmp_path):
        """Verify SANs are present in the generated cert via cryptography."""
        pytest.importorskip("cryptography")
        from cryptography import x509

        cert = str(tmp_path / "cert.pem")
        key = str(tmp_path / "key.pem")
        selfsign(
            out_cert=cert,
            out_key=key,
            cn="sancheck",
            san_dns=["my.example.com"],
            san_ip=["10.0.0.1"],
        )
        cert_obj = x509.load_pem_x509_certificate(Path(cert).read_bytes())
        san_ext = cert_obj.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        san = san_ext.value
        dns_names = san.get_values_for_type(x509.DNSName)
        ip_addrs = san.get_values_for_type(x509.IPAddress)
        assert "my.example.com" in dns_names
        import ipaddress

        assert ipaddress.ip_address("10.0.0.1") in ip_addrs

    def test_ca_signs_leaf(self, tmp_path):
        """The CA cert should be the issuer of the leaf cert."""
        pytest.importorskip("cryptography")
        from cryptography import x509

        cert = str(tmp_path / "cert.pem")
        key = str(tmp_path / "key.pem")
        ca = str(tmp_path / "ca.pem")
        selfsign(out_cert=cert, out_key=key, out_ca=ca, cn="leafcheck")
        ca_obj = x509.load_pem_x509_certificate(Path(ca).read_bytes())
        leaf_obj = x509.load_pem_x509_certificate(Path(cert).read_bytes())
        # CA subject should equal leaf's issuer.
        assert ca_obj.subject == leaf_obj.issuer
