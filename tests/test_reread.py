"""独立复读：输入边界（不给数据行）、程序比对（列、常数、分档点、筛选取值、日期）、结束前拦下和放过。"""
import json
from types import SimpleNamespace as NS

from bonus_agent import reread as rr
from bonus_core.data.mapping import DatasetMapping
from bonus_core.data.table_detect import SheetTable


def _ws(drafts, sheet_rows=None):
    detail = SheetTable("数据.xlsx", "明细", 1, ["单号", "金额", "成本", "费用", "状态", "日期"],
                        sheet_rows or [(2, ["A1", 100, 60, 10, "已审批", "2026-07-03"])])
    param = SheetTable("参数.xlsx", "参数", 1, ["名称", "数值"], [(2, ["提成比例", 0.05])])
    return NS(
        drafts=drafts, inv=NS(sheets=[detail, param], docs=[], sheet_text={"参数.xlsx/参数": "| 名称 | 数值 |"}),
        mapping={"明细": DatasetMapping(file="数据.xlsx", sheet="明细", columns={
            "sid": "单号", "amount": "金额", "cost": "成本", "fee": "费用", "status": "状态", "day": "日期"})},
        datasets={"明细": NS(key="sid", link=None)}, questions=[])


DRAFTS = {"共用": {"variables": {"profit": {"expr": 'sum(amount - cost, where: status == "已审批" and '
                                                  'between(day, "2026-07-01", "2026-09-30"))'},
                                 "bonus": {"expr": "tier(R, profit) * 5%"}},
                   "tables": {"R": {"kind": "tier", "steps": [[0, 0.01], [30, 0.02], [60, 0.03]]}}}}


def _diff(items):
    return dict(rr.compare(_ws(DRAFTS), {"items": items, "error": ""}))


def test_compare_finds_what_rules_missed():
    d = _diff([{"name": "净利润", "columns": ["明细/金额", "明细/成本", "明细/费用", "明细/单号"],
                "constants": [{"value": 0.05}, {"value": 0.07, "meaning": "封顶比例"}, {"value": 2026}],
                "filters": [{"column": "明细/状态", "values": ["已审批", "已复核", "是"], "from": "2026-07-01", "to": "2026-09-30"}]},
               {"name": "系数", "tiers": [{"input": "比值", "cutoffs": [0, 1, 2], "rates": [0.01, 0.02, 0.03]}]}])
    assert set(d) == {"复读 列 明细/费用", "复读 常数 0.07", "复读 取值 已复核", "复读 分档 R"}, d
    assert "费用" in d["复读 列 明细/费用"] and "规则的分档表 R 是 0、30、60" in d["复读 分档 R"]


def test_compare_tolerates_equivalent_writing():
    """百分数 / 小数、日期差一天、整数分档“超过 30”和“第 31 起”、第一档起点写 0 或 1、文字编号列：都不算出入。"""
    d = _diff([{"name": "奖金", "columns": ["明细/单号", "明细/金额"], "constants": [{"value": 5, "unit": "%"}, {"value": 1}],
                "filters": [{"column": "明细/日期", "from": "2026-06-30", "to": "2026-10-01"}],
                "tiers": [{"cutoffs": [1, 31, 61], "rates": [1, 2, 3]}, {"cutoffs": [0, 30], "rates": [0.02, 0.03]}]}])
    assert d == {}, d


def test_ambiguous_tier_is_not_compared():
    drafts = json.loads(json.dumps(DRAFTS))
    drafts["共用"]["tables"]["S"] = {"kind": "tier", "steps": [[0, 0.02], [75, 0.03], [90, 0.04]]}
    d = dict(rr.compare(_ws(drafts), {"items": [{"name": "x", "tiers": [{"cutoffs": [0, 5], "rates": [0.02, 0.03]}]}],
                                      "error": ""}))
    assert d == {}                      # 两张表都有 0.02、0.03 又都不完全一样：认不准，不比


def test_partial_rate_overlap_is_not_compared():
    """复读把另一个系数（如按耗时取的时效系数 1.10、0.80）写成分档，恰好和规则里某张表的几档比例相同：档数不同，不是同一张表。"""
    drafts = json.loads(json.dumps(DRAFTS))
    drafts["共用"]["tables"]["Q"] = {"kind": "tier", "steps": [[0, 0], [75, 0.8], [85, 1], [95, 1.1]]}
    d = dict(rr.compare(_ws(drafts), {"items": [{"name": "时效系数", "tiers": [{"input": "耗时，分钟", "cutoffs": [0, 30],
                                                                            "rates": [1.1, 0.8]}]}], "error": ""}))
    assert d == {}


