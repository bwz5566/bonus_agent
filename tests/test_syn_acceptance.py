"""财务验收样例（evals/cases/syn_acceptance）：季度 R0/R1/R2、过程奖、年度奖（按回款比例发放、扣减预付和季度 R2）。

- 手写的标准规则经引擎计算，与独立参考实现（reference.py）逐分一致；
- 与财务给的 05_期望奖金结果.xlsx 自动核对一致；
- 10 个异常用例中 8 个数据类被拦截（另 2 个是政策未确认，由智能体的阻断问题处理）；
- 智能体按同样的规则操作时，自动先算被引用的季度 R2，结果一致、可复算。
"""
import json
import sys
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest
import yaml

CASE = Path(__file__).parent.parent / "evals" / "cases" / "syn_acceptance"
sys.path.insert(0, str(CASE))
import reference  # noqa: E402
import run_case  # noqa: E402

from bonus_core.engine.issues import EngineError  # noqa: E402

EXPECTED_XLSX = CASE / "expected" / "05_期望奖金结果.xlsx"
SHEET = {"sales": "销售明细", "projects": "项目", "participation": "项目参与", "receipts": "回款明细", "scores": "绩效评分",
         "targets": "目标值"}


@pytest.fixture(scope="module")
def results():
    return run_case.run()


def _reference():
    t = reference.load()
    reference.GROUP.update({e["employee_id"]: e["group_id"] for e in t["员工"]})
    reference.DEPT.update({g["group_id"]: g["dept_id"] for g in t["小组"]})
    return reference.compute(t)


def test_reference_matches_expected_workbook():
    """参考实现与 05 期望结果的部门、小组、个人、项目各列全部一致（独立验证 05 本身）。"""
    lines = reference.check_expected(_reference(), EXPECTED_XLSX)
    assert lines[0].endswith("不一致 0 个"), lines[:10]


def test_engine_matches_reference(results):
    ref = json.loads((CASE / "reference.json").read_text(encoding="utf-8"))
    assert run_case.diff_reference(results, ref) == []
    for s, revs in results.items():
        for rev, r in revs.items():
            for d in ("D01", "D02"):        # 部门 = 小组合计 = 个人合计
                groups = [n for n in r.nodes.values() if n.parent == f"部门:{d}"]
                people = [n for n in r.nodes.values() if n.parent in {g.id for g in groups}]
                assert sum(Decimal(g.final) for g in groups) == sum(Decimal(p.final) for p in people) \
                    == Decimal(r.nodes[f"部门:{d}"].final), (s, rev, d)
            for p in r.payouts.values():    # 应得 = 本次实发 + 暂缓 + 已发抵扣 − 超发
                assert Decimal(p.final) == Decimal(p.paid) + Decimal(p.held) + Decimal(p.offset) - Decimal(p.overpaid)


def test_annual_offsets_quarter_r2(results):
    a, q = results["ANNUAL"]["R0"].payouts["个人:E001"], results["QUARTER"]["R2"].payouts["个人:E001"]
    assert Decimal(a.offset) == Decimal("300000") + Decimal(q.paid)
    assert a.paid == "0" and Decimal(a.overpaid) == Decimal(a.offset) - Decimal(a.cap)
    assert "QUARTER/R2:个人:E001" in results["ANNUAL"]["R0"].traces["个人:E001/已发抵扣/prior"].inputs


def test_compare_finds_all_sheets(results, tmp_path):
    from bonus_agent.compare import compare_expected, report
    ms = compare_expected(results, EXPECTED_XLSX)
    assert all(m.ok for m in ms), report(ms)
    got = {(m.scheme, m.revision, m.metric): m.total for m in ms}
    assert got[("QUARTER", "R2", "应得")] == 32 and got[("ANNUAL", "R0", "超发待核对")] == 26
    # 改错一个人的期望金额，能定位出来
    wb = openpyxl.load_workbook(EXPECTED_XLSX)
    ws = wb["个人结果"]
    head = [c.value for c in ws[5]]
    ws.cell(6, head.index("应得金额") + 1).value = 1
    wb.save(tmp_path / "05.xlsx")
    m = next(m for m in compare_expected(results, tmp_path / "05.xlsx") if (m.scheme, m.revision, m.metric) == ("QUARTER", "R0", "应得"))
    assert m.matched == 31 and m.diffs[0][0] == "E001"


def _patched(case, tmp_path) -> Path:
    p = case["patch"]
    wb = openpyxl.load_workbook(run_case.XLSX)
    ws = wb[SHEET[p["table"]]]
    head = [c.value for c in ws[1]]
    r = p["index"] + 2
    if p["op"] == "append_copy":
        ws.append([c.value for c in ws[r]])
    else:
        v = p["value"]
        if isinstance(v, str):
            try:
                v = float(v)
            except ValueError:
                pass
        ws.cell(r, head.index(p["field"]) + 1).value = v
    out = tmp_path / run_case.XLSX.name      # 文件名要和映射里指定的一致
    wb.save(out)
    return out


