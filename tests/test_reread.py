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
