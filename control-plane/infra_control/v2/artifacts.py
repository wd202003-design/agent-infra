"""Version-pinned S3 staging and verified final objects; never trust ETag as SHA."""

import hashlib
import json
from ..db import encode
from ..service import Fault
from .service import uid, iso, TERMINAL
from .store import get_task


class S3Storage:
    def __init__(self, client, bucket):
        self.client, self.bucket = client, bucket

    def ready(self):
        # Version IDs are required to prevent replacing a verified staging object.
        if (
            self.client.get_bucket_versioning(Bucket=self.bucket).get("Status")
            != "Enabled"
        ):
            raise ValueError("S3 bucket versioning must be enabled")

    def upload_url(self, key, media_type):
        return self.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": media_type},
            ExpiresIn=300,
            HttpMethod="PUT",
        )

    def check(self, key, version, expected_size, expected_hash):
        if not version or version == "null":
            raise Fault("storage_versioning_required", 503)
        response = self.client.get_object(
            Bucket=self.bucket, Key=key, VersionId=version
        )
        body = response["Body"]
        digest = hashlib.sha256()
        size = 0
        try:
            if response["ContentLength"] != expected_size:
                raise Fault("artifact_size_mismatch", 422)
            while True:
                chunk = body.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > expected_size:
                    raise Fault("artifact_size_mismatch", 422)
                digest.update(chunk)
        finally:
            body.close()
        if size != expected_size or digest.hexdigest() != expected_hash:
            raise Fault("artifact_checksum_mismatch", 422)

    def finalize(self, artifact):
        head = self.client.head_object(Bucket=self.bucket, Key=artifact["staging_key"])
        version = head.get("VersionId")
        self.check(
            artifact["staging_key"], version, artifact["size"], artifact["sha256"]
        )
        result = self.client.copy_object(
            Bucket=self.bucket,
            Key=artifact["final_key"],
            CopySource={
                "Bucket": self.bucket,
                "Key": artifact["staging_key"],
                "VersionId": version,
            },
            CopySourceIfMatch=head["ETag"],
            MetadataDirective="REPLACE",
            Metadata={"sha256": artifact["sha256"]},
            ContentType=artifact["media_type"],
        )
        final_version = result.get("VersionId")
        self.check(
            artifact["final_key"], final_version, artifact["size"], artifact["sha256"]
        )
        return final_version

    def download_url(self, artifact):
        return self.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": artifact["final_key"],
                "VersionId": artifact["version_id"],
                "ResponseContentDisposition": 'attachment; filename="'
                + artifact["name"]
                + '"',
            },
            ExpiresIn=60,
            HttpMethod="GET",
        )

    def delete(self, artifact):
        # Remove all versions, including abandoned retries, for both exact keys.
        for key in (artifact["staging_key"], artifact["final_key"]):
            paginator = self.client.get_paginator("list_object_versions")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=key):
                items = [
                    {"Key": v["Key"], "VersionId": v["VersionId"]}
                    for group in ("Versions", "DeleteMarkers")
                    for v in page.get(group, [])
                    if v["Key"] == key
                ]
                if items:
                    result = self.client.delete_objects(
                        Bucket=self.bucket, Delete={"Objects": items, "Quiet": True}
                    )
                    if result.get("Errors"):
                        raise Fault("artifact_delete_incomplete", 503)


