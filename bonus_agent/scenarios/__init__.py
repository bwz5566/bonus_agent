"""Scenario metadata only; monetary calculations remain in bonus_core."""
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

PACKS = Path(__file__).parent / "packs"
COMPONENTS = ("input_contract", "rule_package", "validation_suite", "output_contract")


class ScenarioError(ValueError):
    pass


class Placeholder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pending_finance"]
    todo: Literal["TODO"]


class PackSchema(Placeholder):
    schema_version: Literal[1]
    scenario: str
    # Capability allowlists (deny by default). Empty = only global read-only skills.
    # MCP entries are exact "server/tool" names; a server name alone grants nothing.
    allowed_tools: list[str] = Field(default_factory=list)
    allowed_mcp: list[str] = Field(default_factory=list)


class ScenarioPack(BaseModel):
    metadata: PackSchema
    input_contract: Placeholder
    rule_package: Placeholder
    validation_suite: Placeholder
    output_contract: Placeholder


def _read(path: Path) -> dict:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ScenarioError(f"{path}: expected YAML mapping")
        return value
    except (OSError, yaml.YAMLError) as exc:
        raise ScenarioError(f"Cannot load {path}: {exc}") from exc


def load_pack(scenario: str | None, *, root: Path = PACKS) -> ScenarioPack:
    """Validate the Phase A envelope, never infer a scenario from materials."""
    import re
    if not isinstance(scenario, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", scenario):
        raise ScenarioError("Explicit scenario required (no universal-auto)")
    directory = Path(root) / scenario
    try:
        metadata = PackSchema.model_validate(_read(directory / "pack.yaml"))
        if metadata.scenario != scenario:
            raise ScenarioError("Pack scenario must match its directory")
        return ScenarioPack(metadata=metadata, **{
            name: Placeholder.model_validate(_read(directory / f"{name}.yaml"))
            for name in COMPONENTS
        })
    except ValidationError as exc:
        raise ScenarioError(f"Invalid scenario pack {scenario}: {exc}") from exc


def list_scenarios() -> list[str]:
    return sorted(p.name for p in PACKS.iterdir() if p.is_dir() and (p / "pack.yaml").is_file())


def require_settlement(scenario: str | None) -> ScenarioPack:
    pack = load_pack(scenario)
    # Phase A deliberately has no published-rule format or activation path.
    # Finance must supply contracts, rules and validation before this gate opens.
    raise ScenarioError(f"{pack.metadata.scenario}: pending_finance; published rules missing (TODO)")
