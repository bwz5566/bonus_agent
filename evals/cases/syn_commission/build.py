"""造一套“销售提成”结构的样例：材料（制度、口径、销售单和参考表、分配名单）和独立算出的期望结果。
全部是造的公司、客户和人名，数字由固定的随机种子生成；规则是为样例编的，不对应任何真实制度。
用来让智能体从头做一遍“读材料 → 写规则 → 试算 → 出结果”，核对能不能做完、对不对。

    python evals/cases/syn_commission/build.py      # 重新生成 inputs/、scenario/ 和 expected/

样例里特意放了真实材料常见的难点：同一张表两列同名、几列合成项目编号、金额为 0 的剔除行、年内累计、
参考表里多出来的客户、查不到的系数、负数、名单里“姓名+百分比”和空格子。
"""
from __future__ import annotations

import random
from collections import defaultdict
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal as D
from pathlib import Path

import openpyxl

HERE = Path(__file__).resolve().parent
MONTH = 202609
CENT = D("0.01")

# 客户编码, 销售单第一列简称（集团口径）, 最后一列简称（参考表用的口径）, 区域
CUSTOMERS = [
    ("C001", "蓝杉科技", "蓝杉科技", "华东"), ("C002", "云栖电子", "云栖电子", "华东"),
    ("C003", "北辰实业", "北辰实业", "华北"), ("C004", "南屿通讯", "南屿通讯", "华南"),
    ("C005", "禾木智能", "禾木智能", "华南"), ("C006", "青岚光电", "青岚光电", "华北"),
    ("C007", "安禾集团", "安禾", "华东"), ("C007", "安禾集团", "安禾海外", "海外"),
    ("C008", "泽川微", "泽川微", "西南"), ("C009", "明岳", "明岳", "华东"),
    ("C010", "森蔚", "森蔚", "华北"),
]
MODELS = ["MA-100", "MA-200", "MB-300", "MC-500"]
PEOPLE = ["林一舟", "赵青", "何闻溪", "顾南乔", "沈砚", "陆知夏", "秦朗", "苏禾"]
ROLES = [("销售负责人", D("0.5")), ("销售执行", D("0.3")), ("技术支持", D("0.2"))]

SIZE = {"森蔚": 0.12, "泽川微": 0.1, "禾木智能": 0.45, "安禾海外": 0.3}      # 小客户，月均落在不同档

TIER = [(D(0), D("0.010")), (D(100000), D("0.015")), (D(500000), D("0.020"))]      # 超额累进


def tier_amount(x: D) -> D:
    if x <= 0:
        return x * TIER[0][1]            # 负数按第一档比例照算
    total = D(0)
    for i, (lo, rate) in enumerate(TIER):
        hi = TIER[i + 1][0] if i + 1 < len(TIER) else None
        if x > lo:
            total += (min(x, hi) - lo if hi is not None else x - lo) * rate
    return total


def reach_coef(rate: D | None) -> D:
    if rate is None:
        return D(1)
    return D("1.1") if rate >= D("1.2") else D("1.0") if rate >= 1 else D("0.9") if rate >= D("0.8") else D("0.8")


def scale_coef(avg: D) -> D:
    return D("1.05") if avg >= 200000 else D("1.0") if avg >= 50000 else D("0.9")


def margin_coef(m: D | None) -> D:
    if m is None:
        return D(1)
    return D("1.0") if m >= D("0.2") else D("0.9") if m >= D("0.1") else D("0.8")


def stock_coef(v: D | None) -> D:
    return D(1) if not v else D("0.9") if v >= 100000 else D("0.95")


def overdue_coef(v: D | None) -> D:
    return D(1) if not v else D("0.85") if v >= 50000 else D("0.95")


