"""Shared business batch identity, case ownership and single confirmation."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

from bonus_agent.infra.storage import fingerprint
from bonus_agent.scenarios.releases import ReleaseError, load_release, root_dir, safe_id


class BatchError(ReleaseError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"[{code}] {message}")


class Registry:
    LEASE = 60

    def __init__(self, releases=None):
        self.releases = releases
        self.scope = os.environ.get("BONUS_BUSINESS_SCOPE", "local")
        if not self.scope.strip():
            raise BatchError("batch_scope_invalid", "业务作用域不能为空")
        root = Path(os.environ.get("BONUS_BUSINESS_STATE_DIR") or root_dir(releases).parent / "business_state")
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "batches.sqlite3"
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS batches (
                key TEXT PRIMARY KEY, scope TEXT, entity TEXT, scenario TEXT, period TEXT, batch_id TEXT,
                registered_by TEXT, owner TEXT, task_id TEXT, token TEXT, lease REAL, confirmation TEXT,
                UNIQUE(scope,entity,scenario,period,batch_id))""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def register(self, scenario, version, period, entity, batch_id, by, *, releases=None):
        _, spec, _ = load_release(scenario, version, releases if releases is not None else self.releases)
        if period not in spec.periods or entity not in spec.entities or not by.strip():
            raise BatchError("batch_contract", "期间或核算主体未获此规则版本批准，或登记人为空")
        safe_id(batch_id)
        key = fingerprint([self.scope, entity, scenario, period, batch_id])
        with self.connect() as db:
            db.execute("INSERT INTO batches VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO NOTHING",
                (key, self.scope, entity, scenario, period, batch_id, by, "", "", "", 0, ""))
        return self.get(key)

    def get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM batches WHERE key=? AND scope=?", (key, self.scope)).fetchone()
        if not row:
            raise BatchError("batch_missing", "业务批次不存在；须由获授权人员先登记，不能随机创建新案卷绕过去重")
        return dict(row)

    def list(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM batches WHERE scope=? ORDER BY rowid DESC", (self.scope,))]

    def bind(self, key, owner):
        owner = str(Path(owner).resolve())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owner FROM batches WHERE key=? AND scope=?", (key, self.scope)).fetchone()
            if not row:
                raise BatchError("batch_missing", "业务批次未登记")
            if row["owner"] and row["owner"] != owner:
                raise BatchError("batch_duplicate", f"此业务批次已绑定案卷，请进入原案卷：{row['owner']}")
            db.execute("UPDATE batches SET owner=? WHERE key=?", (owner, key))

    def begin(self, key, owner, task):
        self.bind(key, owner)
        token = secrets.token_hex(16)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM batches WHERE key=? AND scope=?", (key, self.scope)).fetchone()
            if row["confirmation"] and row["task_id"] != task:
                raise BatchError("batch_confirmed", "此批次已确认，禁止以修改数据或规则建立另一份结算；须先办理财务更正流程")
            if row["token"] and row["lease"] > time.time():
                raise BatchError("batch_busy", "此业务批次正在运行，请等待或在租约到期后恢复")
            db.execute("UPDATE batches SET task_id=?,token=?,lease=? WHERE key=?", (task, token, time.time()+self.LEASE, key))
        return token

    def heartbeat(self, key, token):
        with self.connect() as db:
            count = db.execute("UPDATE batches SET lease=? WHERE key=? AND scope=? AND token=? AND lease>?",
                               (time.time()+self.LEASE, key, self.scope, token, time.time())).rowcount
            if count != 1:
                raise BatchError("batch_lease_lost", "业务批次租约已失效")

    def release(self, key, token):
        with self.connect() as db:
            db.execute("UPDATE batches SET token='',lease=0 WHERE key=? AND scope=? AND token=?", (key,self.scope,token))

    def confirm(self, key, owner, task, record):
        self.bind(key, owner)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM batches WHERE key=? AND scope=?", (key,self.scope)).fetchone()
            if row["task_id"] != task or (row["token"] and row["lease"] > time.time()):
                raise BatchError("batch_revision_changed", "批次修订已变化或正在运行，不能确认旧结果")
            if row["confirmation"]:
                return json.loads(row["confirmation"])
            db.execute("UPDATE batches SET confirmation=? WHERE key=?", (json.dumps(record,ensure_ascii=False),key))
        return record

    def require_mutable(self, key):
        row = self.get(key)
        if row["confirmation"]:
            raise BatchError("batch_confirmed", "批次已确认，不能继续替换材料；须办理财务更正流程")
        if row["token"] and row["lease"] > time.time():
            raise BatchError("batch_busy", "此业务批次正在运行，不能更换材料")
