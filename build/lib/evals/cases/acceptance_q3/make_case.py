"""生成多表关联样例：仿照财务验收测试集的结构（部门 → 小组 → 个人，项目、销售单、回款、参与比例、里程碑）。

用来检验引擎的关联实体、日期期间、按部门算奖金包、数据校验等能力。规则：
- 季度奖（QUARTER，2026 年第三季度）：逐项目算有效销售额 = 期间销售额 × 区域系数 × 新老项目系数 × 毛利率系数；
  部门奖金包 = ROUND((全额阶梯(部门有效销售额) + MAX(部门净利润, 0) × 5%) × 完成系数, 2)；
  小组权重 = ROUND(绩效评分 / 100 × MIN(小组实际销售额 / 小组目标, 1.2), 6)；
  个人权重 = 工龄、贡献评分、项目难度、销售完成、项目数量、产出评分六项归一化后加权，ROUND 到 6 位；
- 过程奖（PROCESS）：本季度已验收里程碑，单条 = ROUND(基础奖金 × 区域系数 × 新老系数 × 难度系数, 2)，部门合计。

同目录的 rules_quarter.yaml / rules_process.yaml 是手写的标准规则，expected.json 由本文件里独立的
Python 实现算出（不经过引擎），测试用它核对引擎结果。运行：python evals/cases/acceptance_q3/make_case.py
"""
import json
import random
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal as D
from pathlib import Path

import openpyxl

HERE = Path(__file__).parent
rng = random.Random(20260930)

Q3 = (date(2026, 7, 1), date(2026, 9, 30))
REGION = {"华东": D("1.10"), "华南": D("1.00"), "华北": D("0.90"), "西部": D("1.20")}
DIFF = {1: D("1.00"), 2: D("1.10"), 3: D("1.25")}

DEPTS = [("D01", "企业业务部"), ("D02", "行业业务部")]
GROUPS = [("G01", "D01", "1部1组", 94), ("G02", "D01", "1部2组", 88), ("G03", "D02", "2部1组", 85), ("G04", "D02", "2部2组", 96)]
EMPS = [("E001", "G01", date(2017, 1, 1)), ("E002", "G01", date(2020, 10, 1)), ("E003", "G02", date(2023, 1, 1)),
        ("E004", "G02", date(2026, 1, 1)), ("E005", "G03", date(2017, 6, 30)), ("E006", "G03", date(2021, 9, 30)),
        ("E007", "G04", date(2019, 3, 1)), ("E008", "G04", date(2024, 2, 1))]
DEPT_OF = {g: d for g, d, _, _ in GROUPS}
# 项目：编号、小组、区域、合同日期、难度、成本率、费用率、月销售额基数
PROJECTS = [("P01", "G01", "华东", date(2026, 1, 1), 1, D("0.62"), D("0.05"), 900000),
            ("P02", "G01", "华南", date(2025, 7, 1), 2, D("0.78"), D("0.06"), 700000),
            ("P03", "G02", "华北", date(2026, 3, 1), 3, D("0.70"), D("0.05"), 800000),
            ("P04", "G02", "西部", date(2025, 11, 30), 1, D("0.88"), D("0.20"), 500000),   # 低毛利、亏损
            ("P05", "G03", "华东", date(2025, 12, 31), 2, D("0.66"), D("0.04"), 450000),
            ("P06", "G03", "华南", date(2026, 2, 1), 3, D("0.75"), D("0.05"), 380000),
            ("P07", "G04", "西部", date(2026, 1, 1), 2, D("0.60"), D("0.05"), 420000),
            ("P08", "G04", "华北", date(2024, 5, 1), 1, D("0.80"), D("0.03"), 300000)]
SHARES = {"P01": [("E001", "0.5"), ("E002", "0.5")], "P02": [("E002", "0.34"), ("E001", "0.33"), ("E003", "0.33")],
          "P03": [("E003", "0.6"), ("E004", "0.4")], "P04": [("E004", "1")],
          "P05": [("E005", "0.5"), ("E006", "0.5")], "P06": [("E006", "0.7"), ("E005", "0.3")],
          "P07": [("E007", "0.5"), ("E008", "0.5")], "P08": [("E008", "0.25"), ("E007", "0.75")]}


def money(x) -> D:
    return D(x).quantize(D("0.01"), ROUND_HALF_UP)


