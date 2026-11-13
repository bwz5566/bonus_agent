"""Isolated, bounded repairs. Validation evidence is frozen outside model-controlled parts."""
from __future__ import annotations

import copy
import hashlib
import json

LIMIT = 6
ALLOWED = {"tables", "variables", "pool", "allocation", "payout", "offsets", "report"}


def _base(ws):
    payload = [ws.validation_fingerprint(), [f.model_dump(mode='json') for f in ws.facts],
               [c.model_dump(mode='json') for c in ws.clauses], ws.reread, ws.verified]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _clone(ws, snapshot):
    from bonus_agent.agent import Workspace
    from bonus_agent import case
    candidate = Workspace(ws.llm, ws.inv)
    snap = copy.deepcopy({k: v for k, v in snapshot.items() if k != "repairs"})
    case.restore(candidate, snap)
    candidate.period, candidate.report_required, candidate.said = ws.period, ws.report_required, ws.said
    candidate.repair_regressions = ws.repair_regressions
    candidate.round = ws.round
    for scheme in candidate._order(candidate.schemes):
        candidate._trial(scheme)
    return candidate


def _report(ws, candidate):
    """Holdouts are evaluated by trusted code; their answers are not returned to the repair model."""
    from bonus_agent import case
    from bonus_agent.agent import Workspace
    from bonus_agent.auto import build_inventory
    from bonus_agent.compare import compare_expected
    from bonus_agent.llm import FakeLLM
    from bonus_core.data.period import Period
    from bonus_agent.validation import CheckResult, Status
    from pathlib import Path
    report = candidate.validation_report()
    from bonus_agent.agent import _verify_key
    prior_errors = []
    for scheme, revs in ws.results.items():
        nodes = set(revs['R0'].nodes) | {r.node_id for r in revs['R0'].payout_rows()}
        for node in nodes:
            for amount in ('应得', '实发'):
                reference = ws.verified.get(_verify_key(scheme, node, amount))
                if reference:
                    try:
                        error = candidate._verify_mismatch(scheme, node, reference)
                    except Exception as e:
                        error = str(e)
                    if error:
                        prior_errors.append({'scheme':scheme, 'node':node, 'amount':amount, 'error':error})
    frozen = CheckResult('repair.frozen_references', Status.FAIL if prior_errors else Status.PASS,
                         '原有验算不会因抽样范围改变或候选重写而失效', examples=prior_errors)
    report['checks'].append(frozen.to_dict())
    if prior_errors:
        report['status']='FAIL'
    failures = []
    for test in ws.repair_regressions:
        try:
            ref = Workspace(FakeLLM([]), build_inventory([Path(p) for p in test['files']]))
            case.restore(ref, case.snapshot(candidate))
            ref.period = Period.parse(test.get('period')) or ws.period
            for scheme in ref._order(ref.schemes):
                ref._trial(scheme)
            matches = compare_expected(ref.results, test['expected'])
            if not any(m.total for m in matches) or not all(m.ok for m in matches if m.total or m.ambiguous):
                failures.append({'case':test['id'], 'status':'FAIL'})
        except Exception:
            failures.append({'case':test['id'], 'status':'ERROR'})
    status = Status.FAIL if failures else Status.PASS if ws.repair_regressions else Status.NOT_APPLICABLE
    check = CheckResult('repair.holdouts', status, '冻结回归；参考答案不交给修复过程',
                        len(ws.repair_regressions), len(ws.repair_regressions)-len(failures), failures)
    report['checks'].append(check.to_dict())
    if not check.ok:
        report['status'] = 'INCOMPLETE'
    return report


def preview(ws, scheme, parts, reason):
    from bonus_agent.agent import ToolError
    from bonus_agent import case
    ws._check_source_version()
    if not reason.strip() or set(parts)-ALLOWED:
        raise ToolError("修复需说明原文依据；不能更改输入、答案、参数台账、校验器、容差或忽略项")
    if "patches" in parts or "checks" in parts:
        raise ToolError("自动修复不能修改数据修订或删除/放宽原有校验；需要单独的证据审查")
    stamp = _base(ws)
    budget_key = ws.validation_fingerprint()
    attempts = ws.repair_attempts.get(budget_key, 0)
    if attempts >= LIMIT:
        raise ToolError("本版本的隔离修复已达到预算；请核对证据或向业务提问，不能继续盲目重试")
    ws.repair_attempts[budget_key] = attempts + 1
    candidate = _clone(ws, case.snapshot(ws))
    old_report = candidate._merged(scheme).get("report") or {}
    if "report" in parts:
        for field in ('balances', 'distributions', 'pending', 'details'):
            proposed = parts['report'].get(field, [])
            if any(item not in proposed for item in old_report.get(field, [])):
                raise ToolError("自动修复不能删除或更改已冻结的对账契约及待分配去向")
    # Keep original checks and references frozen. New numeric constants still need exact source bindings.
    candidate.set_rules(scheme=scheme, **copy.deepcopy(parts))
    for name in candidate._order(candidate.schemes):
        candidate._trial(name)
    report = _report(ws, candidate)
    ident = hashlib.sha256(json.dumps([stamp, scheme, parts, attempts], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    ws.repairs[ident] = {"base": stamp, "reason": reason, "scheme": scheme,
                         "snapshot": case.snapshot(candidate), "report": report, "status": "proposed"}
    return {"candidate": ident, "report": report, "changes": {
        name: {"before": ws.results.get(name, {}).get("R0").pool if ws.results.get(name, {}).get("R0") else None,
               "after": revs["R0"].pool} for name, revs in candidate.results.items()}}


def verify(ws, candidate_id, **args):
    from bonus_agent import case
    from bonus_agent.agent import ToolError
    record = ws.repairs[candidate_id]
    if record["base"] != _base(ws):
        raise ToolError("原工作区或材料已变化，修复候选失效")
    candidate = _clone(ws, record["snapshot"])
    result = candidate.verify(**args)
    record["snapshot"] = case.snapshot(candidate)
    record["report"] = _report(ws, candidate)
    return {"verification": result, "report": record["report"]}


def apply(ws, candidate_id):
    from bonus_agent import case, reread
    from bonus_agent.agent import ToolError
    record = ws.repairs[candidate_id]
    if record["base"] != _base(ws) or record["status"] != "proposed":
        raise ToolError("修复候选失效或已经使用")
    candidate = _clone(ws, record["snapshot"])
    report = _report(ws, candidate)
    semantic = reread.compare(candidate, candidate.reread)
    gates = [g for rep in candidate.lint.values() for g in rep.gates]
    if report["status"] != "PASS" or semantic or gates or candidate.pending:
        raise ToolError("修复尚未通过冻结的验算、证据或程序检查，不能应用：" + json.dumps({"report": report, "semantic": semantic, "gates": gates}, ensure_ascii=False))
    # Apply only after a fresh computation and a full recheck. No publication or source file writes.
    history, attempts = ws.repairs, ws.repair_attempts
    case.restore(ws, case.snapshot(candidate))
    ws.repairs = history
    ws.repair_attempts = attempts
    ws.results, ws.untested = candidate.results, set()
    ws.lint, ws.read_nums = candidate.lint, candidate.read_nums
    ws._trial_versions, ws._trial_details = candidate._trial_versions, candidate._trial_details
    ws.done = False
    record["status"] = "case_validated"
    record["report"] = report
    ws.notes[f"修复经验 {candidate_id}"] = ("仅本案验证通过，不能作为其他业务默认口径。依据：" + record["reason"]
                                           + "；跨案复用时重新核对来源、适用条件和全部检查。")
    return {"candidate": candidate_id, "status": "case_validated", "report": report}
