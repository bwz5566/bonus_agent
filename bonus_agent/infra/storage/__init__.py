"""Durable storage ports. SQLite is the local adapter; callers use StateStore."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Protocol


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def fingerprint(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class ObjectStore:
    """Immutable, content-addressed local blobs. References are SHA-256 IDs."""
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def put(self, data: bytes) -> str:
        ref = hashlib.sha256(data).hexdigest()
        atomic_write(self.root / ref[:2] / ref, data)
        return ref

    def get(self, ref: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{64}", ref):
            raise ValueError("Invalid object reference")
        data = (self.root / ref[:2] / ref).read_bytes()
        if hashlib.sha256(data).hexdigest() != ref:
            raise ValueError("Object checksum mismatch")
        return data

    def put_json(self, value) -> str:
        return self.put(canonical(value))

    def get_json(self, ref: str):
        return json.loads(self.get(ref))


class StateStore(Protocol):
    def put(self, kind: str, scope: str, id: str, value: dict, *, append: bool = False) -> None: ...
    def get(self, kind: str, scope: str, id: str) -> dict | None: ...
    def list(self, kind: str, scope: str) -> list[dict]: ...


class SQLiteStateStore:
    """Transactions per write, scope on every query; no process-local facts.

    Generic JSON records keep the port independent of SQLite SQL/connection types.
    A PostgreSQL adapter can implement the same three operations.
    """
    APPEND_ONLY = {"decisions", "checkpoints", "result_revisions"}

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS records (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, scope TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(kind,scope,id))")
            db.executescript("""
                CREATE TRIGGER IF NOT EXISTS immutable_update BEFORE UPDATE ON records
                WHEN OLD.kind IN ('decisions','checkpoints','result_revisions')
                BEGIN SELECT RAISE(ABORT, 'append-only record'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_delete BEFORE DELETE ON records
                WHEN OLD.kind IN ('decisions','checkpoints','result_revisions')
                BEGIN SELECT RAISE(ABORT, 'append-only record'); END;
            """)

    @contextmanager
    def _connect(self):
        # sqlite3's own context manager commits/rolls back but never closes.
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, kind, scope, id, value, *, append=False):
        if not scope or not id:
            raise ValueError("Scope and ID required")
        sql = "INSERT INTO records(kind,scope,id,payload) VALUES(?,?,?,?)"
        if not append and kind not in self.APPEND_ONLY:
            sql += " ON CONFLICT(kind,scope,id) DO UPDATE SET payload=excluded.payload"
        with self._connect() as db:
            db.execute(sql, (kind, scope, id, canonical(value).decode()))

    def get(self, kind, scope, id):
        with self._connect() as db:
            row = db.execute("SELECT payload FROM records WHERE kind=? AND scope=? AND id=?", (kind, scope, id)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, kind, scope):
        with self._connect() as db:
            rows = db.execute("SELECT payload FROM records WHERE kind=? AND scope=? ORDER BY seq", (kind, scope)).fetchall()
        return [json.loads(r[0]) for r in rows]
