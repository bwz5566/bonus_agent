"""D1（真实提成业务）暴露的问题：按本对象的编号取别的表、没有主表时按编号建对象、无表头的列、重复表头行、空值口径、数字写法。"""
from decimal import Decimal as D

import openpyxl
import pytest
from pydantic import ValidationError

from bonus_core.data.mapping import DatasetMapping, apply_mapping
from bonus_core.data.table import Row, StdTable, convert
from bonus_core.data.table_detect import detect
from bonus_core.dsl.models import FieldDef, RuleSet, to_decimal
from bonus_core.dsl.validator import validate
from bonus_core.engine.run import compute


def ds(key=None, link=None, distinct=False, **f):
    return {"key": key, "link": link, "distinct": distinct, "fields": {k: {"type": t} for k, t in f.items()}}


def t(n, *rows):
    return StdTable(n, [Row(dict(r)) for r in rows])


def test_level_reads_referenced_entity():
    """项目层按本项目的客户取呆滞表（关联实体，key=客户）：取到对应那一行；客户不在表里时没有行，sum 为 0。"""
    rs = RuleSet.model_validate({"name": "t", "version": "1",
        "datasets": {"底稿": ds("pid", pid="id", cust="text", amt="money"), "呆滞": ds("cust", cust="text", stock="money")},
        "variables": {"r": {"scope": "底稿", "expr": 'ratio(sum(stock, of: "呆滞"), sum(amt, of: "底稿"), zero: 0)'}},
        "pool": {"expr": "0"}, "allocation": [{"level": "底稿", "key": "pid", "weight": "r"}]})
    assert not [i for i in validate(rs) if i.severity == "blocking"]
    data = {"底稿": t("底稿", {"pid": "P1", "cust": "A", "amt": D(100)}, {"pid": "P2", "cust": "C", "amt": D(70)}),
            "呆滞": t("呆滞", {"cust": "A", "stock": D(200)})}
    w = {k: D(n.weight) for k, n in compute(rs, data).nodes.items()}
    assert w["底稿:P1"] == 2 and w["底稿:P2"] == 0


def test_distinct_entity_sums_across_projects():
    """没有客户主表：客户对象由底稿的客户编码建出来（distinct）。在客户上汇总全部项目的销售额和月数，
    呆滞、逾期按客户挂上来（逾期里同一客户的两行合计），项目层引用自己客户的数。"""
    rs = RuleSet.model_validate({"name": "t", "version": "1",
        "datasets": {"底稿": ds("pid", pid="id", cust="text", month="text", amt="money"),
                     "客户": ds("cust", distinct=True, cust="text"),
                     "呆滞": ds(link="cust", cust="text", stock="money"),
                     "逾期": ds(link="cust", cust="text", overdue="money")},
        "variables": {"cust_avg": {"scope": "客户", "expr": 'ratio(sum(amt, of: "底稿"), count_distinct(month, of: "底稿"), zero: 0)'},
                      "cust_overdue": {"scope": "客户", "expr": 'sum(overdue, of: "逾期")'},
                      "cust_stock": {"scope": "客户", "expr": 'sum(stock, of: "呆滞")'},
                      "stock_ratio": {"scope": "底稿", "expr": 'ratio(cust_stock, cust_avg, zero: 0)'},
                      "overdue_ratio": {"scope": "底稿", "expr": 'ratio(cust_overdue, cust_avg, zero: 0)'}},
        "pool": {"expr": "0"},
        "allocation": [{"level": "底稿", "key": "pid", "weight": "stock_ratio * 100 + overdue_ratio"}]})
    assert not [i for i in validate(rs) if i.severity == "blocking"]
    rows = [{"pid": "P1", "cust": "A", "month": "01", "amt": D(100)}, {"pid": "P2", "cust": "A", "month": "02", "amt": D(300)},
            {"pid": "P3", "cust": "B", "month": "01", "amt": D(50)}]
    data = {"底稿": t("底稿", *rows), "客户": t("客户", *[{"cust": r["cust"]} for r in rows]),
            "呆滞": t("呆滞", {"cust": "A", "stock": D(400)}),
            "逾期": t("逾期", {"cust": "A", "overdue": D(10)}, {"cust": "A", "overdue": D(30)})}
    data["客户"].rows = data["客户"].rows[::2]          # apply_mapping 会去重；这里直接给去重后的
    res = compute(rs, data)
    w = {k: D(n.weight) for k, n in res.nodes.items()}
    assert w["底稿:P1"] == w["底稿:P2"] == D(200) + D("0.2")    # 400 / (400/2) × 100 + 40 / 200
    assert w["底稿:P3"] == 0                                      # B 没有呆滞库存、没有逾期


