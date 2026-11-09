"""Scoped task state, immutable decisions, checkpoints and engine result pointers."""
from datetime import datetime, timezone
from uuid import uuid4

from bonus_agent.infra.storage import ObjectStore, StateStore


class TaskState:
    def __init__(self, store: StateStore, objects: ObjectStore, scope: str):
        if not scope:
            raise ValueError("Scope required")
        self.store, self.objects, self.scope = store, objects, scope

    def save_session(self, session_id, **values):
        self.store.put("sessions", self.scope, session_id, {"session_id": session_id, **values})

    def save_case(self, case_id, value):
        self.store.put("cases", self.scope, case_id, value)

    def load_case(self, case_id):
        return self.store.get("cases", self.scope, case_id)

    def _append(self, kind, **values):
        rec = {**values, "id": uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat()}
        self.store.put(kind, self.scope, rec["id"], rec, append=True)
        return rec

    def decide(self, case_id, value, *, supersedes=None):
        if supersedes:
            prior = self.store.get("decisions", self.scope, supersedes)
            if not prior or prior["case_id"] != case_id:
                raise ValueError("Decision outside case scope")
        return self._append("decisions", case_id=case_id, value_ref=self.objects.put_json(value), supersedes=supersedes)

    def checkpoint(self, case_id, run_id, step, payload, fingerprint):
        return self._append("checkpoints", case_id=case_id, run_id=run_id, step=step,
                            payload_ref=self.objects.put_json(payload), fingerprint=fingerprint)

    def resume(self, case_id, run_id, fingerprint):
        records = [r for r in self.store.list("checkpoints", self.scope)
                   if r["case_id"] == case_id and r["run_id"] == run_id]
        if not records:
            return None
        latest = records[-1]
        if latest["fingerprint"] != fingerprint:
            raise ValueError("Checkpoint fingerprint mismatch")
        return self.objects.get_json(latest["payload_ref"])

    def result_revision(self, case_id, result_ref, validation, **versions):
        self.objects.get(result_ref)  # never commit a dangling result pointer
        return self._append("result_revisions", case_id=case_id, result_ref=result_ref,
                            validation=validation, versions=versions)
