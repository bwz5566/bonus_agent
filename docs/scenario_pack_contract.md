# Phase A scenario pack contract

Owner constraint (2026-11-06), aligned with section 0 of
[scenario_refactor_plan.md](scenario_refactor_plan.md): finance supplies all real
input formats and business rules later. This phase defines infrastructure only.

Packs live in `bonus_agent/scenarios/packs/<scenario>/`. The initial identifiers
are `sales_commission`, `eng_dev_bonus`, and `annual_bonus`.

Each directory contains:

- `pack.yaml`: `schema_version: 1`, matching `scenario`, `status: pending_finance`, `todo: TODO`.
- `input_contract.yaml`, `rule_package.yaml`, `validation_suite.yaml`, and
  `output_contract.yaml`: only `status: pending_finance` and `todo: TODO`.
- `README.md`: finance handoff notice.

`bonus_agent.scenarios.load_pack` validates all five YAML mappings with strict
Pydantic envelopes. Missing files, unknown fields, unsupported schema versions,
incorrect identifiers, and unsupported statuses fail closed. Pack YAML and READMEs
are included in distribution package data. No columns, rule IDs, numeric profiles,
formulas, or golden business examples are prescribed by this schema.

## Entry points and settlement boundary

`bonus-agent auto --scenario <id> --out <case> <materials...>` selects and loads a
pack explicitly for exploratory work, including `--pipeline`. Existing Python
agent/engine APIs remain available for exploration and tests. Their trial outputs
are not finance publication. Triage may suggest a scenario; it cannot select the
formal path. There is no universal-auto default.

The web case creation form and server require a valid explicit scenario and store
it in `meta.json`. Runner entry validates that selection; legacy cases without it
cannot run. Existing trial calculations remain exploratory.

`bonus-agent confirm --scenario <id>`, `bonus run --scenario <id>`, and
`bonus run-template --scenario <id>` require explicit selection. Confirmation,
web template settlement, and `Template.run` / `Template.write` all call the
settlement gate before producing formal artifacts. Missing scenarios and all
current packs are rejected. `--no-review` cannot bypass this gate.

Phase A intentionally accepts **only pending envelopes**: changing a status to
`published` cannot activate settlement. Published rules and their binding format
are TODO. No rule file is inferred from agent drafts, templates, or arbitrary
material. `bonus_core` remains the sole monetary calculator; no calculation logic
was added to the pack loader.

## Finance handoff (TODO)

Finance supplies the actual input/output contracts, versioned published rules,
validation cases and approval criteria. A later implementation must extend the
schema and gate together, bind rules to the chosen pack and frozen template,
validate the supplied contracts and suite, and then delegate calculation to
`bonus_core`. No production-ready pack or activation mechanism is claimed here.
