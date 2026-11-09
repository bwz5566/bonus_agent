"""Product written approval for formal settlement of a migrated pack.

Structured state is the authority when ``BONUS_AGENT_STATE_DIR`` is set.
Otherwise the case directory's ``product_approval.json`` is the record.
Neither record means finance has signed the pack.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from bonus_agent.infra.checkpoints import PlatformRuntime
from bonus_agent.scenarios.binding import BindingError, binding_fingerprint
from bonus_agent.scenarios.schema import MigratedPack, ScenarioPack

APPROVAL_FILE = "product_approval.json"
STATEMENT = (
    "Approve formal settlement using the migrated in-repo rules for this scenario. "
    "This is a product written approval, not a finance signature."
)
_DECISION_PREFIX = "scenario:"


class ProductSettlementApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["product_settlement_approval"]
    scenario: str
    pack_status: Literal["migrated_draft"]
    finance_signed: Literal[False]
    scope: Literal["formal_settlement"]
    statement: str
    approver: str
    approved_at: str
    binding_fingerprint: str
    decision_id: str | None = None

    @field_validator("statement")
    @classmethod
    def _statement(cls, value: str) -> str:
        if value != STATEMENT:
            raise ValueError("statement must be the product approval sentence")
        return value

    @field_validator("approver")
    @classmethod
    def _approver(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("approver required")
        return value.strip()

    @field_validator("approved_at")
    @classmethod
    def _when(cls, value: str) -> str:
        datetime.fromisoformat(value)
        return value

    @field_validator("binding_fingerprint")
    @classmethod
    def _fingerprint(cls, value: str) -> str:
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("binding fingerprint must be a sha256 hex digest")
        return value


def _body(record: dict) -> dict:
    return {key: value for key, value in record.items() if key != "decision_id"}


def _read_record(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BindingError(f"Cannot read product approval {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BindingError(f"{path}: expected a JSON object")
    return value


def _coerce(approval: dict | str | Path | None) -> dict | None:
    if approval is None:
        return None
    if isinstance(approval, (str, Path)):
        return _read_record(Path(approval))
    if isinstance(approval, dict):
        return dict(approval)
    raise BindingError("Product approval must be a record or a path")


def _case_id(scenario: str) -> str:
    return f"{_DECISION_PREFIX}{scenario}"


def _latest_state(runtime: PlatformRuntime, scenario: str, digest: str) -> tuple[dict, dict] | None:
    matched = []
    for row in runtime.state.store.list("decisions", runtime.state.scope):
        if row.get("case_id") != _case_id(scenario):
            continue
        value = runtime.state.objects.get_json(row["value_ref"])
        if value.get("kind") == "product_settlement_approval" and value.get("binding_fingerprint") == digest:
            matched.append((row, value))
    return matched[-1] if matched else None


def _validate(pack: ScenarioPack, record: dict) -> dict:
    if not isinstance(pack.metadata, MigratedPack):
        raise BindingError(f"{pack.metadata.scenario}: product approval does not apply to {pack.metadata.status}")
    try:
        parsed = ProductSettlementApproval.model_validate(record)
    except ValidationError as exc:
        raise BindingError(f"Invalid product approval: {exc}") from exc
    if parsed.scenario != pack.metadata.scenario:
        raise BindingError("Product approval scenario does not match the pack")
    digest = binding_fingerprint(pack)
    if parsed.binding_fingerprint != digest:
        raise BindingError("Product approval does not match the current migrated binding")
    if parsed.finance_signed:
        raise BindingError("Product approval cannot mark the pack finance-signed")
    return parsed.model_dump()


def resolve_product_approval(pack: ScenarioPack, approval: dict | str | Path | None = None, *,
                             out: str | Path | None = None, runtime: PlatformRuntime | None = None) -> dict | None:
    """Return the approval that authorizes this binding, or None when it is absent."""
    if not isinstance(pack.metadata, MigratedPack):
        return None
    supplied = _coerce(approval)
    file_record = None
    if out is not None:
        path = Path(out) / APPROVAL_FILE
        if path.is_file():
            file_record = _read_record(path)
    if supplied is not None and file_record is not None and _body(supplied) != _body(file_record):
        raise BindingError("Product approval file does not match the supplied record")
    chosen = supplied or file_record
    active = runtime if runtime is not None else PlatformRuntime.from_env()
    digest = binding_fingerprint(pack)
    if active is None:
        return _validate(pack, chosen) if chosen else None
    state = _latest_state(active, pack.metadata.scenario, digest)
    if state is None:
        return None
    row, value = state
    if chosen is not None:
        if chosen.get("decision_id") != row["id"] or _body(chosen) != _body(value):
            raise BindingError("Product approval file does not match structured state")
    merged = dict(value)
    merged["decision_id"] = row["id"]
    return _validate(pack, merged)


def record_product_approval(scenario: str, approver: str, *, out: str | Path | None = None,
                            approval_out: str | Path | None = None,
                            runtime: PlatformRuntime | None = None) -> dict:
    from bonus_agent.scenarios import load_pack

    pack = load_pack(scenario)
    if not isinstance(pack.metadata, MigratedPack):
        raise BindingError(f"{scenario}: product approval applies only to migrated_draft packs")
    active = runtime if runtime is not None else PlatformRuntime.from_env()
    record = {
        "kind": "product_settlement_approval",
        "scenario": scenario,
        "pack_status": "migrated_draft",
        "finance_signed": False,
        "scope": "formal_settlement",
        "statement": STATEMENT,
        "approver": approver.strip(),
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "binding_fingerprint": binding_fingerprint(pack),
    }
    if active is not None:
        decision = active.state.decide(_case_id(scenario), record)
        record["decision_id"] = decision["id"]
    validated = _validate(pack, record)
    text = json.dumps(validated, ensure_ascii=False, indent=2)
    if out is not None:
        path = Path(out) / APPROVAL_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if approval_out is not None:
        path = Path(approval_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return validated
