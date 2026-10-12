"""Bounded, anonymous HTTPS GET/HEAD inspection proxy. CONNECT is NOT a raw tunnel.

Each CONNECT carries a task credential, then TLS terminates here. Upstream TLS is
independently verified, with a pinned validated DNS address and canonical SNI/Host.
"""

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import ssl
import tempfile
import time
import uuid

import h11
import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from .policy import domain, public_ip, resolve

MAX_BODY = 2 * 1024 * 1024
MAX_HEADERS = 16384
LOG = logging.getLogger("agent_egress")


class Certificates:
    def __init__(self, cert_file, key_file):
        self.ca = x509.load_pem_x509_certificate(Path(cert_file).read_bytes())
        self.key = serialization.load_pem_private_key(
            Path(key_file).read_bytes(), password=None
        )
        if not self.ca.extensions.get_extension_for_class(
            x509.BasicConstraints
        ).value.ca:
            raise ValueError("ca_required")
        if self.ca.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        ) != self.key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        ):
            raise ValueError("ca_key_mismatch")
        self.cache = {}

    def context(self, host):
        domain(host)
        if self.ca.not_valid_after_utc <= datetime.now(timezone.utc) + timedelta(
            days=1
        ):
            raise ValueError("ca_expired")
        cached = self.cache.get(host)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(self.ca.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False
            )
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True
            )
            .sign(self.key, hashes.SHA256())
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.set_alpn_protocols(["http/1.1"])

        def sni(sock, name, ctx):
            if name != host:
                return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME

        context.set_servername_callback(sni)
        with tempfile.TemporaryDirectory(prefix="egress-cert-") as directory:
            cert_path, key_path = (
                Path(directory) / "cert.pem",
                Path(directory) / "key.pem",
            )
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            key_path.chmod(0o600)
            context.load_cert_chain(cert_path, key_path)
        if len(self.cache) >= 128:
            self.cache.clear()
        self.cache[host] = (time.monotonic() + 3600, context)
        return context


async def event(reader, connection):
    while True:
        value = connection.next_event()
        if value is h11.NEED_DATA:
            chunk = await reader.read(16384)
            connection.receive_data(chunk)
        elif value is h11.PAUSED:
            raise ValueError("unexpected_pipeline")
        else:
            return value


async def request(reader):
    connection = h11.Connection(h11.SERVER, max_incomplete_event_size=MAX_HEADERS)
    value = await event(reader, connection)
    if not isinstance(value, h11.Request):
        raise ValueError("request_required")
    if sum(len(k) + len(v) for k, v in value.headers) > MAX_HEADERS:
        raise ValueError("header_limit")
    names = [k for k, _ in value.headers]
    if len(set(names)) != len(names):
        raise ValueError("duplicate_header")
    headers = dict(value.headers)
    if b"transfer-encoding" in headers or headers.get(b"content-length", b"0") != b"0":
        raise ValueError("request_body_forbidden")
    if (
        not isinstance(await event(reader, connection), h11.EndOfMessage)
        or connection.trailing_data[0]
    ):
        raise ValueError("pipeline_forbidden")
    return connection, value, headers


def connect_target(value, headers):
    if value.method != b"CONNECT":
        raise ValueError("https_connect_required")
    target = value.target.decode("ascii")
    if (
        not target.endswith(":443")
        or headers.get(b"host", b"").decode("ascii") != target
    ):
        raise ValueError("https_443_required")
    host = domain(target[:-4])
    raw = headers.get(b"proxy-authorization", b"")
    if not raw.startswith(b"Basic "):
        raise PermissionError("proxy_auth_required")
    credentials = base64.b64decode(raw[6:], validate=True).decode("ascii")
    username, token = credentials.split(":", 1)
    if username != "agent" or not 24 <= len(token) <= 4096:
        raise PermissionError("proxy_auth_required")
    return host, token


def upstream_request(value, headers, host):
    if value.method not in {b"GET", b"HEAD"}:
        raise ValueError("method_forbidden")
    if headers.get(b"host") not in {host.encode(), (host + ":443").encode()}:
        raise ValueError("host_mismatch")
    target = value.target
    if (
        not target.startswith(b"/")
        or target.startswith(b"//")
        or b"#" in target
        or b"\\" in target
        or len(target) > 8192
    ):
        raise ValueError("origin_form_required")
    if any(
        x in headers
        for x in (
            b"upgrade",
            b"expect",
            b"authorization",
            b"cookie",
            b"proxy-authorization",
        )
    ):
        raise ValueError("anonymous_read_only_required")
    allowed = {
        b"accept",
        b"accept-language",
        b"user-agent",
        b"if-none-match",
        b"if-modified-since",
    }
    out = [(k, v) for k, v in headers.items() if k in allowed]
    out += [
        (b"host", host.encode()),
        (b"connection", b"close"),
        (b"accept-encoding", b"identity"),
    ]
    return h11.Request(method=value.method, target=target, headers=out)


