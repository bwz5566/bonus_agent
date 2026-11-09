"""Fixed step execution with scoped idempotency, leases and verified checkpoints."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import lru_cache
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time

from bonus_agent.infra.storage import atomic_write, canonical, fingerprint
from bonus_agent.scenarios.releases import (
    ReleaseError, definitions, load_release, map_fixed, reconcile, sha,
)
from bonus_core.data.table import Row, RowRef, StdTable
from bonus_core.engine.evaluator import Trace
from bonus_core.engine.issues import Issue
from bonus_core.engine.run import ENGINE_VERSION, Adjustment, Engine, NodeResult, PayoutResult, Result, RollupResult
from bonus_core.report.excel import write_excel
from bonus_agent.scenarios.periods import apply_period
from bonus_agent.scenarios.batches import Registry


class RunBusy(ReleaseError):
    pass


def encode(value):
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, (datetime, date)):
        return {"$date": value.isoformat()}
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    return value


def decode(value):
    if isinstance(value, dict):
        if set(value) == {"$decimal"}:
            return Decimal(value["$decimal"])
        if set(value) == {"$date"}:
            return date.fromisoformat(value["$date"])
        return {k: decode(v) for k, v in value.items()}
    return [decode(v) for v in value] if isinstance(value, list) else value


def result_data(result):
    d = asdict(result)
    d.pop("tier_stats", None); d.pop("coverage", None)
    return encode(d)


def result_load(payload):
    d = decode(payload)
    for field, model in (("nodes", NodeResult), ("payouts", PayoutResult), ("rollups", RollupResult), ("traces", Trace)):
        d[field] = {k: model(**v) for k, v in d.get(field, {}).items()}
    d["adjustments"] = [Adjustment(**x) for x in d["adjustments"]]
    d["issues"] = [Issue(**x) for x in d["issues"]]
    return Result(**d)


@lru_cache(maxsize=1)
def engine_code():
    root = Path(__file__).resolve().parents[2]
    files = sorted((root / "bonus_core").rglob("*.py")) + sorted(Path(__file__).parent.glob("*.py"))
    return fingerprint({p.relative_to(root).as_posix(): sha(p) for p in files})


@dataclass
class FixedRun:
    task_id: str
    directory: Path
    result: Result
    cached: bool


class Ledger:
    LEASE = 60

    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "tasks.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, token TEXT, lease REAL, status TEXT, metadata TEXT)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def owns(self, key, token):
        with self.connect() as db:
            row = db.execute("SELECT token,lease,status FROM tasks WHERE id=?", (key,)).fetchone()
        return bool(row and row[0] == token and row[1] > time.time() and row[2] == "running")

    def claim(self, key):
        token = secrets.token_hex(16)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT lease,status,metadata FROM tasks WHERE id=?", (key,)).fetchone()
            if row and row[1] == "completed":
                return None, json.loads(row[2])
            if row and row[1] == "running" and row[0] > time.time():
                raise RunBusy(f"同一批次任务 {key[:12]} 正在执行，请等待或在租约过期后恢复")
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET token=excluded.token,lease=excluded.lease,status=excluded.status",
                       (key, token, time.time() + self.LEASE, "running", "{}"))
        return token, None

    def update(self, key, token, status, metadata=None):
        with self.connect() as db:
            changed = db.execute("UPDATE tasks SET status=?,lease=?,metadata=? WHERE id=? AND token=?",
                                 (status, time.time() + self.LEASE, json.dumps(metadata or {}), key, token)).rowcount
            if changed != 1:
                raise RunBusy("执行租约已变更，拒绝覆盖另一个执行者的结果")

    def finish(self, key, token, publish):
        """Fence the final file publication and completion against lease takeover."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT token,lease,status FROM tasks WHERE id=?", (key,)).fetchone()
            if not row or row[0] != token or row[1] <= time.time() or row[2] != "running":
                raise RunBusy("执行租约丢失，拒绝发布结果")
            metadata = publish()
            db.execute("UPDATE tasks SET status='completed',metadata=? WHERE id=? AND token=?",
                       (json.dumps(metadata), key, token))


