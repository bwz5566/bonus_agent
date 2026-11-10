"""T2（提成 9 月）暴露的通用问题：读表静默截断、只能按单列关联、名单比例不足 100% 余下部分丢失、
汇总看不出各类暂缓、提问拿自己设的占位数当数据事实、案卷不记模型和代码版本。数据都是造的。"""
from datetime import datetime
from decimal import Decimal

import openpyxl
import pytest

from bonus_agent.agent import ToolError, Workspace, _cell
from bonus_agent.auto import build_inventory, collect_files
from bonus_agent.llm import FakeLLM
from bonus_core.data.mapping import apply_mapping
from bonus_core.dsl.models import RuleSet


def _book(tmp_path):
    p = tmp_path / "数据.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "销售"
    ws.append(["月份", "客户", "型号", "金额"])
    for r in ([202609, "C1", "M1", 100], [202609, "C1", "M1", 50], [202609, "C2", "M1", 70], [202608, "C1", "M1", 30]):
        ws.append(r)
    s = wb.create_sheet("首发")
    s.append(["客户", "型号", "首发日期"])
    s.append(["C1", "M1", datetime(2025, 2, 1)])
    s.append(["C2", "M1", datetime(2026, 9, 1)])
    n = wb.create_sheet("名单")
    n.append(["月份", "客户", "型号", "负责人", "执行人"])
    n.append([202609, "C1", "M1", "甲50%", "乙"])
    n.append([202609, "C2", "M1", "甲", "丙"])
    wb.save(p)
    return p


def _ws(tmp_path):
    _book(tmp_path)
    return Workspace(FakeLLM(["x"]), build_inventory(collect_files([tmp_path])))


def test_long_cells_are_marked_not_silently_cut():
    assert _cell("短") == "短" and _cell(None) == ""
    long = "项目" * 100
    assert _cell(long).endswith(f"（共{len(long)}字，已截断）")


def test_combine_columns_into_project_key(tmp_path):
    w = _ws(tmp_path)
    out = w.define_dataset("销售", "数据.xlsx", "销售", {"pid": {"column": "项目编号", "type": "id"},
                                                    "amt": {"column": "金额", "type": "money", "unit": "元"}},
                           combine={"项目编号": ["月份", "客户", "型号"]})
    assert "已保存" in out
    assert sorted(set(w._values("销售", "pid"))) == ["202608|C1|M1", "202609|C1|M1", "202609|C2|M1"]
    assert w.mapping["销售"].combine == {"项目编号": ["月份", "客户", "型号"]}
    out = w.define_dataset("首发", "数据.xlsx", "首发", {"ck": {"column": "客户型号", "type": "id"},
                                                    "first": {"column": "首发日期", "type": "date"}},
                           combine={"客户型号": ["客户", "型号"]})
    assert sorted(w._values("首发", "ck")) == ["C1|M1", "C2|M1"]       # 同型号不同客户分得开
    rs = RuleSet.model_validate({"name": "t", "version": "1", "pool": {"expr": "0"}, "allocation": [],
                                 "datasets": {n: d.model_dump(mode="json") for n, d in w.datasets.items()}})
    data, issues = apply_mapping(rs, [tmp_path / "数据.xlsx"], w.mapping)
    assert not [i for i in issues if i.severity == "blocking"], issues
    assert sorted(r.values["pid"] for r in data["销售"].rows)[0] == "202608|C1|M1"
    with pytest.raises(ToolError, match="不在表头"):
        w.define_dataset("x", "数据.xlsx", "销售", {"pid": {"column": "k", "type": "id"}}, combine={"k": ["月份", "没有"]})


def test_share_below_full_needs_remainder(tmp_path):
    w = _ws(tmp_path)
    spec = {"columns": ["负责人", "执行人"], "value": "人员", "label": "角色", "share": "比例"}
    fields = {"emp": {"column": "人员", "type": "text"}, "r": {"column": "比例", "type": "ratio"}}
    with pytest.raises(ToolError, match="比例不足 100%.*甲50%"):
        w.define_dataset("参与", "数据.xlsx", "名单", fields, unpivot=spec)
    out = w.define_dataset("参与", "数据.xlsx", "名单", fields, unpivot={**spec, "remainder": "{label}未分配"})
    assert "已保存" in out and "负责人未分配" in w._values("参与", "emp")


def test_question_flags_own_placeholder_numbers(tmp_path):
    w = _ws(tmp_path)
    w.define_dataset("销售", "数据.xlsx", "销售", {"amt": {"column": "金额", "type": "money", "unit": "元"},
                                                "m": {"column": "月份", "type": "text"}})
    w.set_rules(pool={"expr": "sum(if(amt > 0, amt, -77))"}, allocation=[])
    w.ask_question("部分金额最低到 -77，是否按 0 算？", ["按 0", "照算"], 0)
    assert "程序提示：问题里的 77" in w.questions[-1].text
    w.ask_question("金额 70 的项目怎么算？", ["a", "b"], 0)
    assert "程序提示" not in w.questions[-1].text      # 数据里有的数不提示


def test_summary_sheet_splits_holds_by_condition(tmp_path):
    from bonus_core.engine.run import compute
    from bonus_core.report.excel import write_excel
    rules = {"name": "t", "version": "1",
             "datasets": {"人员": {"key": "eid", "fields": {"eid": {"type": "id"}, "w": {"type": "number"},
                                                         "a": {"type": "ratio"}, "b": {"type": "ratio"}}}},
             "pool": {"expr": "1000"}, "allocation": [{"level": "个人", "key": "eid", "weight": "w"}],
             "payout": [{"id": "欠款暂扣", "scope": "个人", "hold_ratio": "a"},
                        {"id": "呆滞逾期暂扣", "scope": "个人", "hold_ratio": "b"}]}
    rs = RuleSet.model_validate(rules)
    p = tmp_path / "人.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "人员"
    ws.append(["工号", "权重", "欠款", "呆滞"])
    ws.append(["E1", 1, 1, 0])
    ws.append(["E2", 1, 0, 0.2])
    wb.save(p)
    from bonus_core.data.mapping import DatasetMapping
    mp = {"人员": DatasetMapping.model_validate({"sheet": "人员", "columns": {"eid": "工号", "w": "权重", "a": "欠款", "b": "呆滞"}})}
    data, issues = apply_mapping(rs, [p], mp)
    res = compute(rs, data)
    out = tmp_path / "r.xlsx"
    write_excel(rs, res, out)
    rows = {r[0]: r[1] for r in openpyxl.load_workbook(out)["汇总"].iter_rows(min_row=2, values_only=True)}
    assert Decimal(str(rows["  其中 挂起：欠款暂扣"])) == Decimal("500")
    assert Decimal(str(rows["  其中 挂起：呆滞逾期暂扣"])) == Decimal("100")
    assert Decimal(str(rows["应得扣除挂起后（正常发放）"])) == Decimal("400")


def test_round_records_model_and_code(tmp_path):
    import json
    from bonus_agent.agent import run_agent
    from bonus_agent.case import load_case
    m = tmp_path / "材料"
    m.mkdir()
    _book(m)
    llm = FakeLLM([json.dumps({"tool": "finish", "args": {"summary": "x"}})] * 5, name="甲模型")
    llm.tool_mode = "json"
    out = tmp_path / "案卷"
    run_agent(llm, [m], out, max_steps=3)
    rnd = load_case(out)["rounds"][-1]
    assert rnd["model"] == "甲模型" and "code" in rnd
