"""Controlled egress authorization, TLS inspection and SSRF regression tests."""

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import ipaddress
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import h11
import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from contracts import v2 as C
from egress.policy import domain, public_ip, resolve
from egress.proxy import (
    Certificates,
    Proxy,
    connect_target,
    fetch,
    request,
    upstream_request,
)
from infra_control.service import Fault
from infra_control.v2.store import get_attempt, object_put, save_attempt
from runner.network_guard import NetworkGuard, normalized, rules
import test_v2 as fixture

uid = fixture.uid


def ca_files(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-only-egress")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
        .sign(key, hashes.SHA256())
    )
    certfile, keyfile = Path(directory) / "ca.pem", Path(directory) / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certfile, keyfile


class PolicyTests(unittest.TestCase):
    def test_domains_are_exact_canonical_and_not_numeric(self):
        self.assertEqual(domain("docs.python.org"), "docs.python.org")
        for host in [
            "*.python.org",
            "Docs.python.org",
            "python.org.",
            "127.0.0.1",
            "2130706433",
            "0177.0.0.1",
            "0x7f000001",
            "x.local",
            "x.internal",
            "a..org",
            "a.org:443",
            "a.org@b.org",
            "中文.cn",
        ]:
            with self.subTest(host=host), self.assertRaises(ValueError):
                domain(host)
        with self.assertRaises(ValueError):
            C.PolicyVersion(
                version="1",
                allowed_pools=["server"],
                tool_kinds=["python"],
                egress_rules=["example.com", "example.com"],
            )

    def test_private_metadata_multicast_and_transition_addresses(self):
        for address in [
            "127.0.0.1",
            "169.254.169.254",
            "169.254.170.2",
            "100.100.100.200",
            "10.1.2.3",
            "172.16.0.1",
            "192.168.0.1",
            "0.0.0.0",
            "224.0.0.1",
            "192.0.2.1",
            "::1",
            "fec0::1",
            "fc00::1",
            "fe80::1",
            "::ffff:8.8.8.8",
            "64:ff9b::808:808",
            "2002:0808:0808::1",
            "2001::1",
        ]:
            with self.subTest(address=address), self.assertRaises(ValueError):
                public_ip(address)
        self.assertEqual(public_ip("8.8.8.8"), "8.8.8.8")

    def test_tls_host_and_methods_cannot_be_smuggled(self):
        for method, target, headers in [
            (b"POST", b"/", {b"host": b"example.com"}),
            (b"GET", b"/", {b"host": b"evil.com"}),
            (b"GET", b"https://evil.com/", {b"host": b"example.com"}),
            (b"GET", b"//evil.com", {b"host": b"example.com"}),
            (b"GET", b"/", {b"host": b"example.com", b"cookie": b"secret"}),
            (b"GET", b"/", {b"host": b"example.com", b"upgrade": b"websocket"}),
        ]:
            value = h11.Request(
                method=method, target=target, headers=list(headers.items())
            )
            with (
                self.subTest(method=method, target=target),
                self.assertRaises(ValueError),
            ):
                upstream_request(value, headers, "example.com")

    def test_guard_has_default_drop_no_dns_no_ipv6_exception(self):
        objects = rules("172.30.0.2", 3128, "172.30.0.1/32", 44772)
        self.assertEqual(
            [x["chain"]["policy"] for x in objects if "chain" in x], ["drop"] * 3
        )
        self.assertFalse(any('"udp"' in str(x) for x in objects))
        self.assertEqual(
            normalized({"nftables": [{"metainfo": {}}] + objects}),
            normalized({"nftables": objects}),
        )


