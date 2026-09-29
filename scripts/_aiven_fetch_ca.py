#!/usr/bin/env python3
"""抓取 Aiven 服务端完整证书链，落盘 /root/aiven-ca.pem（用作客户端 CA 校验）。"""
import socket
import ssl

from cryptography.hazmat.primitives.serialization import Encoding

HOST = "zhongzhuan-maxiuquan1.c.aivencloud.com"
PORT = 26357

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

with socket.create_connection((HOST, PORT), timeout=15) as sock:
    with ctx.wrap_socket(sock, server_hostname=HOST) as tls:
        chain = tls.get_verified_chain() or []
        print(f"chain length: {len(chain)}")
        pems = []
        import hashlib

        for i, cert in enumerate(chain):
            der = cert.public_bytes(Encoding.PEM)
            pem = der.decode()
            pems.append(pem)
            print(f"  cert[{i}] subject={cert.subject.rfc4514_string()[:80]} sha256={hashlib.sha256(der).hexdigest()[:16]}")

with open("/root/aiven-ca.pem", "w") as f:
    f.write("\n".join(pems))
print("saved /root/aiven-ca.pem")
