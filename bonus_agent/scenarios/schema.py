"""Strict scenario-pack envelopes.

`pending_finance` stays a placeholder. `migrated_draft` means the pack binds
in-repo rule instances and is not finance-signed. Unknown fields fail closed.
"""
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Placeholder(_Strict):
    status: Literal["pending_finance"]
    todo: Literal["TODO"]


class OriginRef(_Strict):
    path: str
    role: Literal["scheme", "shared_library", "datasets", "mapping", "golden", "engine_flow"]

    @field_validator("path")
    @classmethod
    def _relative(cls, value: str) -> str:
        if not value or value.startswith(("/", "\\")) or ".." in value.split("/"):
            raise ValueError("origin path must be a relative repository path")
        return value


class MigrationRecord(_Strict):
    source: Literal["in_repo"]
    primary_scheme: Literal["QUARTER"]
    override_policy: Literal["finance_mapped_rules_replace_migrated_instances"]
    origins: list[OriginRef]
    note: str


class PendingPack(_Strict):
    schema_version: Literal[1]
    scenario: str
    status: Literal["pending_finance"]
    todo: Literal["TODO"]
    allowed_tools: list[str] = Field(default_factory=list)
    allowed_mcp: list[str] = Field(default_factory=list)


class MigratedPack(_Strict):
    schema_version: Literal[1]
    scenario: str
    status: Literal["migrated_draft"]
    todo: Literal["TODO"]
    finance_signed: Literal[False]
    allowed_tools: list[str] = Field(default_factory=list)
    allowed_mcp: list[str] = Field(default_factory=list)
    migration: MigrationRecord


class MigratedInputContract(_Strict):
    """Field names are an allowlist. Types are read from the origin dictionary."""
    status: Literal["migrated_draft"]
    todo: Literal["TODO"]
    finance_signed: Literal[False]
    provenance: str
    mapping_provenance: str
    datasets: dict[str, list[str]]
    excluded_datasets: list[str]
    excluded_fields: dict[str, list[str]] = Field(default_factory=dict)


class SharedSelection(_Strict):
    path: str
    tables: list[str]
    variables: list[str]
    checks: list[str]


class SchemeBinding(_Strict):
    path: str


class SeparatePackTodo(_Strict):
    pack_id: Literal["process_bonus"]
    origin: str
    reason: str


class MigratedRulePackage(_Strict):
    status: Literal["migrated_draft"]
    todo: Literal["TODO"]
    finance_signed: Literal[False]
    primary_scheme: Literal["QUARTER"]
    override_policy: Literal["finance_mapped_rules_replace_migrated_instances"]
    finance_overlay: None
    scheme: SchemeBinding
    shared_library: SharedSelection
    excluded_schemes: list[str]
    separate_pack_todo: list[SeparatePackTodo]


class ValidationCase(_Strict):
    id: Literal["syn_acceptance_quarter"]
    data: str
    mapping: str
    reference: str
    reference_scheme: Literal["QUARTER"]
    revisions: list[Literal["R0", "R1", "R2"]]
    expected_workbook: str


class UnboundMaterial(_Strict):
    path: str
    reason: str


class MigratedValidationSuite(_Strict):
    status: Literal["migrated_draft"]
    todo: Literal["TODO"]
    finance_signed: Literal[False]
    engine: Literal["bonus_core"]
    cases: list[ValidationCase]
    unbound_sales_materials: list[UnboundMaterial]


class MigratedOutputContract(_Strict):
    status: Literal["migrated_draft"]
    todo: Literal["TODO"]
    finance_signed: Literal[False]
    source: Literal["bonus_core.engine.run.Result"]
    levels: list[Literal["部门", "小组", "个人"]]
    metrics: list[Literal["final", "cap", "paid", "held", "offset", "overpaid"]]
    revisions_from: Literal["scheme"]


PackMetadata = Annotated[Union[PendingPack, MigratedPack], Field(discriminator="status")]


class ScenarioPack(_Strict):
    metadata: PackMetadata
    input_contract: Placeholder | MigratedInputContract
    rule_package: Placeholder | MigratedRulePackage
    validation_suite: Placeholder | MigratedValidationSuite
    output_contract: Placeholder | MigratedOutputContract


def parse_pack_metadata(data: dict) -> PendingPack | MigratedPack:
    status = data.get("status") if isinstance(data, dict) else None
    model = {"pending_finance": PendingPack, "migrated_draft": MigratedPack}.get(status)
    if model is None:
        # Reuse a strict model so unknown statuses and extra fields fail closed.
        return PendingPack.model_validate(data)
    return model.model_validate(data)


class PackSchema:
    """Backward-compatible loader for pack metadata. Returns a strict model."""

    @staticmethod
    def model_validate(data: dict) -> PendingPack | MigratedPack:
        return parse_pack_metadata(data)
