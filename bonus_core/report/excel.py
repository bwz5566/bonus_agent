"""结果 Excel：按分配树动态生成 sheet。只写数值不写公式，避免打开后被 Excel 重算。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from bonus_core.dsl.models import RuleSet
from bonus_core.engine.run import Result

MONEY = "#,##0.00"
HEAD_FILL = PatternFill("solid", fgColor="DDEBF7")
LOCK_FILL = PatternFill("solid", fgColor="FFF2CC")


def _sheet(wb, title, headers, rows, money_cols=(), widths=None):
    ws = wb.create_sheet(title[:31])
    ws.append(headers)
    for c in ws[1]:
        c.font, c.fill = Font(bold=True), HEAD_FILL
        c.alignment = Alignment(horizontal="center")
    for r in rows:
        ws.append(r)
    for col in money_cols:
        for (cell,) in ws.iter_rows(min_row=2, min_col=col, max_col=col):
            cell.number_format = MONEY
    for i, h in enumerate(headers, start=1):
        w = (widths or {}).get(h) or max(10, min(60, max(len(str(h)) * 2, *(len(str(r[i - 1])) for r in rows) if rows else [0])))
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    return ws


def _d(s) -> Decimal:
    return Decimal(str(s))


def write_excel(rs: RuleSet, res: Result, path: str | Path, ledger: list[dict] | None = None,
                extra_sheets: list[tuple[str, list[str], list[list], dict]] | None = None) -> Path:
    """extra_sheets：(标题, 表头, 行, 列宽) 列表，放在「汇总」之后（如智能体的规划说明、待确认问题）。"""
    path = Path(path)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    nodes = res.nodes
    leaf = res.levels[-1]

    # 汇总
    total_held = sum((_d(p.held) for p in res.payouts.values()), Decimal(0))
    total_paid = sum((_d(p.paid) for p in res.payouts.values()), Decimal(0)) if res.payouts else _d(res.pool)
    rows = [
        ["规则", f"{res.rule_set}（{res.rule_version}）"],
        ["奖金池（精确值）", float(_d(res.pool_exact))],
        ["奖金池（保留到分）", float(_d(res.pool))],
        ["本期实发合计", float(total_paid)],
        ["本期挂起合计", float(total_held)],
        ["调整次数", len([a for a in res.adjustments if not a.revoked])],
    ]
    for lv in res.levels:
        cnt = sum(1 for n in nodes.values() if n.level == lv)
        rows.append([f"「{lv}」节点数", cnt])
    ws = _sheet(wb, "汇总", ["项目", "值"], rows, widths={"项目": 22, "值": 30})
    for r in (3, 4, 5, 6):
        ws.cell(r, 2).number_format = MONEY
    for title, headers, extra_rows, widths in extra_sheets or []:
        _sheet(wb, title, headers, extra_rows, widths=widths)

    # 各层级明细
    for lv in res.levels:
        lv_rows = []
        for n in nodes.values():
            if n.level != lv:
                continue
            p = nodes.get(n.parent)
            t = res.traces.get(f"{n.id}/权重")
            lv_rows.append([
                n.id, n.label, p.label if p else "", float(_d(n.weight)),
                float(_d(n.exact)), float(_d(n.final)), "是" if n.locked else "", t.rendered if t else "",
            ])
        ws = _sheet(wb, f"{lv}明细", ["节点", "名称", "上级", "权重", "精确金额", "金额（到分）", "已调整锁定", "权重算式"],
                    lv_rows, money_cols=(5, 6), widths={"权重算式": 80})
        for i, r in enumerate(lv_rows, start=2):
            if r[6]:
                for c in ws[i]:
                    c.fill = LOCK_FILL

    # 个人发放
    conds = [c.id for c in rs.payout]
    pay_rows = []
    for nid, p in res.payouts.items():
        n = nodes[nid]
        pay_rows.append([nid, n.label, nodes[n.parent].label, float(_d(p.final)), float(_d(p.held)),
                         float(_d(p.cap or p.paid)), float(_d(p.offset)), float(_d(p.paid)), float(_d(p.overpaid))]
                        + [float(_d(p.holds[c])) for c in conds])
    _sheet(wb, f"{leaf}发放", ["节点", "名称", "上级", "应得", "暂缓", "可发上限", "已发抵扣", "本次实发", "超发待核对"]
           + [f"挂起：{c}" for c in conds], pay_rows, money_cols=range(4, 10 + len(conds)))

    # 调整记录
    adj_rows = [[a.created_at, a.node_id, nodes[a.node_id].label if a.node_id in nodes else "", float(a.amount),
                 a.actor, a.reason, "已撤销" if a.revoked else "生效"] for a in res.adjustments]
    _sheet(wb, "调整记录", ["时间", "节点", "名称", "调整为", "调整人", "原因", "状态"], adj_rows, money_cols=(4,))

    # 未发放台账
    led = ledger if ledger is not None else [
        {"node_id": nid, "condition": c, "held": p.holds[c], "rule_version": res.rule_version}
        for nid, p in res.payouts.items() for c in conds if _d(p.holds[c]) > 0
    ]
    _sheet(wb, "未发放台账", ["节点", "名称", "挂起条件", "挂起金额", "规则版本"],
           [[e["node_id"], nodes[e["node_id"]].label, e["condition"], float(_d(e["held"])), e["rule_version"]] for e in led],
           money_cols=(4,))

    # 计算链
    _sheet(wb, "计算链", ["追踪 ID", "规则元素", "公式", "代入后算式", "结果", "依赖", "明细行"],
           [[t.id, t.rule_item, t.expr, t.rendered, t.value, ", ".join(t.inputs), ", ".join(t.rows[:20])]
            for t in res.traces.values()], widths={"公式": 50, "代入后算式": 60, "依赖": 40, "明细行": 40})

    # 异常与版本
    _sheet(wb, "异常与提示", ["级别", "代码", "说明", "位置"],
           [[i.severity, i.code, i.message, i.where] for i in res.issues], widths={"说明": 70, "位置": 40})
    _sheet(wb, "规则与数据版本", ["项目", "值"], [
        ["规则集", res.rule_set], ["规则版本", res.rule_version], ["引擎版本", res.engine_version],
        ["输入哈希（规则+数据+调整）", res.input_hash],
        ["数据哈希（规则+数据）", res.data_hash],
    ], widths={"项目": 14, "值": 70})

    wb.save(path)
    _self_check(path, res)
    return path


def _self_check(path: Path, res: Result) -> None:
    """重新读取文件，核对各层级合计与快照一致。"""
    wb = openpyxl.load_workbook(path, data_only=True)
    pool = _d(res.pool)
    for lv in res.levels:
        ws = wb[f"{lv}明细"[:31]]
        s = sum((_d(round(r[5], 2)) for r in ws.iter_rows(min_row=2, values_only=True)), Decimal(0))
        if s.quantize(Decimal("0.01")) != pool:
            raise AssertionError(f"结果 Excel 自检失败：「{lv}」合计 {s} ≠ 奖金池 {pool}")
