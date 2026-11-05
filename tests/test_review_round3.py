"""代码审核（第三轮）：分段换列顺序、核对漏人、口径单 scope 变了仍算已确认、两层抵扣汇总少记，
以及没有唯一编号的明细表当最末层、重名列不自动猜、名称+百分比只查规则用到的字段、阶梯算法只认财务的话。"""
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest
import yaml

from bonus_agent.agent import ToolError, Workspace
from bonus_agent.auto import _fix_column, build_inventory, collect_files
from bonus_agent.llm import FakeLLM
from bonus_core.data.table_detect import detect


def _book(p: Path, rows, title="明细"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title
    for r in rows:
        ws.append(r)
    wb.save(p)
    return p


def test_second_segment_with_swapped_columns_is_read_by_name(tmp_path):
    p = _book(tmp_path / "a.xlsx", [["编号", "收入", "成本"], ["A", 3000, 1200],
                                    ["编号", "成本", "收入"], ["B", 1200, 3000]])
    st = next(s for s in detect(p) if s.sheet == "明细")
    assert [v for _, v in st.rows] == [["A", 3000, 1200], ["B", 3000, 1200]]
    assert st.remaps and not st.unsure
    w = Workspace(FakeLLM(["x"]), build_inventory(collect_files([tmp_path])))
    out = w.define_dataset("明细", "a.xlsx", "明细", {"id": {"column": "编号", "type": "id"},
                                                   "income": {"column": "收入", "type": "money"},
                                                   "cost": {"column": "成本", "type": "money"}}, key="id")
    assert "已按列名挪回" in out
    from bonus_core.data.mapping import apply_mapping
    from bonus_core.dsl.models import RuleSet
    rs = RuleSet.model_validate({"name": "t", "version": "1", "pool": {"expr": "0"}, "allocation": [],
                                 "datasets": {n: d.model_dump(mode="json") for n, d in w.datasets.items()}})
    data, issues = apply_mapping(rs, [p], w.mapping)
    assert not [i for i in issues if i.level == "block"]
    assert {r.values["id"]: (r.values["income"], r.values["cost"]) for r in data["明细"].rows} == \
        {"A": (Decimal("3000"), Decimal("1200")), "B": (Decimal("3000"), Decimal("1200"))}


def test_segment_that_cannot_be_matched_by_name_blocks(tmp_path):
    p = _book(tmp_path / "a.xlsx", [["编号", "金额", "金额", "备注"], ["A", 1, 2, "x"],
                                    ["编号", "备注", "金额", "金额"], ["B", "y", 3, 4]])
    st = next(s for s in detect(p) if s.sheet == "明细")
    assert st.unsure
    w = Workspace(FakeLLM(["x"]), build_inventory(collect_files([tmp_path])))
    with pytest.raises(ToolError, match="没法按列名对回第一段"):
        w.define_dataset("明细", "a.xlsx", "明细", {"id": {"column": "编号", "type": "id"},
                                                 "amt": {"column": "金额", "type": "money"}}, key="id")


def test_compare_reports_people_missing_from_results():
    from bonus_agent.compare import _SheetMatch, Match, report
    m = Match("S", "R0", "应得", sources=[("个人", "应得")], matched=1, total=2, missing=[("E2", Decimal("200"))])
    assert not m.ok
    line = "\n".join(report([m]))
    assert "期望结果里有、本次结果里没有的 1 个：E2（期望 200）" in line and "逐分一致" not in line
    assert _SheetMatch().missing == []


def test_compare_expected_flags_missing_person(tmp_path):
    from bonus_agent.compare import compare_expected
    from bonus_core.engine.run import compute
    from bonus_core.data.table import from_records
    from bonus_core.dsl.models import RuleSet
    rs = RuleSet.model_validate(yaml.safe_load('''
name: t
version: v1
datasets:
  人员: {key: emp_id, fields: {emp_id: {type: id}}}
pool: {scope: global, expr: "100"}
allocation:
  - {level: 人员, key: emp_id, weight: "1"}
'''))
    res = compute(rs, from_records(rs, {"人员": [{"emp_id": "E1"}]}))
    p = _book(tmp_path / "exp.xlsx", [["工号", "应得"], ["E1", 100], ["E2", 200]], title="个人")
    ms = compare_expected({"S": {"R0": res}}, p)
    m = next(m for m in ms if m.metric == "应得")
    assert m.missing == [("E2", Decimal("200"))] and not m.ok


def test_review_confirmation_expires_when_scope_changes():
    from types import SimpleNamespace as NS
    from bonus_agent import review as rv

    def ws(scope):
        return NS(mapping={}, datasets={}, clauses=[], lint={}, ignored=[], questions=[],
                  drafts={"S": {"variables": {"x": {"scope": scope, "expr": "1", "desc": "x"}}}})
    k1 = {r["item"]: r["key"] for r in rv.build(ws("个人"))}
    k2 = {r["item"]: r["key"] for r in rv.build(ws(None))}
    assert k1["x（x）"] != k2["x（x）"]


def test_rollup_lists_offsets_of_both_layers():
    from bonus_core.data.table import from_records
    from bonus_core.dsl.models import RuleSet
    from bonus_core.engine.run import compute
    rs = RuleSet.model_validate(yaml.safe_load('''
name: t
version: v1
datasets:
  项目: {key: project_id, fields: {project_id: {type: id}}}
  参与: {key: part_id, fields: {part_id: {type: id}, project_id: {type: id}, emp_id: {type: id}, pre: {type: money}}}
  人员: {key: emp_id, fields: {emp_id: {type: id}, pre2: {type: money}}}
pool: {scope: global, expr: "100"}
allocation:
  - {level: 项目, key: project_id, weight: "1"}
  - {level: 参与记录, key: part_id, parent_key: project_id, weight: "1"}
offsets:
  - {id: 记录扣, scope: 参与记录, expr: "pre"}
  - {id: 个人扣, scope: 人员, expr: "pre2"}
'''))
    data = from_records(rs, {"项目": [{"project_id": "J1"}],
                             "参与": [{"part_id": "A1", "project_id": "J1", "emp_id": "U1", "pre": 20}],
                             "人员": [{"emp_id": "U1", "pre2": 30}]})
    r = compute(rs, data).rollups["人员:U1"]
    assert (r.final, r.cap, r.offset, r.deducted, r.paid) == ("100.00", "100.00", "50.00", "50.00", "50.00")
    assert Decimal(r.cap) - Decimal(r.deducted) == Decimal(r.paid)


def test_detail_table_without_unique_id_can_use_row_numbers(tmp_path):
    p = _book(tmp_path / "名单.xlsx", [["项目", "负责人", "执行人"], ["P1", "甲", "乙"], ["P2", "甲", None]], title="名单")
    w = Workspace(FakeLLM(["x"]), build_inventory(collect_files([tmp_path])))
    out = w.define_dataset("参与", "名单.xlsx", "名单", {"part_id": {"column": "（行号）", "type": "id"},
                                                     "pid": {"column": "项目", "type": "id"},
                                                     "emp": {"column": "人员", "type": "text"}},
                           key="part_id", unpivot={"columns": ["负责人", "执行人"], "value": "人员", "label": "角色"})
    assert "已保存" in out and "3 行" in out
    assert sorted(w._values("参与", "part_id")) == ["2-1", "2-2", "3"]
    from bonus_core.data.mapping import apply_mapping
    from bonus_core.dsl.models import RuleSet
    rs = RuleSet.model_validate({"name": "t", "version": "1", "pool": {"expr": "0"}, "allocation": [],
                                 "datasets": {n: d.model_dump(mode="json") for n, d in w.datasets.items()}})
    data, issues = apply_mapping(rs, [p], w.mapping)
    assert not [i for i in issues if i.level == "block"], issues
    assert sorted(r.values["part_id"] for r in data["参与"].rows) == ["2-1", "2-2", "3"]


def test_close_column_name_is_not_guessed_when_headers_repeat():
    heads = ["客户名称", "金额", "客户名称(E列)"]
    assert _fix_column("客户名称2", heads) is None
    assert _fix_column("金 额", heads) == "金额"


def test_name_percent_gate_only_on_fields_rules_use():
    from bonus_agent import lint
    from bonus_core.data.table import Row, StdTable
    from bonus_core.dsl.models import RuleSet
    rs = RuleSet.model_validate({"name": "t", "version": "1", "pool": {"expr": "1"}, "allocation": [],
                                 "datasets": {"名单": {"key": "emp", "fields": {"emp": {"type": "id"}, "note": {"type": "text"}}}}})
    data = {"名单": StdTable("名单", [Row({"emp": e, "note": n}) for e, n in
                                     (("甲", "回款比例50%"), ("乙", "无"), ("丙30%", "无"))])}
    keys = lint.check(rs, data, "按人分配").keys
    assert "名称百分比 丙30%" in keys and not any("回款比例" in k for k in keys)


def test_question_text_alone_does_not_count_as_finance_stating_tier_method():
    from bonus_agent.extract_rules import Question
    w = Workspace(FakeLLM(["x"]), build_inventory([]))
    w.questions = [Question(id="q1", text="NPD 份额怎么处理？另外阶梯是超额累进吗？", options=["列为待分配", "不分"],
                            answer="NPD 列为待分配，其余按材料")]
    assert "累进" not in w._finance_said()
    w.questions[0].answer = "1"
    assert "累进" in w._finance_said()        # 只选了选项：问题和选项算财务确认过的内容
