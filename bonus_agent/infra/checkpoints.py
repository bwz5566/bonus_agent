"""Case/run checkpoint seam: mirrors each saved case round into durable platform state.

Opt-in: enabled when ``BONUS_AGENT_STATE_DIR`` is set (or a runtime is passed explicitly),
so existing case directories behave exactly as before. case.json stays the primary
artifact; this adds a crash-safe, content-addressed copy that a new process can resume.

- case_id = fingerprint of the resolved case (output) directory
- run_id  = "round-<n>"
- fingerprint = fingerprint of that round's input file sha256 map
Payloads live in the ObjectStore; StateStore only holds IDs, status and refs.
Nothing here computes or alters money; it only stores what the engine already produced.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from bonus_agent.infra.state import TaskState
from bonus_agent.infra.storage import ObjectStore, SQLiteStateStore, fingerprint

ENV_DIR, ENV_SCOPE = "BONUS_AGENT_STATE_DIR", "BONUS_AGENT_STATE_SCOPE"
DEFAULT_SCOPE = "local"


@dataclass
class PlatformRuntime:
    state: TaskState

    @classmethod
    def open(cls, root: str | Path, scope: str = DEFAULT_SCOPE) -> "PlatformRuntime":
        root = Path(root)
        return cls(TaskState(SQLiteStateStore(root / "state.sqlite3"), ObjectStore(root / "objects"), scope))

    @classmethod
    def from_env(cls) -> "PlatformRuntime | None":
        root = os.environ.get(ENV_DIR)
        return cls.open(root, os.environ.get(ENV_SCOPE) or DEFAULT_SCOPE) if root else None


def case_id(out: str | Path) -> str:
    return fingerprint(str(Path(out).resolve()))


def run_id(n: int) -> str:
    return f"round-{int(n)}"


def round_fingerprint(files: dict[str, str]) -> str:
    return fingerprint(dict(files or {}))


def save_round(out: str | Path, case: dict, runtime: PlatformRuntime | None = None) -> dict | None:
    """Checkpoint the latest round of ``case``. Returns the checkpoint record (or None if disabled)."""
    rt = runtime or PlatformRuntime.from_env()
    if rt is None or not case or not case.get("rounds"):
        return None
    cid, rnd = case_id(out), case["rounds"][-1]
    rec = rt.state.checkpoint(cid, run_id(rnd["n"]), "case_saved", case, round_fingerprint(rnd.get("files")))
    # Case index: IDs/status/pointers only (what context_manifest reads), never workspace detail.
    rt.state.save_case(cid, {
        "case_id": cid, "scenario_id": case.get("scenario_id") or case.get("scenario"),
        "out": str(Path(out).resolve()), "checkpoint_id": rec["id"], "case_ref": rec["payload_ref"],
        "rounds": [{"n": r.get("n"), "status": r.get("status"), "pending": list(r.get("pending") or [])}
                   for r in case["rounds"]],
    })
    return rec


def resume_round(out: str | Path, n: int, files: dict[str, str], runtime: PlatformRuntime | None = None) -> dict | None:
    """Reload the case saved at round ``n``; raises ValueError if the input fingerprint differs."""
    rt = runtime or PlatformRuntime.from_env()
    if rt is None:
        return None
    return rt.state.resume(case_id(out), run_id(n), round_fingerprint(files))


def latest_case(out: str | Path, runtime: PlatformRuntime | None = None) -> dict | None:
    """Most recent checkpointed case for this directory (used when case.json is missing)."""
    rt = runtime or PlatformRuntime.from_env()
    if rt is None:
        return None
    idx = rt.state.load_case(case_id(out))
    return rt.state.objects.get_json(idx["case_ref"]) if idx else None