def make_data():
    rnd = random.Random(20261110)
    sales, order = [], 0
    for m in range(1, 10):
        month = 202600 + m
        for code, grp, short, region in CUSTOMERS:
            if short == "森蔚" and m not in (2, 5, 9):          # 只有几个月出货
                continue
            for model in rnd.sample(MODELS, rnd.choice([1, 2, 2])):
                for _ in range(rnd.choice([1, 1, 2, 3])):
                    order += 1
                    amt = rnd.choice([1, 1, 1, 2]) * rnd.randrange(8000, 260000, 500)
                    amt = int(amt * SIZE.get(short, 1) / 100) * 100
                    sales.append([month, f"SO{order:05d}", code, grp, region, model, rnd.randrange(10, 900), amt, "正常", "", short])
    # 退货（负数）：其中一个项目整体为负
    order += 1
    sales.append([MONTH, f"SO{order:05d}", "C006", "青岚光电", "华北", "MC-500", -40, -18500, "正常", "退货", "青岚光电"])
    sales[:] = [r for r in sales if not (r[0] == MONTH and r[10] == "青岚光电" and r[5] == "MC-500" and r[7] > 0)]
    # 剔除行：金额为 0。一个只有剔除行的 9 月项目；一个只有剔除行的月份（不算有出货的月份）
    for month, code, grp, region, model, short in [(MONTH, "C003", "北辰实业", "华北", "MB-300", "北辰实业"),
                                                    (MONTH, "C001", "蓝杉科技", "华东", "MA-100", "蓝杉科技"),
                                                    (202603, "C010", "森蔚", "华北", "MA-200", "森蔚"),
                                                    (202607, "C010", "森蔚", "华北", "MA-200", "森蔚")]:
        order += 1
        sales.append([month, f"SO{order:05d}", code, grp, region, model, 0, 0, "剔除", "内部调拨", short])
    sales[:] = [r for r in sales if not (r[0] == MONTH and r[10] == "北辰实业" and r[5] == "MB-300" and r[7] != 0)]
    sales.sort(key=lambda r: (r[0], r[1]))

    budget = []
    for region in ["华东", "华北", "华南", "海外"]:          # 西南没有预算
        for m in range(1, 10):
            real = sum(r[7] for r in sales if r[0] == 202600 + m and r[4] == region)
            b = int(real * rnd.choice([0.75, 0.9, 1.05, 1.3]) / 1000) * 1000
            if region == "华南" and m == 9:
                b = 0                                          # 当月预算为 0
            budget.append([region, 202600 + m, b])
    pairs = sorted({(r[10], r[5]) for r in sales})
    margin = [[s, mo, round(rnd.choice([0.05, 0.12, 0.18, 0.25, 0.31]), 4)] for s, mo in pairs if rnd.random() < 0.8]
    margin = [r for r in margin if not (r[0] == "云栖电子")]              # 云栖电子全部查不到
    margin.append(["禾木智能", "MZ-900", 0.22])                           # 销售单里没有的型号
    for r in margin:
        if r[0] == "南屿通讯":
            r[2] = -0.04                                                 # 负毛利
    first = []
    for s, mo in pairs:
        if rnd.random() < 0.9:
            y, mth = rnd.choice([(2024, 3), (2024, 11), (2025, 2), (2025, 3), (2025, 4), (2025, 10), (2026, 2), (2026, 6)])
            first.append([s, mo, date(y, mth, rnd.randrange(1, 28))])
    stock = [["蓝杉科技", 46000], ["北辰实业", 152000], ["安禾海外", 99999.5], ["未合作客户甲", 300000]]
    overdue = [["北辰实业", 20000], ["禾木智能", 76000], ["安禾", 50000], ["未合作客户乙", 12000]]
    debt = [["明岳"], ["未合作客户丙"]]

    roster = []
    for month in (202608, MONTH):
        projs = sorted({(r[2], r[10], r[5]) for r in sales if r[0] == month and r[7] != 0})
        for code, short, model in projs:
            cells = []
            for _ in ROLES:
                k = rnd.random()
                who = rnd.choice(PEOPLE)
                cells.append(who if k < 0.6 else f"{who}{rnd.choice([50, 60, 80])}%" if k < 0.82 else None)
            roster.append([month, code, short, model, *cells])
    roster.append([MONTH, "C011", "未出货客户", "MA-100", "秦朗", "苏禾", None])       # 名单里多出来的
    return dict(sales=sales, budget=budget, margin=margin, first=first, stock=stock, overdue=overdue, debt=debt, roster=roster)