def _checkpoint(path, payload):
    atomic_write(path, canonical({"payload": payload, "checksum": fingerprint(payload)}))


def _read_checkpoint(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if fingerprint(value["payload"]) != value["checksum"]:
        raise ReleaseError(f"检查点被修改：{path.name}")
    return value["payload"]


def task_key(scenario, version, period, data_files, *, releases=None, business_batch=None):
    _, spec, publication = load_release(scenario, version, releases)
    if period not in spec.periods:
        raise ReleaseError("期间不在批准范围")
    batch = Registry(releases).get(business_batch or "")
    if batch["scenario"] != scenario or batch["period"] != period or batch["entity"] not in spec.entities:
        raise ReleaseError("业务批次的场景、核算主体或期间与所选规则版本不一致")
    files = [Path(p) for p in data_files]
    if not files or len({p.name for p in files}) != len(files):
        raise ReleaseError("本批数据不能为空或包含重名文件")
    identity = {"scenario": scenario, "version": version, "period": period,
                "business_batch": batch["key"], "batch_id": batch["batch_id"], "entity": batch["entity"], "scope": batch["scope"],
                "publication": publication["fingerprint"], "inputs": {p.name: sha(p) for p in files},
                "engine": ENGINE_VERSION, "code": engine_code(), "numeric_profile": "decimal-half-up-v1"}
    return fingerprint(identity), identity


def read_completed(runs, key):
    """Read a verified completed task; never claim, compute or change task state."""
    if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise ReleaseError("无效任务编号")
    ledger = Ledger(runs)
    with ledger.connect() as db:
        row = db.execute("SELECT status,metadata FROM tasks WHERE id=?", (key,)).fetchone()
    if not row or row[0] != "completed":
        raise ReleaseError("任务尚未完成")
    task = ledger.root / key
    for name, digest in json.loads(row[1])["files"].items():
        if sha(task / name) != digest:
            raise ReleaseError("已完成结果被修改，拒绝使用")
    return FixedRun(key, task, result_load(_read_checkpoint(task / "computed.json")), True)


def run_fixed(scenario, version, period, data_files, runs, *, releases=None, business_batch=None, should_stop=lambda: False):
    files = list(data_files)
    key, _ = task_key(scenario, version, period, files, releases=releases, business_batch=business_batch)
    registry = Registry(releases)
    token = registry.begin(business_batch, runs, key)
    stop, lost = threading.Event(), threading.Event()
    def heartbeat():
        while not stop.wait(10):
            try:
                registry.heartbeat(business_batch, token)
            except Exception:
                lost.set()
                return
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    def cancelled():
        if lost.is_set():
            raise ReleaseError("业务批次租约已丢失")
        return should_stop()
    try:
        result = _run_fixed(scenario, version, period, files, runs, releases=releases,
                            business_batch=business_batch, should_stop=cancelled)
        registry.heartbeat(business_batch, token)
        return result
    finally:
        stop.set(); thread.join()
        registry.release(business_batch, token)


def _run_fixed(scenario, version, period, data_files, runs, *, releases=None, business_batch=None, should_stop=lambda: False):
    """`runs` belongs to ONE business case/batch. There is no cross-case cache."""
    directory, spec, publication = load_release(scenario, version, releases)
    if period not in spec.periods:
        raise ReleaseError(f"期间 {period!r} 不在此规则版本批准范围 {spec.periods}")
    files = [Path(p) for p in data_files]
    if not files or len({p.name for p in files}) != len(files):
        raise ReleaseError("本批数据不能为空或包含重名文件")
    key, identity = task_key(scenario, version, period, files, releases=releases, business_batch=business_batch)
    inputs = identity["inputs"]
    ledger = Ledger(runs)
    token, done = ledger.claim(key)
    task = ledger.root / key
    if done is not None:
        return read_completed(runs, key)

    task.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    lost = threading.Event()

    def heartbeat():
        while not stop.wait(10):
            try:
                ledger.update(key, token, "running")
            except Exception:
                lost.set()
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()

    def guard():
        if lost.is_set() or not ledger.owns(key, token):
            raise RunBusy("执行租约丢失")
        if should_stop():
            raise ReleaseError("已手动停止；保留已完成步骤，可重新提交相同任务恢复")

    try:
        guard()
        rs, mapping = definitions(directory, spec)
        input_dir = task / "inputs"
        input_dir.mkdir(exist_ok=True)
        for p in files:
            dst = input_dir / p.name
            if not dst.exists():
                atomic_write(dst, p.read_bytes())
            if sha(dst) != inputs[p.name]:
                raise ReleaseError("上传期间输入发生变化，请重新提交")
        mapped = task / "mapped.json"
        if mapped.exists():
            payload = decode(_read_checkpoint(mapped))
            data = {name: StdTable(name, [Row(row["values"], RowRef(**row["src"]) if row["src"] else None)
                                         for row in rows]) for name, rows in payload["tables"].items()}
            issues = [Issue(**i) for i in payload["issues"]]
        else:
            data, issues = map_fixed(rs, mapping, [input_dir / p.name for p in files])
            _checkpoint(mapped, encode({"tables": {name: [{"values": r.values, "src": asdict(r.src) if r.src else None}
                                               for r in table.rows] for name, table in data.items()},
                                       "issues": [i.to_dict() for i in issues]}))
        guard()
        computed = task / "computed.json"
        if computed.exists():
            result = result_load(_read_checkpoint(computed))
            period_report = decode(_read_checkpoint(task / "period.json"))
        else:
            data, period_report = apply_period(spec, period, data)
            _checkpoint(task / "period.json", encode(period_report))
            engine = Engine(rs, data)
            engine.issues += issues
            result = engine.compute()
            reconcile(result)
            _checkpoint(computed, result_data(result))
        guard()
        reconcile(result)
        report = {"status": "passed", "object_count": len(result.payout_rows()),
                  "input_hash": result.input_hash, "identity": identity,
                  "checks": [i.to_dict() for i in result.issues], "task_id": key}
        report["period_validation"] = period_report
        _checkpoint(task / "validated.json", report)
        output = task / "result.xlsx"
        staging = task / f".result-{token}.xlsx"
        period_rows, excluded_rows = [], []
        for name, item in period_report["tables"].items():
            period_rows.append(["数据表：" + name, period, item["mode"], item["input_count"], item["kept_count"],
                                item["excluded_count"], "说明：" + item["reason"]])
            excluded_rows += [["数据表：" + name, "来源：" + r["source"], r["date"], "原因：" + r["reason"],
                              json.dumps(r["values"],ensure_ascii=False)] for r in item["excluded"]]
        write_excel(rs, result, staging, extra_sheets=[
            ("业务批次", ["项目", "内容"], [["批次号", identity["batch_id"]], ["核算主体", identity["entity"]],
             ["场景", scenario], ["期间", period], ["规则版本", version], ["任务编号", key]], {}),
            ("期间校验", ["数据表", "期间", "契约", "输入行数", "纳入行数", "排除行数", "说明"], period_rows, {}),
            ("跨期排除明细", ["数据表", "来源", "业务日期", "批准排除原因", "原始字段值"], excluded_rows, {})])
        guard()
        stop.set(); thread.join()
        guard()
        def publish_result():
            staging.replace(output)
            atomic_write(task / "run.json", canonical({**report, "approved_by": publication["approved_by"],
                                                       "approved_at": publication["approved_at"]}))
            return {"files": {name: sha(task / name) for name in
                    ("mapped.json", "period.json", "computed.json", "validated.json", "result.xlsx", "run.json")}}
        ledger.finish(key, token, publish_result)
        return FixedRun(key, task, result, False)
    except BaseException:
        stop.set(); thread.join()
        if ledger.owns(key, token):
            ledger.update(key, token, "failed")
            (task / "result.xlsx").unlink(missing_ok=True)
        # Failed tasks retain checkpoints, never a downloadable final result.
        (task / f".result-{token}.xlsx").unlink(missing_ok=True)
        raise
    finally:
        stop.set(); thread.join()
