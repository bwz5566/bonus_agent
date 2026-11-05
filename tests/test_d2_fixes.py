"""D2 暴露的问题：重名列选不中、规则照材料叫法写取值和数据对不上、百分号误报引发反复读文档、
程序检查只列一部分、名称+百分比只提示不拦、阶梯算法只要材料任何地方出现“累进”就放过、同一文档整份反复读、
编号对不上时绕来绕去不提问。"""
from pathlib import Path

import openpyxl
from bonus_agent import lint
from bonus_agent.agent import Workspace
from bonus_agent.auto import build_inventory, collect_files
from bonus_agent.llm import FakeLLM
from bonus_core.data.table import Row, StdTable
from bonus_core.dsl.models import RuleSet


def test_duplicate_headers_get_column_letters_and_can_be_chosen(tmp_path):
    p = tmp_path / "预算.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "预算"
    for r in (["区域", "客户名称", "金额", "区域", "客户名称", "区域"], ["华东", "A", 1, "东一", "a", "x"],
              ["华南", "B", 2, "南一", "b", "y"], ["区域", "客户名称", "金额", "区域", "客户名称", "区域"]):
        ws.append(r)
    wb.save(p)
    w = Workspace(FakeLLM(["x"]), build_inventory(collect_files([tmp_path])))
    st = w.inv.sheet("预算.xlsx/预算")
    assert st.headers == ["区域", "客户名称", "金额", "区域(D列)", "客户名称(E列)", "区域(F列)"]
    assert len(st.rows) == 2 and st.repeats       # 重复的表头行照样认得出
    out = w.define_dataset("预算", "预算.xlsx", "预算", {"cust": {"column": "客户名称(E列)", "type": "text"},
                                                     "region": {"column": "区域(D列)", "type": "text"}}, key="cust")
    assert "已保存" in out
    assert sorted(w._values("预算", "region")) == ["东一", "南一"]


def _rs(pool, tables=None, variables=None):
    return RuleSet.model_validate({"name": "t", "version": "1", "datasets": {}, "pool": {"expr": pool}, "allocation": [],
                                   "tables": tables or {}, "variables": variables or {}})


def _data(field, values):
    return {"分配": StdTable("分配", [Row({field: v}) for v in values])}


def test_rule_value_from_materials_that_data_writes_differently_is_gated():
    data = _data("role", ["客户经理1", "客户经理2", "销售负责人", "客户经理1"])
    rep = lint.check(_rs('if(role == "客户经理", 0.1, 0) + if(role == "销售负责人", 0.2, 0)'), data,
                     "客户经理 10%，销售负责人 20%")
    g = next(g for g, k in zip(rep.gates, rep.keys) if k == "取值 客户经理")
    assert "客户经理1（2 行）" in g and "客户经理2" in g
    assert "取值 销售负责人" not in rep.keys
    # 材料里有、数据里没有、也没有相近写法的：只提示
    rep = lint.check(_rs('if(role == "空缺", 0.1, 0)'), data, "空缺 10%")
    assert "取值 空缺" not in rep.keys and any("空缺" in w for w in rep.warns)


def test_percent_written_as_whole_numbers_elsewhere_is_not_a_missing_percent():
    text = "分配比例：销售负责人 25%，执行人 35%。提成比例 0.25%、0.35%。\n角色 | 分配比例\n执行人 | 42.5"
    rep = lint.check(_rs("0.25 + 0.35 + 0.425 + 0.0025"), {}, text)
    assert not [k for k in rep.keys if k.startswith(("百分号", "常数"))], rep.gates
    # 材料只写了 0.24%：规则写 0.24 仍要拦
    rep = lint.check(_rs("0.24"), {}, "提成比例 0.24%")
    assert "百分号 0.24" in rep.keys


def test_name_with_percent_in_data_is_a_gate():
    rep = lint.check(_rs('if(emp == "张三", 1, 1)'), _data("emp", ["张三", "李四", "王五50%"]), "按人分配 张三")
    assert "名称百分比 王五50%" in rep.keys