def test_distinct_merges_rows_and_rejects_conflicting_fields(tmp_path):
    p = tmp_path / "d.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "明细"
    for r in (["客户", "区域", "金额"], ["A", "欧洲", 1], ["A", "欧洲", 2], ["B", "美洲", 3]):
        ws.append(r)
    wb.save(p)
    mk = lambda f: RuleSet.model_validate({"name": "t", "version": "1", "pool": {"expr": "0"}, "allocation": [],   # noqa: E731
                                           "datasets": {"客户": ds("cust", distinct=True, **f)}})
    m = {"客户": DatasetMapping(file="d.xlsx", sheet="明细", columns={"cust": "客户", "region": "区域", "amt": "金额"})}
    data, issues = apply_mapping(mk({"cust": "text", "region": "text"}), [p], m)
    assert [r.values["cust"] for r in data["客户"].rows] == ["A", "B"] and not issues
    _, issues = apply_mapping(mk({"cust": "text", "amt": "money"}), [p], m)
    assert any(i.code == "distinct_conflict" for i in issues)


def test_blank_header_column_and_repeated_header_row(tmp_path):
    p = tmp_path / "s.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    for r in ([None, "日期", "金额"], ["K1", "202601", 1], ["K2", "202601", 2], [None, "日期", "金额"], ["K3", "202602", 3]):
        ws.append(r)
    wb.save(p)
    st = next(s for s in detect(p) if s.sheet == "Sheet1")
    assert st.headers[0] == "第1列(无表头)" and [v[0] for _, v in st.rows] == ["K1", "K2", "K3"] and st.repeats == [4]


def test_second_header_with_different_names_starts_a_segment(tmp_path):
    """两段数据拼在一张表，第二段的表头有几列改了名或空着（如“客户经理1”改成“客户经理”、“金额1”改成“金额”）：
    那一行不当数据（原来在数字列报“无法解析为数字”），并说明哪几列名字变了；数据仍按列的位置读。"""
    from bonus_agent.agent import _segments_text
    p = tmp_path / "s.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "名单"
    head = ["日期", "客户", "负责人", "金额", "客户经理1", "客户经理2", "金额1", "其他金额"]
    for r in (head, [202601, "C1", "甲", None, "乙", None, None, None], [202601, "C2", "甲", None, "丙", None, None, None],
              ["日期", "客户", "负责人", "金额", "客户经理", None, "金额", "其他金额"],
              [202602, "C3", None, None, "丁", "戊", "丁80%，戊20%", None]):
        ws.append(r)
    wb.save(p)
    st = next(s for s in detect(p) if s.sheet == "名单")
    assert [v[1] for _, v in st.rows] == ["C1", "C2", "C3"] and not st.repeats
    assert st.segments and st.segments[0][0] == 4
    assert st.rows[-1][1][5] == "戊" and st.rows[-1][1][6] == "丁80%，戊20%"
    text = _segments_text(st)
    assert "第 4 行是另一段数据的表头" in text and "「客户经理1」在这一段叫「客户经理」" in text
    assert "「客户经理2」在这一段没有列名" in text and "「金额1」在这一段叫「金额」" in text


def test_text_data_rows_are_not_mistaken_for_headers(tmp_path):
    p = tmp_path / "s.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "部门"
    for r in (["部门编号", "部门名称", "负责人"], ["D1", "销售部", "甲"], ["D2", "部门名称", "乙"], ["D3", "研发部", "丙"]):
        ws.append(r)
    wb.save(p)
    st = next(s for s in detect(p) if s.sheet == "部门")
    assert len(st.rows) == 3 and not st.segments and not st.repeats


def test_numbers_with_units_and_bad_text():
    assert to_decimal("0.24%") == D("0.0024") and to_decimal("5000万") == D(50000000) and to_decimal("1.5亿") == D(150000000)
    with pytest.raises(ValidationError):       # 原来是 decimal.InvalidOperation，整次运行崩掉
        RuleSet.model_validate({"name": "t", "version": "1", "datasets": {}, "pool": {"expr": "0"}, "allocation": [],
                                "tables": {"x": {"kind": "tier", "mode": "whole", "steps": [["五千", "0.1"]]}}})


def test_if_empty_is_the_confirmed_way_to_fill_blanks():
    fd = FieldDef(type="ratio", if_empty="0")
    assert convert(None, fd) == 0 and convert("", fd) == 0 and convert("5%", fd) == D("0.05")
    assert convert(None, FieldDef(type="ratio")) is None


