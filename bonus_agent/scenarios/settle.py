"""Formal settlement of a migrated pack. Amounts come only from bonus_core."""
from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

import yaml

from bonus_agent.scenarios.approval import resolve_product_approval
from bonus_agent.scenarios.binding import compose
from bonus_agent.scenarios.schema import MigratedPack
from bonus_core.data.mapping import DatasetMapping, apply_mapping
from bonus_core.engine.issues import EngineError
from bonus_core.engine.ledger import ledger_entries
from bonus_core.engine.run import Adjustment, Engine
from bonus_core.report.excel import write_excel


class SettlementError(ValueError):
    pass


def _require(scenario: str, approval, out):
    from bonus_agent.scenarios import ScenarioError, require_settlement
    try:
        pack = require_settlement(scenario, approval, out=out)
    except ScenarioError as exc:
        raise SettlementError(str(exc)) from exc
    if not isinstance(pack.metadata, MigratedPack):
        raise SettlementError(f"{scenario}: formal pack settlement requires migrated_draft")
    return pack


def settle_bound(scenario: str, data_files: list[str | Path], out: str | Path, approval=None, *,
                 case_out: str | Path | None = None) -> dict:
    """Run the migrated binding. ``case_out`` is where a product approval file may live."""
    pack = _require(scenario, approval, case_out)
    bound = compose(pack)
    mapping = {name: DatasetMapping.model_validate(spec) for name, spec in bound.mapping.items()}
    data, issues = apply_mapping(bound.ruleset, list(data_files), mapping)
    blocking = [item for item in issues if item.severity == "blocking"]
    if blocking:
        raise EngineError(blocking)
    engine = Engine(bound.ruleset, data, {})
    engine.issues += issues
    results = {"R0": engine.compute()}
    applied: list[Adjustment] = []
    for revision in bound.revisions:
        applied += [Adjustment(item["node"], Decimal(str(item["amount"])), reason=item.get("reason", ""),
                               created_at=f"{len(applied) + index:06d}")
                    for index, item in enumerate(revision.get("adjust") or [])]
        results[revision["name"]] = engine.compute(list(applied))
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    written = []
    for name, result in results.items():
        path = target if name == "R0" else target.with_name(f"{target.stem}_{name}{target.suffix}")
        write_excel(bound.ruleset, result, path, ledger_entries(result))
        written.append(str(path))
    approval_record = resolve_product_approval(pack, approval, out=case_out)
    sidecar = {
        "scenario": scenario,
        "pack_status": "migrated_draft",
        "finance_signed": False,
        "engine": "bonus_core",
        "binding_fingerprint": bound.fingerprint,
        "rule_authority": "migrated_in_repo_binding",
        "outputs": written,
        "decision_id": None if not approval_record else approval_record.get("decision_id"),
    }
    target.with_suffix(".settlement.json").write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


def materialize_template(scenario: str, dest: str | Path, approval) -> Path:
    """Write a bonus_core template that repeats the migrated binding."""
    from bonus_agent.scenarios.binding import _template_adjustments
    from bonus_core.template import sha256

    pack = _require(scenario, approval, None)
    bound = compose(pack)
    record = approval if isinstance(approval, dict) else json.loads(Path(approval).read_text(encoding="utf-8"))
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "rules.yaml").write_text(bound.ruleset.dump_yaml(), encoding="utf-8")
    (dest / "mapping.yaml").write_text(yaml.safe_dump(bound.mapping, allow_unicode=True, sort_keys=False), encoding="utf-8")
    revisions = []
    for revision in bound.revisions:
        filename = f"adjust_{revision['name']}.yaml"
        (dest / filename).write_text(yaml.safe_dump(_template_adjustments(revision), allow_unicode=True, sort_keys=False),
                                     encoding="utf-8")
        revisions.append({"name": revision["name"], "adjust": filename})
    files = ["rules.yaml", "mapping.yaml", *[item["adjust"] for item in revisions]]
    manifest = {
        "scenario": scenario,
        "product_approval": record,
        "binding_fingerprint": bound.fingerprint,
        "finance_signed": False,
        "rule_authority": "migrated_in_repo_binding",
        "confirmed": {
            "by": record["approver"],
            "at": record["approved_at"],
            "round": 0,
            "note": "migrated_draft product approval; not finance-signed",
            "summary": "",
        },
        "mapping": "mapping.yaml",
        "schemes": [{"name": pack.rule_package.primary_scheme, "rules": "rules.yaml", "revisions": revisions}],
        "files": {name: sha256(dest / name) for name in files},
        "data": {},
    }
    (dest / "manifest.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return dest
