"""用真实模型把样例从头做一遍，算完后和期望结果核对（期望结果不会交给智能体）。

    python evals/cases/syn_commission/syn_run.py --model deepseek-flash --out 输出目录 [--checker M2] [--max-tokens N]

智能体停下提问时，统一答复“按场景说明执行”（样例的口径都写在场景说明里），最多答复 --answers 轮。
核对：分配人员金额逐人比对（每人允许几分钱的尾差，合计要一致），汇总核算比对合计。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path

import openpyxl
import yaml

HERE = Path(__file__).resolve().parent
ANSWER = "按场景说明（guide.md）和管理办法的原文执行；两份材料里都写了的以场景说明为准。"
TOL = D("0.05")


def expected():
    wb = openpyxl.load_workbook(HERE / "expected" / "期望结果_202609.xlsx", data_only=True)
    people = {r[0]: D(str(r[1])) for r in list(wb["分配人员"].iter_rows(values_only=True))[1:]}
    totals = {r[0]: D(str(r[1])) for r in list(wb["汇总"].iter_rows(values_only=True))[1:]}
    proj = {r[0]: D(str(r[-1])) for r in list(wb["项目"].iter_rows(values_only=True))[1:]}
    return people, totals, proj


def check(res) -> tuple[bool, list[str]]:
    """res：方案的基准结果（bonus_core.engine.run.Result）。"""
    people, totals, proj = expected()
    lines, ok = [], True
    use_paid = bool(res.payouts)
    got = {p["name"]: D(p["paid"] if use_paid else p["final"]) for p in res.people}
    if not got:
        return False, ["结果里没有分配人员金额（report.people）"]
    bad = [(n, e, got.get(n)) for n, e in people.items() if n not in got or abs(got[n] - e) > TOL]
    extra = [(n, v) for n, v in got.items() if n not in people and v != 0]
    lines.append(f"分配人员：期望 {len(people)} 个，结果 {len(got)} 个，对上 {len(people) - len(bad)} 个")
    for n, e, g in bad[:20]:
        lines.append(f"  {n}：期望 {e}，结果 {g}")
    for n, v in extra[:10]:
        lines.append(f"  多出 {n}：{v}")
    gsum, esum = sum(got.values()), sum(people.values())
    lines.append(f"分配合计：期望 {esum}，结果 {gsum}")
    ok = not bad and not extra and abs(gsum - esum) <= TOL
    # 项目一层：结果里有同样个数的对象且金额对得上时一并报告（编号写法由智能体定，按金额集合比）
    keys = sorted(D(n.final) if not use_paid else D(n.final) for n in res.nodes.values() if n.level not in ("全部",))
    lines.append("汇总核算：" + "；".join(f"{n} {v}" for n, v in res.totals) + "｜期望：" + "；".join(f"{k} {v}" for k, v in totals.items()))
    vals = {D(v) for _, v in res.totals}
    miss = [k for k, v in totals.items() if not any(abs(v - x) <= TOL for x in vals)]
    if miss:
        ok = False
        lines.append(f"  汇总核算里没有对上的：{'、'.join(miss)}")
    return ok, lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--checker")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-tokens", type=int, default=8_000_000)
    ap.add_argument("--max-steps", type=int, default=150)
    ap.add_argument("--answers", type=int, default=3, help="最多答复几轮")
    ap.add_argument("--retries", type=int, default=2, help="失败后自动接着做几次")
    a = ap.parse_args()
    from bonus_agent.agent import run_agent
    from bonus_agent.llm import LLM, load_config
    from bonus_agent.scenarios import Registry
    from bonus_agent.scenarios.context import prepare
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    reg = Registry([HERE / "scenario"], home=out / "_home", builtin=False)
    files = [HERE / "inputs"]
    scn = prepare(reg, "syn_commission", files, use_memory=False)
    llm = LLM.from_config(a.model, load_config(), a.checker)
    t0, new, answered, retried, rounds = time.time(), True, 0, 0, []
    while True:
        ar = run_agent(llm, files, out / "case", resume=not new, max_steps=a.max_steps, max_tokens=a.max_tokens,
                       scenario=scn, period="2026-09",
                       on_step=lambda n, s: print(f"  [{n}] {s.tool} {'ok' if s.ok else 'ERR ' + s.result[:160]}", flush=True))
        new = False
        rounds.append({"round": ar.round, "status": ar.status, "steps": len(ar.steps), "error": ar.error[:300],
                       "questions": [q.text[:200] for q in ar.ws.pending]})
        print(f"第 {ar.round} 轮：{ar.status} {ar.error[:200]}", flush=True)
        if ar.status == "NEEDS_CONFIRMATION" and answered < a.answers:
            p = out / "case" / "questions.yaml"
            qs = yaml.safe_load(p.read_text(encoding="utf-8")) or []
            for q in qs:
                if q.get("answer") in (None, "") and not q.get("withdrawn"):
                    q["answer"], q["answered_by"] = ANSWER, "样例脚本"
            p.write_text(yaml.safe_dump(qs, allow_unicode=True, sort_keys=False), encoding="utf-8")
            answered += 1
            continue
        if ar.status == "FAILED" and retried < a.retries:
            retried += 1
            continue
        break
    ok, lines = False, ["没有出结果"]
    res = next((revs.get("R0") for revs in (ar.results or {}).values()), None)
    if ar.status == "OK" and res is not None:
        ok, lines = check(res)
    u = llm.usage
    summary = {"model": a.model, "status": ar.status, "correct": ok, "rounds": rounds, "minutes": round((time.time() - t0) / 60, 1),
               "calls": u.calls, "prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens, "check": lines}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
