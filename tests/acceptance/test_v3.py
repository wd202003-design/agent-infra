"""Image scheduler failure/concurrency tests, SQLite locally and PostgreSQL in CI."""

from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock
from PIL import Image
from pydantic import ValidationError
from fastapi.testclient import TestClient
from contracts import v3 as C
from infra_control.db import Database
from infra_control.service import Fault
from infra_control.v2.security import Keys, sha
from infra_control.v3.service import Images
from infra_control.v3.api import create_api
from infra_control.v3.artifacts import ImageStorage, Artifacts


def uid():
    return str(uuid.uuid4())


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dsn = os.getenv("TEST_DATABASE_URL")
        self.schema = None
        if self.dsn:
            import psycopg

            self.schema = "test_" + uuid.uuid4().hex
            with psycopg.connect(self.dsn) as con:
                con.execute("CREATE SCHEMA " + self.schema)
            self.db = Database(self.dsn, schema=self.schema)
        else:
            self.db = Database("sqlite:" + self.tmp.name + "/db", test_sqlite=True)
        self.db.migrate()
        self.now = [time.time()]
        self.p = Images(self.db, Keys(b"t" * 32), clock=lambda: self.now[0])
        self.users = [
            dict(
                id=n,
                role="admin" if n == "admin" else "user",
                pools=["demo"],
                token_sha256=sha(n + "x" * 32),
                issued_at=self.now[0] - 1,
                expires_at=self.now[0] + 10000,
            )
            for n in ["admin", "alice", "bob"]
        ]
        self.admin, self.user, self.other = self.users
        self.w = dict(
            id="worker",
            slots=8,
            pools=["demo"],
            token_sha256=sha("worker" + "x" * 32),
            issued_at=self.now[0] - 1,
            expires_at=self.now[0] + 10000,
        )
        self.api = create_api(self.p, self.users, [self.w])
        self.http = TestClient(self.api)
        for purpose in ["agent", "image"]:
            self.p.publish(
                self.admin,
                purpose,
                C.Binding(
                    version="1",
                    purpose=purpose,
                    provider="test",
                    api="openai-completions" if purpose == "agent" else "openai-images",
                    base_url="https://provider.invalid/v1",
                    model="test",
                    secret="fake-test-secret-12345",
                    allowed_pools=["demo"],
                    quota_pool_id=purpose,
                    reserve_micro=100,
                    max_inflight=2,
                    price_version="1",
                    input_micro_per_million=100,
                    output_micro_per_million=200,
                ),
                uid(),
            )

    def tearDown(self):
        self.http.close()
        if self.schema:
            import psycopg

            with psycopg.connect(self.dsn) as con:
                con.execute("DROP SCHEMA " + self.schema + " CASCADE")
        self.tmp.cleanup()

    def root(self, n=3, budget=200, parallel=2, deps=False):
        r = self.p.submit(
            self.user,
            C.Submit(
                instruction="blue landscape",
                pool="demo",
                agent_binding={"id": "agent", "version": "1"},
                image_binding={"id": "image", "version": "1"},
                cost_limit_micro=n * budget,
                policy=C.Policy(max_parallel_children=parallel),
            ),
            uid(),
        )
        children = [
            C.Child(
                child_key="child" + str(i),
                instruction="image " + str(i),
                context_version=1,
                budget_limit_micro=budget,
                depends_on=["child0"] if deps and i else [],
            )
            for i in range(n)
        ]
        self.p.plan(
            self.user, r["id"], C.Plan(expected_version=1, children=children), uid()
        )
        return r

    def claim(self):
        id = uid()
        return self.p.claim(self.w, C.Claim(claim_id=id, available_slots=8), id)

    def fence(self, j):
        return dict(task_id=j["task"]["id"], epoch=j["attempt"]["epoch"])

    def op(self, j, kind="agent"):
        return self.p.reserve(
            self.w,
            j["attempt"]["id"],
            C.Operation(
                **self.fence(j), operation_id=uid(), tool_call_id=uid(), kind=kind
            ),
            uid(),
        )

    def dispatch(self, j, o):
        return self.p.dispatch(self.w, o["id"], C.Fence(**self.fence(j)), uid())

    def report(self, j, o, state="succeeded", cost=50):
        return self.p.report(
            self.w,
            o["id"],
            C.OperationReport(**self.fence(j), state=state, actual_cost_micro=cost),
            uid(),
        )

    def test_parallel_claims_never_exceed_root_limit(self):
        self.root(8)
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(lambda _: self.claim(), range(8)))
        self.assertEqual(sum(j["task"] is not None for j in jobs), 2)
        self.assertEqual(len({j["attempt"]["slot"] for j in jobs if j["task"]}), 2)

    def test_worker_slots_are_global_across_roots(self):
        self.w["slots"] = 1
        self.root()
        self.root()
        self.assertIsNotNone(self.claim()["task"])
        self.assertIsNone(self.claim()["task"])

    def test_budget_atomic_and_unknown_holds_quota(self):
        r = self.root(3, budget=150, parallel=3)
        jobs = [self.claim() for _ in range(3)]
        with ThreadPoolExecutor(max_workers=3) as pool:

            def reserve(j):
                try:
                    return self.op(j)
                except Fault as e:
                    return e.code

            results = list(pool.map(reserve, jobs))
        self.assertEqual(results.count("QUOTA_EXCEEDED"), 1)
        for j, o in zip(jobs, results):
            if isinstance(o, dict):
                self.dispatch(j, o)
                with self.assertRaises(Fault) as e:
                    self.op(j, "generate")
                self.assertEqual(e.exception.code, "BUDGET_EXCEEDED")
        for j, o in zip(jobs, results):
            if isinstance(o, dict):
                self.report(j, o, "unknown", None)
        view = self.p.inspect(self.user, r["id"])
        self.assertEqual(view["task"]["reserved_micro"], 200)
        self.assertEqual(view["task"]["state"], "needs_review")

    def test_dispatch_one_shot_and_expiry_fences(self):
        r = self.root()
        j = self.claim()
        o = self.op(j)
        self.dispatch(j, o)
        with self.assertRaises(Fault):
            self.dispatch(j, o)
        self.now[0] += 31
        v = self.p.inspect(self.user, r["id"])
        self.assertEqual(v["task"]["reserved_micro"], 100)
        with self.assertRaises(Fault):
            self.report(j, o)
        self.p.review(
            self.admin,
            o["id"],
            C.Review(
                actual_cost_micro=65,
                outcome="failed",
                provider_stopped=True,
                evidence="Provider confirms stopped",
            ),
            uid(),
        )
        self.assertEqual(self.p.inspect(self.user, r["id"])["task"]["actual_micro"], 65)

    def test_context_is_pinned_and_cas(self):
        r = self.root()
        self.p.context(
            self.user,
            r["id"],
            C.Context(expected_version=2, brief="red updated"),
            uid(),
        )
        j = self.claim()
        self.assertEqual(j["context"]["brief"], "blue landscape")
        self.assertEqual(j["context"]["version"], 1)
        with self.assertRaises(Fault):
            self.p.context(
                self.user, r["id"], C.Context(expected_version=2, brief="stale"), uid()
            )
        with self.assertRaises(Fault):
            self.p.inspect(self.other, r["id"])

    def test_complete_retry_and_cancel_dependents(self):
        r = self.root(deps=True)
        j = self.claim()
        f = C.Complete(**self.fence(j), outcome="failed")
        key = uid()
        a = self.p.complete(self.w, j["attempt"]["id"], f, key)
        b = self.p.complete(self.w, j["attempt"]["id"], f, key)
        self.assertEqual(a, b)
        self.assertEqual(self.p.inspect(self.user, r["id"])["task"]["state"], "failed")

    def test_cancel_requires_stopping_before_success(self):
        r = self.root()
        j = self.claim()
        view = self.p.inspect(self.user, r["id"])
        self.p.cancel(
            self.user,
            r["id"],
            C.CAS(expected_version=view["task"]["state_version"]),
            uid(),
        )
        reply = self.p.renew(
            self.w, j["attempt"]["id"], C.Fence(**self.fence(j)), uid()
        )
        self.assertTrue(reply["stop_requested"])
        with self.assertRaises(Fault):
            self.op(j)

    def test_revocation_stops_lease_authorization(self):
        self.root()
        j = self.claim()
        self.p.revoke_binding("image:1")
        self.assertFalse(
            self.p.renew(self.w, j["attempt"]["id"], C.Fence(**self.fence(j)), uid())[
                "authorized"
            ]
        )
        with self.assertRaises(Fault):
            self.op(j)

    def test_abandon_reserved_refunds_and_can_close_review(self):
        r = self.root(1)
        j = self.claim()
        self.op(j)
        self.p.abandon(self.w, j["attempt"]["id"], C.Fence(**self.fence(j)))
        self.assertEqual(
            self.p.inspect(self.user, r["id"])["task"]["reserved_micro"], 0
        )
        self.p.close_review(
            self.admin,
            j["task"]["id"],
            C.CloseReview(
                evidence="Worker terminated before dispatch", provider_stopped=True
            ),
            uid(),
        )
        self.assertEqual(self.p.inspect(self.user, r["id"])["task"]["state"], "failed")

    def test_checkpoint_cas(self):
        self.root()
        j = self.claim()
        p = C.Checkpoint(**self.fence(j), expected_revision=0, phase="planning")
        self.assertEqual(
            self.p.checkpoint(self.w, j["attempt"]["id"], p, uid())["revision"], 1
        )
        with self.assertRaises(Fault):
            self.p.checkpoint(self.w, j["attempt"]["id"], p, uid())

    def test_event_order_is_root_monotonic(self):
        r = self.root()
        j = self.claim()
        self.p.events(
            self.w,
            j["attempt"]["id"],
            C.Events(
                **self.fence(j),
                events=[
                    C.Event(event_id=uid(), seq=i, type="agent.phase_changed")
                    for i in range(1, 13)
                ],
            ),
            uid(),
        )
        resp = self.http.get(
            "/image-tasks/" + r["id"] + "/events?after=9",
            headers={"Authorization": "Bearer alice" + "x" * 32},
        )
        self.assertEqual([x["sequence"] for x in resp.json()["data"]], [10, 11, 12])

    def test_secrets_encrypted_and_only_issued_to_reserved_attempt(self):
        self.root()
        j = self.claim()
        o = self.op(j)
        p = C.Credential(**self.fence(j), operation_id=o["id"])
        self.assertEqual(
            self.p.credentials(self.w, j["attempt"]["id"], p)["secret"],
            "fake-test-secret-12345",
        )
        with self.p.tx() as tx:
            self.assertNotIn(
                "fake-test-secret-12345", json.dumps(self.p.rows(tx, "binding"))
            )
            self.assertNotIn(
                "fake-test-secret-12345",
                str(tx.execute("SELECT response FROM v3_idempotency").fetchall()),
            )
        self.dispatch(j, o)
        with self.assertRaises(Fault):
            self.p.credentials(self.w, j["attempt"]["id"], p)
        resp = self.http.post(
            "/model-bindings/x",
            json={"secret": "fake-test-secret-12345"},
            headers={
                "Authorization": "Bearer admin" + "x" * 32,
                "Idempotency-Key": uid(),
            },
        )
        self.assertEqual(resp.status_code, 422)
        self.assertNotIn("fake-test-secret", resp.text)

    def test_dag_cycle_and_missing_dependency_rejected(self):
        for deps in [["a"], ["missing"]]:
            with self.assertRaises(ValidationError):
                C.Plan(
                    expected_version=1,
                    children=[
                        C.Child(
                            child_key="a",
                            depends_on=deps,
                            instruction="x",
                            context_version=1,
                            budget_limit_micro=100,
                        )
                    ],
                )

    def test_overrun_records_truth_and_revokes_model(self):
        r = self.root()
        j = self.claim()
        o = self.op(j)
        self.dispatch(j, o)
        self.report(j, o, cost=150)
        v = self.p.inspect(self.user, r["id"])
        self.assertEqual(v["task"]["actual_micro"], 150)
        self.assertTrue(v["task"]["billing_violation"])
        self.assertNotIn("agent:1", [b["id"] for b in self.p.list_bindings(self.user)])

    def test_full_artifact_commit_releases_dependency_and_root_result(self):
        r = self.root(2, deps=True)
        j = self.claim()
        o = self.op(j)
        self.dispatch(j, o)
        self.report(j, o)
        image = self.op(j, "generate")
        self.dispatch(j, image)
        self.report(j, image, cost=100)
        self.p.storage = Mock()
        self.p.storage.upload.return_value = "https://storage.invalid/upload"
        self.p.storage.commit.return_value = {
            "version_id": "final-v1",
            "width": 2,
            "height": 2,
        }
        artifacts = Artifacts(self.p)
        upload = artifacts.create(
            self.w,
            C.Upload(
                **self.fence(j),
                operation_id=image["id"],
                size_bytes=5,
                sha256="a" * 64,
                media_type="image/png",
            ),
            uid(),
            j["attempt"]["id"],
        )
        artifacts.commit(
            self.w,
            upload["artifact_id"],
            C.Commit(**self.fence(j), version_id="source-v1"),
            j["attempt"]["id"],
        )
        self.p.complete(
            self.w,
            j["attempt"]["id"],
            C.Complete(
                **self.fence(j),
                outcome="succeeded",
                artifact_ids=[upload["artifact_id"]],
            ),
            uid(),
        )
        next_job = self.claim()
        self.assertEqual(next_job["task"]["child_key"], "child1")
        self.assertEqual(
            next_job["dependency_results"][0]["artifact_ids"], [upload["artifact_id"]]
        )
        with self.assertRaises(Fault):
            artifacts.download(self.other, upload["artifact_id"])
        with self.assertRaises(Fault):
            artifacts.commit(
                self.user, upload["artifact_id"], C.InputCommit(version_id="another")
            )
        view = self.p.inspect(self.user, r["id"])
        self.assertEqual(view["task"]["actual_micro"], 150)

    def test_storage_validates_image_and_pins_copy_and_download_versions(self):
        content = io.BytesIO()
        Image.new("RGB", (2, 3), "blue").save(content, format="PNG")
        data = content.getvalue()
        client = Mock()
        client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        client.get_object.side_effect = lambda **kw: {
            "ContentLength": len(data),
            "Body": io.BytesIO(data),
        }
        client.copy_object.return_value = {"VersionId": "final-version"}
        store = ImageStorage(client, "private")
        a = {
            "id": uid(),
            "staging_key": "stage",
            "final_key": "final",
            "size_bytes": len(data),
            "sha256": "pending",
            "media_type": "image/png",
        }
        # security.sha accepts bytes or strings depending on caller; use canonical hashlib here.
        import hashlib

        a["sha256"] = hashlib.sha256(data).hexdigest()
        out = store.commit(a, "input-version")
        self.assertEqual((out["width"], out["height"]), (2, 3))
        self.assertEqual(
            client.copy_object.call_args.kwargs["CopySource"]["VersionId"],
            "input-version",
        )
        store.download(a | out)
        self.assertEqual(
            client.generate_presigned_url.call_args.kwargs["Params"]["VersionId"],
            "final-version",
        )
        with self.assertRaises(Fault):
            store.commit(a | {"sha256": "0" * 64}, "input-version")
        with self.assertRaises(Fault):
            store.commit(a | {"media_type": "image/jpeg"}, "input-version")


if __name__ == "__main__":
    unittest.main()