NEG = [c for c in json.loads((CASE / "negative_cases.json").read_text(encoding="utf-8")) if c["patch"]["target"] == "data"]
CODE = {"DUPLICATE_KEY": "key_dup", "UNKNOWN_REGION": "R_REGION", "SHARE_SUM_NOT_ONE": "share_sum",
        "UNKNOWN_SALES": "link_unmatched", "SCORE_NOT_APPROVED": "score_approved", "NONPOSITIVE_TARGET": "target_dept",
        "RECEIPT_EXCEEDS_SALES": "receipt_le_sales", "SCORE_OUT_OF_RANGE": "score_range"}


@pytest.mark.parametrize("case", NEG, ids=[c["case_id"] for c in NEG])
def test_negative_cases_blocked(case, tmp_path):
    with pytest.raises(EngineError) as e:
        run_case.run(_patched(case, tmp_path))
    assert CODE[case["expected_error_code"]] in " ".join(str(i) for i in e.value.issues)


def _script(order=("ANNUAL", "QUARTER", "PROCESS"), blocking=False):
    y = lambda p: yaml.safe_load(p.read_text(encoding="utf-8"))  # noqa: E731
    dsets, maps, shared = y(CASE / "datasets.yaml"), y(CASE / "mapping.yaml"), y(CASE / "rules" / "shared.yaml")
    script = []
    for name, d in dsets.items():
        m = maps[name]
        fields = {f: {"column": m["columns"][f], **fd} for f, fd in d["fields"].items()}
        script.append({"tool": "define_dataset", "args": {"name": name, "file": m["file"], "sheet": m["sheet"],
                                                          "fields": fields, **{k: d[k] for k in ("key", "link") if k in d},
                                                          **({"filter": m["filter"]} if "filter" in m else {})}})
    script.append({"tool": "set_rules", "args": {"scheme": "共用", **shared}})
    for s in order:     # 故意先写年度：试算时引擎要先算被引用的季度
        own = y(CASE / "rules" / f"rules_{s}.yaml")
        if blocking and s == "ANNUAL":
            own.pop("offsets")
        script.append({"tool": "set_rules", "args": {"scheme": s, **{k: v for k, v in own.items()
                                                                    if k not in ("name", "version")}}})
    if blocking:
        script.append({"tool": "ask_question", "args": {"text": "跨期抵扣口径尚未确认：年度奖扣减哪些已发金额？",
                                                        "options": ["待财务确认"], "blocking": True}})
    script += [{"tool": "trial_run", "args": {"scheme": "ANNUAL"}}, {"tool": "trial_run"},
               {"tool": "finish", "args": {"summary": "季度奖、过程奖、年度奖"}}]
    return script


def test_agent_orders_schemes_by_reference(tmp_path):
    from bonus_agent.agent import run_agent
    from bonus_agent.llm import FakeLLM
    ar = run_agent(FakeLLM(_script()), [run_case.XLSX], tmp_path)
    assert ar.ok and ar.status == "OK", (ar.error, [s.result for s in ar.steps if not s.ok])
    first = next(s for s in ar.steps if s.tool == "trial_run")
    assert first.ok and "引用：QUARTER/R2" in first.result and "已发抵扣" in first.result
    ref = json.loads((CASE / "reference.json").read_text(encoding="utf-8"))
    assert run_case.diff_reference(ar.results, ref) == []
    assert ar.recheck.count("一致") == 5 and "不一致" not in ar.recheck and "失败" not in ar.recheck, ar.recheck


def test_agent_changing_quarter_retests_annual(tmp_path):
    from bonus_agent.agent import Workspace
    from bonus_agent.auto import build_inventory
    from bonus_agent.llm import FakeLLM
    ws = Workspace(FakeLLM([]), build_inventory([run_case.XLSX]))
    for step in _script()[:-3]:
        getattr(ws, step["tool"])(**step["args"])
    ws.trial_run()
    assert not ws.untested
    ws.trial_run("QUARTER")
    assert ws.untested == {"ANNUAL"}          # 季度重算后，引用它的年度要重算
    ws.set_rules(scheme="ANNUAL", offsets=[{"id": "x", "scope": "个人", "expr": 'scheme_paid("QUARTER", "R9")'}])
    from bonus_agent.agent import ToolError
    with pytest.raises(ToolError, match="R9"):
        ws.trial_run("ANNUAL")


def test_agent_blocking_question_marks_needs_confirmation(tmp_path):
    from bonus_agent.agent import run_agent
    from bonus_agent.llm import FakeLLM
    ar = run_agent(FakeLLM(_script(blocking=True)), [run_case.XLSX], tmp_path)
    assert ar.ok and ar.status == "NEEDS_CONFIRMATION"
    wb = openpyxl.load_workbook(tmp_path / "result.xlsx")
    plan = {r[0]: r[1] for r in wb["智能体规划"].iter_rows(min_row=2, values_only=True)}
    assert plan["状态"].startswith("待财务确认（NEEDS_CONFIRMATION）")
    q = yaml.safe_load((tmp_path / "questions.yaml").read_text(encoding="utf-8").split("\n", 1)[1])
    q[0]["answer"] = "扣本年度预付和季度 R2 实发"
    ar2 = run_agent(FakeLLM(_script(blocking=True)), [run_case.XLSX], tmp_path / "again", answers=q)
    assert ar2.status == "OK"