def reference(d) -> tuple[list[dict], dict[str, D]]:
    """独立算一遍：每个项目的各项系数和金额，每个人的分配金额。"""
    live = [r for r in d["sales"] if r[7] != 0]
    proj = defaultdict(D)
    for r in live:
        if r[0] == MONTH:
            proj[(r[2], r[10], r[5], r[4])] += D(str(r[7]))
    region_real = defaultdict(D)
    for r in live:
        if r[0] == MONTH:
            region_real[r[4]] += D(str(r[7]))
    budget = {(b[0], b[1]): D(str(b[2])) for b in d["budget"]}
    ytd, months = defaultdict(D), defaultdict(set)
    for r in live:
        if r[0] <= MONTH:
            ytd[r[10]] += D(str(r[7]))
            months[r[10]].add(r[0])
    margin = {(m[0], m[1]): D(str(m[2])) for m in d["margin"]}
    first = {(f[0], f[1]): f[2] for f in d["first"]}
    stock = {s[0]: D(str(s[1])) for s in d["stock"]}
    overdue = {s[0]: D(str(s[1])) for s in d["overdue"]}
    debt = {s[0] for s in d["debt"]}
    roster = {(r[1], r[2], r[3]): r[4:] for r in d["roster"] if r[0] == MONTH}
    rows, people = [], defaultdict(D)
    for (code, short, model, region), amt in sorted(proj.items()):
        b = budget.get((region, MONTH))
        rate = region_real[region] / b if b else None
        avg = ytd[short] / len(months[short])
        fd = first.get((short, model))
        old = fd is not None and (2026 * 12 + 9) - (fd.year * 12 + fd.month) > 18
        base = tier_amount(amt)
        coefs = [reach_coef(rate), scale_coef(avg), margin_coef(margin.get((short, model))), D("0.8") if old else D(1)]
        total = base
        for c in coefs:
            total *= c
        total = total.quantize(CENT, ROUND_HALF_UP)
        sc, oc = stock_coef(stock.get(short)), overdue_coef(overdue.get(short))
        hold = total if short in debt else (total * (1 - sc) + total * (1 - oc)).quantize(CENT, ROUND_HALF_UP)
        pay = total - hold
        # 分到人：每条记录的权重 = 角色比例 × 个人比例；最大余数法保留到分
        recs = []
        for (role, share), cell in zip(ROLES, roster[(code, short, model)]):
            if cell is None:
                recs.append((f"{role}未分配", share))
            elif cell.endswith("%"):
                name = cell.rstrip("%0123456789")
                pct = D(cell[len(name):-1]) / 100
                recs += [(name, share * pct), (f"{role}未分配", share * (1 - pct))]
            else:
                recs.append((cell, share))
        exact = [pay * w for _, w in recs]
        got = [e.quantize(CENT, ROUND_DOWN) for e in exact]
        n = int((pay - sum(got)) / CENT)
        for i in sorted(range(len(recs)), key=lambda i: -abs(exact[i] - got[i]))[:abs(n)]:
            got[i] += CENT if n > 0 else -CENT
        for (name, _), g in zip(recs, got):
            people[name] += g
        rows.append(dict(项目编号=f"{MONTH}|{code}|{short}|{model}", 月份=MONTH, 客户编码=code, 客户简称=short, 产品型号=model, 区域=region,
                         项目销售额=amt, 阶梯提成=base.quantize(D("0.0001")), 达成系数=coefs[0], 规模系数=coefs[1], 毛利系数=coefs[2],
                         新老系数=coefs[3], 提成总额=total, 库存系数=sc, 逾期系数=oc, 是否欠款="是" if short in debt else "否",
                         暂扣金额=hold, 可分配金额=pay))
    return rows, dict(people)


RULES = """销售提成管理办法（样例）

一、适用范围
本办法适用于按月核算的销售提成。每月按当月销售单核算，核算单位是“项目”。

二、项目
项目按 月份、客户编码、客户简称、产品型号 归集：这四项都相同的销售单行属于同一个项目，先合计项目销售额，再计算提成。

三、阶梯提成
按项目销售额超额累进计算：10 万元以内的部分按 1.0%；超过 10 万元至 50 万元的部分按 1.5%；超过 50 万元的部分按 2.0%。

四、考核系数
1. 达成系数：达成率 = 项目所在区域当月实际销售额 ÷ 该区域当月预算。达成率低于 80% 系数 0.8；80%（含）至 100% 系数 0.9；100%（含）至 120% 系数 1.0；120%（含）以上系数 1.1。
2. 规模系数：按客户年内月均销售额。月均低于 5 万元系数 0.9；5 万元（含）至 20 万元系数 1.0；20 万元（含）以上系数 1.05。
3. 毛利系数：按客户和产品型号的毛利率。毛利率低于 10% 系数 0.8；10%（含）至 20% 系数 0.9；20%（含）以上系数 1.0。
4. 新老项目系数：老项目系数 0.8，新项目系数 1.0。

五、提成总额
项目提成总额 = 阶梯提成 × 达成系数 × 规模系数 × 毛利系数 × 新老项目系数，四舍五入到分。

六、暂扣
1. 库存系数：客户没有呆滞库存系数 1；呆滞库存金额低于 10 万元系数 0.95；10 万元（含）以上系数 0.9。
2. 逾期系数：客户没有逾期应收系数 1；逾期金额低于 5 万元系数 0.95；5 万元（含）以上系数 0.85。
3. 暂扣金额 = 提成总额 ×（1 − 库存系数）+ 提成总额 ×（1 − 逾期系数），四舍五入到分。
4. 欠款清单里的客户，其项目的提成总额全部暂扣。
5. 可分配金额 = 提成总额 − 暂扣金额。

七、分配
项目的可分配金额按角色分配：销售负责人 50%，销售执行 30%，技术支持 20%。各角色的人员见当月分配名单。
"""

