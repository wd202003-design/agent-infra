"""Same suite runs against SQLite test adapter or isolated real PostgreSQL schemas."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from fastapi.testclient import TestClient
import httpx

from contracts.models import Agent, AttemptRequest, Claim, ModelRequest, NodePatch, NodeSpec, Report, Review, Submit, UsageReview
from infra_control.app import create_app
from infra_control.db import Database
from infra_control.service import Fault, Service, digest
from runner.client import Client
from runner.journal import Journal
from runner.worker import Runner, identity, report


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dsn = os.environ.get("TEST_DATABASE_URL")
        self.schema = None
        if self.dsn:
            import psycopg
            self.schema = "test_" + uuid.uuid4().hex
            with psycopg.connect(self.dsn) as conn:
                conn.execute("CREATE SCHEMA " + self.schema)
            self.db = Database(self.dsn, schema=self.schema)
        else:
            self.db = Database("sqlite:" + self.temp.name + "/test.db", test_sqlite=True)
        self.db.migrate()
        self.now = [time.time()]
        self.service = Service(self.db, clock=lambda: self.now[0])
        self.users = [{"id": role, "role": "admin" if role == "admin" else "user", "pools": ["demo"],
                       "token_sha256": digest(role + "x" * 32)} for role in ["admin", "alice", "bob"]]
        self.admin, self.alice, self.bob = self.users
        self.app = create_app(self.db, self.users, service=self.service)
        self.http = TestClient(self.app)
        self.service.add_agent(self.admin, Agent(agent_id="example", version="1", description="fixture", source="print(20)"))
        self.node = self.service.add_node(self.admin, NodeSpec(name="node", pool="demo"))
        self.node_id = self.node["node"]["id"]

    def tearDown(self):
        self.http.close()
        if self.schema:
            import psycopg
            with psycopg.connect(self.dsn) as conn:
                conn.execute("DROP SCHEMA " + self.schema + " CASCADE")
        self.temp.cleanup()

    def headers(self, role="alice"):
        return {"Authorization": "Bearer " + role + "x" * 32}

    def submit(self, key="k", **updates):
        return self.service.submit(self.alice, Submit(**({"agent_id":"example", "version":"1", "pool":"demo"} | updates)), key)

    def claim(self, claim_id=None):
        return self.service.claim(self.node_id, Claim(claim_id=claim_id or str(uuid.uuid4())))

    def done(self, task, **updates):
        return Report(**(report(task, "succeeded", "simulated test cleanup", result="20", stopped=True, cleanup=True) | updates))

    def model_task(self):
        self.service.add_agent(self.admin, Agent(agent_id="model", version="1", description="model", mode="agent", instruction="test"))
        self.node = self.service.add_node(self.admin, NodeSpec(name="real-candidate", pool="demo", backend="opensandbox"))
        self.node_id = self.node["node"]["id"]
        self.submit(agent_id="model")
        task = self.claim()
        req = ModelRequest(**identity(task), request_id=str(uuid.uuid4()), messages=[{"role":"user", "content":"calculate"}])
        return task, req

    def test_auth_and_schema_fail_closed(self):
        self.assertEqual(self.http.get("/v1/tasks").status_code, 401)
        self.assertEqual(self.http.post("/v1/nodes", headers=self.headers(), json={"name":"x", "pool":"demo"}).status_code,403)
        self.assertEqual(self.http.post("/v1/tasks", headers=self.headers(), json={"unknown":"secret"}).status_code,422)
        self.assertNotIn("secret", self.http.post("/v1/tasks", headers=self.headers(), json={"unknown":"secret"}).text)

    def test_cross_user_access_is_hidden(self):
        task = self.submit()
        for suffix in ["", "/events", "/result"]:
            self.assertEqual(self.http.get("/v1/tasks/"+task["id"]+suffix, headers=self.headers("bob")).status_code,404)
        self.assertEqual(self.http.post("/v1/tasks/"+task["id"]+"/cancel", headers=self.headers("bob")).status_code,404)
        self.assertEqual(self.http.get("/v1/tasks", headers=self.headers("bob")).json(),[])

    def test_pool_authorization(self):
        with self.assertRaises(Fault) as exc:
            self.submit(pool="other")
        self.assertEqual(exc.exception.status,403)

    def test_agent_version_is_immutable(self):
        with self.assertRaises(Fault):
            self.service.add_agent(self.admin, Agent(agent_id="example", version="1", description="changed", source="print(1)"))

    def test_concurrent_idempotency(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            tasks = list(pool.map(lambda _: self.submit(), range(8)))
        self.assertEqual(len({t["id"] for t in tasks}),1)
        with self.assertRaises(Fault):
            self.submit(prompt="different")

    def test_claim_replay_and_single_slot(self):
        self.submit()
        self.submit("second")
        claim_id = str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=5) as pool:
            claims=list(pool.map(lambda _: self.claim(claim_id),range(5)))
        self.assertEqual(len({t["attempt_id"] for t in claims}),1)
        self.assertIsNone(self.claim())
        self.service.report(self.node_id,claims[0]["id"],self.done(claims[0]))
        self.assertEqual(self.claim(claim_id)["state"],"succeeded")
        self.assertIsNotNone(self.claim())

    def test_lease_expiry_does_not_reassign(self):
        self.submit()
        task=self.claim()
        self.now[0]+=31
        self.assertIsNone(self.claim())
        self.assertEqual(self.service.task(self.alice,task["id"])["state"],"needs_review")
        self.assertFalse(self.service.renew(self.node_id,task["id"],AttemptRequest(**identity(task)))["authorized"])

    def test_stale_epoch_rejected(self):
        self.submit();task=self.claim()
        with self.assertRaises(Fault):
            self.service.report(self.node_id,task["id"],self.done(task,epoch=task["epoch"]+1))

    def test_cancel_before_complete_wins(self):
        self.submit();task=self.claim()
        self.service.cancel(self.alice,task["id"])
        self.assertEqual(self.service.report(self.node_id,task["id"],self.done(task))["state"],"cancelled")

    def test_complete_before_cancel_is_immutable(self):
        self.submit();task=self.claim()
        self.service.report(self.node_id,task["id"],self.done(task))
        self.assertEqual(self.service.cancel(self.alice,task["id"])["state"],"succeeded")

    def test_queued_cancel_and_expired_queue(self):
        task=self.submit()
        self.assertEqual(self.service.cancel(self.alice,task["id"])["state"],"cancelled")
        later=self.submit("later")
        self.now[0]+=601
        self.assertEqual(self.service.task(self.alice,later["id"])["state"],"failed")

    def test_unknown_cleanup_requires_review(self):
        self.submit();task=self.claim()
        value=self.service.report(self.node_id,task["id"],self.done(task,cleanup_confirmed=False))
        self.assertEqual(value["state"],"needs_review")
        reviewed=self.service.review(self.admin,task["id"],Review(outcome="succeeded",stopped=True,cleanup_confirmed=True,evidence="host resources verified by administrator"))
        self.assertEqual(reviewed["state"],"succeeded")

    def test_report_dedupe_conflict_and_event_order(self):
        self.submit();task=self.claim();done=self.done(task)
        first=self.service.report(self.node_id,task["id"],done)
        self.assertEqual(first,self.service.report(self.node_id,task["id"],done))
        with self.assertRaises(Fault):
            self.service.report(self.node_id,task["id"],self.done(task,result="other"))
        events=self.service.events(self.alice,task["id"],1)
        self.assertEqual([e["sequence"] for e in events],[2,3])

    def test_drain_and_revoke(self):
        self.submit()
        self.service.patch_node(self.admin,self.node_id,NodePatch(draining=True))
        self.assertIsNone(self.claim())
        self.service.patch_node(self.admin,self.node_id,NodePatch(revoked=True))
        with self.assertRaises(Fault):
            self.service.authenticate_node(self.node["token"])

    def test_unknown_model_call_no_replay_or_release(self):
        task,request=self.model_task()
        call,fresh=self.service.reserve_model(self.node_id,task["id"],request)
        self.assertTrue(fresh)
        with self.assertRaises(Fault):
            self.service.reserve_model(self.node_id,task["id"],request)
        self.service.finish_model(call["id"])
        current=self.service.task(self.alice,task["id"])
        self.assertEqual(current["state"],"needs_review")
        self.assertGreater(current["model_reserved"],0)

    def test_budget_and_global_quota(self):
        task,request=self.model_task()
        self.service.quota["parallel"]=1
        self.service.reserve_model(self.node_id,task["id"],request)
        second=request.model_copy(update={"request_id":str(uuid.uuid4())})
        with self.assertRaises(Fault) as exc:
            self.service.reserve_model(self.node_id,task["id"],second)
        self.assertEqual(exc.exception.code,"model_quota_exhausted")

    def test_model_usage_settlement_and_cache(self):
        task,request=self.model_task()
        call,_=self.service.reserve_model(self.node_id,task["id"],request)
        self.service.finish_model(call["id"],{"content":"final"},42)
        cached,fresh=self.service.reserve_model(self.node_id,task["id"],request)
        self.assertFalse(fresh)
        self.assertEqual(cached["response"]["content"],"final")
        current=self.service.task(self.alice,task["id"])
        self.assertEqual(current["model_reserved"],0)
        self.assertEqual(current["model_actual"],42)

    def test_gateway_request_is_bounded_and_key_stays_control_side(self):
        task,request=self.model_task()
        calls=[]
        def gateway(req):
            calls.append(req)
            return httpx.Response(200,json={"choices":[{"message":{"content":'{"action":"final","content":"20"}'}}],"usage":{"total_tokens":50}})
        app=create_app(self.db,self.users,service=self.service,gateway={"url":"https://gateway.test/v1","key":"upstream-secret","model":"test"},transport=httpx.MockTransport(gateway))
        with TestClient(app) as client:
            response=client.post("/v1/runner/tasks/"+task["id"]+"/model",json=request.model_dump(),headers={"Authorization":"Bearer "+self.node["token"]})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(len(calls),1)
        self.assertNotIn("upstream-secret",response.text)

    def test_request_body_limit(self):
        response=self.http.post("/v1/tasks",headers=self.headers(),content=b"x"*131073)
        self.assertEqual(response.status_code,413)

    def test_sqlite_requires_explicit_test_mode(self):
        with self.assertRaises(ValueError):
            Database("sqlite:/tmp/forbidden")

    def test_demo_runner_end_to_end(self):
        task=self.submit()
        async def run():
            client=Client("http://127.0.0.1",self.node["token"],transport=httpx.ASGITransport(app=self.app))
            journal=Journal(self.temp.name+"/runner")
            try:
                self.assertTrue(await Runner(client,journal).tick())
                self.assertIsNone(journal.load())
            finally:
                journal.close();await client.close()
        asyncio.run(run())
        item=self.service.task(self.alice,task["id"])
        self.assertEqual(item["state"],"succeeded")
        self.assertIn("SIMULATED",item["result"])

    def test_runner_crash_journal_does_not_reexecute(self):
        self.submit();task=self.claim()
        async def run():
            journal=Journal(self.temp.name+"/runner")
            journal.save({"claim_id":str(uuid.uuid4()),"task":task,"started":True,"report":None})
            client=Client("http://127.0.0.1",self.node["token"],transport=httpx.ASGITransport(app=self.app))
            def forbidden():
                self.fail("must not recreate or replay sandbox")
            try:
                await Runner(client,journal,factory=forbidden).tick()
            finally:
                journal.close();await client.close()
        asyncio.run(run())
        self.assertEqual(self.service.task(self.alice,task["id"])["state"],"needs_review")

    def test_journal_exclusive_process_ownership(self):
        first=Journal(self.temp.name+"/runner")
        try:
            with self.assertRaises(RuntimeError):
                Journal(self.temp.name+"/runner")
        finally:
            first.close()

    def test_cube_is_not_exposed_as_formal_backend(self):
        with self.assertRaises(ValueError):
            Runner(None,None,backend="cubesandbox")

    def test_revocation_cannot_be_undone_or_reset_by_partial_patch(self):
        self.service.patch_node(self.admin,self.node_id,NodePatch(revoked=True))
        patched=self.service.patch_node(self.admin,self.node_id,NodePatch(draining=True))
        self.assertTrue(patched["revoked"])
        with self.assertRaises(Fault):
            self.service.patch_node(self.admin,self.node_id,NodePatch(revoked=False))

    def test_usage_reconciliation_does_not_resume_execution(self):
        task,request=self.model_task()
        call,_=self.service.reserve_model(self.node_id,task["id"],request)
        review=UsageReview(actual_tokens=12,evidence="upstream usage record independently verified")
        with self.assertRaises(Fault):
            self.service.reconcile_usage(self.admin,task["id"],request.request_id,review)
        self.service.finish_model(call["id"])
        self.service.reconcile_usage(self.admin,task["id"],request.request_id,review)
        value=self.service.task(self.alice,task["id"])
        self.assertEqual(value["model_reserved"],0)
        self.assertEqual(value["model_actual"],12)
        self.assertEqual(value["state"],"needs_review")

    def test_runner_model_tool_loop_with_simulated_infrastructure(self):
        task,_=self.model_task()
        calls=[]
        def gateway(request):
            calls.append(request)
            action={"action":"python","content":"print(20)"} if len(calls)==1 else {"action":"final","content":"20"}
            return httpx.Response(200,json={"choices":[{"message":{"content":json.dumps(action)}}],"usage":{"total_tokens":50}})
        app=create_app(self.db,self.users,service=self.service,gateway={"url":"https://gateway.test/v1","key":"private-key","model":"test"},transport=httpx.MockTransport(gateway))
        executed=[]
        class Sandbox:
            id=None
            async def create(self,owner): self.id=owner
            async def execute(self,payload):
                executed.append(payload)
                return {"exit_code":0,"stdout":"20"}
            async def destroy(self): pass
        async def run():
            client=Client("http://127.0.0.1",self.node["token"],transport=httpx.ASGITransport(app=app))
            journal=Journal(self.temp.name+"/runner")
            # Simulate a received claim before execution; all infrastructure in this test is mocked.
            journal.save({"claim_id":str(uuid.uuid4()),"task":task,"started":False,"report":None})
            try:
                await Runner(client,journal,"opensandbox",factory=Sandbox).tick()
            finally:
                journal.close();await client.close()
        asyncio.run(run())
        value=self.service.task(self.alice,task["id"])
        self.assertEqual(len(executed),1)
        self.assertEqual(executed[0]["source"],"print(20)")
        self.assertEqual(value["model_actual"],100)
        self.assertIn('"final": "20"',value["result"])
        self.assertEqual(value["state"],"needs_review") # mock management deletion cannot prove host cleanup

    def test_cancellation_prevents_new_tool_execution(self):
        self.submit();task=self.claim();self.service.cancel(self.alice,task["id"])
        from runner.backends import Demo
        class Forbidden(Demo):
            async def create(self,owner):
                raise AssertionError("unauthorized create")
        async def run():
            client=Client("http://127.0.0.1",self.node["token"],transport=httpx.ASGITransport(app=self.app))
            try:
                result=await Runner(client,None,factory=Forbidden).execute(task)
                self.assertNotIn("AssertionError",result["evidence"])
            finally:
                await client.close()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
