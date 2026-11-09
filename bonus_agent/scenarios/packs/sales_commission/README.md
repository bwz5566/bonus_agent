# sales_commission

Status: `migrated_draft` / TODO. This pack binds the in-repo quarter scheme. It is not finance-signed and is not `published`.

Finance-mapped rules replace this binding on conflict (`override_policy: finance_mapped_rules_replace_migrated_instances`). `finance_overlay` stays null until that delivery exists.

## What was migrated

Only the quarter primary scheme, by reference to existing files:

| Role | Origin |
| --- | --- |
| Scheme instance | `evals/cases/syn_acceptance/rules/rules_QUARTER.yaml` |
| Shared library selection | Quarter tables, variables, and checks in `evals/cases/syn_acceptance/rules/shared.yaml` |
| Input fields | Subset of `evals/cases/syn_acceptance/datasets.yaml` |
| Column binding | The same subset of `evals/cases/syn_acceptance/mapping.yaml` |
| Golden | `evals/cases/syn_acceptance/reference.json` (`QUARTER`) and `expected/05_期望奖金结果.xlsx` |
| Flow | `evals/cases/syn_acceptance/run_case.py` (orchestration only; amounts still come from `bonus_core`) |

Annual-only symbols in the shared file (`perf_fy` and the full-year variables) are not selected. `历史已发` is not a contract dataset. `rules_ANNUAL.yaml` stays with `annual_bonus`.

Project `difficulty` stays in the quarter contract because the existing quarter personal weight reads it. That does not bind the process scheme.

## Not in this pack

Process award is a separate future pack (`process_bonus`). `rules_PROCESS.yaml` and the `里程碑` dataset are excluded. One pack remains one primary scheme.

These sales files are recorded as unbound and are not merged into the contract:

- `examples/sales_q3/rules.yaml` (illustrative rules with a different column set)
- `evals/cases/sales_q3/case.yaml`
- `evals/cases/acceptance_q3/rules_quarter.yaml` (older partial slice)

## Formal settlement

Exploration and trial (`bonus-agent auto`, agent trial runs) do not pass the settlement gate.

Formal settlement uses the migrated binding only after a product written approval:

1. `bonus-agent approve-settlement --scenario sales_commission --by <name> --approval-out approval.json`
2. When `BONUS_AGENT_STATE_DIR` is set, that command also appends a `product_settlement_approval` decision. Structured state is then the authority; a file alone is not enough, and the file must carry the same `decision_id`.
3. `bonus run --scenario sales_commission --from-pack --product-approval approval.json --out result.xlsx <data.xlsx>`

The approval statement is fixed. It cannot set `finance_signed`. The record stores the binding fingerprint, so a later edit of a bound file invalidates it. `--from-pack` refuses alternate rules, mappings, and adjustments. Agent-round `confirm` still cannot freeze an exploratory draft as formal rules.

The web case page records the same approval into `out/product_approval.json` and can run the bound engine on uploaded data. That run is not a finance publication.
