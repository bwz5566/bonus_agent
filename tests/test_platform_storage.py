"""Platform storage/state/cache/memory/skills/MCP tests; no finance rules involved."""
import json

import pytest

from bonus_agent import case as cs
from bonus_agent.infra import checkpoints as cp
from bonus_agent.infra.cache import CacheKey, ResultCache
from bonus_agent.infra.state import TaskState
from bonus_agent.infra.storage import ObjectStore, SQLiteStateStore
from bonus_agent.mcp import MCPClient
from bonus_agent.memory import Memory
from bonus_agent.memory.context_manifest import build_context_manifest
from bonus_agent.scenarios import PackSchema, list_scenarios, load_pack
from bonus_agent.skills import SkillRegistry, allowed


def _state(root, scope="tenant-a"):
    return TaskState(SQLiteStateStore(root / "state.sqlite3"), ObjectStore(root / "objects"), scope)


# ---- restart / resume -------------------------------------------------------

def test_restart_resume_from_checkpoint(tmp_path):
    payload = {"step": "mapped", "pending": ["Q1"], "workspace": {"notes": {"a": "b"}}}
    first = _state(tmp_path)
    first.checkpoint("case-1", "run-1", "mapped", payload, "fp-1")
    del first                                    # simulate process exit
    second = _state(tmp_path)                    # fresh connections, same paths
    assert second.resume("case-1", "run-1", "fp-1") == payload
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        second.resume("case-1", "run-1", "fp-other")
    assert second.resume("case-1", "run-unknown", "fp-1") is None
    assert _state(tmp_path, scope="tenant-b").resume("case-1", "run-1", "fp-1") is None   # scoped


def test_latest_checkpoint_wins_and_records_are_append_only(tmp_path):
    st = _state(tmp_path)
    st.checkpoint("c", "r", "s1", {"v": 1}, "fp")
    st.checkpoint("c", "r", "s2", {"v": 2}, "fp")
    assert _state(tmp_path).resume("c", "r", "fp") == {"v": 2}
    import sqlite3
    db = sqlite3.connect(tmp_path / "state.sqlite3")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM records WHERE kind='checkpoints'")
    db.close()


def test_case_seam_checkpoints_and_resumes_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv(cp.ENV_DIR, str(tmp_path / "platform"))
    out = tmp_path / "out"
    out.mkdir()
    files = {"data.xlsx": "a" * 64}
    rnd = cs.new_round(None, files)
    rnd.update(status="NEED_INPUT", pending=["Q1"])
    case = {"created": rnd["at"], "rounds": [rnd], "workspace": {"summary": "s"}}
    cs.save_case(out, case)

    # New process: open a brand-new runtime from the same paths.
    rt = cp.PlatformRuntime.open(tmp_path / "platform")
    assert cp.resume_round(out, 1, files, runtime=rt) == case
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        cp.resume_round(out, 1, {"data.xlsx": "b" * 64}, runtime=rt)

    # case.json lost (crash mid-write): load_case falls back to the checkpoint.
    (out / cs.CASE_FILE).unlink()
    assert cs.load_case(out) == case

    # Manifest built from the case index carries ids/status only.
    m = build_context_manifest(rt.state, cp.case_id(out))
    assert m["status"] == "NEED_INPUT" and m["round"] == 1 and m["pending_ids"] == ["Q1"]
    assert "summary" not in json.dumps(m)


def test_case_seam_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv(cp.ENV_DIR, raising=False)
    rnd = cs.new_round(None, {})
    cs.save_case(tmp_path, {"rounds": [rnd]})
    assert sorted(p.name for p in tmp_path.iterdir()) == [cs.CASE_FILE]
    assert cs.load_case(tmp_path / "missing") is None


# ---- cache ------------------------------------------------------------------

def _key(**kw):
    return CacheKey(**{"scenario": "annual_bonus", "rule_package_version": "pending", "input_fingerprint": "i",
                       "engine_version": "e", "code_version": "c", "numeric_profile": "decimal", **kw})


def test_result_cache_unauthorized_miss(tmp_path):
    cache = ResultCache(tmp_path)
    cache.put(_key(), "tenant-a", {"result_ref": "r1"})
    assert list(tmp_path.rglob("*.json"))                       # entry exists on disk
    assert cache.get(_key(), "tenant-a", authorized=False) is None
    assert cache.get(_key(), "tenant-a", authorized=True) == {"result_ref": "r1"}
    assert cache.get(_key(), "tenant-b", authorized=True) is None
    assert cache.get(_key(engine_version="e2"), "tenant-a", authorized=True) is None