def _tier_rs():
    return _rs("tier(rate, 1000)", {"rate": {"kind": "tier", "mode": "whole", "steps": [[0, "0.01"], [5000000, "0.02"]]}})


def test_tier_method_must_be_stated_near_the_table_or_by_finance():
    far = "提成比例：500 万以下 1%，500 万以上 2%。" + "说明。" * 400 + "绩效分段考核，逐级累进"
    assert "阶梯方式 rate" in lint.check(_tier_rs(), {}, far).keys
    near = "提成比例：500 万以下 1%，500 万以上 2%，超额累进。"
    assert "阶梯方式 rate" not in lint.check(_tier_rs(), {}, near).keys
    assert "阶梯方式 rate" not in lint.check(_tier_rs(), {}, far, said="按全额计算，整笔按所在档").keys


def test_tier_rate_on_commission_rates_also_needs_the_method():
    rs = _rs("1000 * tier_rate(rate, 1000) * tier_rate(coef, 1)",
             {"rate": {"kind": "tier", "mode": "whole", "steps": [[0, "0.01"], [5000000, "0.02"]]},
              "coef": {"kind": "tier", "mode": "whole", "steps": [[0, "0.8"], [1, "1.1"]]}})
    keys = lint.check(rs, {}, "500 万以下 1%，500 万以上 2%；系数 0.8、1.1").keys
    assert "阶梯方式 rate" in keys and "阶梯方式 coef" not in keys


def test_gates_are_ordered_and_the_rest_counted(tmp_path):
    from bonus_agent.agent import GATES_SHOWN
    w = Workspace(FakeLLM(["x"]), build_inventory([]))
    rep = lint.Report(gates=[f"常数 {i} 没出处" for i in range(GATES_SHOWN)] + ["取值 客户经理 对不上"],
                      keys=[f"常数 {i}" for i in range(GATES_SHOWN)] + ["取值 客户经理"])
    gates = w._open_gates(rep)
    assert gates[0].startswith("取值 客户经理")


def _doc_ws():
    from bonus_agent.parsing.docs import Block, ParsedDoc
    ws = Workspace(FakeLLM(["x"]), build_inventory([]))
    ws.inv.docs.append(ParsedDoc("制度.pptx", [Block("制度.pptx", p, "text", text=f"第{p}页 " + "条款内容" * 2000)
                                             for p in range(1, 4)]))
    ws._file = lambda name: Path(name)
    return ws


def test_whole_document_reread_again_and_again_is_cut_short():
    from bonus_agent.agent import REREAD_SMALL
    ws = _doc_ws()
    full = len(ws._render_doc(ws.inv.docs[0]))
    for _ in range(4):
        off = 0
        while off < full:
            out = ws.read_document("制度.pptx", off)
            assert "之后每次只给" not in out
            off += 5800
            if ws.doc_reads["制度.pptx"] >= 3 * full:
                break
        if ws.doc_reads["制度.pptx"] >= 3 * full:
            break
    out = ws.read_document("制度.pptx", 300)
    assert "之后每次只给" in out and out.index("（制度.pptx 这一轮") <= REREAD_SMALL + 40
    small = ws.read_document("制度.pptx", 100, limit=1000)      # 只读一小段：照常给，但也计数
    assert "条款内容" in small and small.index("（制度.pptx 这一轮") <= 1040
    assert "条款内容" in ws.read_document("制度.pptx", 0, limit=10 ** 9)     # 程序自己读全文不受限


def test_unmatched_links_summarised_and_second_time_asks_finance():
    from bonus_core.engine.issues import Issue
    w = Workspace(FakeLLM(["x"]), build_inventory([]))
    issues = [Issue.block("link_unmatched", f"「库存」中 cust=C{i} 在「客户」中找不到") for i in (1, 2, 2, 3)]
    first = w._unmatched_text(issues)
    assert "「库存」有 4 行的 cust 在「客户」里找不到（3 个编号" in first and "第 2 次" not in first
    second = w._unmatched_text(issues)
    assert "第 2 次" in second and "ask_question" in second
