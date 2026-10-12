"""Trusted Linux data preparation; never run task code on this host.

Only admin-mapped roots are readable. dir-fd traversal refuses symlinks, hardlinks,
special files, mount crossings and changed files. Guests receive a fresh RO copy.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat

MAX_BYTES = 100 * 1024 * 1024
MAX_FILES = 1000


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def open_root(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("absolute_admin_root_required")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def walk_files(root):
    fd = open_root(root)
    device = os.fstat(fd).st_dev
    used = 0
    count = 0

    def walk(directory, prefix="", depth=0):
        nonlocal used, count
        if depth > 16:
            raise ValueError("workspace_depth_limit")
        for name in sorted(os.listdir(directory)):
            entry = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            try:
                before = os.fstat(entry)
                if before.st_dev != device:
                    raise ValueError("workspace_mount_crossing")
                relative = prefix + name
                if stat.S_ISDIR(before.st_mode):
                    yield from walk(entry, relative + "/", depth + 1)
                elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
                    count += 1
                    if count > MAX_FILES or before.st_size > MAX_BYTES - used:
                        raise ValueError("workspace_limit")
                    chunks = []
                    length = 0
                    while True:
                        chunk = os.read(entry, 65536)
                        if not chunk:
                            break
                        used += len(chunk)
                        length += len(chunk)
                        if used > MAX_BYTES:
                            raise ValueError("workspace_limit")
                        chunks.append(chunk)
                    after = os.fstat(entry)
                    if (
                        before.st_ino,
                        before.st_size,
                        before.st_mtime_ns,
                        before.st_ctime_ns,
                    ) != (
                        after.st_ino,
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    ) or length != before.st_size:
                        raise ValueError("workspace_changed_during_snapshot")
                    yield relative, b"".join(chunks)
                else:
                    raise ValueError("workspace_special_or_linked_file")
            finally:
                os.close(entry)

    try:
        yield from walk(fd)
    finally:
        os.close(fd)


def summarize(files, spec):
    """Fixed numeric JSONL aggregate. Admin sets columns/range/minimum cohort."""
    if spec.get("algorithm") != "numeric-jsonl-v1":
        raise ValueError("unapproved_summary_algorithm")
    columns = spec["columns"]
    minimum = spec.get("min_rows", 10)
    if not 1 <= len(columns) <= 16 or type(minimum) is not int or minimum < 10:
        raise ValueError("invalid_summary_policy")
    for name in columns:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name):
            raise ValueError("invalid_summary_column")
    stats = {
        name: {"count": 0, "min": None, "max": None, "sum": 0.0} for name in columns
    }
    for name, data in files:
        if not name.endswith(".jsonl"):
            raise ValueError("summary_requires_jsonl")
        for line in data.splitlines():
            if len(line) > 65536:
                raise ValueError("summary_row_limit")
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("summary_row_object_required")
            for column, bounds in columns.items():
                value = row[column]
                if (
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or not bounds["min"] <= value <= bounds["max"]
                ):
                    raise ValueError("summary_value_outside_policy")
                value = float(value)
                s = stats[column]
                s["count"] += 1
                s["sum"] += value
                if not math.isfinite(s["sum"]):
                    raise ValueError("summary_overflow")
                s["min"] = value if s["min"] is None else min(s["min"], value)
                s["max"] = value if s["max"] is None else max(s["max"], value)
    result = {}
    for name, s in stats.items():
        if s["count"] < minimum:
            raise ValueError("summary_cohort_too_small")
        result[name] = {
            "count": s["count"],
            "min": round(s["min"], 2),
            "max": round(s["max"], 2),
            "mean": round(s["sum"] / s["count"], 2),
        }
    if len(canonical(result).encode()) > 8192:
        raise ValueError("summary_limit")
    return result


class Workspaces:
    def __init__(self, mappings, snapshot_root):
        self.mappings = mappings
        self.root = Path(snapshot_root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = open_root(self.root)
        os.close(fd)
        if stat.S_IMODE(self.root.stat().st_mode) & 0o022:
            raise ValueError("snapshot_root_writable_by_others")

    def prepare(self, task, attempt_id):
        if not re.fullmatch(r"[a-f0-9-]{36}", attempt_id):
            raise ValueError("invalid_attempt")
        spec = self.mappings.get(task["workspace_ref"])
        if (
            not spec
            or spec["owner"] != task["owner"]
            or spec["data_class"] != task["data_class"]
        ):
            raise ValueError("workspace_not_authorized")
        source = Path(spec["root"])
        if self.root.resolve().is_relative_to(
            source.resolve()
        ) or source.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("input_output_overlap")
        destination = self.root / attempt_id
        destination.mkdir(mode=0o700)  # existing intent is never replayed
        try:
            files = walk_files(source)
            summary = None
            if task["data_class"] == "edge_summary":
                summary = summarize(files, spec["summary"])
                files = [("summary.json", canonical(summary).encode())]
            for name, data in files:
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with target.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                target.chmod(0o444)
            for directory, dirs, names in os.walk(destination, topdown=False):
                Path(directory).chmod(0o555)
            parent_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            return {
                "path": str(destination),
                "summary": summary,
                "summary_digest": hashlib.sha256(
                    canonical(summary).encode()
                ).hexdigest()
                if summary is not None
                else None,
            }
        except BaseException:
            self.remove(attempt_id)
            raise

    def save_private_result(self, attempt_id, value):
        root = self.root.parent / "private-results"
        root.mkdir(mode=0o700, exist_ok=True)
        target = root / (attempt_id + ".json")
        fd = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "w") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())

    def delete_local_content(self, attempt_id):
        if not re.fullmatch(r"[a-f0-9-]{36}", attempt_id):
            raise ValueError("invalid_attempt")
        self.remove(attempt_id)
        target = self.root.parent / "private-results" / (attempt_id + ".json")
        target.unlink(missing_ok=True)
        if target.parent.exists():
            fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def remove(self, attempt_id):
        target = self.root / attempt_id
        if not target.exists():
            return
        for directory, dirs, names in os.walk(target, topdown=False):
            Path(directory).chmod(0o700)
        shutil.rmtree(target)
