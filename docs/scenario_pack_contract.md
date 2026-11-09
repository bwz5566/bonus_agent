# Scenario pack contract

Owner constraint (2026-11-06), aligned with section 0 of
[scenario_refactor_plan.md](scenario_refactor_plan.md): finance supplies real
business rules. This repository may bind rules that already exist in the repo,
but it must not invent finance rules or mark a pack `published`.

Packs live in `bonus_agent/scenarios/packs/<scenario>/`. The identifiers are
`sales_commission`, `eng_dev_bonus`, and `annual_bonus`. There is no fourth
commission pack, and process award is not folded into `sales_commission`.

## Statuses

`bonus_agent.scenarios.load_pack` validates every YAML mapping with strict
Pydantic envelopes (`extra=forbid`). Missing files, unknown fields, a wrong
schema version, a mismatched identifier, and an unknown status fail closed.

`pending_finance` is the placeholder envelope:

- `pack.yaml`: `schema_version: 1`, matching `scenario`, `status: pending_finance`, `todo: TODO`.
- Each component file: only `status: pending_finance` and `todo: TODO`.
- `eng_dev_bonus` stays on this envelope. The repo has no migratable engineering
  or development bonus rule instance. Delivery-bonus fixtures are not this pack.
- `annual_bonus` stays on this envelope. Annual instances keep this pack id and
  were not copied into `sales_commission`.

`migrated_draft` means the pack binds in-repo instances and finance has not
signed them. It is not a side field on `pending_finance`, and it is not
`published`. `finance_signed` is required and must be false. `todo` stays `TODO`.

`sales_commission` is the only `migrated_draft` pack. It binds the quarter
primary scheme. A later finance-mapped rule package replaces that binding on
conflict (`override_policy: finance_mapped_rules_replace_migrated_instances`).
`finance_overlay` stays null until that delivery exists.

## sales_commission binding

The pack does not copy formulas into a second calculator. Shared DSL operators
stay in `bonus_core`. The pack names existing files and the quarter subset to
select:

| Role | Origin |
| --- | --- |
| Scheme | `evals/cases/syn_acceptance/rules/rules_QUARTER.yaml` |
| Shared library | Quarter tables, variables, and checks in `evals/cases/syn_acceptance/rules/shared.yaml` |
| Input contract | Field names that are a subset of `evals/cases/syn_acceptance/datasets.yaml` |
| Mapping | The same subset of `evals/cases/syn_acceptance/mapping.yaml` |
| Golden | `reference.json` section `QUARTER` and `expected/05_期望奖金结果.xlsx` |

Full-year symbols and `历史已发` are not selected. `rules_ANNUAL.yaml` is excluded.
`rules_PROCESS.yaml` and the `里程碑` dataset are excluded. Process award waits
for its own pack (`process_bonus` is a TODO, not a directory). Project
`difficulty` remains because the existing quarter personal weight reads it.

Illustrative `examples/sales_q3` rules and the older `acceptance_q3` quarter
slice are listed as unbound. They are not merged into the contract, so the pack
does not invent their columns.

Changing a bound file changes the binding fingerprint.

## Entry points and settlement boundary

`bonus-agent auto --scenario <id>` loads a pack for exploratory work. Trial
calculation does not call the settlement gate, including when the pack is
`migrated_draft`. Trial output is not finance publication. There is no
universal-auto default.

Formal settlement is `require_settlement`:

- A missing or unknown scenario is rejected.
- `pending_finance` is rejected. Writing another status by hand does not open the gate.
- `migrated_draft` is rejected until a product written approval matches the current binding fingerprint.

Record the approval with:

```text
bonus-agent approve-settlement --scenario sales_commission --by <name> --approval-out approval.json
```

The statement is fixed. It approves formal settlement of the migrated in-repo
rules and says it is not a finance signature. The record stores the approver,
the time, `finance_signed: false`, and the binding fingerprint.

When `BONUS_AGENT_STATE_DIR` is set, the command also appends a
`product_settlement_approval` decision in the platform state store. That
decision is the authority. A file is accepted only when its `decision_id` and
body match the decision. When the state directory is unset, `product_approval.json`
in the case output directory (or the explicit approval file) is the structured
record.

Then:

```text
bonus run --scenario sales_commission --from-pack --product-approval approval.json --out result.xlsx data.xlsx
```

`--from-pack` calculates the migrated binding with `bonus_core` and refuses
alternate rules, mappings, and adjustments. `confirm` still refuses to freeze an
agent round for this pack, so an exploratory draft is not published as finance
rules. `run-template` runs only when the template repeats that same binding and
carries the same approval.

The web case page can record the approval and upload data for the same bound
run. The page states that the run is not a finance signature.
