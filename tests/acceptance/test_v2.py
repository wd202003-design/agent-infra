"""D1 failure tests; run unchanged against PostgreSQL in CI."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import os
import tempfile
import time
import unittest
import uuid
from unittest.mock import AsyncMock, Mock
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from contracts import v2 as C
from infra_control.db import Database
from infra_control.service import Fault
from infra_control.v2.api import create_api
from infra_control.v2.artifacts import Artifacts, S3Storage
from infra_control.v2.model import ModelGateway
from infra_control.v2.security import Keys, sha
from infra_control.v2.service import Platform
from infra_control.v2.store import get_attempt, object_put, save_attempt
from runner.v2 import Client, Runner
from runner.wal import WAL


def uid():
    return str(uuid.uuid4())


class V2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dsn = os.environ.get("TEST_DATABASE_URL")
        self.schema = None
        if self.dsn:
            import psycopg

            self.schema = "test_" + uuid.uuid4().hex
            with psycopg.connect(self.dsn) as conn:
                conn.execute("CREATE SCHEMA " + self.schema)
            self.db = Database(self.dsn, schema=self.schema)
        else:
            self.db = Database("sqlite:" + self.tmp.name + "/db", test_sqlite=True)
        self.db.migrate()
        self.now = [time.time()]
        self.p = Platform(
            self.db,
            Keys(b"t" * 32),
            clock=lambda: self.now[0],
            verifier_keys=Keys(b"v" * 32),
        )
        self.users = [
            {
                "id": name,
                "role": "admin" if name == "admin" else "user",
                "pools": ["demo"],
                "token_sha256": sha(name + "x" * 32),
                "issued_at": self.now[0] - 1,
                "expires_at": self.now[0] + 10000,
            }
            for name in ("admin", "alice", "bob")
        ]
        self.admin, self.alice, self.bob = self.users
        api, internal = create_api(
            self.p, self.users, internal_token_hash=sha("internal" + "x" * 32)
        )
        self.app = FastAPI()
        self.app.mount("/v2", api)
        self.app.mount("/internal/v2", internal)
        self.http = TestClient(self.app)
        self.policy = self.p.publish(
            self.admin,
            "policy",
            "offline",
            C.PolicyVersion(version="1", allowed_pools=["demo"], tool_kinds=["python"]),
            uid(),
        )
        self.p.publish(
            self.admin,
            "agent",
            "calc",
            C.AgentVersion(
                agent_id="calc",
                version="1",
                description="test",
                source="print(42)",
                policy_ref={"id": "offline", "version": "1"},
            ),
            uid(),
        )
        self.register_node()

    def tearDown(self):
        self.http.close()
        if self.schema:
            import psycopg

            with psycopg.connect(self.dsn) as conn:
                conn.execute("DROP SCHEMA " + self.schema + " CASCADE")
        self.tmp.cleanup()

    def headers(self, name="alice"):
        return {"Authorization": "Bearer " + name + "x" * 32, "Idempotency-Key": uid()}

    def register_node(self, **kwargs):
        ticket = self.p.ticket(
            self.admin, C.NodePolicy(pool="demo", owner="admin", **kwargs), uid()
        )
        payload = C.Register(
            ticket_id=ticket["ticket_id"],
            ticket_secret=ticket["ticket_secret"],
            registration_id=uid(),
            protocol="2.0",
            instance_public_info="test",
        )
        result = self.p.register(payload, payload.registration_id)
        self.node = result["node_id"]
        self.token = result["node_token"]
        return ticket, payload, result

    def submit(self, key=None, **kwargs):
        return self.p.submit(
            self.alice,
            C.Submit(
                **(
                    {
                        "agent_ref": {"id": "calc", "version": "1"},
                        "policy_ref": {"id": "offline", "version": "1"},
                        "pool": "demo",
                    }
                    | kwargs
                )
            ),
            key or uid(),
        )

    def command(self, task, kind="make_ready"):
        return self.p.command(
            C.Command(
                command_id=uid(),
                task_id=task["id"],
                expected_version=task["state_version"],
                expected_epoch=task["epoch"],
                type=kind,
            )
        )

    def claim(self, task=None, slots=1):
        if task is not None:
            self.command(task)
        return self.p.claim(
            self.node,
            C.Claim(claim_id=uid(), protocol="2.0", advertised_slots=slots),
            uid(),
        )

    def report(self, job, **kwargs):
        payload = C.Report(
            **(
                {
                    "epoch": job["attempt"]["epoch"],
                    "outcome": "succeeded",
                    "result": "42",
                    "stop_evidence_ref": "SIMULATED",
                    "cleanup_evidence_ref": "SIMULATED",
                    "unknown_effects": False,
                }
                | kwargs
            )
        )
        return self.p.report(self.node, job["attempt"]["id"], payload, uid())

    def test_auth_expiry_cross_user_internal_identity(self):
        t = self.submit()
        self.assertEqual(
            self.http.get(
                "/v2/tasks/" + t["id"], headers=self.headers("bob")
            ).status_code,
            404,
        )
        self.assertEqual(
            self.http.get(
                "/v2/tasks", headers={"Authorization": "Bearer " + self.token}
            ).status_code,
            401,
        )
        self.assertEqual(
            self.http.get(
                "/internal/v2/tasks/" + t["id"], headers=self.headers("admin")
            ).status_code,
            401,
        )
        self.now[0] += 10001
        self.assertEqual(
            self.http.get("/v2/tasks", headers=self.headers()).status_code, 401
        )

    def test_strict_models_do_not_echo_input(self):
        response = self.http.post(
            "/v2/tasks", headers=self.headers(), json={"secret": "do-not-echo"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("do-not-echo", response.text)
        self.assertEqual(
            self.http.post(
                "/v2/nodes/tickets",
                headers=self.headers(),
                json={"pool": "demo", "owner": "alice"},
            ).status_code,
            403,
        )

    def test_concurrent_submit_idempotency(self):
        with ThreadPoolExecutor(max_workers=5) as executor:
            items = list(executor.map(lambda _: self.submit("shared"), range(5)))
        self.assertEqual(len({t["id"] for t in items}), 1)
        with self.assertRaises(Fault):
            self.submit("shared", input={"x": 1})

    def test_registration_capsule_and_consumption(self):
        ticket, payload, result = self.register_node()
        self.assertEqual(self.p.register(payload, payload.registration_id), result)
        with self.assertRaises(Fault):
            self.p.register(
                payload.model_copy(update={"registration_id": uid()}), uid()
            )
        with self.db.transaction(serialized=False) as tx:
            data = str(
                tx.execute("SELECT response FROM v2_idempotency").fetchall()
            ) + str(tx.execute("SELECT body FROM v2_objects").fetchall())
        self.assertNotIn(result["node_token"], data)
        self.assertNotIn(ticket["ticket_secret"], data)

    def test_rotation_overlap_and_permanent_revocation(self):
        old = self.token
        req = C.Rotate(rotation_id=uid(), protocol="2.0")
        new = self.p.rotate(self.node, req, req.rotation_id)
        self.assertEqual(new, self.p.rotate(self.node, req, req.rotation_id))
        self.assertEqual(self.node, self.p.authenticate(old))
        self.now[0] += 61
        with self.assertRaises(Fault):
            self.p.authenticate(old)
        self.assertEqual(self.node, self.p.authenticate(new["node_token"]))
        self.p.revoke(self.admin, self.node, C.Revoke(reason="test"), uid())
        with self.assertRaises(Fault):
            self.p.authenticate(new["node_token"])

    def test_temporal_readiness_and_concurrent_two_slots(self):
        self.register_node(slots_limit=2)
        first = self.submit()
        second = self.submit()
        self.assertIsNone(self.claim())
        self.command(first)
        self.command(second)
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = list(pool.map(lambda _: self.claim(slots=2), range(2)))
        self.assertEqual({j["attempt"]["slot"] for j in jobs}, {0, 1})
        self.assertIsNone(self.claim(slots=2))

    def test_cancel_wins_and_cas(self):
        job = self.claim(self.submit())
        task = self.p.task(self.alice, job["task"]["id"])
        with self.assertRaises(Fault):
            self.p.cancel(self.alice, task["id"], C.Cancel(expected_version=1), uid())
        self.p.cancel(
            self.alice,
            task["id"],
            C.Cancel(expected_version=task["state_version"]),
            uid(),
        )
        self.assertEqual(self.report(job)["state"], "cancelled")

    def test_expired_lease_never_releases_slot(self):
        job = self.claim(self.submit())
        self.now[0] += 31
        self.assertEqual(
            self.p.task(self.alice, job["task"]["id"])["state"], "needs_review"
        )
        self.assertFalse(
            self.p.renew(
                self.node,
                job["attempt"]["id"],
                C.Renew(task_id=job["task"]["id"], epoch=1),
            )["authorized"]
        )
        self.assertIsNone(self.claim())

    def test_capability_binding_and_live_revocation(self):
        job = self.claim(self.submit())
        req = C.Authorize(
            epoch=1,
            step_id=uid(),
            operation_id=uid(),
            tool="python",
            args_hash="a" * 64,
            policy_digest=self.policy["digest"],
        )
        token = self.p.authorize(self.node, job["attempt"]["id"], req, uid())[
            "capability"
        ]
        check = C.VerifyCapability(capability=token, tool="python", args_hash="a" * 64)
        self.assertTrue(self.p.verify_capability(self.node, check)["authorized"])
        with self.assertRaises(Fault):
            self.p.verify_capability(
                self.node, check.model_copy(update={"args_hash": "b" * 64})
            )
        with self.db.transaction(serialized=False) as tx:
            object_put(tx, "revoked_policy", self.policy["digest"], {"revoked": True})
        with self.assertRaises(Fault):
            self.p.verify_capability(self.node, check)

    def test_event_gap_duplicate_conflict(self):
        job = self.claim(self.submit())
        one = {
            "runner_seq": 1,
            "event_id": uid(),
            "type": "log",
            "payload": {"text": "one"},
        }
        two = {
            "runner_seq": 2,
            "event_id": uid(),
            "type": "log",
            "payload": {"text": "two"},
        }
        call = lambda e: self.p.runner_events(
            self.node, job["attempt"]["id"], C.Events(epoch=1, events=e), uid()
        )
        self.assertEqual(call([two]), {"contiguous_ack": 0, "gaps": [1]})
        self.assertEqual(call([one])["contiguous_ack"], 2)
        self.assertEqual(call([one, two])["contiguous_ack"], 2)
        with self.assertRaises(Fault):
            call([one | {"payload": {"text": "tampered"}}])

    def test_cursor_binding_and_expiry(self):
        t = self.submit()
        other = self.submit()
        items, cursor = self.p.events(self.alice, t["id"])
        self.assertTrue(items)
        with self.assertRaises(Fault):
            self.p.events(self.alice, other["id"], cursor)
        with self.assertRaises(Fault):
            self.p.events(self.bob, t["id"], cursor)
        self.now[0] += 86401
        with self.assertRaises(Fault):
            self.p.events(self.alice, t["id"], cursor)

    def test_real_cleanup_requires_independent_proof(self):
        self.register_node(backend="opensandbox")
        job = self.claim(self.submit())
        self.assertEqual(self.report(job)["state"], "needs_review")

    def test_signed_cleanup_bound_to_attempt(self):
        self.register_node(backend="opensandbox")
        job = self.claim(self.submit())
        aid = job["attempt"]["id"]
        with self.db.transaction(serialized=False) as tx:
            a = get_attempt(tx, aid)
            a["sandbox_ref"] = "sandbox"
            save_attempt(tx, a)
        proof = self.p.verifier_keys.sign(
            "cleanup",
            {
                "attempt": aid,
                "epoch": 1,
                "sandbox": "sandbox",
                "management_absent": True,
                "host_absent": True,
                "observed": self.now[0],
                "expires": self.now[0] + 30,
            },
        )
        self.assertEqual(
            self.report(
                job,
                sandbox_ref="sandbox",
                stop_evidence_ref=proof,
                cleanup_evidence_ref=proof,
            )["state"],
            "succeeded",
        )

    def test_safe_retry_epoch_limit(self):
        job = self.claim(self.submit())
        failed = self.report(job, outcome="failed")
        self.command(failed, "schedule_safe_retry")
        again = self.claim()
        self.assertEqual(again["attempt"]["epoch"], 2)
        with self.assertRaises(Fault):
            self.report(job)
        failed = self.report(again, outcome="failed")
        with self.assertRaises(Fault):
            self.command(failed, "schedule_safe_retry")

    def test_unknown_effects_block_retry(self):
        job = self.claim(self.submit())
        task = self.report(job, outcome="failed", unknown_effects=True)
        self.assertEqual(task["state"], "needs_review")
        with self.assertRaises(Fault):
            self.command(task, "schedule_safe_retry")

    def test_sensitive_paths_and_hard_budget_fail_closed(self):
        for kwargs in (
            {"workspace_ref": "private"},
            {"budget": {"mode": "hard"}},
            {"data_class": "private"},
        ):
            with self.assertRaises(Fault):
                self.submit(**kwargs)

    def test_schema_external_refs_forbidden(self):
        with self.assertRaises(Fault):
            self.p.publish(
                self.admin,
                "policy",
                "evil",
                C.PolicyVersion(
                    version="1",
                    allowed_pools=["demo"],
                    tool_kinds=["python"],
                    upload_schema={"$ref": "https://example.org/schema"},
                ),
                uid(),
            )

    def test_internal_command_dedup_and_conflict(self):
        t = self.submit()
        req = C.Command(
            command_id=uid(),
            task_id=t["id"],
            expected_version=t["state_version"],
            type="make_ready",
        )
        self.assertEqual(self.p.command(req), self.p.command(req))
        with self.assertRaises(Fault):
            self.p.command(req.model_copy(update={"type": "request_stop"}))

    def model_job(self):
        self.p.publish(
            self.admin,
            "agent",
            "think",
            C.AgentVersion(
                agent_id="think",
                version="1",
                description="model",
                mode="agent",
                instruction="calculate",
                policy_ref={"id": "offline", "version": "1"},
            ),
            uid(),
        )
        self.register_node(backend="opensandbox")
        job = self.claim(self.submit(agent_ref={"id": "think", "version": "1"}))
        with self.db.transaction(serialized=False) as tx:
            a = get_attempt(tx, job["attempt"]["id"])
            a["isolation_verified"] = True
            save_attempt(tx, a)
        req = C.Model(
            epoch=1,
            step_id=uid(),
            request_id=uid(),
            messages=[{"role": "user", "content": "hello"}],
            budget_ref=job["task"]["id"],
        )
        cfg = {
            "url": "http://gateway/v1",
            "key": "test-only",
            "model": "fixture",
            "quota_pool_id": "shared",
            "price_version": "test-1",
            "currency": "USD",
            "input_micro_per_million": 1000000,
            "output_micro_per_million": 2000000,
        }
        return job, req, cfg

    def test_unknown_model_keeps_reservation_and_reconciliation(self):
        job, req, cfg = self.model_job()
        gateway = ModelGateway(self.p, cfg)
        call, fresh, task = gateway.reserve(self.node, job["attempt"]["id"], req)
        gateway.finish(call)
        with self.assertRaises(Fault):
            gateway.reserve(self.node, job["attempt"]["id"], req)
        self.assertEqual(
            gateway.usage(self.alice, task["id"])["requests"][0]["status"], "unknown"
        )
        self.assertGreater(self.p.task(self.alice, task["id"])["reserved_tokens"], 0)
        self.now[0] += 61
        reconciled = gateway.reconcile(
            self.admin,
            task["id"],
            call["id"],
            C.ReconcileUsage(
                expected_version=2,
                actual_tokens=20,
                cost=25,
                currency="USD",
                evidence_ref="billing-provider-reference",
            ),
            uid(),
        )
        self.assertEqual(reconciled["status"], "reconciled")
        self.assertEqual(self.p.task(self.alice, task["id"])["state"], "needs_review")

    def test_known_model_usage_ledger_cache(self):
        job, req, cfg = self.model_job()
        calls = []

        def upstream(request):
            calls.append(request)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": '{"action":"final","content":"42"}'}}
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
            )

        gateway = ModelGateway(self.p, cfg, transport=httpx.MockTransport(upstream))
        first = asyncio.run(gateway.invoke(self.node, job["attempt"]["id"], req))
        second = asyncio.run(gateway.invoke(self.node, job["attempt"]["id"], req))
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["cost"], 20)
        self.assertEqual(
            sum(
                r["kind"] == "charge"
                for r in gateway.usage(self.alice, job["task"]["id"])["ledger"]
            ),
            1,
        )

    def test_model_transport_unknown_never_retried(self):
        job, req, cfg = self.model_job()
        calls = []

        def upstream(request):
            calls.append(request)
            raise httpx.ReadTimeout("test")

        gateway = ModelGateway(self.p, cfg, transport=httpx.MockTransport(upstream))
        with self.assertRaises(Fault):
            asyncio.run(gateway.invoke(self.node, job["attempt"]["id"], req))
        self.assertEqual(len(calls), 1)

    def test_delete_content_scrubs_idempotency(self):
        t = self.submit("sensitive", input={"secret": "private-test-marker"})
        job = self.claim(t)
        t = self.report(job)
        artifacts = Artifacts(self.p)
        artifacts.delete_content(
            self.alice, t["id"], C.CAS(expected_version=t["state_version"]), uid()
        )
        self.assertTrue(self.p.task(self.alice, t["id"])["deleted"])
        with self.assertRaises(Fault):
            self.p.events(self.alice, t["id"])
        with self.db.transaction(serialized=False) as tx:
            self.assertNotIn(
                "private-test-marker",
                str(tx.execute("SELECT response FROM v2_idempotency").fetchall()),
            )
        self.assertEqual(artifacts.sweep(), 1)

    def test_demo_runner_two_slots_end_to_end(self):
        self.register_node(slots_limit=2)
        tasks = [self.submit(), self.submit()]
        for t in tasks:
            self.command(t)

        async def run():
            wal = WAL(self.tmp.name + "/runner")
            client = Client(
                "http://control",
                self.token,
                transport=httpx.ASGITransport(app=self.app),
            )
            runner = Runner(client, wal, slots=2)
            try:
                self.assertEqual(
                    await asyncio.gather(runner.tick(0), runner.tick(1)), [True, True]
                )
            finally:
                await client.close()
                wal.close()

        asyncio.run(run())
        for t in tasks:
            self.assertEqual(self.p.task(self.alice, t["id"])["state"], "succeeded")

    def test_crash_never_replays_tool(self):
        job = self.claim(self.submit())
        wal = WAL(self.tmp.name + "/runner")
        wal.save(
            "slot:0",
            job | {"journal_key": "slot:0", "claim_id": uid(), "started": True},
        )

        async def run():
            client = Client(
                "http://control",
                self.token,
                transport=httpx.ASGITransport(app=self.app),
            )
            runner = Runner(
                client, wal, factory=lambda: self.fail("must never create backend")
            )
            try:
                await runner.tick()
            finally:
                await client.close()

        asyncio.run(run())
        wal.close()
        self.assertEqual(
            self.p.task(self.alice, job["task"]["id"])["state"], "needs_review"
        )

    def test_wal_reserve_critical_retention_single_owner(self):
        wal = WAL(self.tmp.name + "/wal")
        wal.LIMIT = 1000
        wal.RESERVE = 200
        try:
            with self.assertRaises(RuntimeError):
                WAL(self.tmp.name + "/wal")
            wal.save("log", {"text": "x" * 770}, critical=False)
            with self.assertRaises(RuntimeError):
                wal.save("log2", {"text": "x" * 100}, critical=False)
            wal.save("stop", {"critical": "x" * 100})
            wal.prune(time.time() + 90000)
            self.assertIsNone(wal.load("log"))
            self.assertIsNotNone(wal.load("stop"))
        finally:
            wal.close()

    def test_restore_invalidates_credentials_and_scrubs_recovered_deleted_content(self):
        from infra_control.v2.maintenance import restore_fence
        from infra_control.db import encode

        task = self.submit(
            "restore-secret", input={"secret": "recovered-sensitive-marker"}
        )
        self.claim(task)
        with self.db.transaction(serialized=False) as tx:
            tx.execute(
                "INSERT INTO v2_deletions(task_id,requested,body) VALUES(?,?,?)",
                (task["id"], self.now[0], encode({"deadline": self.now[0] + 86400})),
            )
        self.now[0] += 5
        restore_fence(self.p)
        with self.assertRaises(Fault):
            self.p.authenticate(self.token)
        self.assertEqual(
            self.http.get("/v2/tasks", headers=self.headers()).status_code, 401
        )
        restored = self.p.task(self.alice, task["id"])
        self.assertEqual(restored["state"], "needs_review")
        self.assertTrue(restored["deleted"])
        with self.db.transaction(serialized=False) as tx:
            self.assertNotIn(
                "recovered-sensitive-marker",
                str(tx.execute("SELECT response FROM v2_idempotency").fetchall()),
            )

    def test_artifact_manifest_complete_and_download_authorization(self):
        storage = Mock()
        storage.upload_url.return_value = "https://objects.test/upload"
        storage.finalize.return_value = "immutable-final-version"
        storage.download_url.return_value = "https://objects.test/download"
        self.p.storage = storage
        artifacts = Artifacts(self.p)
        job = self.claim(self.submit())
        digest = hashlib.sha256(b"result").hexdigest()
        upload = artifacts.create(
            self.node,
            job["attempt"]["id"],
            C.Upload(
                epoch=1,
                manifest={
                    "name": "answer.txt",
                    "media_type": "text/plain",
                    "size": 6,
                    "sha256": digest,
                },
            ),
            uid(),
        )
        artifact_id = upload["upload_session"]
        with self.assertRaises(Fault):
            artifacts.download(self.alice, artifact_id, C.Download(), uid())
        with self.assertRaises(Fault):
            artifacts.complete(
                self.node,
                artifact_id,
                C.Complete(epoch=1, sha256="0" * 64, size=6),
                uid(),
            )
        completed = artifacts.complete(
            self.node, artifact_id, C.Complete(epoch=1, sha256=digest, size=6), uid()
        )
        self.assertEqual(completed["status"], "committed")
        self.assertNotIn("version_id", completed)
        with self.assertRaises(Fault):
            artifacts.download(self.bob, artifact_id, C.Download(), uid())
        self.assertEqual(
            artifacts.download(self.alice, artifact_id, C.Download(), uid())["url"],
            "https://objects.test/download",
        )
        self.assertEqual(self.report(job, result_ref=artifact_id)["state"], "succeeded")

    def test_real_step_requires_certified_isolation(self):
        self.register_node(backend="opensandbox")
        job = self.claim(self.submit())
        req = C.Authorize(
            epoch=1,
            step_id=uid(),
            operation_id=uid(),
            tool="python",
            args_hash="a" * 64,
            policy_digest=self.policy["digest"],
        )
        with self.assertRaises(Fault):
            self.p.authorize(self.node, job["attempt"]["id"], req, uid())
        with self.db.transaction(serialized=False) as tx:
            attempt = get_attempt(tx, job["attempt"]["id"])
            attempt["sandbox_ref"] = "sandbox-real"
            save_attempt(tx, attempt)
        proof = self.p.verifier_keys.sign(
            "isolation",
            {
                "attempt": attempt["id"],
                "task": job["task"]["id"],
                "epoch": 1,
                "sandbox": "sandbox-real",
                "isolated": True,
                "policy": "offline-v2",
                "observed": self.now[0],
                "expires": self.now[0] + 30,
            },
        )
        self.p.verify_isolation(
            self.node, attempt["id"], C.Isolation(epoch=1, proof=proof)
        )
        self.assertIn(
            "capability", self.p.authorize(self.node, attempt["id"], req, uid())
        )

    def test_outbox_unknown_start_reuses_workflow_id(self):
        from infra_control.v2.coordination import dispatch_once

        t = self.submit()
        client = AsyncMock()
        client.start_workflow.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            asyncio.run(dispatch_once(self.db, client, "test"))
        first = client.start_workflow.call_args.kwargs["id"]
        client.start_workflow.side_effect = None
        asyncio.run(dispatch_once(self.db, client, "test"))
        self.assertEqual(first, "task/" + t["id"])
        self.assertEqual(first, client.start_workflow.call_args.kwargs["id"])


class ObjectStoreTests(unittest.TestCase):
    def test_version_pinned_staging_and_final_checksum(self):
        raw = b"hello"
        client = Mock()
        client.head_object.return_value = {
            "VersionId": "staging-1",
            "ETag": "not-a-sha",
        }
        client.copy_object.return_value = {"VersionId": "final-1"}
        client.get_object.side_effect = [
            {"ContentLength": 5, "Body": io.BytesIO(raw)},
            {"ContentLength": 5, "Body": io.BytesIO(raw)},
        ]
        artifact = {
            "staging_key": "staging/id",
            "final_key": "final/id",
            "size": 5,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "media_type": "text/plain",
        }
        self.assertEqual(S3Storage(client, "private").finalize(artifact), "final-1")
        self.assertEqual(
            client.copy_object.call_args.kwargs["CopySource"]["VersionId"], "staging-1"
        )
        self.assertEqual(client.get_object.call_args.kwargs["VersionId"], "final-1")

    def test_wrong_hash_or_missing_version_rejected(self):
        client = Mock()
        client.get_object.return_value = {
            "ContentLength": 3,
            "Body": io.BytesIO(b"bad"),
        }
        storage = S3Storage(client, "private")
        with self.assertRaises(Fault):
            storage.check("object", "version", 3, "0" * 64)
        with self.assertRaises(Fault):
            storage.check("object", None, 3, "0" * 64)


if __name__ == "__main__":
    unittest.main()