def test_material_has_headers_not_rows(tmp_path):
    ws = _ws(DRAFTS, [(2, ["A1", 123456.78, 60, 10, "已审批", "2026-07-03"])])
    ws.read_document = lambda *a, **k: ""
    ws.said = ""
    text = rr.material(ws)
    assert "金额（数字）" in text and "123456" not in text and "A1" not in text     # 数据表只给表头和类型
    assert "## 工作表 参数.xlsx/参数（整表）" in text                                # 参数表整表给


def test_finish_blocks_until_reread_diff_handled(tmp_path):
    from bonus_agent.agent import run_agent
    from bonus_agent.llm import FakeLLM
    from tests.test_syn_acceptance import _script, run_case
    claim = json.dumps({"items": [{"name": "净利润", "columns": ["销售明细/expense_amount"],
                                   "constants": [{"value": 0.123, "meaning": "不存在的比例"}]}]}, ensure_ascii=False)
    llm = FakeLLM(_script())
    llm.reread = claim
    ar = run_agent(llm, [run_case.XLSX], tmp_path / "a")
    blocked = [s for s in ar.steps if s.tool == "finish" and not s.ok]
    assert ar.status != "OK" and blocked and "[复读 常数 0.123]" in blocked[0].result
    # 核对后认为规则对：写进 ignored 放过，口径单上标出请财务看
    script = _script()
    script[-1] = {"tool": "finish", "args": {"summary": "三个方案", "ignored": [
        {"ref": "复读 常数 0.123", "reason": "材料里没有这个比例"}, {"ref": "复读 列 销售明细/expense_amount", "reason": "x"}]}}
    llm = FakeLLM(script)
    llm.reread = claim
    ar = run_agent(llm, [run_case.XLSX], tmp_path / "b")
    assert ar.status == "OK", ar.error
    from bonus_agent import review as rv
    rows = {r["item"]: r for r in rv.load(tmp_path / "b")}
    assert rows["复读 常数 0.123"]["flag"].startswith("独立复读和规则在这里不一致")
    # 复读调用失败不拦，口径单上标出
    llm = FakeLLM(_script())
    llm.reread = "不是 JSON"
    ar = run_agent(llm, [run_case.XLSX], tmp_path / "c")
    assert ar.status == "OK" and ar.ws.reread["error"]
    assert any(r["item"] == "独立复读没有完成" and r["flag"] for r in rv.load(tmp_path / "c"))


def _calc(pool, helpers=None):
    tables = {"r": {"kind": "lookup", "mapping": {"华东": 1.1, "华南": 1.0, "华北": 0.9, "西部": 1.2}},
              "d": {"kind": "lookup", "mapping": {"1": 1.0, "2": 1.1, "3": 1.25}}}
    return json.dumps({"schemes": {"PROCESS": {"tables": tables, "helpers": helpers or {}, "pool": pool}}}, ensure_ascii=False)


PROCESS_OK = ('sum(round(base_bonus * lookup(r, region) * if(contract_date >= "2026-01-01", 1.10, 0.95) * lookup(d, difficulty), 2), '
              'of: "里程碑", where: status == "已验收" and between(acceptance_date, "2026-07-01", "2026-09-30"))')


