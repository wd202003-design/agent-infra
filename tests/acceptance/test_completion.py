"""D1 completion gates, fault paths and trust boundaries."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import test_v2 as fixture
from contracts import v2 as C
from contracts.wire import convert
from infra_control.service import Fault
from infra_control.v2.budget import route_snapshot
from infra_control.v2.model import ModelGateway
from infra_control.v2.artifacts import Artifacts
from infra_control.v2.maintenance import retention
from infra_control.v2.store import get_attempt, save_attempt
from runner.workspaces import Workspaces, walk_files, summarize
from runner.v2 import Client, Runner
from runner.wal import WAL
import httpx

uid = fixture.uid


class CompletionTests(unittest.TestCase):
    for name in (
        "setUp",
        "tearDown",
        "headers",
        "register_node",
        "submit",
        "command",
        "claim",
        "report",
        "model_job",
    ):
        locals()[name] = getattr(fixture.V2Tests, name)

    def hard_job(self):
        _, _, config = self.model_job()
        config["billing_bound"] = {
            "algorithm": "utf8-byte-upper-bound-v1",
            "model": config["model"],
            "price_version": config["price_version"],
            "evidence_ref": "test-reviewed-provider-contract",
            "approved_by": "test-operator",
            "valid_until": self.now[0] + 3600,
            "per_message_overhead": 64,
            "request_overhead": 64,
            "output_parameter": "max_tokens",
            "output_limit_includes_all_billable_tokens": True,
            "no_other_billable_items": True,
        }
        gateway = ModelGateway(self.p, config)
        self.register_node(backend="opensandbox")
        job = self.claim(
            self.submit(
                agent_ref={"id": "think", "version": "1"}, budget={"mode": "hard"}
            )
        )
        with self.db.transaction(serialized=False) as tx:
            a = get_attempt(tx, job["attempt"]["id"])
            a["isolation_verified"] = True
            save_attempt(tx, a)
        req = C.Model(
            epoch=1,
            step_id=uid(),
            request_id=uid(),
            messages=[{"role": "user", "content": "中文🙂"}],
            budget_ref=job["task"]["id"],
        )
        return job, req, config, gateway

    def test_hard_reservation_pins_route_and_rejects_price_change(self):
        job, req, config, gateway = self.hard_job()
        call, fresh, _ = gateway.reserve(self.node, job["attempt"]["id"], req)
        self.assertTrue(fresh)
        self.assertTrue(call["hard"])
        self.assertEqual(
            call["reserved_cost"],
            (call["input_bound"] * 1000000 + call["output_bound"] * 2000000 + 999999)
            // 1000000,
        )
        config["price_version"] = "changed"
        with self.assertRaises(Fault):
            gateway.reserve(
                self.node,
                job["attempt"]["id"],
                req.model_copy(update={"request_id": uid()}),
            )

    def test_hard_bound_violation_cannot_be_overwritten_by_success_report(self):
        job, req, config, gateway = self.hard_job()
        call, _, _ = gateway.reserve(self.node, job["attempt"]["id"], req)
        gateway.finish(
            call,
            {
                "content": "answer",
                "usage_tokens": 100000,
                "input_tokens": 100000,
                "output_tokens": 0,
                "cost": 100000,
            },
        )
        task = self.p.task(self.alice, job["task"]["id"])
        self.assertTrue(task["billing_violation"])
        self.assertEqual(task["actual_cost"], 100000)
        self.assertEqual(task["state"], "needs_review")
        self.assertEqual(self.report(job)["state"], "needs_review")

    def test_hard_contract_expiry_and_missing_extra_fee_guarantee(self):
        _, _, config, _ = self.hard_job()
        with self.assertRaises(Fault):
            route_snapshot(config, self.now[0] + 3601, True)
        config["billing_bound"]["no_other_billable_items"] = False
        with self.assertRaises(Fault):
            route_snapshot(config, self.now[0], True)

    def sensitive_job(self, data_class, mode="program"):
        self.alice["workspace_refs"] = ["records"]
        self.p.publish(
            self.admin,
            "policy",
            "localdata",
            C.PolicyVersion(
                version="1",
                allowed_pools=["demo"],
                tool_kinds=["python"],
                workspace_refs=["records"],
                data_classes=[data_class],
            ),
            uid(),
        )
        self.p.publish(
            self.admin,
            "agent",
            "localcalc",
            C.AgentVersion(
                agent_id="localcalc",
                version="1",
                description="local",
                mode=mode,
                source='print("private")' if mode == "program" else "",
                instruction="Analyze approved summary" if mode == "agent" else "",
                policy_ref={"id": "localdata", "version": "1"},
            ),
            uid(),
        )
        ticket = self.p.ticket(
            self.admin,
            C.NodePolicy(
                pool="demo",
                owner="alice",
                backend="opensandbox",
                location="edge" if data_class == "edge_summary" else "local",
                workspace_refs=["records"],
            ),
            uid(),
        )
        reg = C.Register(
            ticket_id=ticket["ticket_id"],
            ticket_secret=ticket["ticket_secret"],
            registration_id=uid(),
            protocol="2.0",
            instance_public_info="test",
        )
        node = self.p.register(reg, reg.registration_id)
        self.node = node["node_id"]
        self.token = node["node_token"]
        task = self.submit(
            agent_ref={"id": "localcalc", "version": "1"},
            policy_ref={"id": "localdata", "version": "1"},
            workspace_ref="records",
            data_class=data_class,
        )
        return self.claim(task)

    def test_private_workspace_authorization_and_egress(self):
        job = self.sensitive_job("private")
        self.assertIsNotNone(job)
        with self.assertRaises(Fault):
            self.report(job, result="raw private result")
        with self.assertRaises(Fault):
            self.p.runner_events(
                self.node,
                job["attempt"]["id"],
                C.Events(
                    epoch=1,
                    events=[
                        {
                            "runner_seq": 1,
                            "event_id": uid(),
                            "type": "log",
                            "payload": {"raw": "private"},
                        }
                    ],
                ),
                uid(),
            )
        self.p.storage = Mock()
        with self.assertRaises(Fault):
            Artifacts(self.p).create(
                self.node,
                job["attempt"]["id"],
                C.Upload(
                    epoch=1,
                    manifest={
                        "name": "secret.txt",
                        "media_type": "text/plain",
                        "size": 1,
                        "sha256": "a" * 64,
                    },
                ),
                uid(),
            )
        self.alice["workspace_refs"] = []
        with self.assertRaises(Fault):
            self.submit(
                agent_ref={"id": "localcalc", "version": "1"},
                policy_ref={"id": "localdata", "version": "1"},
                workspace_ref="records",
                data_class="private",
            )

    def test_edge_summary_immutable_and_bound_to_host_proof(self):
        job = self.sensitive_job("edge_summary", "agent")
        aid = job["attempt"]["id"]
        summary = {"value": {"count": 10, "min": 1.0, "max": 3.0, "mean": 2.0}}
        response = self.p.prepared(
            self.node, aid, C.Prepared(epoch=1, summary=summary), uid()
        )
        with self.assertRaises(Fault):
            self.p.prepared(
                self.node,
                aid,
                C.Prepared(
                    epoch=1,
                    summary={
                        "value": {"count": 10, "min": 1.0, "max": 4.0, "mean": 2.0}
                    },
                ),
                uid(),
            )
        with self.db.transaction(serialized=False) as tx:
            a = get_attempt(tx, aid)
            a["sandbox_ref"] = "sandbox"
            save_attempt(tx, a)
        proof = {
            "attempt": aid,
            "task": job["task"]["id"],
            "epoch": 1,
            "sandbox": "sandbox",
            "isolated": True,
            "policy": "offline-v2",
            "observed": self.now[0],
            "expires": self.now[0] + 30,
            "workspace_ref": "records",
            "data_class": "edge_summary",
            "summary_digest": "0" * 64,
        }
        with self.assertRaises(Fault):
            self.p.verify_isolation(
                self.node,
                aid,
                C.Isolation(
                    epoch=1, proof=self.p.verifier_keys.sign("isolation", proof)
                ),
            )
        proof["summary_digest"] = response["summary_digest"]
        self.assertTrue(
            self.p.verify_isolation(
                self.node,
                aid,
                C.Isolation(
                    epoch=1, proof=self.p.verifier_keys.sign("isolation", proof)
                ),
            )["authorized"]
        )

    def test_local_delete_waits_for_node_acknowledgement(self):
        job = self.sensitive_job("private")
        task = self.report(job, result="PRIVATE_RESULT_RETAINED_LOCALLY")
        # The fake real backend has no cleanup evidence; admin independently confirms it.
        task = self.p.review(
            self.admin,
            task["id"],
            C.Review(
                expected_version=task["state_version"],
                outcome="failed",
                stop_confirmed=True,
                cleanup_confirmed=True,
                evidence_refs=["independent-test-evidence"],
            ),
            uid(),
        )
        artifacts = Artifacts(self.p)
        artifacts.delete_content(
            self.alice, task["id"], C.CAS(expected_version=task["state_version"]), uid()
        )
        artifacts.sweep()
        with self.db.transaction(serialized=False) as tx:
            self.assertIsNone(
                tx.execute(
                    "SELECT completed FROM v2_deletions WHERE task_id=?", (task["id"],)
                ).fetchone()[0]
            )
        self.assertEqual(
            self.p.pending_local_deletions(self.node),
            [{"attempt_id": job["attempt"]["id"]}],
        )
        self.p.acknowledge_local_deletion(self.node, job["attempt"]["id"])
        artifacts.sweep()
        with self.db.transaction(serialized=False) as tx:
            self.assertIsNotNone(
                tx.execute(
                    "SELECT completed FROM v2_deletions WHERE task_id=?", (task["id"],)
                ).fetchone()[0]
            )

    def test_secret_capsule_expires_at_original_ticket_deadline(self):
        from infra_control.v2.maintenance import expire_capsules

        ticket = self.p.ticket(
            self.admin, C.NodePolicy(pool="demo", owner="admin"), uid()
        )
        self.now[0] += 590
        reg = C.Register(
            ticket_id=ticket["ticket_id"],
            ticket_secret=ticket["ticket_secret"],
            registration_id=uid(),
            protocol="2.0",
            instance_public_info="test",
        )
        self.p.register(reg, reg.registration_id)
        self.now[0] += 11
        expire_capsules(self.p)
        with self.db.transaction(serialized=False) as tx:
            stored = json.loads(
                tx.execute(
                    "SELECT response FROM v2_idempotency WHERE scope=? AND key=?",
                    ("register:" + ticket["ticket_id"], reg.registration_id),
                ).fetchone()[0]
            )
        self.assertNotIn("capsule", stored)

    def test_settled_usage_expires_after_365_days_but_tombstone_stays(self):
        job, req, cfg = self.model_job()
        gateway = ModelGateway(self.p, cfg)
        call, _, _ = gateway.reserve(self.node, job["attempt"]["id"], req)
        gateway.finish(call, {"content": "done", "usage_tokens": 5, "cost": 5})
        task = self.report(job)
        self.p.review(
            self.admin,
            task["id"],
            C.Review(
                expected_version=task["state_version"],
                outcome="failed",
                stop_confirmed=True,
                cleanup_confirmed=True,
                evidence_refs=["independent-test-evidence"],
            ),
            uid(),
        )
        self.now[0] += 366 * 86400
        retention(self.p)
        self.assertEqual(gateway.usage(self.alice, task["id"])["requests"], [])
        self.assertTrue(self.p.task(self.alice, task["id"])["tombstone"])
        self.db.migrate()  # new ALTER migration is repeat-safe

    def test_http_idempotency_conflict_and_rfc3339(self):
        task = self.submit()
        headers = self.headers()
        path = "/v2/tasks/" + task["id"] + "/cancel"
        response = self.http.post(
            path,
            headers=headers,
            json={"expected_version": task["state_version"], "reason": "first"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["data"]["deadline"].endswith("Z"))
        response = self.http.post(
            path,
            headers=headers,
            json={"expected_version": task["state_version"], "reason": "different"},
        )
        self.assertEqual(response.status_code, 409)
        value = {
            "input": {"deadline": 123},
            "deadline": 123,
            "state_version": 1,
            "owner": "alice",
        }
        self.assertEqual(convert(value)["input"]["deadline"], 123)
        self.assertEqual(convert(convert(value), True), value)

    def test_event_id_cannot_be_reused_at_different_sequence(self):
        job = self.claim(self.submit())
        event = {"runner_seq": 1, "event_id": uid(), "type": "log", "payload": {}}
        self.p.runner_events(
            self.node, job["attempt"]["id"], C.Events(epoch=1, events=[event]), uid()
        )
        with self.assertRaises(Fault):
            self.p.runner_events(
                self.node,
                job["attempt"]["id"],
                C.Events(epoch=1, events=[event | {"runner_seq": 2}]),
                uid(),
            )

    def test_retention_90_day_tombstone_and_capsule_destruction(self):
        task = self.report(self.claim(self.submit(input={"secret": "content-marker"})))
        self.now[0] += 91 * 86400
        retention(self.p)
        item = self.p.task(self.alice, task["id"])
        self.assertTrue(item["tombstone"])
        self.assertNotIn("agent", item)
        self.assertNotIn("input", item)
        self.assertEqual(Artifacts(self.p).sweep(), 1)
        with self.db.transaction(serialized=False) as tx:
            self.assertEqual(
                tx.execute("SELECT COUNT(*) FROM v2_credentials").fetchone()[0], 0
            )

    def test_retention_preserves_unknown_cost_and_active_resources(self):
        job, req, cfg = self.model_job()
        gateway = ModelGateway(self.p, cfg)
        call, _, _ = gateway.reserve(self.node, job["attempt"]["id"], req)
        gateway.finish(call)
        self.now[0] += 366 * 86400
        retention(self.p)
        self.assertEqual(
            gateway.usage(self.alice, job["task"]["id"])["requests"][0]["status"],
            "unknown",
        )
        self.assertEqual(
            self.p.task(self.alice, job["task"]["id"])["state"], "needs_review"
        )

    def test_private_runner_keeps_stdout_off_control_plane(self):
        job = self.sensitive_job("private")
        self.report(job, unknown_effects=True, result="")  # separate new task below
        task = self.submit(
            agent_ref={"id": "localcalc", "version": "1"},
            policy_ref={"id": "localdata", "version": "1"},
            workspace_ref="records",
            data_class="private",
        )
        # Release fixture slot solely for this controlled simulated infrastructure test.
        with self.db.transaction(serialized=False) as tx:
            a = get_attempt(tx, job["attempt"]["id"])
            a["active"] = False
            save_attempt(tx, a)
        self.command(task)
        root = Path(self.tmp.name) / "raw"
        root.mkdir()
        (root / "private.txt").write_text("not-for-central")
        ws = Workspaces(
            {"records": {"root": str(root), "owner": "alice", "data_class": "private"}},
            str(Path(self.tmp.name) / "snapshots"),
        )

        class Backend:
            id = "sandbox"

            async def create(self, *args, **kwargs):
                self.workspace = kwargs["workspace"]

            async def execute(self, args):
                return {"exit_code": 0, "stdout": "not-for-central", "stderr": ""}

            async def destroy(self):
                pass

        async def verify(kind, current):
            value = {
                "attempt": current["attempt"]["id"],
                "task": current["task"]["id"],
                "epoch": 1,
                "sandbox": "sandbox",
                "observed": self.now[0],
                "expires": self.now[0] + 30,
                "workspace_ref": "records",
                "data_class": "private",
                "policy": "offline-v2",
                "isolated": True,
                "management_absent": True,
                "host_absent": True,
            }
            return self.p.verifier_keys.sign(kind, value)

        async def run():
            client = Client(
                "http://control",
                self.token,
                transport=httpx.ASGITransport(app=self.app),
            )
            wal = WAL(self.tmp.name + "/private-runner")
            runner = Runner(
                client,
                wal,
                backend="opensandbox",
                factory=Backend,
                verifier=verify,
                workspaces=ws,
            )
            try:
                await runner.tick()
            finally:
                await client.close()
                wal.close()

        asyncio.run(run())
        task = self.p.task(self.alice, task["id"])
        self.assertEqual(task["state"], "succeeded")
        self.assertNotIn("not-for-central", json.dumps(task))
        self.assertIn(
            "not-for-central",
            next((Path(self.tmp.name) / "private-results").glob("*.json")).read_text(),
        )


class WorkspaceTests(unittest.TestCase):
    def test_snapshot_refuses_symlinks_hardlinks_and_fifo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            (root / "file").write_text("secret")
            os.symlink("/etc/passwd", root / "link")
            with self.assertRaises((OSError, ValueError)):
                list(walk_files(root))
            (root / "link").unlink()
            os.link(root / "file", root / "hard")
            with self.assertRaises(ValueError):
                list(walk_files(root))
            (root / "hard").unlink()
            os.mkfifo(root / "fifo")
            with self.assertRaises(ValueError):
                list(walk_files(root))

    def test_fixed_summary_excludes_free_text_and_requires_cohort(self):
        spec = {
            "algorithm": "numeric-jsonl-v1",
            "columns": {"value": {"min": 0, "max": 100}},
            "min_rows": 10,
        }
        raw = b"\n".join(
            json.dumps({"value": i, "secret": "raw-secret"}).encode() for i in range(10)
        )
        result = summarize([("rows.jsonl", raw)], spec)
        self.assertEqual(result["value"]["mean"], 4.5)
        self.assertNotIn("raw-secret", json.dumps(result))
        with self.assertRaises(ValueError):
            summarize([("rows.jsonl", b'{"value":1}')], spec)

    def test_edge_snapshot_contains_only_summary_and_ro_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "raw"
            root.mkdir()
            (root / "data.jsonl").write_text(
                "\n".join(json.dumps({"x": n, "secret": "RAW"}) for n in range(10))
            )
            ws = Workspaces(
                {
                    "records": {
                        "root": str(root),
                        "owner": "alice",
                        "data_class": "edge_summary",
                        "summary": {
                            "algorithm": "numeric-jsonl-v1",
                            "columns": {"x": {"min": 0, "max": 10}},
                            "min_rows": 10,
                        },
                    }
                },
                str(Path(tmp) / "snapshots"),
            )
            attempt = uid()
            snapshot = ws.prepare(
                {
                    "workspace_ref": "records",
                    "owner": "alice",
                    "data_class": "edge_summary",
                },
                attempt,
            )
            files = list(Path(snapshot["path"]).iterdir())
            self.assertEqual([f.name for f in files], ["summary.json"])
            self.assertNotIn("RAW", files[0].read_text())
            self.assertEqual(files[0].stat().st_mode & 0o222, 0)
            ws.remove(attempt)
            self.assertFalse(Path(snapshot["path"]).exists())