def test_result_cache_tampered_entry_misses(tmp_path):
    cache = ResultCache(tmp_path)
    cache.put(_key(), "s", {"x": 1})
    path = next(tmp_path.rglob("*.json"))
    entry = json.loads(path.read_text())
    entry["result"] = {"x": 2}
    path.write_text(json.dumps(entry))
    assert cache.get(_key(), "s", authorized=True) is None


# ---- context manifest / memory ---------------------------------------------

def test_context_manifest_excludes_detail_and_values(tmp_path):
    st = _state(tmp_path)
    secret_row = {"employee": "张三", "amount": "123456.78"}
    st.save_case("c1", {"scenario_id": "annual_bonus", "rows": [secret_row],
                        "rounds": [{"n": 2, "status": "OK", "pending": ["Q9"], "digest": {"s": {"r": {"n1": ["张三", "123456.78"]}}}}]})
    st.checkpoint("c1", "round-2", "case_saved", {"rows": [secret_row]}, "fp")
    d = st.decide("c1", {"answer": "secret-answer-value"})
    rev_ref = st.objects.put_json({"payouts": [secret_row]})
    rev = st.result_revision("c1", rev_ref, {"ok": True}, engine="e")
    Memory(st).episodic("c1").put("note", {"value": "memory-secret"}, summary="short")

    m = build_context_manifest(st, "c1", session_id="sess")
    text = json.dumps(m, ensure_ascii=False)
    for leaked in ("张三", "123456.78", "secret-answer-value", "memory-secret", "rows", "digest", "payload_ref", rev_ref):
        assert leaked not in text
    assert m["decision_ids"] == [d["id"]] and m["result_revision_ids"] == [rev["id"]]
    assert set(m["checkpoint"]) == {"id", "run_id", "step", "fingerprint"}
    assert (m["status"], m["round"], m["pending_ids"], m["scenario_id"]) == ("OK", 2, ["Q9"], "annual_bonus")


def test_memory_layers_are_durable_and_scoped(tmp_path):
    Memory(_state(tmp_path)).long_term("t1").put("alias", {"col": "姓名"})
    assert Memory(_state(tmp_path)).long_term("t1").get("alias") == {"col": "姓名"}
    assert Memory(_state(tmp_path)).long_term("t2").get("alias") is None
    assert Memory(_state(tmp_path, "other")).long_term("t1").get("alias") is None
    with pytest.raises(ValueError):
        Memory(_state(tmp_path)).put("vector", "o", "k", 1)


# ---- skills / MCP allowlists -----------------------------------------------

def _pack(tools=(), mcp=()):
    base = load_pack("annual_bonus")
    meta = base.metadata.model_copy(update={"allowed_tools": list(tools), "allowed_mcp": list(mcp)})
    return base.model_copy(update={"metadata": meta})


@pytest.mark.parametrize("scenario", list_scenarios())
def test_existing_packs_default_to_empty_allowlists(scenario):
    meta = load_pack(scenario).metadata
    assert meta.allowed_tools == [] and meta.allowed_mcp == [] and meta.status == "pending_finance"


def test_pack_schema_still_forbids_unknown_fields():
    with pytest.raises(Exception):
        PackSchema.model_validate({"schema_version": 1, "scenario": "x", "status": "pending_finance",
                                   "todo": "TODO", "allow_everything": True})


def test_skill_call_requires_pack_allowlist():
    reg = SkillRegistry()
    reg.register("profile_sheet", lambda path: f"profiled {path}")
    reg.register("list_files", lambda: ["a"])
    for pack in (None, load_pack("annual_bonus"), _pack(tools=["other"])):
        with pytest.raises(PermissionError):
            reg.call("profile_sheet", {"path": "x"}, pack=pack)
        assert reg.call("list_files", {}, pack=pack) == ["a"]      # global read-only probe
    assert reg.call("profile_sheet", {"path": "x"}, pack=_pack(tools=["profile_sheet"])) == "profiled x"
    assert [s.name for s in reg.available(_pack(tools=["profile_sheet"]))] == ["profile_sheet", "list_files"]
    assert allowed("profile_sheet", _pack(tools=["profile_sheet"]).metadata)


def test_mcp_call_requires_exact_server_tool():
    calls = []

    class Transport:
        def call(self, server, tool, arguments):
            calls.append((server, tool))
            return "ok"

    client = MCPClient(Transport())
    for pack in (None, load_pack("annual_bonus"), _pack(mcp=["files"]), _pack(tools=["files/read"])):
        with pytest.raises(PermissionError):
            client.call("files", "read", {}, pack=pack)
    with pytest.raises(PermissionError):
        client.call("files", "list_files", {}, pack=None)          # global skill does not leak into MCP
    assert calls == []
    assert client.call("files", "read", {}, pack=_pack(mcp=["files/read"])) == "ok"
    assert calls == [("files", "read")]