class Artifacts:
    def __init__(self, platform):
        self.p = platform

    def create(self, node, attempt_id, payload, key):
        p = self.p
        if not p.storage:
            raise Fault("storage_not_configured", 503)
        with p.db.transaction(serialized=False) as tx:
            p.node(tx, node)
            task, attempt = p.active(tx, node, attempt_id, payload.epoch)

            def apply():
                if task["data_class"] != "public":
                    raise Fault("artifact_egress_forbidden", 403)
                import jsonschema

                try:
                    jsonschema.Draft202012Validator(
                        task["policy"]["upload_schema"]
                    ).validate(payload.manifest.model_dump())
                except Exception:
                    raise Fault("artifact_schema_mismatch", 422) from None
                rows = tx.execute(
                    "SELECT body FROM v2_artifacts WHERE task_id=? AND status!='deleted'",
                    (task["id"],),
                ).fetchall()
                if (
                    len(rows) >= 20
                    or sum(json.loads(r[0])["size"] for r in rows)
                    + payload.manifest.size
                    > 104857600
                ):
                    raise Fault("artifact_limit", 413)
                artifact = {
                    **payload.manifest.model_dump(),
                    "id": uid(),
                    "task_id": task["id"],
                    "attempt_id": attempt_id,
                    "epoch": payload.epoch,
                    "status": "staging",
                    "data_class": task["data_class"],
                    "created": p.clock(),
                    "retention_until": iso(p.clock() + 2592000),
                }
                artifact["staging_key"] = "staging/" + task["id"] + "/" + artifact["id"]
                artifact["final_key"] = "final/" + task["id"] + "/" + artifact["id"]
                tx.execute(
                    "INSERT INTO v2_artifacts(id,task_id,status,created,body) VALUES(?,?,?,?,?)",
                    (
                        artifact["id"],
                        task["id"],
                        "staging",
                        p.clock(),
                        encode(artifact),
                    ),
                )
                return {
                    "upload_session": artifact["id"],
                    "url": p.storage.upload_url(
                        artifact["staging_key"], artifact["media_type"]
                    ),
                    "expires_at": iso(p.clock() + 300),
                    "headers": {"Content-Type": artifact["media_type"]},
                }

            return p.idem(
                tx, attempt_id + ":artifact", key, payload.model_dump(), apply
            )

    def complete(self, node, upload_id, payload, key):
        p = self.p
        if not p.storage:
            raise Fault("storage_not_configured", 503)
        # S3 calls occur outside database locks. Fixed versions make retries safe.
        with p.db.transaction(serialized=False) as tx:
            p.node(tx, node)
            row = tx.execute(
                "SELECT body FROM v2_artifacts WHERE id=?", (upload_id,)
            ).fetchone()
            if not row:
                raise Fault("not_found", 404)
            artifact = json.loads(row[0])
            task, attempt = p.active(tx, node, artifact["attempt_id"], payload.epoch)
            if task["deleted"]:
                raise Fault("content_deleted", 410)
            if payload.sha256 != artifact["sha256"] or payload.size != artifact["size"]:
                raise Fault("manifest_mismatch", 422)
            if artifact["status"] == "committed":
                return self.public(artifact)
            artifact["status"] = "verifying"
            tx.execute(
                "UPDATE v2_artifacts SET status=?,body=? WHERE id=?",
                ("verifying", encode(artifact), upload_id),
            )
        version = p.storage.finalize(artifact)
        with p.db.transaction(serialized=False) as tx:
            p.node(tx, node)
            task, attempt = p.active(tx, node, artifact["attempt_id"], payload.epoch)
            if task["deleted"]:
                raise Fault("content_deleted", 410)
            existing = json.loads(
                tx.execute(
                    "SELECT body FROM v2_artifacts WHERE id=?", (upload_id,)
                ).fetchone()[0]
            )
            if existing["status"] == "committed":
                return self.public(existing)
            artifact.update(status="committed", version_id=version)
            tx.execute(
                "UPDATE v2_artifacts SET status=?,body=? WHERE id=?",
                ("committed", encode(artifact), upload_id),
            )
            p.event(tx, task, "artifact_committed", {"artifact_id": upload_id})
            return self.public(artifact)

    @staticmethod
    def public(artifact):
        return {
            k: v
            for k, v in artifact.items()
            if k not in {"staging_key", "final_key", "version_id"}
        }

    def list(self, actor, task_id):
        with self.p.db.transaction(serialized=False) as tx:
            task = get_task(tx, task_id, actor, locked=False)
            if task["deleted"]:
                raise Fault("content_deleted", 410)
            return [
                self.public(json.loads(r[0]))
                for r in tx.execute(
                    "SELECT body FROM v2_artifacts WHERE task_id=? AND status='committed' ORDER BY id",
                    (task_id,),
                ).fetchall()
            ]

    def download(self, actor, artifact_id, payload, key):
        p = self.p
        with p.db.transaction(serialized=False) as tx:
            row = tx.execute(
                "SELECT body FROM v2_artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
            if not row:
                raise Fault("not_found", 404)
            artifact = json.loads(row[0])
            task = get_task(tx, artifact["task_id"], actor)
            if task["deleted"]:
                raise Fault("content_deleted", 410)
            if artifact["status"] != "committed":
                raise Fault("artifact_not_committed")
            if artifact["data_class"] != "public":
                raise Fault("private_download_requires_proxy", 403)
            return p.idem(
                tx,
                actor["id"] + ":download:" + artifact_id,
                key,
                payload.model_dump(),
                lambda: {
                    "url": p.storage.download_url(artifact),
                    "expires_at": iso(p.clock() + 60),
                },
            )

    def redact(self, tx, task):
        task_id = task["id"]
        task["deleted"] = True
        task["generation"] += 1
        task["input"] = {}
        task["result"] = None
        if "agent" in task:
            task["agent"] = {"digest": task["agent"]["digest"]}
        task.pop("model_route", None)
        tx.execute("DELETE FROM v2_events WHERE task_id=?", (task_id,))
        tx.execute(
            "DELETE FROM v2_runner_events WHERE attempt_id IN (SELECT id FROM v2_attempts WHERE task_id=?)",
            (task_id,),
        )
        for row in tx.execute(
            "SELECT id,body FROM v2_attempts WHERE task_id=?", (task_id,)
        ).fetchall():
            attempt = json.loads(row[1])
            attempt.pop("approved_summary", None)
            tx.execute(
                "UPDATE v2_attempts SET body=? WHERE id=?", (encode(attempt), row[0])
            )
        # Strip cached model responses; retain financial evidence only.
        for row in tx.execute(
            "SELECT id,body FROM v2_usage WHERE task_id=?", (task_id,)
        ).fetchall():
            value = json.loads(row[1])
            value.pop("response", None)
            tx.execute("UPDATE v2_usage SET body=? WHERE id=?", (encode(value), row[0]))
        # Idempotent result capsules may contain task input/result: keep hashes and a tombstone.
        for scope, idem_key, response in tx.execute(
            "SELECT scope,key,response FROM v2_idempotency"
        ).fetchall():
            if task_id in response:
                tx.execute(
                    "UPDATE v2_idempotency SET response=? WHERE scope=? AND key=?",
                    (
                        encode({"task_id": task_id, "deleted": True}),
                        scope,
                        idem_key,
                    ),
                )

    def delete_content(self, actor, task_id, payload, key):
        p = self.p
        with p.db.transaction(serialized=False) as tx:

            def apply():
                task = get_task(tx, task_id, actor)
                if task["deleted"]:
                    return {"task_id": task_id, "deleted": True}
                p.cas(task, payload.expected_version)
                if task["state"] not in TERMINAL or p.unresolved(tx, task_id):
                    raise Fault("delete_not_safe")
                self.redact(tx, task)
                tx.execute(
                    "INSERT INTO v2_deletions(task_id,requested,body) VALUES(?,?,?)",
                    (task_id, p.clock(), encode({"deadline": p.clock() + 86400})),
                )
                p.event(tx, task, "content_deletion_requested")
                p.audit(tx, actor["id"], "delete_content", task_id)
                return {
                    "task_id": task_id,
                    "deleted": True,
                    "physical_delete_deadline": iso(p.clock() + 86400),
                }

            return p.idem(
                tx, actor["id"] + ":delete:" + task_id, key, payload.model_dump(), apply
            )

    def sweep(self):
        p = self.p
        with p.db.transaction(serialized=False) as tx:
            jobs = tx.execute(
                "SELECT task_id FROM v2_deletions WHERE completed IS NULL LIMIT 100"
            ).fetchall()
        for (task_id,) in jobs:
            with p.db.transaction(serialized=False) as tx:
                items = [
                    json.loads(r[0])
                    for r in tx.execute(
                        "SELECT body FROM v2_artifacts WHERE task_id=? AND status!='deleted'",
                        (task_id,),
                    ).fetchall()
                ]
            for artifact in items:
                if not p.storage:
                    raise Fault("storage_not_configured", 503)
                p.storage.delete(artifact)
                artifact["status"] = "deleted"
                with p.db.transaction(serialized=False) as tx:
                    tx.execute(
                        "UPDATE v2_artifacts SET status=?,body=? WHERE id=?",
                        ("deleted", encode(artifact), artifact["id"]),
                    )
            with p.db.transaction(serialized=False) as tx:
                pending = [
                    json.loads(row[0])
                    for row in tx.execute(
                        "SELECT a.body FROM v2_attempts a LEFT JOIN v2_node_deletions d ON d.attempt_id=a.id WHERE a.task_id=? AND d.attempt_id IS NULL",
                        (task_id,),
                    ).fetchall()
                ]
                if not any(a.get("local_content") for a in pending):
                    tx.execute(
                        "UPDATE v2_deletions SET completed=? WHERE task_id=?",
                        (p.clock(), task_id),
                    )
        return len(jobs)
