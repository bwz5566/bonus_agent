"""D（最新一次运行）暴露的问题：几列人名要展开、提问后卡住不算失败、等答复期间不许绕开问题、
崩溃丢答复、Excel 控制字符、转写文字相同的图片。"""
import json
from pathlib import Path

import openpyxl
import yaml

from bonus_agent.agent import Workspace, run_agent
from bonus_agent.auto import build_inventory, collect_files
from bonus_agent.llm import FakeLLM


def _roster(tmp_path) -> Path:
    p = tmp_path / "名单.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "名单"
    for r in (["项目", "负责人", "执行人", "客户经理"], ["P1", "甲", "乙", "丙"], ["P2", "甲", None, "丁"], ["P3", None, "乙", None]):
        ws.append(r)
    wb.save(p)
    return p


def test_unpivot_role_columns_into_people_and_roles(tmp_path):
    _roster(tmp_path)
    w = Workspace(FakeLLM(["x"]), build_inventory(collect_files([tmp_path])))
    roles = ["负责人", "执行人", "客户经理"]
    out = w.define_dataset("人员", "名单.xlsx", "名单", {"emp": {"column": "人员", "type": "text"}}, key="emp", distinct=True,
                           unpivot={"columns": roles, "value": "人员", "label": "角色"})
    assert "已保存" in out and "展开成每个值一行" in out
    assert sorted(w._values("人员", "emp")) == sorted(["甲", "乙", "丙", "甲", "丁", "乙"])
    out = w.define_dataset("参与", "名单.xlsx", "名单", {"pid": {"column": "项目", "type": "id"},
                                                     "emp": {"column": "人员", "type": "text"},
                                                     "role": {"column": "角色", "type": "text"}},
                           link="emp", unpivot={"columns": roles, "value": "人员", "label": "角色"})
    assert "已保存" in out and "6 行" in out and "对不上" not in out
    assert w.mapping["参与"].unpivot["label"] == "角色"

    from bonus_core.data.mapping import apply_mapping
    from bonus_core.dsl.models import RuleSet
    rs = RuleSet.model_validate({"name": "t", "version": "1", "pool": {"expr": "0"}, "allocation": [],
                                 "datasets": {n: d.model_dump(mode="json") for n, d in w.datasets.items()}})
    data, issues = apply_mapping(rs, [tmp_path / "名单.xlsx"], w.mapping)
    assert not [i for i in issues if i.level == "block"], issues
    assert sorted(r.values["emp"] for r in data["人员"].rows) == ["丁", "丙", "乙", "甲"]
    assert [(r.values["pid"], r.values["role"]) for r in data["参与"].rows if r.values["emp"] == "乙"] == [("P1", "执行人"), ("P3", "执行人")]


def test_unpivot_rejects_bad_spec(tmp_path):
    _roster(tmp_path)
    w = Workspace(FakeLLM(["x"]), build_inventory(collect_files([tmp_path])))
    import pytest
    from bonus_agent.agent import ToolError
    with pytest.raises(ToolError, match="不在表头"):
        w.define_dataset("人员", "名单.xlsx", "名单", {"emp": {"column": "人员", "type": "text"}}, key="emp", distinct=True,
                         unpivot={"columns": ["负责人", "经理"], "value": "人员"})
    with pytest.raises(ToolError, match="重名"):
        w.define_dataset("人员", "名单.xlsx", "名单", {"emp": {"column": "项目", "type": "text"}}, key="emp", distinct=True,
                         unpivot={"columns": ["负责人", "执行人"], "value": "项目"})


