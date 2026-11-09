"""核算期间：任意月份、季度、年度或自定义起止。规则里不写死月份，数据表用 period 标出日期/月份字段，计算时按选的期间取行。"""
from datetime import date, datetime
from decimal import Decimal

import openpyxl
import pytest

from bonus_core.data.mapping import DatasetMapping, apply_mapping
from bonus_core.data.period import Period, PeriodError
from bonus_core.dsl.models import RuleSet
from bonus_core.dsl.validator import validate
from bonus_core.engine.issues import EngineError
from bonus_core.engine.run import Engine


def test_parse_and_labels():
    assert Period.parse("2026-09") == Period(date(2026, 9, 1), date(2026, 9, 30))
    assert Period.parse("202609").label == "2026-09" and Period.parse("2026年9月").label == "2026-09"
    assert Period.parse("2026Q3") == Period(date(2026, 7, 1), date(2026, 9, 30)) and Period.parse("2026q3").label == "2026Q3"
    assert Period.parse("2026").label == "2026" and Period.parse("2026").months == 12
    assert Period.parse("2026-07~2026-09").label == "2026Q3"
    assert Period.parse("2026-01 至 2026-09").label == "2026-01~2026-09"
    assert Period.parse("2026-09-01~2026-09-15").label == "2026-09-01~2026-09-15"
    assert Period.parse("") is None and Period.parse(None) is None
    for bad in ("九月", "2026-13", "2026-09~2026-07"):
        with pytest.raises(PeriodError):
            Period.parse(bad)


def test_contains_by_day_month_and_year():
    q3 = Period.parse("2026Q3")
    assert q3.contains(202609) and q3.contains("2026-08") and q3.contains(datetime(2026, 7, 3)) and q3.contains("2026/9/30")
    assert q3.contains(202610) is False and q3.contains(date(2026, 6, 30)) is False
    assert q3.contains("2026") is None            # 年份只覆盖一部分：说不清，不能静默算进来或丢掉
    assert q3.contains(None) is None and q3.contains("剔除") is None
    assert Period.parse("2026").contains("2026") is True
    half = Period.parse("2026-09-01~2026-09-15")
    assert half.contains("2026-09-10") and half.contains("2026-09") is None


RULES = {
    "name": "期间测试", "version": "t",
    "datasets": {
        "人员": {"key": "eid", "fields": {"eid": {"type": "id"}}},
        "销售": {"link": "eid", "period": "month", "fields": {"eid": {"type": "id"}, "month": {"type": "text"},
                                                             "amt": {"type": "money", "unit": "元"}}},
    },
    "variables": {"当期月份": {"scope": "global", "expr": "month(period_end())"}},
    "pool": {"expr": "sum(amt) * 0.1"},
    "allocation": [{"level": "个人", "key": "eid", "weight": "sum(amt)"}],
}
MAPPING = {"人员": {"sheet": "人员", "columns": {"eid": "工号"}},
           "销售": {"sheet": "销售", "columns": {"eid": "工号", "month": "月份", "amt": "金额"}}}


