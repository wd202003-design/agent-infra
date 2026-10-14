"""Version-pinned images; decoding and object IO occur outside scheduler locks."""

import hashlib
import io
import warnings
from PIL import Image
from ..service import Fault
from .service import uid


class ImageStorage:
    def __init__(self, client, bucket):
        self.client, self.bucket = client, bucket
        if client.get_bucket_versioning(Bucket=bucket).get("Status") != "Enabled":
            raise ValueError("versioned_bucket_required")

    def upload(self, a):
        return self.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": a["staging_key"],
                "ContentType": a["media_type"],
            },
            ExpiresIn=300,
        )

    def read(self, key, version, size):
        if version == "null":
            raise Fault("VERSION_REQUIRED", 422)
        response = self.client.get_object(
            Bucket=self.bucket, Key=key, VersionId=version
        )
        stream = response["Body"]
        try:
            if response["ContentLength"] != size:
                raise Fault("ARTIFACT_SIZE_MISMATCH", 422)
            data = stream.read(size + 1)
            if len(data) != size:
                raise Fault("ARTIFACT_SIZE_MISMATCH", 422)
            return data
        finally:
            stream.close()

    def commit(self, a, version):
        data = self.read(a["staging_key"], version, a["size_bytes"])
        if hashlib.sha256(data).hexdigest() != a["sha256"]:
            raise Fault("CHECKSUM_MISMATCH", 422)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(data))
            width, height = image.size
            if (
                width * height > 16777216
                or max(width, height) > 8192
                or getattr(image, "n_frames", 1) != 1
            ):
                raise Fault("IMAGE_LIMIT", 422)
            if Image.MIME.get(image.format) != a["media_type"]:
                raise Fault("IMAGE_TYPE_MISMATCH", 422)
            image.verify()
            # Decode after verify to reject truncated image streams.
            decoded = Image.open(io.BytesIO(data))
            decoded.load()
            decoded.close()
        result = self.client.copy_object(
            Bucket=self.bucket,
            Key=a["final_key"],
            CopySource={
                "Bucket": self.bucket,
                "Key": a["staging_key"],
                "VersionId": version,
            },
            MetadataDirective="REPLACE",
            Metadata={"sha256": a["sha256"]},
            ContentType=a["media_type"],
        )
        final_version = result.get("VersionId")
        if not final_version or final_version == "null":
            raise Fault("VERSION_REQUIRED", 503)
        final = self.read(a["final_key"], final_version, a["size_bytes"])
        if hashlib.sha256(final).hexdigest() != a["sha256"]:
            raise Fault("CHECKSUM_MISMATCH", 422)
        return {"version_id": final_version, "width": width, "height": height}

    def download(self, a):
        return self.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": a["final_key"],
                "VersionId": a["version_id"],
                "ResponseContentDisposition": 'attachment; filename="'
                + a["id"]
                + "."
                + a["media_type"].split("/")[1]
                + '"',
            },
            ExpiresIn=60,
        )


class Artifacts:
    def __init__(self, p):
        self.p = p

    def available(self):
        if not self.p.storage:
            raise Fault("STORAGE_UNAVAILABLE", 503)

    def create(self, actor, payload, key, attempt=None):
        self.available()
        p = self.p
        data = payload.model_dump()
        with p.tx() as tx:

            def apply():
                if attempt:
                    root, child, a = p.active(tx, actor, attempt, payload, True)
                    op = p.get(tx, "operation", payload.operation_id)
                    if (
                        op["attempt_id"] != attempt
                        or op["kind"] == "agent"
                        or op["state"] != "succeeded"
                    ):
                        raise Fault("OPERATION_NOT_COMPLETE", 409)
                    if (
                        sum(
                            x.get("attempt_id") == attempt
                            for x in p.rows(tx, "artifact", root["id"])
                        )
                        >= child["count"]
                    ):
                        raise Fault("ARTIFACT_LIMIT", 429)
                    owner, rootid = root["owner"], root["id"]
                else:
                    owner, rootid = actor["id"], "input:" + actor["id"]
                    if len(p.rows(tx, "artifact", rootid)) >= 100:
                        raise Fault("INPUT_ARTIFACT_LIMIT", 429)
                id = uid()
                value = data | {
                    "id": id,
                    "root_id": rootid,
                    "owner": owner,
                    "attempt_id": attempt,
                    "state": "staging",
                    "staging_key": "v3/staging/" + id,
                    "final_key": "v3/final/" + id,
                }
                p.put(tx, "artifact", value)
                return value

            a = p.idem(tx, actor["id"] + ":upload:" + str(attempt), key, data, apply)
        return {"artifact_id": a["id"], "upload_url": p.storage.upload(a)}

    def commit(self, actor, id, payload, attempt=None):
        self.available()
        p = self.p
        with p.tx() as tx:
            a = p.get(tx, "artifact", id)
            if attempt:
                p.active(tx, actor, attempt, payload, True)
                if a["attempt_id"] != attempt:
                    raise Fault("NOT_FOUND", 404)
            elif a["attempt_id"] or a["owner"] != actor["id"]:
                raise Fault("NOT_FOUND", 404)
            if a["state"] == "committed":
                if a["source_version"] != payload.version_id:
                    raise Fault("ARTIFACT_VERSION_CONFLICT", 409)
                return p.artifact_view(a)
            if a.get("source_version") and a["source_version"] != payload.version_id:
                raise Fault("ARTIFACT_VERSION_CONFLICT", 409)
            a["source_version"] = payload.version_id
            a["state"] = "verifying"
            p.put(tx, "artifact", a)
        result = p.storage.commit(a, payload.version_id)
        with p.tx() as tx:
            if attempt:
                p.active(tx, actor, attempt, payload, True)
            a = p.get(tx, "artifact", id)
            a.update(result, state="committed")
            p.put(tx, "artifact", a)
            return p.artifact_view(a)

    def download(self, actor, id, attempt=None, payload=None):
        self.available()
        p = self.p
        with p.tx() as tx:
            a = p.get(tx, "artifact", id)
            if a["state"] != "committed":
                raise Fault("ARTIFACT_NOT_COMMITTED", 409)
            if attempt:
                r, c, _ = p.active(tx, actor, attempt, payload)
                refs = p.get(tx, "context", r["id"] + ":" + str(c["context_version"]))[
                    "reference_artifact_ids"
                ]
                refs += [
                    aid
                    for d in p.rows(tx, "child", r["id"])
                    if d["child_key"] in c["depends_on"]
                    for aid in d["artifact_ids"]
                ]
                if id not in refs and a["attempt_id"] != attempt:
                    raise Fault("NOT_FOUND", 404)
            elif actor["role"] != "admin" and a["owner"] != actor["id"]:
                raise Fault("NOT_FOUND", 404)
        return {
            "url": p.storage.download(a),
            "expires_in": 60,
            "media_type": a["media_type"],
            "sha256": a["sha256"],
            "size_bytes": a["size_bytes"],
        }