def test_filter_may_not_drop_rows_a_pending_question_asks_about(tmp_path):
    _roster(tmp_path)
    w = Workspace(FakeLLM(["x"]), build_inventory(collect_files([tmp_path])))
    w.ask_question("名单里的项目 P3 在底稿里找不到，怎么处理？", options=["排除", "保留"])
    import pytest
    from bonus_agent.agent import ToolError
    with pytest.raises(ToolError, match="等财务答复"):
        w.define_dataset("名单", "名单.xlsx", "名单", {"pid": {"column": "项目", "type": "id"}}, key="pid",
                         filter={"项目": ["P1", "P2"]})
    assert "已保存" in w.define_dataset("名单", "名单.xlsx", "名单", {"pid": {"column": "项目", "type": "id"}}, key="pid")


def test_temporary_wording_in_rules_is_refused():
    from bonus_agent.agent import _temp_wording
    corpus = lambda: {"规则.docx": "提成按回款额计算"}
    assert _temp_wording({"pool": {"expr": "sum(x) * 0.001", "desc": "临时口径，等答复后再改"}}, corpus) == "临时口径"
    assert _temp_wording({"pool": {"expr": "sum(x)", "sources": [{"quote": "暂按上年标准执行"}]}}, corpus) == ""
    # 材料原文里就有这个说法：不拦
    assert _temp_wording({"pool": {"desc": "暂按上年标准"}}, lambda: {"a": "本季度暂按上年标准执行"}) == ""


def test_stuck_after_asking_waits_for_answer_instead_of_failing(tmp_path):
    _roster(tmp_path)
    ask = {"tool": "ask_question", "args": {"text": "名单的口径不清楚，怎么处理？", "options": ["A", "B"]}}
    bad = {"tool": "trial_run", "args": {}}
    ar = run_agent(FakeLLM([ask] + [bad] * 12), [tmp_path / "名单.xlsx"], max_steps=14)
    assert ar.status == "NEEDS_CONFIRMATION" and not ar.error, ar.error
    assert any(s.tool == "（等答复）" for s in ar.steps) and not any(s.tool == "（反思）" for s in ar.steps)


def test_answers_survive_a_round_that_did_not_save_its_workspace(tmp_path):
    """上一次运行写了 questions.yaml 却没存下 case.json（中途崩溃），财务填的答复下一轮要用上，新问题不能撞号。"""
    (tmp_path / "in").mkdir()
    _roster(tmp_path / "in")
    out = tmp_path / "out"
    out.mkdir()
    (out / "questions.yaml").write_text(yaml.safe_dump(
        [{"id": "q1", "text": "金额列为空，按什么口径分到人？", "options": ["按比例"], "answer": "按制度的角色比例"}],
        allow_unicode=True), encoding="utf-8")
    ask = {"tool": "ask_question", "args": {"text": "另一个问题", "options": ["A", "B"]}}
    ar = run_agent(FakeLLM([ask, {"tool": "finish", "args": {"summary": "等答复"}}]), [tmp_path / "in"], out, max_steps=3)
    qs = {q.id: q for q in ar.ws.questions}
    assert qs["q1"].answer == "按制度的角色比例" and "q2" in qs and qs["q2"].text == "另一个问题"
    saved = yaml.safe_load((out / "questions.yaml").read_text(encoding="utf-8"))
    assert {q["id"] for q in saved} >= {"q1", "q2"}


def test_excel_control_characters_do_not_crash(tmp_path):
    import bonus_core  # noqa: F401  导入时装好
    wb = openpyxl.Workbook()
    wb.active["A1"] = "老客户\x0b（旧项目）\x01"
    wb.save(tmp_path / "x.xlsx")
    assert openpyxl.load_workbook(tmp_path / "x.xlsx").active["A1"].value == "老客户\n（旧项目）"


def test_images_with_the_same_transcript_are_shown_once():
    from bonus_agent.parsing.docs import Block, ParsedDoc
    llm = FakeLLM(["x"])
    llm.images = "Firstunion"
    ws = Workspace(llm, build_inventory([]))
    ws.inv.docs.append(ParsedDoc("规则.pptx", [Block("规则.pptx", p, "image", image=f"\x89PNG-{p}".encode(), mime="image/png")
                                               for p in (1, 2, 3)]))
    ws._file = lambda name: Path(name)
    ws.transcribe_all()
    text = ws.read_document("规则.pptx", 0, limit=10 ** 9)
    assert text.count("Firstunion") == 1 and text.count("转写内容和图片 #1 相同") == 2