def book(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "人员"
    ws.append(["工号", "姓名"])
    ws.append(["E1", "甲"])
    ws.append(["E2", "乙"])
    s = wb.create_sheet("销售")
    s.append(["工号", "月份", "金额"])
    for r in rows:
        s.append(r)
    wb.save(path)
    return path


ROWS = [["E1", 202607, 1000], ["E1", 202608, 2000], ["E2", 202609, 3000], ["E1", 202609, 1000]]


def run(f, period):
    rs = RuleSet.model_validate(RULES)
    mp = {k: DatasetMapping.model_validate(v) for k, v in MAPPING.items()}
    p = Period.parse(period)
    data, issues = apply_mapping(rs, [f], mp, p)
    blocking = [i for i in issues if i.severity == "blocking"]
    if blocking:
        raise EngineError(blocking)
    return Engine(rs, data, {}, p), issues


def test_same_rules_any_period(tmp_path):
    f = book(tmp_path / "数据.xlsx", ROWS)
    eng, issues = run(f, "2026-09")
    assert Decimal(eng.compute().pool) == Decimal("400.00")
    assert any(i.code == "period_filtered" and "期间外 2 行" in i.message for i in issues)
    assert eng.ev.evaluate("当期月份", eng.root, "x", "x") == 9
    assert Decimal(run(f, "2026Q3")[0].compute().pool) == Decimal("700.00")
    assert Decimal(run(f, "2026-07~2026-08")[0].compute().pool) == Decimal("300.00")
    eng, issues = run(f, None)                    # 没选期间：全部行都参与，用到 period_end() 的地方报错说清楚
    with pytest.raises(Exception, match="没有指定期间"):
        eng.ev.evaluate("当期月份", eng.root, "x", "x")


def test_unclear_period_values_block(tmp_path):
    f = book(tmp_path / "数据.xlsx", ROWS + [["E2", None, 50], ["E2", "剔除", 60]])
    with pytest.raises(EngineError, match="看不出是否属于核算期间"):
        run(f, "2026-09")


def test_period_field_must_exist():
    bad = {**RULES, "datasets": {**RULES["datasets"], "销售": {**RULES["datasets"]["销售"], "period": "nope"}}}
    assert any(i.code == "period_field" for i in validate(RuleSet.model_validate(bad)))


def test_agent_dataset_period_and_template_reruns_other_month(tmp_path):
    """智能体按 9 月编制：define_dataset 写 period；确认后的模板换 8 月不改规则直接算。"""
    from bonus_agent import review as rv
    from bonus_agent.agent import run_agent
    from bonus_agent.case import confirm, load_case
    from bonus_agent.llm import FakeLLM
    from bonus_core.template import Template
    m = tmp_path / "材料"
    m.mkdir()
    book(m / "数据.xlsx", ROWS)
    script = [
        {"tool": "define_dataset", "args": {"name": "人员", "file": "数据.xlsx", "sheet": "人员", "key": "eid",
                                            "fields": {"eid": {"column": "工号", "type": "id"}}}},
        {"tool": "define_dataset", "args": {"name": "销售", "file": "数据.xlsx", "sheet": "销售", "link": "eid", "period": "nope",
                                            "fields": {"eid": {"column": "工号", "type": "id"}, "month": {"column": "月份", "type": "text"},
                                                       "amt": {"column": "金额", "type": "money", "unit": "元"}}}},
        {"tool": "define_dataset", "args": {"name": "销售", "file": "数据.xlsx", "sheet": "销售", "link": "eid", "period": "month",
                                            "fields": {"eid": {"column": "工号", "type": "id"}, "month": {"column": "月份", "type": "text"},
                                                       "amt": {"column": "金额", "type": "money", "unit": "元"}}}},
        {"tool": "set_rules", "args": {"pool": {"expr": "sum(amt) * 0.1"},
                                       "allocation": [{"level": "个人", "key": "eid", "weight": "sum(amt)"}]}},
        {"tool": "trial_run", "args": {}},
        {"tool": "finish", "args": {"summary": "按月计提"}},
    ]
    import json
    llm = FakeLLM([json.dumps({"tool": s["tool"], "args": s["args"]}) for s in script])
    llm.tool_mode = "json"
    out = tmp_path / "案卷"
    ar = run_agent(llm, [m], out, period="2026-09", max_steps=len(script) + 5)
    task = next(x["content"] for x in llm.calls[0] if x["role"] == "user")
    assert "【核算期间】本次算 2026-09" in task
    dd = [x for x in ar.steps if x.tool == "define_dataset"]
    assert not dd[1].ok and "period 要写" in dd[1].result
    assert dd[2].ok and "数据覆盖 2026-07-01 ~ 2026-09-30" in dd[2].result
    assert ar.ws.datasets["销售"].period == "month"
    if ar.status != "OK":
        raise AssertionError(f"{ar.status} {ar.error} " + " | ".join(f"{s.tool}:{s.ok}:{s.result[:300]}" for s in ar.steps))
    assert Decimal(next(iter(ar.results.values()))["R0"].pool) == Decimal("400.00")
    assert load_case(out)["rounds"][-1]["period"] == "2026-09"
    rv.mark(out, {r["key"]: (rv.OK, "") for r in rv.load(out)}, "财务甲")
    tpl = Template.load(confirm(out, "财务甲"))
    assert tpl.manifest["period"] == "2026-09"
    res, _ = tpl.run([m / "数据.xlsx"], "2026-08")
    assert Decimal(next(iter(res.values()))["R0"].pool) == Decimal("200.00")
    res, _ = tpl.run([m / "数据.xlsx"])               # 不选期间：用确认时的 9 月
    assert Decimal(next(iter(res.values()))["R0"].pool) == Decimal("400.00")