def test_link_that_matches_nothing_is_not_saved(tmp_path):
    """D1：毛利率表的项目编号列没有表头，模型拿“日期”列顶替 link，保存成功却一个项目都挂不上。
    现在一个都对不上就不保存；没有表头的列可以按名字选用；编号和文字类型的同名字段可以互相关联。"""
    from bonus_agent.agent import run_agent
    from bonus_agent.llm import FakeLLM
    p = tmp_path / "data.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "底稿"
    for r in (["项目编号", "日期", "客户", "金额"], ["202601A", "202601", "A", 1], ["202601B", "202601", "B", 2]):
        ws.append(r)
    ws2 = wb.create_sheet("毛利率")
    for r in ([None, "日期", "客户", "毛利率"], ["202601A", "202601", "A", 0.1], ["202601B", "202601", "B", 0.2]):
        ws2.append(r)
    wb.save(p)
    main = {"pid": {"column": "项目编号", "type": "id"}, "cust": {"column": "客户", "type": "id"}, "amt": {"column": "金额", "type": "money", "unit": "元"}}
    wrong = {"pid": {"column": "日期", "type": "id"}, "gm": {"column": "毛利率", "type": "ratio"}}
    right = {"pid": {"column": "第1列(无表头)", "type": "id"}, "cust": {"column": "客户", "type": "text"}, "gm": {"column": "毛利率", "type": "ratio"}}
    script = [{"tool": "define_dataset", "args": {"name": "底稿", "file": "data.xlsx", "sheet": "底稿", "key": "pid", "fields": main}},
              {"tool": "define_dataset", "args": {"name": "毛利率", "file": "data.xlsx", "sheet": "毛利率", "link": "pid", "fields": wrong}},
              {"tool": "define_dataset", "args": {"name": "毛利率", "file": "data.xlsx", "sheet": "毛利率", "link": "pid", "fields": right}}]
    ar = run_agent(FakeLLM(script), [p], max_steps=3)
    steps = [s for s in ar.steps if not s.tool.startswith("（")]
    assert [s.ok for s in steps] == [True, False, True], [s.result[:200] for s in steps]
    assert "一个都对不上" in steps[1].result and ar.ws.mapping["毛利率"].columns["pid"] == "第1列(无表头)"


def test_identical_images_are_transcribed_once():
    """PPT 每页都有的 logo：一模一样的图只转写一次，文档里后面的只留一句“和 #1 相同”。"""
    from bonus_agent.agent import Workspace
    from bonus_agent.auto import build_inventory
    from bonus_agent.llm import FakeLLM
    from bonus_agent.parsing.docs import Block, ParsedDoc
    llm = FakeLLM(["x"])
    llm.images = "LOGO"
    ws = Workspace(llm, build_inventory([]))
    logo, chart = b"\x89PNG-logo", b"\x89PNG-chart"
    ws.inv.docs.append(ParsedDoc("规则.pptx", [Block("规则.pptx", 1, "image", image=logo, mime="image/png"),
                                               Block("规则.pptx", 2, "image", image=chart, mime="image/png"),
                                               Block("规则.pptx", 3, "image", image=logo, mime="image/png")]))
    from pathlib import Path
    ws._file = lambda name: Path(name)          # 测试里没有真实文件
    n0 = llm.usage.calls
    ws.transcribe_all()
    assert llm.usage.calls - n0 == 2 and ws.transcripts["规则.pptx#3"] == "LOGO"
    assert "和图片 #1 完全相同" in ws.read_document("规则.pptx", 0, limit=10 ** 9)


def test_define_dataset_warns_when_used_column_is_renamed_in_a_segment(tmp_path):
    from bonus_agent.agent import Workspace
    from bonus_agent.auto import build_inventory, collect_files
    from bonus_agent.llm import FakeLLM
    p = tmp_path / "s.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "名单"
    for r in (["客户", "负责人", "客户经理1", "比例说明"], ["C1", "甲", "乙", None], ["C2", "甲", "丙", None],
              ["客户", "负责人", "客户经理", "拆分"], ["C3", "甲", "丁", "丁80%，戊20%"]):
        ws.append(r)
    wb.save(p)
    w = Workspace(FakeLLM(["x"]), build_inventory(collect_files([tmp_path])))
    out = w.define_dataset("名单", "s.xlsx", "名单", {"cust": {"column": "客户", "type": "id"},
                                                     "mgr": {"column": "客户经理1", "type": "text"}}, link=None)
    assert "3 行" in out and "注意：第 5 行是另一段数据的表头" not in out
    assert "第 4 行是另一段数据的表头" in out and "「客户经理1」在这一段叫「客户经理」" in out and "ask_question" in out
    assert "比例说明" not in out.split("注意：", 1)[1].split("\n")[0]      # 没用到的列不在提醒里
