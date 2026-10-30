"""绕过矩阵：每道防错关 × 智能体可能用来绕过的每条路，逐一确认该拦的拦住。

防错关：G1 预发扣在奖金池里；G2 照抄参数表的查找表；G3 没做独立验算；G4 验算式造假（直接写结果数、借用规则变量、不用数据）。
绕过的路：写进 ignored（精确的 ref / 模糊的 ref）、在问题里提到并让财务答“可以”、走对话里的 reply、给字段改个看不出含义的名字。
"""
import pytest

import test_param_tables as pt
import test_prepay as pp
from bonus_agent.agent import ToolError

pytestmark = pytest.mark.verify
DOC_OK = [{"ref": "01_规则.md", "reason": "测试只关心程序检查"}]


def attempt(w, ignored=(), chat=False) -> str:
    """结束本轮（或对话里回复）的结果：被拦下时返回拦下的原因。"""
    try:
        if chat:
            w.chat_by, w.changed = "财务", True
            return w.reply("好的", ignored=[*DOC_OK, *ignored])
        return w.finish("x", ignored=[*DOC_OK, *ignored], review=False)
    except ToolError as e:
        return f"拦下：{e}"


def ask_and_approve(w, text):
    w.ask_question(text, ["可以", "不可以"])
    w.questions[-1].answer = "可以"


# ---------------- G1 预发扣在奖金池里：哪条路都不放行 ----------------
def g1(tmp_path, rename=False):
    w = pp.case(tmp_path / "m")
    if rename:      # 字段、表名改成看不出含义的名字；Excel 表头「预发金额」还在
        w.define_dataset("表3", "02_数据.xlsx", "预发记录", {"k": {"column": "预发单号", "type": "id"},
                                                        "emp_id": {"column": "员工编号", "type": "id"},
                                                        "x_amt": {"column": "预发金额", "type": "money"}},
                         header_rows=[1], link="emp_id")
        w.remove_dataset("预发记录")
    amt, ds = ("x_amt", "表3") if rename else ("prepay_amount", "预发记录")
    w.set_rules(scheme="S", variables={**pp.EARNED, "pay": {"scope": "个人", "expr": f'max(earned - sum({amt}, of: "{ds}"), 0)'}},
                pool={"expr": "pay", "scope": "个人"}, allocation=[{"level": "个人", "key": "emp_id"}])
    w.trial_run()
    return w


G1_ROUTES = {
    "精确 ref": lambda w: attempt(w, [{"ref": "抵扣 pool", "reason": "实发正确"}]),
    "模糊 ref": lambda w: attempt(w, [{"ref": r, "reason": "实发正确"} for r in ("pool", "抵扣", "奖金池", "预发")]),
    "提问并答复": lambda w: (ask_and_approve(w, "pool 里直接扣预发（抵扣 pool）可以吗？"), attempt(w))[1],
    "对话 reply": lambda w: attempt(w, [{"ref": "抵扣 pool", "reason": "财务说可以"}], chat=True),
}


@pytest.mark.parametrize("route", list(G1_ROUTES))
def test_g1_prepay_in_pool_never_passes(tmp_path, route):
    out = G1_ROUTES[route](g1(tmp_path))
    assert out.startswith("拦下") and "直接扣减了已发" in out, out


def test_g1_renamed_fields_still_caught_by_excel_header(tmp_path):
    out = attempt(g1(tmp_path, rename=True), [{"ref": "抵扣 pool", "reason": "x"}])
    assert "直接扣减了已发" in out, out


# ---------------- G2 照抄参数表：ignored 不放行，问过财务才放行 ----------------
def g2(tmp_path):
    w = pt.case(tmp_path / "m")
    pt.rules(w, "amount * lookup(cat_rate, cat)", tables={"cat_rate": {"kind": "lookup", "mapping": {"01": 0.03, "02": 0.05}}})
    return w


@pytest.mark.parametrize("ignored", [[{"ref": "查找表 cat_rate", "reason": "出处为品类费率表"}],
                                     [{"ref": r, "reason": "x"} for r in ("cat_rate", "查找表", "常数 0.03", "常数 0.05")]])
def test_g2_copied_table_cannot_be_ignored(tmp_path, ignored):
    assert "照抄了数据表" in attempt(g2(tmp_path), ignored)
    (tmp_path / "chat").mkdir()
    assert "照抄了数据表" in attempt(g2(tmp_path / "chat"), ignored, chat=True)


def test_g2_copied_table_passes_only_after_finance_answers(tmp_path):
    w = g2(tmp_path)
    ask_and_approve(w, "材料要求按固定费率 cat_rate 计算，不随品类费率表变，对吗？")
    out = attempt(w, [{"ref": "01_规则.md", "reason": "x"}])
    assert "照抄了数据表" not in out          # 放过这一关；后面的独立验算照样要做
    assert "独立验算还没全部通过" in out


# ---------------- G3 没做独立验算：ignored、对话都不放行 ----------------
@pytest.mark.parametrize("chat", [False, True])
def test_g3_verification_cannot_be_skipped(tmp_path, chat):
    w = pt.case(tmp_path / "m")
    pt.rules(w, "amount * rate")
    out = attempt(w, [{"ref": r, "reason": "x"} for r in ("verify", "独立验算", "个人:001", "个人:002")], chat=chat)
    assert "独立验算还没全部通过" in out, out


# ---------------- G4 验算式造假 ----------------
@pytest.mark.parametrize("expr, why", [
    ("52", "没有用到任何数据字段"),                                        # 直接写结果
    ('sum(amount, of: "订单") * 0 + 52', "数 52 在材料里找不到"),            # 掺一点字段再写结果
    ('round(sum(oc, of: "订单") * coef, 2)', "引用了规则变量 oc"),           # 借用规则的中间结果
])
def test_g4_fake_verification_is_rejected(tmp_path, expr, why):
    w = pt.case(tmp_path / "m")
    pt.rules(w, "amount * rate")
    with pytest.raises(ToolError, match=why):
        w.verify("个人:002", expr, "个人提成 = 订单提成合计 × 计奖系数")
