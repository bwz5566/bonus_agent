"""Allowlist projection: never traverse workspace, result or memory detail values."""

def build_context_manifest(state, case_id, *, session_id=None):
    case = state.load_case(case_id) or {}
    rounds = case.get("rounds") or []
    last = rounds[-1] if rounds else {}
    checkpoints = [r for r in state.store.list("checkpoints", state.scope) if r["case_id"] == case_id]
    decisions = [r for r in state.store.list("decisions", state.scope) if r["case_id"] == case_id]
    results = [r for r in state.store.list("result_revisions", state.scope) if r["case_id"] == case_id]
    manifest = {"case_id": case_id, "session_id": session_id, "scenario_id": case.get("scenario_id"),
                "status": last.get("status", "not_started"), "round": last.get("n", 0),
                "pending_ids": list(last.get("pending", [])),
                "decision_ids": [r["id"] for r in decisions],
                "result_revision_ids": [r["id"] for r in results],
                "checkpoint": ({k: checkpoints[-1][k] for k in ("id", "run_id", "step", "fingerprint")} if checkpoints else None)}
    return manifest