GUIDE = """# 销售提成（样例）场景说明

财务对制度的补充口径，与制度同等效力。

## 核算单位
- 项目 = 月份 + 客户编码 + 客户简称 + 产品型号。销售单里有两列都叫“客户简称”，项目和所有参考表用的是**最后一列**（与参考表里的客户简称写法一致的那一列）。
- 销售金额为 0 的行（包括判断为“剔除”的行）不参与核算，也不算作有出货的月份。
- 只核算当月的项目；分配名单只取核算月的行。

## 数据匹配
- 所有参考表（预算、呆滞库存、逾期应收、毛利率、首发日期、欠款清单、分配名单）只跟销售单匹配。参考表里有、销售单里没有的客户、型号或项目，不参与核算。
- 呆滞库存、逾期应收、欠款清单按客户简称匹配；毛利率、首发日期按客户简称 + 产品型号匹配；预算按区域 + 月份匹配。

## 系数
- 达成率用区域当月实际销售额（当月该区域全部项目销售额之和）。查不到预算、或当月预算为 0，达成系数取 1。
- 月均销售额 = 客户当年 1 月到核算月的销售额合计 ÷ 这段时间里实际有出货的月数。
- 查不到毛利率，毛利系数取 1；毛利率为负，取最低一档。
- 新老项目：核算月份与首发日期所在月份相差超过 18 个月的是老项目；查不到首发日期按新项目。
- 项目销售额为负（退货）照算：阶梯提成按第一档比例计算，结果为负。

## 分配
- 名单里一格写“姓名”的，这个角色的份额全部归他；写“姓名60%”的，他拿这个角色份额的 60%，其余 40% 记为“××未分配”（××为角色名，如“销售执行未分配”）；空着的，这个角色的份额全部记为“××未分配”。
- 分到每条记录的金额保留到分，尾差按最大余数法调整，各条合计等于项目可分配金额。
- “××未分配”和人一样列在分配结果里。
"""

SCENARIO = """id: syn_commission
name: 销售提成（样例）
description: 按月核算销售提成，先算到项目，再按名单分到人。
outputs: |
  结果固定两部分：
  1. 汇总核算：当月的提成总额、暂扣金额、可分配金额。
  2. 分配人员金额：每个人（含各角色的“未分配”）当月分到的可分配金额合计。
  项目一层的核算底稿要能看到每个项目的销售额、各项系数、提成总额、暂扣金额和可分配金额。
"""


def write(d, rows, people):
    inp, exp, scn = HERE / "inputs", HERE / "expected", HERE / "scenario" / "syn_commission"
    for p in (inp, exp, scn):
        p.mkdir(parents=True, exist_ok=True)
    import docx
    doc = docx.Document()
    for line in RULES.strip().split("\n"):
        if line.strip():
            doc.add_paragraph(line)
    doc.save(inp / "01_销售提成管理办法.docx")
    (scn / "guide.md").write_text(GUIDE, encoding="utf-8")
    (scn / "scenario.yaml").write_text(SCENARIO, encoding="utf-8")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "销售单"
    ws.append(["月份", "订单号", "客户编码", "客户简称", "区域", "产品型号", "数量", "销售金额", "判断", "备注", "客户简称"])
    for r in d["sales"]:
        ws.append(r)
    for title, head, key in [("预算", ["区域", "月份", "预算金额"], "budget"), ("呆滞库存", ["客户简称", "呆滞金额"], "stock"),
                             ("逾期应收", ["客户简称", "逾期金额"], "overdue"), ("毛利率", ["客户简称", "产品型号", "毛利率"], "margin"),
                             ("首发日期", ["客户简称", "产品型号", "首发日期"], "first"), ("欠款清单", ["客户简称"], "debt")]:
        s = wb.create_sheet(title)
        s.append(head)
        for r in d[key]:
            s.append(r)
    wb.save(inp / "02_销售单与参考数据.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "分配名单"
    ws.append(["月份", "客户编码", "客户简称", "产品型号", *[r for r, _ in ROLES]])
    for r in d["roster"]:
        ws.append(r)
    wb.save(inp / "03_分配名单.xlsx")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "汇总"
    ws.append(["核算类型", "汇总金额"])
    for k in ("提成总额", "暂扣金额", "可分配金额"):
        ws.append([k, float(sum(r[k] for r in rows))])
    s = wb.create_sheet("分配人员")
    s.append(["姓名", "可分配金额"])
    for n, v in sorted(people.items()):
        s.append([n, float(v)])
    s = wb.create_sheet("项目")
    s.append(list(rows[0]))
    for r in rows:
        s.append([float(v) if isinstance(v, D) else v for v in r.values()])
    wb.save(exp / "期望结果_202609.xlsx")


if __name__ == "__main__":
    data = make_data()
    rows, people = reference(data)
    write(data, rows, people)
    print(f"销售单 {len(data['sales'])} 行；{MONTH} 项目 {len(rows)} 个；提成总额 {sum(r['提成总额'] for r in rows)}；"
          f"暂扣 {sum(r['暂扣金额'] for r in rows)}；可分配 {sum(r['可分配金额'] for r in rows)}；分配对象 {len(people)} 个")
