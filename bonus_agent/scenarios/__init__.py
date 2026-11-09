"""Scenario metadata only. Monetary calculations stay in bonus_core."""
from pathlib import Path
import re

import yaml
from pydantic import ValidationError

from bonus_agent.scenarios.approval import (
    APPROVAL_FILE,
    STATEMENT,
    ProductSettlementApproval,
    record_product_approval,
    resolve_product_approval,
)
from bonus_agent.scenarios.binding import BindingError, validate_binding
from bonus_agent.scenarios.schema import (
    MigratedInputContract,
    MigratedOutputContract,
    MigratedPack,
    MigratedRulePackage,
    MigratedValidationSuite,
    PackSchema,
    PendingPack,
    Placeholder,
    ScenarioPack,
    parse_pack_metadata,
)

PACKS = Path(__file__).parent / "packs"
COMPONENTS = ("input_contract", "rule_package", "validation_suite", "output_contract")
_MIGRATED = {
    "input_contract": MigratedInputContract,
    "rule_package": MigratedRulePackage,
    "validation_suite": MigratedValidationSuite,
    "output_contract": MigratedOutputContract,
}


class ScenarioError(ValueError):
    pass


def _read(path: Path) -> dict:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ScenarioError(f"{path}: expected YAML mapping")
        return value
    except (OSError, yaml.YAMLError) as exc:
        raise ScenarioError(f"Cannot load {path}: {exc}") from exc


def load_pack(scenario: str | None, *, root: Path = PACKS) -> ScenarioPack:
    """Validate a pack envelope. Never infer a scenario from materials."""
    if not isinstance(scenario, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", scenario):
        raise ScenarioError("Explicit scenario required (no universal-auto)")
    directory = Path(root) / scenario
    try:
        metadata = parse_pack_metadata(_read(directory / "pack.yaml"))
        if metadata.scenario != scenario:
            raise ScenarioError("Pack scenario must match its directory")
        model = _MIGRATED if isinstance(metadata, MigratedPack) else {name: Placeholder for name in COMPONENTS}
        components = {}
        for name in COMPONENTS:
            document = _read(directory / f"{name}.yaml")
            if document.get("status") != metadata.status:
                raise ScenarioError(f"Invalid scenario pack {scenario}: {name} status must match the pack")
            components[name] = model[name].model_validate(document)
        pack = ScenarioPack(metadata=metadata, **components)
    except (ValidationError, BindingError) as exc:
        raise ScenarioError(f"Invalid scenario pack {scenario}: {exc}") from exc
    if isinstance(pack.metadata, MigratedPack):
        try:
            validate_binding(pack)
        except (BindingError, ValidationError) as exc:
            raise ScenarioError(f"Invalid scenario pack {scenario}: {exc}") from exc
    return pack


def list_scenarios() -> list[str]:
    return sorted(p.name for p in PACKS.iterdir() if p.is_dir() and (p / "pack.yaml").is_file())


def scenario_metadata(scenario):
    """Registration only: published releases do not depend on migration fixtures."""
    if not isinstance(scenario, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", scenario):
        raise ScenarioError("Explicit registered scenario required")
    try:
        metadata = parse_pack_metadata(_read(PACKS / scenario / "pack.yaml"))
        if metadata.scenario != scenario:
            raise ScenarioError("Scenario registration mismatch")
        return metadata
    except (ValueError, OSError) as exc:
        raise ScenarioError(f"Invalid scenario registration: {exc}") from exc


def require_settlement(scenario: str | None, approval=None, *, out=None) -> ScenarioPack:
    """Formal settlement gate.

    Exploration and trial do not call this. ``pending_finance`` stays closed.
    ``migrated_draft`` opens only for a product written approval of the current
    binding. That approval is not a finance signature, and a later finance-mapped
    rule package replaces the migrated instance.
    """
    pack = load_pack(scenario)
    if isinstance(pack.metadata, PendingPack) or pack.metadata.status == "pending_finance":
        raise ScenarioError(f"{pack.metadata.scenario}: pending_finance; published rules missing (TODO)")
    if not isinstance(pack.metadata, MigratedPack):
        raise ScenarioError(f"{pack.metadata.scenario}: unsupported pack status")
    try:
        record = resolve_product_approval(pack, approval, out=out)
    except BindingError as exc:
        raise ScenarioError(str(exc)) from exc
    if record is None:
        raise ScenarioError(
            f"{pack.metadata.scenario}: migrated_draft; product written approval required "
            f"({APPROVAL_FILE} or structured state). Exploration and trial do not use this gate. "
            "This pack is not finance-signed."
        )
    return pack


__all__ = [
    "APPROVAL_FILE",
    "COMPONENTS",
    "PACKS",
    "STATEMENT",
    "MigratedPack",
    "PackSchema",
    "PendingPack",
    "Placeholder",
    "ProductSettlementApproval",
    "ScenarioError",
    "ScenarioPack",
    "list_scenarios",
    "load_pack",
    "record_product_approval",
    "require_settlement",
    "resolve_product_approval",
]