def build():
    sales, receipts = [], []
    for pid, _, _, _, _, cr, er, base in PROJECTS:
        for m in range(1, 13):
            amt = money(base * (1 + rng.randint(-15, 25) / 100))
            sid = f"S{pid[1:]}{m:02d}"
            sales.append((sid, pid, date(2026, m, 15), amt, money(amt * cr), money(amt * er)))
            got = money(amt * D(rng.choice(["0.4", "0.6", "1"])))
            receipts.append((f"R{sid[1:]}1", sid, date(2026, m, 25), got))
            if got < amt and rng.random() < 0.5:
                receipts.append((f"R{sid[1:]}2", sid, date(2026, m, 28) if m < 12 else date(2027, 1, 10), money((amt - got) / 2)))
    scores = []
    for eid, _, _ in EMPS:
        for period in ("Q3", "FY"):
            scores.append((period, eid, rng.randint(60, 98), rng.randint(60, 98), True))
    scores.append(("Q3", "E003", 99, 99, False))   # 未审批的 AI 建议分，不得使用
    milestones = []
    for pid, *_ in PROJECTS:
        for k, (name, d_, st, bb) in enumerate([("交付", date(2026, 8, 20), "已验收", 5000),
                                                ("验收", rng.choice([date(2026, 9, 20), date(2026, 10, 5)]),
                                                 rng.choice(["已验收", "已验收", "待验收"]), 10000)], start=1):
            milestones.append((f"M{pid[1:]}{k}", pid, name, d_, st, bb))
    q3 = {pid: sum((s[3] for s in sales if s[1] == pid and Q3[0] <= s[2] <= Q3[1]), D(0)) for pid, *_ in PROJECTS}
    targets = []
    for did, _ in DEPTS:
        actual = sum(q3[p[0]] for p in PROJECTS if DEPT_OF[p[1]] == did)
        targets.append(("Q3", "department", did, money(actual / D("0.95") if did == "D01" else actual / D("1.08"))))
        for gid, gd, _, _ in GROUPS:
            if gd != did:
                continue
            ga = sum(q3[p[0]] for p in PROJECTS if p[1] == gid)
            targets.append(("Q3", "group", gid, money(ga / D(rng.choice(["0.9", "1.1", "1.4"])))))
            for eid, eg, _ in EMPS:
                if eg == gid:
                    targets.append(("Q3", "employee", eid, money(rng.randint(80, 260) * 10000)))
    return sales, receipts, scores, milestones, targets


def write_xlsx(path, sales, receipts, scores, milestones, targets):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    def sheet(name, header, rows):
        ws = wb.create_sheet(name)
        ws.append(header)
        for r in rows:
            ws.append([float(v) if isinstance(v, D) else v for v in r])
    sheet("部门", ["dept_id", "dept_name"], DEPTS)
    sheet("小组", ["group_id", "dept_id", "group_name", "perf_q3"], GROUPS)
    sheet("员工", ["employee_id", "employee_name", "group_id", "dept_id", "join_date"],
          [(e, f"样例员工{e[1:]}", g, DEPT_OF[g], d) for e, g, d in EMPS])
    sheet("项目", ["project_id", "project_name", "dept_id", "group_id", "region", "contract_date", "difficulty"],
          [(p, f"样例项目{p[1:]}", DEPT_OF[g], g, r, cd, df) for p, g, r, cd, df, *_ in PROJECTS])
    sheet("销售明细", ["sales_id", "project_id", "recognition_date", "sales_amount", "cost_amount", "expense_amount"], sales)
    sheet("回款明细", ["receipt_id", "sales_id", "receipt_date", "receipt_amount"], receipts)
    sheet("项目参与", ["project_id", "employee_id", "share"],
          [(p, e, float(s)) for p, lst in SHARES.items() for e, s in lst])
    sheet("绩效评分", ["period", "employee_id", "contribution_score", "output_score", "approved"], scores)
    sheet("目标值", ["period", "entity_type", "entity_id", "sales_target"], targets)
    sheet("里程碑", ["milestone_id", "project_id", "milestone_name", "acceptance_date", "status", "base_bonus"], milestones)
    wb.save(path)


# ---------------- 独立参考实现（不经过引擎） ----------------
def tier_whole(x, steps):
    rate = D(0)
    for lo, r in steps:
        if x >= lo:
            rate = r
    return rate


def allocate(total: D, weights: dict[str, D]) -> dict[str, D]:
    """按权重分到分：向下取整，剩余的分按余数从大到小补，余数相同按编号升序。"""
    ws = sum(weights.values(), D(0))
    exact = {k: total * w / ws for k, w in weights.items()}
    floor = {k: v.quantize(D("0.01"), ROUND_DOWN) for k, v in exact.items()}
    n = int((total - sum(floor.values(), D(0))) / D("0.01"))
    for k in sorted(exact, key=lambda k: (-(exact[k] - floor[k]), k))[:n]:
        floor[k] += D("0.01")
    return floor