async def fetch(value, host, *, resolver=resolve, ssl_context=None):
    ips = await resolver(host)
    # Revalidate even when an injected/custom resolver is used. No second DNS lookup.
    ips = [public_ip(ip) for ip in ips]
    if not ips:
        raise ValueError("no_public_address")
    context = ssl_context or ssl.create_default_context()
    context.set_alpn_protocols(["http/1.1"])
    reader, writer = await asyncio.open_connection(
        ips[0],
        443,
        ssl=context,
        server_hostname=host,
        ssl_handshake_timeout=3,
        ssl_shutdown_timeout=1,
        limit=MAX_HEADERS,
    )
    try:
        if public_ip(writer.get_extra_info("peername")[0]) != ips[0]:
            raise ValueError("peer_mismatch")
        conn = h11.Connection(h11.CLIENT, max_incomplete_event_size=MAX_HEADERS)
        writer.write(conn.send(value) + conn.send(h11.EndOfMessage()))
        await writer.drain()
        response = await event(reader, conn)
        if not isinstance(response, h11.Response) or response.status_code == 101:
            raise ValueError("http_response_required")
        headers = dict(response.headers)
        if headers.get(b"content-encoding", b"identity") != b"identity":
            raise ValueError("encoded_response_forbidden")
        if int(headers.get(b"content-length", b"0")) > MAX_BODY:
            raise ValueError("response_limit")
        body = bytearray()
        while True:
            part = await event(reader, conn)
            if isinstance(part, h11.EndOfMessage):
                break
            if not isinstance(part, h11.Data):
                raise ValueError("invalid_response")
            body.extend(part.data)
            if len(body) > MAX_BODY:
                raise ValueError("response_limit")
        allowed = {
            b"content-type",
            b"location",
            b"cache-control",
            b"etag",
            b"last-modified",
            b"content-security-policy",
            b"x-content-type-options",
        }
        filtered = [(k, v) for k, v in response.headers if k in allowed]
        filtered += [
            (b"content-length", str(len(body)).encode()),
            (b"connection", b"close"),
        ]
        # HEAD responses contain no Data; clients may ignore the represented length.
        return response.status_code, filtered, bytes(body)
    finally:
        writer.close()
        await writer.wait_closed()


class Control:
    def __init__(self, url, token):
        if not url.startswith("https://"):
            raise ValueError("control_tls_required")
        self.url = url.rstrip("/") + "/egress/check"
        self.client = httpx.AsyncClient(
            timeout=2,
            follow_redirects=False,
            trust_env=False,
            headers={"Authorization": "Bearer " + token},
        )

    async def check(self, token, host, request_id, reserve=False):
        response = await self.client.post(
            self.url,
            json={
                "token": token,
                "host": host,
                "request_id": request_id,
                "reserve": reserve,
            },
        )
        response.raise_for_status()
        remaining = response.json()["data"]["remaining_seconds"]
        if remaining <= 0:
            raise PermissionError("expired")
        return remaining


class Proxy:
    def __init__(self, certificates, control, fetcher=fetch):
        self.certificates, self.control, self.fetcher = certificates, control, fetcher
        self.active = 0

    async def connection(self, reader, writer):
        if self.active >= 32:
            writer.close()
            return
        self.active += 1
        phase, host, request_id = "connect", None, str(uuid.uuid4())
        try:
            async with asyncio.timeout(15):
                _, value, headers = await request(reader)
                host, token = connect_target(value, headers)
                remaining = await self.control.check(token, host, request_id, True)
                async with asyncio.timeout(remaining):
                    # Certificate contexts are cached; CA material never enters the guest.
                    context = self.certificates.context(host)
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                    phase = "tls"
                    await writer.start_tls(
                        context, ssl_handshake_timeout=3, ssl_shutdown_timeout=1
                    )
                    conn, value, headers = await request(reader)
                    outbound = upstream_request(value, headers, host)
                    phase = "fetch"
                    await self.control.check(token, host, request_id)

                    async def watch():
                        while True:
                            await asyncio.sleep(0.5)
                            await self.control.check(token, host, request_id)

                    watcher = asyncio.create_task(watch())
                    operation = asyncio.create_task(self.fetcher(outbound, host))
                    try:
                        done, _ = await asyncio.wait(
                            {watcher, operation}, return_when=asyncio.FIRST_COMPLETED
                        )
                        if watcher in done:
                            await watcher
                            raise PermissionError("authorization_lost")
                        status, response_headers, body = await operation
                        await self.control.check(token, host, request_id)
                        writer.write(
                            conn.send(
                                h11.Response(
                                    status_code=status, headers=response_headers
                                )
                            )
                        )
                        if body:
                            writer.write(conn.send(h11.Data(data=body)))
                        writer.write(conn.send(h11.EndOfMessage()))
                        await writer.drain()
                    finally:
                        operation.cancel()
                        watcher.cancel()
                        await asyncio.gather(operation, watcher, return_exceptions=True)
                    LOG.info(
                        json.dumps(
                            {
                                "request": request_id,
                                "host": host,
                                "status": status,
                                "bytes": len(body),
                            }
                        )
                    )
        except Exception as exc:
            LOG.info(
                json.dumps(
                    {
                        "request": request_id,
                        "host": host,
                        "denied": type(exc).__name__,
                        "phase": phase,
                    }
                )
            )
            if phase == "connect":
                writer.write(
                    b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="agent-egress"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'
                )
                try:
                    await writer.drain()
                except Exception:
                    pass
        finally:
            self.active -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def main():
    logging.basicConfig(level=logging.INFO)
    certificates = Certificates(
        os.environ["AGENT_EGRESS_CA_CERT"], os.environ["AGENT_EGRESS_CA_KEY"]
    )
    control = Control(
        os.environ["AGENT_EGRESS_CONTROL_URL"], os.environ["AGENT_EGRESS_SERVICE_TOKEN"]
    )
    proxy = Proxy(certificates, control)
    server = await asyncio.start_server(
        proxy.connection,
        os.environ["AGENT_EGRESS_BIND"],
        int(os.environ.get("AGENT_EGRESS_PROXY_PORT", "3128")),
        limit=MAX_HEADERS,
    )
    try:
        async with server:
            await server.serve_forever()
    finally:
        await control.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