def test_independent_pool_formula_compared_on_data(tmp_path):
    """复读只按材料另写奖金池算式（自己的参数表和辅助变量），放到同一份数据上和规则逐个对象比数：
    写法和规则不同但算出来一样的不拦；漏了系数、算出来不一样的拦下；算式写错先带着报错让它自己改。"""
    from bonus_agent.agent import run_agent
    from bonus_agent.llm import FakeLLM
    from tests.test_syn_acceptance import _script, run_case
    llm = FakeLLM(_script())
    llm.calc = _calc(PROCESS_OK)
    ar = run_agent(llm, [run_case.XLSX], tmp_path / "a")
    assert ar.status == "OK", ar.error
    calc = ar.ws.reread["calc"]
    assert "PROCESS" in calc["schemes"] and "PROCESS" not in calc["error"]          # 没写的方案只是不比
    # 漏乘难度系数：两个部门都对不上，结束前拦下，写明两边的金额和复读的算式
    llm = FakeLLM(_script())
    llm.calc = _calc(PROCESS_OK.replace(" * lookup(d, difficulty)", ""))
    ar = run_agent(llm, [run_case.XLSX], tmp_path / "b")
    blocked = [s for s in ar.steps if s.tool == "finish" and not s.ok]
    assert ar.status != "OK" and blocked and "[复读 奖金池 PROCESS]" in blocked[0].result
    assert "部门:D01 复读" in blocked[0].result and "规则 222,727.50" in blocked[0].result
    # 第一次写了不存在的字段：把报错交还它，第二次写对了就用第二次的
    llm = FakeLLM(_script())
    llm.calc = [_calc(PROCESS_OK.replace("base_bonus", "no_such_field")), _calc(PROCESS_OK)]
    ar = run_agent(llm, [run_case.XLSX], tmp_path / "c")
    assert ar.status == "OK" and "PROCESS" not in ar.ws.reread["calc"]["error"], ar.ws.reread["calc"]
    fix = [m for m in llm.calls if str(m[0].get("content", "")).startswith("【独立算式】") and len(m) > 2]
    assert fix and "算不出来" in fix[0][-1]["content"]


def test_independent_pay_ratio_compared_on_data(tmp_path):
    """复读另写可发比例（本次可发上限 ÷ 应得），在发放层每个对象上和规则的可发上限比：
    写法不同但一样的不拦；回款没按本期销售单筛选（比例偏高）的拦下，写明两边的比例。"""
    from bonus_agent.agent import run_agent
    from bonus_agent.llm import FakeLLM
    from tests.test_syn_acceptance import _script, run_case
    helpers = {"pr": {"scope": "项目", "expr": 'sum(receipt_amount, of: "回款明细", where: between(recognition_date, "2026-07-01", '
                                               '"2026-09-30") and receipt_date <= "2026-09-30")'},
               "ps": {"scope": "项目", "expr": 'sum(sales_amount, of: "销售明细", where: between(recognition_date, "2026-07-01", "2026-09-30"))'}}
    pay = 'min(ratio(sum(pr * share, of: "项目参与"), sum(ps * share, of: "项目参与"), zero: 0), 1)'

    def spec(h):
        d = json.loads(_calc(PROCESS_OK, h))
        d["schemes"]["PROCESS"]["pay_ratio"] = pay
        return json.dumps(d, ensure_ascii=False)
    llm = FakeLLM(_script())
    llm.calc = spec(helpers)
    ar = run_agent(llm, [run_case.XLSX], tmp_path / "a")
    assert ar.status == "OK", ar.error
    assert ar.ws.reread["calc"]["schemes"]["PROCESS"]["pay_ratio"] == pay
    wrong = dict(helpers, pr={"scope": "项目", "expr": 'sum(receipt_amount, of: "回款明细", where: receipt_date <= "2026-09-30")'})
    llm = FakeLLM(_script())
    llm.calc = spec(wrong)
    ar = run_agent(llm, [run_case.XLSX], tmp_path / "b")
    blocked = [s for s in ar.steps if s.tool == "finish" and not s.ok]
    assert ar.status != "OK" and blocked and "[复读 可发比例 PROCESS]" in blocked[0].result, [s.result[:300] for s in blocked]
    assert "（可发上限 ÷ 应得）" in blocked[0].result


def test_independent_formula_needs_two_consistent_samples(tmp_path):
    """两次独立写的都和规则对不上、但两次彼此也不一致（复读自己没读准）：不拦，口径单注明没有定论。"""
    from bonus_agent import review as rv
    from bonus_agent.agent import run_agent
    from bonus_agent.llm import FakeLLM
    from tests.test_syn_acceptance import _script, run_case
    llm = FakeLLM(_script())
    # 第一次（其他方案没写，带着报错重试 3 次）都是 A，第二次独立写是 B
    llm.calc = [_calc(PROCESS_OK.replace(" * lookup(d, difficulty)", ""))] * 3 + [_calc(PROCESS_OK.replace("1.10, 0.95", "1.2, 0.9"))]
    ar = run_agent(llm, [run_case.XLSX], tmp_path / "a")
    assert ar.status == "OK", ar.error
    assert ar.ws.reread["calc"]["unsettled"] == {"PROCESS": ["奖金池"]}
    assert any(r["item"] == "独立算式没有定论：PROCESS" for r in rv.load(tmp_path / "a"))
