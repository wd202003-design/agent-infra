"""PostgreSQL transactions; SQLite is an explicitly enabled test double only."""
from contextlib import contextmanager
import json
import re
from pathlib import Path
import sqlite3


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Transaction:
    TABLES = {"agents", "nodes", "tasks", "model_calls", "audit", "claims"}

    def __init__(self, conn, sqlite):
        self.conn, self.sqlite = conn, sqlite

    def execute(self, sql, args=()):
        return self.conn.execute(sql if self.sqlite else sql.replace("?", "%s"), args)

    def get(self, table, key):
        assert table in self.TABLES
        row = self.execute(f"SELECT body FROM {table} WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, table):
        assert table in self.TABLES
        return [json.loads(r[0]) for r in self.execute(f"SELECT body FROM {table}").fetchall()]

    def save(self, table, item):
        assert table in self.TABLES
        if table == "tasks":
            self.execute("INSERT INTO tasks(id,owner,idem_key,input_hash,body) VALUES(?,?,?,?,?) "
                         "ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                         (item["id"], item["owner"], item["idem_key"], item["input_hash"], encode(item)))
        elif table == "model_calls":
            self.execute("INSERT INTO model_calls(id,task_id,body) VALUES(?,?,?) "
                         "ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                         (item["id"], item["task_id"], encode(item)))
        else:
            self.execute(f"INSERT INTO {table}(id,body) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                         (item["id"], encode(item)))


class Database:
    def __init__(self, dsn, *, test_sqlite=False, schema=None):
        self.sqlite = dsn.startswith("sqlite:")
        if self.sqlite and not test_sqlite:
            raise ValueError("SQLite is only permitted in explicit tests")
        if not self.sqlite and not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("A PostgreSQL DSN is required")
        self.dsn = dsn
        if schema and not re.fullmatch(r"test_[a-f0-9]+", schema):
            raise ValueError("Invalid isolated test schema")
        self.schema = schema

    def connect(self):
        if self.sqlite:
            conn = sqlite3.connect(self.dsn.removeprefix("sqlite:"), timeout=30)
            conn.execute("PRAGMA foreign_keys=ON")
            return conn
        import psycopg
        conn = psycopg.connect(self.dsn, connect_timeout=5, options="-c statement_timeout=10000 -c lock_timeout=5000")
        if self.schema:
            conn.execute("SET search_path TO " + self.schema)
        return conn

    @contextmanager
    def transaction(self, *, serialized=True):
        conn = self.connect()
        try:
            if self.sqlite:
                conn.execute("BEGIN IMMEDIATE")
            elif serialized:
                conn.execute("SELECT pg_advisory_xact_lock(31416004)")
            yield Transaction(conn, self.sqlite)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self):
        migrations = sorted((Path(__file__).resolve().parents[1] / "migrations").glob("*.sql"))
        with self.transaction() as tx:
            for migration in migrations:
                for statement in migration.read_text().split(";"):
                    if statement.strip():
                        tx.execute(statement)

    def ready(self):
        with self.transaction() as tx:
            return tx.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 2
