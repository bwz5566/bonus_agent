"""口径单：规则机械翻译成中文、按内容保留确认、有误交给智能体、确认前必须逐条确认。"""
from types import SimpleNamespace as NS

from bonus_agent import review as rv

NM = rv.Names({"amount": "金额", "cost": "成本", "fee": "费用", "status": "状态", "region": "区域"},
              {"profit": "净利润", "rate": "提成比例"}, {"R_REGION": "区域系数", "R_RATE": "提成档位"})


def test_plain_renders_fields_variables_and_functions():
    assert rv.plain('sum(amount - cost, of: "明细", where: status == "已审批")', NM) == \
        "「明细」中满足〔「状态」 为 “已审批”〕的行的（「金额」 − 「成本」） 合计"
    assert rv.plain("profit * lookup(R_REGION, region)", NM) == "〔净利润〕 × 按《区域系数》查 「区域」 对应的值"
    assert rv.plain("profit - (cost - fee)", NM) == "〔净利润〕 − (「成本」 − 「费用」)"      # 右边的减法要加括号
    assert rv.plain("if(profit > 0, round(profit * 0.1, 2), 0)", NM) == \
        "（如果 〔净利润〕 > 0，则 四舍五入（〔净利润〕 × 0.1，保留 2 位小数），否则 0）"
    assert rv.plain("ratio(profit, amount, zero: 0)", NM) == "(〔净利润〕) ÷ (「金额」)（分母为 0 时取 0）"
    assert rv.plain("profit +", NM) == "profit +"                                            # 解析不了原样给出


def test_tier_and_lookup_text():
    t = rv.tier_text({"steps": [[0, 0.01], [100000, 0.02]], "mode": "excess"})
    assert t.startswith("超额累进") and "0（含）至 100000（不含）：0.01" in t and "达到 100000：0.02" in t
    assert rv.tier_text({"steps": [[1, 2]], "lower_inclusive": False}) == "全额：整个数按所在档计算。不超过 1：0；超过 1：2"
    assert rv.lookup_text({"mapping": {"东区": 1.1}}) == "东区 → 1.1；表里没有的取值会报错停下"


def _ws(profit="sum(amount - cost)"):
    field = lambda t: NS(type=t)                                                          # noqa: E731
    return NS(
        mapping={"sales": NS(columns={"amount": "金额", "cost": "成本", "fee": "费用"}, file="数据.xlsx", sheet="明细",
                             filter="")},
        datasets={"sales": NS(fields={"amount": field("money"), "cost": field("money"), "fee": field("money")})},
        drafts={"共用": {"variables": {"profit": {"scope": "团队", "expr": profit, "desc": "净利润"}}},
                "季度奖": {"variables": {"rate": {"scope": "团队", "expr": "0.1", "desc": "提成比例"}},
                         "pool": {"scope": "团队", "expr": "profit * rate"},
                         "tables": {"R_RATE": {"kind": "tier", "steps": [[0, 1]], "desc": "提成档位"}}}},
        clauses=[NS(where="variables.profit", quote="净利润=收入-成本-费用", file="制度.docx")],
        lint={"季度奖": NS(gates=["分档表 R_RATE 的分档点和实际传入值差很多"], warns=[])},
        ignored=[], questions=[])


def test_build_rows_keys_flags_and_sources():
    rows = rv.build(_ws())
    by = {r["item"]: r for r in rows}
    assert [r["id"] for r in rows] == [f"K{i:03d}" for i in range(1, len(rows) + 1)]
    p = by["净利润（profit）"]
    assert p["reading"].startswith("明细（「金额」 − 「成本」） 合计") and "计算对象：团队" in p["reading"]
    assert p["source"] == "净利润=收入-成本-费用（制度.docx）"
    assert by["提成档位（R_RATE）"]["flag"]                                               # 程序检查标出的行
    assert "没用到的金额列：费用" in by["sales"]["reading"]
    assert by["奖金池（按团队计算）"]["reading"] == "〔净利润〕 × 〔提成比例〕"
    # 规则没变 key 不变；改了哪条只有那条的 key 变
    again = {r["item"]: r["key"] for r in rv.build(_ws())}
    assert again == {r["item"]: r["key"] for r in rows}
    changed = {r["item"]: r["key"] for r in rv.build(_ws("sum(amount - cost - fee)"))}
    diff = {k for k in again if again[k] != changed.get(k)}
    assert diff == {"净利润（profit）", "sales"}                                           # 费用列用上了，数据范围那行也变


def test_marks_follow_content_and_wrong_goes_to_agent(tmp_path):
    ws = _ws()
    rv.write(ws, tmp_path)
    rows = rv.load(tmp_path)
    keys = [r["key"] for r in rows]
    assert rv.status(tmp_path)["confirmed"] == 0
    profit = next(r for r in rows if r["item"] == "净利润（profit）")
    assert rv.mark(tmp_path, {profit["key"]: (rv.WRONG, " ")}, "财务甲") == [f"{profit['id']} 标了有误，请写明哪里不对"]
    assert rv.mark(tmp_path, {profit["key"]: (rv.WRONG, "还要减费用")}, "财务甲") == []
    rv.mark(tmp_path, {k: (rv.OK, "") for k in keys if k != profit["key"]}, "财务甲")
    st = rv.status(tmp_path)
    assert [r["id"] for r in st["wrong"]] == [profit["id"]] and st["pending"] == st["wrong"]
    fix = rv.instruction(tmp_path)
    assert "净利润（profit）" in fix and "还要减费用" in fix and "明细（「金额」" in fix
    rv.mark_sent(tmp_path)
    assert rv.instruction(tmp_path) == ""                                                 # 不重复交给智能体
    # 智能体改了这条：只有内容变了的行要重新确认，其余保留
    rv.write(_ws("sum(amount - cost - fee)"), tmp_path)
    st = rv.status(tmp_path)
    assert {r["item"] for r in st["pending"]} == {"净利润（profit）", "sales"} and not st["wrong"]


def test_sheet_has_rows_and_headers(tmp_path):
    rows = rv.build(_ws())
    title, headers, data, _ = rv.sheet(rows=rows)
    assert title == "口径单" and headers[0] == "序号" and len(data) == len(rows)