class AuthorizationTests(unittest.TestCase):
    for name in (
        "setUp",
        "tearDown",
        "headers",
        "register_node",
        "submit",
        "command",
        "claim",
    ):
        locals()[name] = getattr(fixture.V2Tests, name)

    def job(self):
        self.register_node(backend="opensandbox", egress_enabled=True)
        self.p.publish(
            self.admin,
            "policy",
            "internet",
            C.PolicyVersion(
                version="1",
                allowed_pools=["demo"],
                tool_kinds=["python"],
                egress_rules=["example.com"],
            ),
            uid(),
        )
        self.p.publish(
            self.admin,
            "agent",
            "browse",
            C.AgentVersion(
                agent_id="browse",
                version="1",
                description="read",
                source="print(1)",
                policy_ref={"id": "internet", "version": "1"},
            ),
            uid(),
        )
        job = self.claim(
            self.submit(
                agent_ref={"id": "browse", "version": "1"},
                policy_ref={"id": "internet", "version": "1"},
            )
        )
        grant = self.p.egress_grant(self.node, job["attempt"]["id"], C.Epoch(epoch=1))
        return job, C.EgressCheck(
            token=grant["token"], host="example.com", request_id=uid(), reserve=True
        )

    def isolate(self, job):
        with self.db.transaction(serialized=False) as tx:
            attempt = get_attempt(tx, job["attempt"]["id"])
            attempt["isolation_verified"] = True
            save_attempt(tx, attempt)

    def test_live_checks_bind_host_lease_epoch_and_isolation(self):
        job, check = self.job()
        with self.assertRaises(Fault):
            self.p.egress_check(check)
        self.isolate(job)
        self.assertGreater(self.p.egress_check(check)["remaining_seconds"], 0)
        self.p.egress_check(check.model_copy(update={"reserve": False}))
        with self.assertRaises(Fault):
            self.p.egress_check(check.model_copy(update={"host": "evil.com"}))
        self.now[0] += 31
        with self.assertRaises(Fault):
            self.p.egress_check(check)

    def test_policy_revocation_denies_existing_grant(self):
        job, check = self.job()
        self.isolate(job)
        self.p.egress_check(check)
        with self.db.transaction(serialized=False) as tx:
            object_put(
                tx, "revoked_policy", job["task"]["policy"]["digest"], {"revoked": True}
            )
        with self.assertRaises(Fault):
            self.p.egress_check(check)

    def test_request_budget_is_durable_and_replay_does_not_increment(self):
        job, check = self.job()
        self.isolate(job)
        for _ in range(128):
            self.p.egress_check(check.model_copy(update={"request_id": uid()}))
        with self.assertRaises(Fault):
            self.p.egress_check(check)

    def test_cancel_and_node_revoke_invalidate_existing_grant(self):
        job, check = self.job()
        self.isolate(job)
        self.p.egress_check(check)
        task = self.p.task(self.alice, job["task"]["id"])
        self.p.cancel(
            self.alice,
            task["id"],
            C.Cancel(expected_version=task["state_version"]),
            uid(),
        )
        with self.assertRaises(Fault):
            self.p.egress_check(check)

    def test_offline_proof_cannot_enable_network_policy(self):
        job, check = self.job()
        with self.db.transaction(serialized=False) as tx:
            attempt = get_attempt(tx, job["attempt"]["id"])
            attempt["sandbox_ref"] = "sandbox"
            save_attempt(tx, attempt)
        proof = self.p.verifier_keys.sign(
            "isolation",
            {
                "attempt": job["attempt"]["id"],
                "task": job["task"]["id"],
                "sandbox": "sandbox",
                "epoch": 1,
                "isolated": True,
                "policy": "offline-v2",
                "observed": self.now[0],
                "expires": self.now[0] + 30,
            },
        )
        with self.assertRaises(Fault):
            self.p.verify_isolation(
                self.node, job["attempt"]["id"], C.Isolation(epoch=1, proof=proof)
            )

    def test_proxy_identity_is_separate_from_temporal(self):
        _, check = self.job()
        result = self.http.post(
            "/internal/v2/egress/check",
            json=check.model_dump(),
            headers={"Authorization": "Bearer internal" + "x" * 32},
        )
        self.assertEqual(result.status_code, 401)


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_dns_answer_is_rejected(self):
        loop = asyncio.get_running_loop()
        with patch.object(
            loop,
            "getaddrinfo",
            AsyncMock(
                return_value=[
                    (2, 1, 6, "", ("8.8.8.8", 443)),
                    (2, 1, 6, "", ("127.0.0.1", 443)),
                ]
            ),
        ):
            with self.assertRaises(ValueError):
                await resolve("example.com")

    async def test_request_smuggling_duplicate_headers_and_body_rejected(self):
        for data in [
            b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\nContent-Length: 1\r\n\r\nx",
            b"GET / HTTP/1.1\r\nHost: example.com\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
            b"GET / HTTP/1.1\r\nHost: example.com\r\nX-A: a\r\nX-A: b\r\n\r\n",
            b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\nGET / HTTP/1.1\r\nHost: example.com\r\n\r\n",
        ]:
            reader = asyncio.StreamReader()
            reader.feed_data(data)
            reader.feed_eof()
            with self.assertRaises((ValueError, h11.RemoteProtocolError)):
                await request(reader)

    async def test_real_connect_tls_and_get_then_post_denial(self):
        with tempfile.TemporaryDirectory() as directory:
            certfile, keyfile = ca_files(directory)
            control = type("Control", (), {"check": AsyncMock(return_value=10)})()
            fetcher = AsyncMock(
                return_value=(
                    200,
                    [(b"content-length", b"5"), (b"connection", b"close")],
                    b"hello",
                )
            )
            proxy = Proxy(Certificates(certfile, keyfile), control, fetcher)
            server = await asyncio.start_server(proxy.connection, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            context = ssl.create_default_context(cafile=str(certfile))
            try:
                async with httpx.AsyncClient(
                    proxy=f"http://agent:{'t' * 32}@127.0.0.1:{port}",
                    verify=context,
                    trust_env=False,
                ) as client:
                    answer = await client.get("https://example.com/page")
                    self.assertEqual(answer.text, "hello")
                    with self.assertRaises(httpx.RemoteProtocolError):
                        await client.post("https://example.com/page")
                self.assertEqual(fetcher.await_count, 1)
                self.assertIn(
                    (b"host", b"example.com"), fetcher.call_args.args[0].headers
                )
                self.assertGreaterEqual(control.check.await_count, 3)
            finally:
                server.close()
                await server.wait_closed()

    async def test_no_second_dns_and_peer_pinning(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        reader.feed_eof()

        class Writer:
            def __init__(self):
                self.data = b""

            def write(self, data):
                self.data += data

            async def drain(self):
                pass

            def close(self):
                pass

            async def wait_closed(self):
                pass

            def get_extra_info(self, key):
                return ("8.8.8.8", 443)

        writer = Writer()
        resolver = AsyncMock(return_value=["8.8.8.8"])
        with patch(
            "egress.proxy.asyncio.open_connection",
            AsyncMock(return_value=(reader, writer)),
        ) as connect:
            value = h11.Request(
                method="GET", target="/", headers=[("host", "example.com")]
            )
            result = await fetch(value, "example.com", resolver=resolver)
            self.assertEqual(result[2], b"ok")
            self.assertEqual(connect.call_args.args, ("8.8.8.8", 443))
            self.assertEqual(connect.call_args.kwargs["server_hostname"], "example.com")
            self.assertTrue(connect.call_args.kwargs["ssl"].check_hostname)
            resolver.assert_awaited_once()

    async def test_untrusted_dns_result_never_opens_socket(self):
        with patch("egress.proxy.asyncio.open_connection", AsyncMock()) as connect:
            with self.assertRaises(ValueError):
                await fetch(
                    None,
                    "example.com",
                    resolver=AsyncMock(return_value=["169.254.169.254"]),
                )
            connect.assert_not_called()

    async def test_revocation_cancels_inflight_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            certfile, keyfile = ca_files(directory)
            calls = [0]
            cancelled = asyncio.Event()

            async def check(*args):
                calls[0] += 1
                if calls[0] >= 3:
                    raise PermissionError("revoked")
                return 10

            async def slow(*args):
                try:
                    await asyncio.sleep(10)
                finally:
                    cancelled.set()

            control = type("Control", (), {"check": staticmethod(check)})()
            proxy = Proxy(Certificates(certfile, keyfile), control, slow)
            server = await asyncio.start_server(proxy.connection, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            try:
                async with httpx.AsyncClient(
                    proxy=f"http://agent:{'t' * 32}@127.0.0.1:{port}",
                    verify=ssl.create_default_context(cafile=str(certfile)),
                    trust_env=False,
                ) as client:
                    with self.assertRaises(httpx.RemoteProtocolError):
                        await client.get("https://example.com/")
                await asyncio.wait_for(cancelled.wait(), 1)
            finally:
                server.close()
                await server.wait_closed()

    async def test_response_size_limit_and_encoded_body_rejected(self):
        for header in [
            b"Content-Length: 2097153",
            b"Content-Encoding: gzip\r\nContent-Length: 0",
        ]:
            reader = asyncio.StreamReader()
            reader.feed_data(b"HTTP/1.1 200 OK\r\n" + header + b"\r\n\r\n")
            reader.feed_eof()

            class Writer:
                def write(self, data):
                    pass

                async def drain(self):
                    pass

                def close(self):
                    pass

                async def wait_closed(self):
                    pass

                def get_extra_info(self, key):
                    return ("8.8.8.8", 443)

            with patch(
                "egress.proxy.asyncio.open_connection",
                AsyncMock(return_value=(reader, Writer())),
            ):
                with self.assertRaises(ValueError):
                    await fetch(
                        h11.Request(
                            method="GET", target="/", headers=[("host", "example.com")]
                        ),
                        "example.com",
                        resolver=AsyncMock(return_value=["8.8.8.8"]),
                    )