def _rs(extra_vars=None, pool="tier(rate, 1000)"):
    from bonus_core.dsl.models import RuleSet
    return RuleSet.model_validate({
        "name": "t", "version": "1", "datasets": {}, "pool": {"expr": pool}, "allocation": [],
        "tables": {"rate": {"kind": "tier", "mode": "whole", "steps": [[0, "0.01"], [500, "0.02"]]},
                   "coef": {"kind": "tier", "mode": "whole", "steps": [[0, "1"], [1, "0.9"]]}},
        "variables": extra_vars or {}})


def test_parameter_tables_and_coefficients_left_out_of_the_calculation_are_gated():
    """D：区域 / 客户任务系数、呆滞逾期系数、欠款冻结的表都建了，提成里却没用上就结束了。"""
    from bonus_agent import lint
    rep = lint.check(_rs({"k": {"expr": "tier_rate(coef, 2)"}}), {}, "提成按全额计算")
    assert "未用 k" in rep.keys and any("没有接进" in g for g in rep.gates)
    assert "未用 coef" not in rep.keys          # 有变量用它，先提示变量
    rep = lint.check(_rs(), {}, "提成按全额计算")
    assert "未用 coef" in rep.keys and "未用 rate" not in rep.keys
    rep = lint.check(_rs({"k": {"expr": "tier_rate(coef, 2)"}}, pool="tier(rate, 1000) * k"), {}, "提成按全额计算")
    assert not [k for k in rep.keys if k.startswith("未用")]
    # 别的方案用到了：不拦
    rep = lint.check(_rs(), {}, "提成按全额计算", reached_elsewhere={"coef"})
    assert "未用 coef" not in rep.keys


def test_tier_method_must_be_stated_in_materials():
    from bonus_agent import lint
    assert "阶梯方式 rate" in lint.check(_rs(), {}, "销售额 500 万以上提成 2%").keys
    assert "阶梯方式 rate" not in lint.check(_rs(), {}, "500 以上 2%，超额累进，每段按各自比例").keys


def test_name_with_percent_in_one_cell_is_flagged(tmp_path):
    p = tmp_path / "名单.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "名单"
    for r in (["项目", "负责人"], ["P1", "甲"], ["P2", "乙50%"]):
        ws.append(r)
    wb.save(p)
    w = Workspace(FakeLLM(["x"]), build_inventory(collect_files([tmp_path])))
    out = w.define_dataset("名单", "名单.xlsx", "名单", {"pid": {"column": "项目", "type": "id"},
                                                     "emp": {"column": "负责人", "type": "text"}}, key="pid")
    assert "名称+百分比" in out and "乙50%" in out


def test_steps_and_rounds_record_time_and_tokens(tmp_path):
    _roster(tmp_path)
    out = tmp_path / "out"
    llm = FakeLLM([{"tool": "list_files"}, {"tool": "finish", "args": {"summary": "x"}}])
    ar = run_agent(llm, [tmp_path / "名单.xlsx"], out, max_steps=2)
    s = next(s for s in ar.steps if s.tool == "list_files")
    assert s.at and s.secs >= 0 and hasattr(s, "tokens_in")
    assert ar.usage["steps"] == len(ar.steps) and ar.usage["calls"] >= 2
    case = json.loads((out / "case.json").read_text(encoding="utf-8"))
    assert case["rounds"][-1]["usage"]["calls"] == ar.usage["calls"]
    log = json.loads((out / "agent_log.json").read_text(encoding="utf-8"))
    assert log["usage"] == ar.usage and "model_secs" in log["steps"][0]
    from bonus_agent.web import round_text
    assert "本轮用时" in round_text(ar)