def reference(sales, scores, milestones, targets):
    in_q3 = [s for s in sales if Q3[0] <= s[2] <= Q3[1]]
    proj = {p[0]: p for p in PROJECTS}
    newc = {p: D("1.10") if proj[p][3] >= date(2026, 1, 1) else D("0.95") for p in proj}
    q_sales = {p: sum((s[3] for s in in_q3 if s[1] == p), D(0)) for p in proj}
    q_cost = {p: sum((s[4] for s in in_q3 if s[1] == p), D(0)) for p in proj}
    margin_steps = [(D(0), D(0)), (D("0.15"), D("0.8")), (D("0.25"), D("1.0")), (D("0.35"), D("1.2"))]
    eff = {p: q_sales[p] * REGION[proj[p][2]] * newc[p]
           * tier_whole((q_sales[p] - q_cost[p]) / q_sales[p], margin_steps) for p in proj}
    tgt = {(t[1], t[2]): t[3] for t in targets if t[0] == "Q3"}
    pools, ppools = {}, {}
    for did, _ in DEPTS:
        ps = [p for p in proj if DEPT_OF[proj[p][1]] == did]
        d_eff = sum((eff[p] for p in ps), D(0))
        d_net = sum((s[3] - s[4] - s[5] for s in in_q3 if s[1] in ps), D(0))
        rate = tier_whole(d_eff, [(D(0), D("0.08")), (D(10000000), D("0.10")), (D(20000000), D("0.12"))])
        done = sum((q_sales[p] for p in ps), D(0)) / tgt[("department", did)]
        coef = tier_whole(done, [(D(0), D("0.8")), (D("0.8"), D("1.0")), (D(1), D("1.1"))])
        pools[did] = money((d_eff * rate + max(d_net, D(0)) * D("0.05")) * coef)
        ppools[did] = sum((money(D(m[5]) * REGION[proj[m[1]][2]] * newc[m[1]] * DIFF[proj[m[1]][4]])
                           for m in milestones if m[1] in ps and m[4] == "已验收" and Q3[0] <= m[3] <= Q3[1]), D(0))
    gw = {}
    for gid, did, _, perf in GROUPS:
        ga = sum((q_sales[p] for p in proj if proj[p][1] == gid), D(0))
        gw[gid] = (D(perf) / 100 * min(ga / tgt[("group", gid)], D("1.2"))).quantize(D("0.000001"), ROUND_HALF_UP)
    ew = {}
    sc = {s[1]: s for s in scores if s[0] == "Q3" and s[4]}
    for eid, gid, joined in EMPS:
        parts = [(p, D(s)) for p, lst in SHARES.items() for e, s in lst if e == eid]
        cut = Q3[1]
        years = cut.year - joined.year - ((cut.month, cut.day) < (joined.month, joined.day))
        diff = sum((sh * proj[p][4] for p, sh in parts), D(0)) / sum((sh for _, sh in parts), D(0))
        attrib = sum((q_sales[p] * sh for p, sh in parts), D(0))
        w = (D("0.1") * min(D(years), D(5)) / 5 + D("0.2") * D(sc[eid][2]) / 100 + D("0.15") * diff / 3
             + D("0.3") * min(attrib / tgt[("employee", eid)], D("1.5")) / D("1.5")
             + D("0.1") * min(D(len(parts)), D(8)) / 8 + D("0.15") * D(sc[eid][3]) / 100)
        ew[eid] = w.quantize(D("0.000001"), ROUND_HALF_UP)

    def spread(dept_pools):
        out = {}
        for did, amt in dept_pools.items():
            out[f"部门:{did}"] = amt
            gs = allocate(amt, {g: gw[g] for g, d, _, _ in GROUPS if d == did})
            for g, ga in gs.items():
                out[f"小组:{g}"] = ga
                for e, ea in allocate(ga, {e: ew[e] for e, eg, _ in EMPS if eg == g}).items():
                    out[f"个人:{e}"] = ea
        return {k: str(v) for k, v in out.items()}
    return {"QUARTER": spread(pools), "PROCESS": spread(ppools),
            "weights": {**{f"小组:{k}": str(v) for k, v in gw.items()}, **{f"个人:{k}": str(v) for k, v in ew.items()}}}


if __name__ == "__main__":
    s, r, sc, ms, tg = build()
    write_xlsx(HERE / "业务输入.xlsx", s, r, sc, ms, tg)
    (HERE / "expected.json").write_text(json.dumps(reference(s, sc, ms, tg), ensure_ascii=False, indent=1), encoding="utf-8")
    print("已生成", HERE / "业务输入.xlsx", HERE / "expected.json")
