"""Bind existing YAML instances into a scenario pack.

This module selects and validates repository files. It does not calculate money.
`bonus_core` remains the only engine that produces amounts.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from bonus_agent.infra.storage import fingerprint
from bonus_agent.scenarios.schema import MigratedPack, ScenarioPack
from bonus_core.dsl.models import RuleSet

ROOT = Path(__file__).resolve().parents[2]

# Symbols that belong to annual or process schemes. Quarter weight may still
# read project difficulty; that field is not a process-scheme binding.
_EXCLUDED_TEXT = (
    "历史已发", "里程碑", "perf_fy", "fy_sales", "fy_cost", "fy_net", "fy_recv", "fy_eff",
    "d_eff_fy", "d_sales_fy", "d_net_fy", "d_coef_fy", "g_w_fy", "attrib_fy",
    "attrib_recv_fy", "e_w_fy", "scheme_paid", "R_A_TIER", "R_DIFF",
)
_EXCLUDED_SCHEME_NAMES = ("PROCESS", "ANNUAL")


class BindingError(ValueError):
    pass


@dataclass(frozen=True)
class BoundInstance:
    ruleset: RuleSet
    revisions: list[dict]
    mapping: dict
    fingerprint: str


def _load_yaml(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BindingError(f"{path}: expected a YAML mapping")
    return value


def _repo_path(relative: str) -> Path:
    path = ROOT / relative
    if not path.is_file():
        raise BindingError(f"Missing migration origin: {relative}")
    return path


def _dataset_subset(source: dict, fields: list[str]) -> dict:
    missing = [name for name in fields if name not in source.get("fields", {})]
    if missing:
        raise BindingError(f"Contract field is not in the origin dictionary: {missing}")
    out = {}
    for key in ("desc", "key", "link"):
        if source.get(key) not in (None, ""):
            out[key] = source[key]
    for key in ("distinct", "unmatched"):
        if key in source:
            out[key] = source[key]
    out["fields"] = {name: source["fields"][name] for name in fields}
    return out


def _mapping_subset(source: dict, fields: list[str]) -> dict:
    columns = source.get("columns") or {}
    missing = [name for name in fields if name not in columns]
    if missing:
        raise BindingError(f"Origin mapping has no column for contract field: {missing}")
    entry = {"columns": {name: columns[name] for name in fields}}
    for key in ("file", "sheet", "filter"):
        if source.get(key) not in (None, {}, ""):
            entry[key] = source[key]
    return entry


def _check_by_id(checks: list) -> dict[str, dict]:
    found = {}
    for item in checks:
        if not isinstance(item, dict) or "id" not in item:
            raise BindingError("Shared checks must be mappings with an id")
        found[item["id"]] = item
    return found


def _reject_excluded_text(payload: str) -> None:
    for token in _EXCLUDED_TEXT:
        if token in payload:
            raise BindingError(f"Quarter binding selects an excluded symbol: {token}")


def binding_fingerprint(pack: ScenarioPack) -> str:
    if not isinstance(pack.metadata, MigratedPack):
        raise BindingError("Fingerprint is only defined for migrated_draft")
    rules = pack.rule_package
    contract = pack.input_contract
    files = [
        rules.scheme.path,
        rules.shared_library.path,
        contract.provenance,
        contract.mapping_provenance,
    ]
    return fingerprint({
        "scenario": pack.metadata.scenario,
        "primary_scheme": rules.primary_scheme,
        "files": {relative: _repo_path(relative).read_bytes().hex() for relative in files},
        "tables": list(rules.shared_library.tables),
        "variables": list(rules.shared_library.variables),
        "checks": list(rules.shared_library.checks),
        "datasets": {name: list(fields) for name, fields in contract.datasets.items()},
        "excluded_datasets": list(contract.excluded_datasets),
        "excluded_fields": {name: list(fields) for name, fields in contract.excluded_fields.items()},
        "excluded_schemes": list(rules.excluded_schemes),
        "finance_overlay": None,
    })


def validate_binding(pack: ScenarioPack) -> BoundInstance:
    """Fail closed when the pack disagrees with the repository files it names."""
    if not isinstance(pack.metadata, MigratedPack):
        raise BindingError("Not a migrated pack")
    if pack.metadata.finance_signed or pack.rule_package.finance_signed:
        raise BindingError("migrated_draft cannot be finance-signed")
    if pack.metadata.migration.primary_scheme != pack.rule_package.primary_scheme:
        raise BindingError("Pack and rule package name different primary schemes")
    if pack.metadata.migration.override_policy != pack.rule_package.override_policy:
        raise BindingError("Override policy mismatch")
    origins = {item.path: item.role for item in pack.metadata.migration.origins}
    rules, contract = pack.rule_package, pack.input_contract
    expected = {
        rules.scheme.path: "scheme",
        rules.shared_library.path: "shared_library",
        contract.provenance: "datasets",
        contract.mapping_provenance: "mapping",
    }
    for path, role in expected.items():
        if origins.get(path) != role:
            raise BindingError(f"Migration origins do not record {path} as {role}")
    for path in rules.excluded_schemes:
        if path in expected or path == rules.scheme.path:
            raise BindingError(f"Excluded scheme is also bound: {path}")
        if Path(path).name.upper().startswith("RULES_QUARTER"):
            raise BindingError("The quarter scheme cannot be listed as excluded")
        _repo_path(path)
    for item in rules.separate_pack_todo:
        if item.pack_id == pack.metadata.scenario or item.origin == rules.scheme.path:
            raise BindingError("Process award must stay out of this pack")
        if item.origin not in rules.excluded_schemes:
            raise BindingError("Process origin must be an excluded scheme")
        _repo_path(item.origin)
    for case in pack.validation_suite.cases:
        for relative in (case.data, case.mapping, case.reference, case.expected_workbook):
            _repo_path(relative)
        if case.mapping != contract.mapping_provenance:
            raise BindingError("Validation mapping must be the contract mapping origin")
        if case.reference_scheme != rules.primary_scheme:
            raise BindingError("Validation scheme must be the primary scheme")
    for item in pack.validation_suite.unbound_sales_materials:
        _repo_path(item.path)
        if item.path == rules.scheme.path or item.path in {row.path for row in pack.metadata.migration.origins}:
            raise BindingError(f"Unbound material is also a bound origin: {item.path}")
    bound = compose(pack)
    levels = [level.level for level in bound.ruleset.allocation]
    if levels != list(pack.output_contract.levels):
        raise BindingError("Output levels do not match the bound scheme allocation")
    if pack.output_contract.source != "bonus_core.engine.run.Result":
        raise BindingError("Output contract must point at the bonus_core result")
    return bound


def compose(pack: ScenarioPack) -> BoundInstance:
    """Assemble a RuleSet from the named instances. No amounts are computed."""
    rules, contract = pack.rule_package, pack.input_contract
    for name in _EXCLUDED_SCHEME_NAMES:
        if name in Path(rules.scheme.path).name.upper():
            raise BindingError(f"Refusing to bind scheme file for {name}")
    scheme_doc = _load_yaml(_repo_path(rules.scheme.path))
    shared_doc = _load_yaml(_repo_path(rules.shared_library.path))
    datasets_doc = _load_yaml(_repo_path(contract.provenance))
    mapping_doc = _load_yaml(_repo_path(contract.mapping_provenance))
    revisions = list(scheme_doc.pop("revisions", []) or [])
    if not str(scheme_doc.get("name", "")).startswith(rules.primary_scheme):
        raise BindingError("Scheme instance name does not start with the primary scheme")

    known = set(datasets_doc)
    selected = set(contract.datasets)
    excluded = set(contract.excluded_datasets)
    if selected & excluded:
        raise BindingError("A dataset cannot be both selected and excluded")
    if not excluded <= known or not selected <= known:
        raise BindingError("Contract datasets must be a subset of the origin dictionary")
    for name, fields in contract.excluded_fields.items():
        origin_fields = set((datasets_doc.get(name) or {}).get("fields") or {})
        if not set(fields) <= origin_fields:
            raise BindingError(f"Excluded field is not in the origin dictionary: {name}")
        if set(fields) & set(contract.datasets.get(name, [])):
            raise BindingError(f"Excluded field is selected: {name}")

    datasets = {name: _dataset_subset(datasets_doc[name], fields) for name, fields in contract.datasets.items()}
    mapping = {name: _mapping_subset(mapping_doc[name], fields) for name, fields in contract.datasets.items()}

    shared_tables = shared_doc.get("tables") or {}
    shared_variables = shared_doc.get("variables") or {}
    shared_checks = _check_by_id(list(shared_doc.get("checks") or []))
    missing_tables = [name for name in rules.shared_library.tables if name not in shared_tables]
    missing_variables = [name for name in rules.shared_library.variables if name not in shared_variables]
    missing_checks = [name for name in rules.shared_library.checks if name not in shared_checks]
    if missing_tables or missing_variables or missing_checks:
        raise BindingError(
            f"Selection is not in the shared library: tables={missing_tables} "
            f"variables={missing_variables} checks={missing_checks}"
        )
    tables = {name: shared_tables[name] for name in rules.shared_library.tables}
    variables = {name: shared_variables[name] for name in rules.shared_library.variables}
    checks = [shared_checks[name] for name in rules.shared_library.checks]
    tables.update(scheme_doc.get("tables") or {})
    variables.update(scheme_doc.get("variables") or {})
    scheme_doc["datasets"] = datasets
    scheme_doc["tables"] = tables
    scheme_doc["variables"] = variables
    scheme_doc["checks"] = checks
    _reject_excluded_text(yaml.safe_dump(scheme_doc, allow_unicode=True))
    for revision in revisions:
        _reject_excluded_text(yaml.safe_dump(revision, allow_unicode=True))
    ruleset = RuleSet.model_validate(scheme_doc)
    return BoundInstance(ruleset, revisions, mapping, binding_fingerprint(pack))


def _template_adjustments(revision: dict) -> list[dict]:
    """Template files use bonus_core.Adjustment field names."""
    return [{"node_id": item["node"], "amount": item["amount"], "reason": item.get("reason", "")}
            for item in revision.get("adjust") or []]


def assert_template_matches(template_dir: Path, manifest: dict, pack: ScenarioPack) -> None:
    """A formal template may repeat the migrated binding and nothing else."""
    bound = compose(pack)
    schemes = manifest.get("schemes") or []
    if [item.get("name") for item in schemes] != [pack.rule_package.primary_scheme]:
        raise BindingError("Formal template must contain only the migrated primary scheme")
    loaded = RuleSet.load(template_dir / schemes[0]["rules"])
    roundtripped = RuleSet.model_validate(yaml.safe_load(bound.ruleset.dump_yaml()))
    if loaded.model_dump(mode="json") != roundtripped.model_dump(mode="json"):
        raise BindingError("Template rules are not the migrated binding")
    revisions = schemes[0].get("revisions") or []
    if [item.get("name") for item in revisions] != [item.get("name") for item in bound.revisions]:
        raise BindingError("Template revisions do not match the migrated scheme")
    for item, origin in zip(revisions, bound.revisions):
        payload = yaml.safe_load((template_dir / item["adjust"]).read_text(encoding="utf-8"))
        if payload != _template_adjustments(origin):
            raise BindingError("Template adjustment is not the migrated revision")
    mapped = _load_yaml(template_dir / manifest["mapping"])
    if mapped != bound.mapping:
        raise BindingError("Template mapping is not the migrated binding")
