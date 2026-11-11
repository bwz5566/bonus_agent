"""造第二套“销售提成”样例，比第一套更接近真实材料：规则放在 PPT 里，另有新旧两版补充规则（旧版和新版口径冲突），
销售单上方有标题行，名单跨 1–9 月、中间夹着重复表头，技术支持分两列按比例拆，毛利率有空值，几十个客户、上百个项目。
全部是造的公司、客户和人名，数字由固定的随机种子生成。

    python evals/cases/syn_commission2/build.py      # 重新生成 inputs/、scenario/ 和 expected/
"""
from __future__ import annotations

import random
import sys
from collections import defaultdict
from decimal import ROUND_DOWN, Decimal as D
from pathlib import Path

import openpyxl

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "syn_commission"))
import build as b1  # noqa: E402  第一套样例的造数和参照算法

MONTH = b1.MONTH
PREFIX = ["恒", "远", "瑞", "嘉", "宏", "泰", "同", "鼎", "盛", "越", "航", "拓", "信", "诚", "和", "鸿", "华", "晟", "启", "卓"]
SUFFIX = ["达电子", "芯科技", "思通讯", "联智造", "创光电", "诺能源", "合精密", "辰传感", "昇半导体", "坤材料"]
REGIONS = ["华东", "华北", "华南", "西南", "海外", "东北"]
TECH = ["吕昊怡", "钟一鸣", "邵文", "韩晓雯", "魏尧", "岑欣"]


def customers(rnd: random.Random):
    out = list(b1.CUSTOMERS)
    names = [p + s for p in PREFIX for s in SUFFIX]
    rnd.shuffle(names)
    for i, n in enumerate(names[:49]):
        out.append((f"C{12 + i:03d}", n, n, rnd.choice(REGIONS)))
    return out


def build_data():
    b1.MODELS[:] = ["MA-100", "MA-200", "MB-300", "MC-500", "MD-700", "ME-800", "MF-900", "MG-110"]
    rnd = random.Random(20261111)
    b1.CUSTOMERS[:] = customers(rnd)
    d = b1.make_data()
    # 毛利率：一部分表里有这一行但毛利率是空的（口径：按查不到处理，系数 1）
    blank = [i for i, r in enumerate(d["margin"]) if rnd.random() < 0.06]
    return d, blank, rnd


def roster_rows(d, rnd: random.Random):
    """1–9 月全部有出货的项目：销售负责人、销售执行（姓名 / 姓名+百分比 / 空）、技术支持 1、2 和拆分比例。"""
    people = b1.PEOPLE + ["朱可", "杜衡", "梁子墨", "温澜"]
    rows = []
    for month in range(202601, MONTH + 1):
        projs = sorted({(r[2], r[10], r[5]) for r in d["sales"] if r[0] == month and r[7] != 0})
        for code, short, model in projs:
            cells = []
            for _ in range(2):
                k, who = rnd.random(), rnd.choice(people)
                cells.append(who if k < 0.6 else f"{who}{rnd.choice([50, 60, 80])}%" if k < 0.82 else None)
            if rnd.random() < 0.12:
                cells[1] = cells[0]                      # 负责人和执行同一个人
            t1 = rnd.choice(TECH) if rnd.random() < 0.85 else None
            t2 = rnd.choice([t for t in TECH if t != t1]) if t1 and rnd.random() < 0.4 else None
            q = rnd.choice([0.5, 0.6, 0.7, 0.8]) if t2 else (1 if t1 else None)
            rows.append([month, code, short, model, *cells, t1, t2, q])
    rows.append([MONTH, "C011", "未出货客户", "MA-100", "秦朗", "苏禾", TECH[0], None, 1])      # 名单里多出来的
    return rows


def allocate(rows, roster):
    """每个项目的可分配金额按名单分到人：角色份额 × 个人比例；技术支持 1、2 按 Q 拆；最大余数法保留到分。"""
    cur = {(r[1], r[2], r[3]): r[4:] for r in roster if r[0] == MONTH}
    people = defaultdict(D)
    for row in rows:
        s1, s2, t1, t2, q = cur[(row["客户编码"], row["客户简称"], row["产品型号"])]
        recs = []
        for (role, share), cell in zip(b1.ROLES[:2], (s1, s2)):
            if cell is None:
                recs.append((f"{role}未分配", share))
            elif str(cell).endswith("%"):
                name = str(cell).rstrip("%0123456789")
                pct = D(str(cell)[len(name):-1]) / 100
                recs += [(name, share * pct), (f"{role}未分配", share * (1 - pct))]
            else:
                recs.append((cell, share))
        share = b1.ROLES[2][1]
        if t1 is None and t2 is None:
            recs.append(("技术支持1未分配", share))
        elif t2 is None:
            recs.append((t1, share))
        else:
            qq = D(str(q))
            recs += [(t1 or "技术支持未分配", share * qq), (t2, share * (1 - qq))]
        pay = row["可分配金额"]
        exact = [pay * w for _, w in recs]
        got = [e.quantize(b1.CENT, ROUND_DOWN) for e in exact]
        n = int((pay - sum(got)) / b1.CENT)
        for i in sorted(range(len(recs)), key=lambda i: -abs(exact[i] - got[i]))[:abs(n)]:
            got[i] += b1.CENT if n > 0 else -b1.CENT
        for (name, _), g in zip(recs, got):
            people[name] += g
    return dict(people)


PPT = [
    ("销售提成管理办法（样例）", ["本办法适用于按月核算的销售提成。每月按当月销售单核算，核算单位是“项目”。",
                          "项目按 月份、客户编码、客户简称、产品型号 归集：这四项都相同的销售单行属于同一个项目，先合计项目销售额，再计算提成。"]),
    ("阶梯提成", ["按项目销售额超额累进计算：10 万元以内的部分按 1.0%；超过 10 万元至 50 万元的部分按 1.5%；超过 50 万元的部分按 2.0%。"]),
    ("考核系数（一）", ["达成系数：达成率 = 项目所在区域当月实际销售额 ÷ 该区域当月预算。达成率低于 80% 系数 0.8；80%（含）至 100% 系数 0.9；"
                  "100%（含）至 120% 系数 1.0；120%（含）以上系数 1.1。",
                  "规模系数：按客户年内月均销售额。月均低于 5 万元系数 0.9；5 万元（含）至 20 万元系数 1.0；20 万元（含）以上系数 1.05。"]),
    ("考核系数（二）", ["毛利系数：按客户和产品型号的毛利率。毛利率低于 10% 系数 0.8；10%（含）至 20% 系数 0.9；20%（含）以上系数 1.0。",
                  "新老项目系数：老项目系数 0.8，新项目系数 1.0。"]),
    ("提成总额与暂扣", ["项目提成总额 = 阶梯提成 × 达成系数 × 规模系数 × 毛利系数 × 新老项目系数，四舍五入到分。",
                  "库存系数：客户没有呆滞库存系数 1；呆滞库存金额低于 10 万元系数 0.95；10 万元（含）以上系数 0.9。",
                  "逾期系数：客户没有逾期应收系数 1；逾期金额低于 5 万元系数 0.95；5 万元（含）以上系数 0.85。",
                  "暂扣金额 = 提成总额 ×（1 − 库存系数）+ 提成总额 ×（1 − 逾期系数），四舍五入到分。欠款清单里的客户，其项目的提成总额全部暂扣。",
                  "可分配金额 = 提成总额 − 暂扣金额。"]),
    ("分配", ["项目的可分配金额按角色分配：销售负责人 50%，销售执行 30%，技术支持 20%。各角色的人员见当月分配名单。"]),
]

OLD = """销售提成补充规则说明（旧版，2026-09）

一、逾期系数调整：逾期金额低于 3 万元系数 0.95；3 万元（含）以上系数 0.9。
二、暂扣金额 = 提成总额 ×（1 − 库存系数 × 逾期系数），四舍五入到分。
三、毛利率查不到的项目，毛利系数取 0.8。
"""

NEW = """销售提成补充规则说明（新版，2026-11，替代 2026-09 旧版）

一、逾期系数、暂扣公式以管理办法为准，不再单独调整。暂扣金额 = 提成总额 ×（1 − 库存系数）+ 提成总额 ×（1 − 逾期系数），四舍五入到分。
二、毛利率：毛利率表里没有这个项目的，毛利系数取 1；表里有这个项目但毛利率单元格是空的，也按查不到处理，毛利系数取 1。
三、技术支持：分配名单里技术支持分两列（技术支持 1、技术支持 2），“技术支持比例”列用文字写了各自的百分比（如“甲60%，乙40%”），按这个比例拆技术支持的份额；只有技术支持 1 时比例列为空，全部归技术支持 1。
四、销售负责人和销售执行是同一个人的，两份都算给这个人。
"""

GUIDE = """# 销售提成（样例二）场景说明

财务对制度的补充口径，与制度同等效力；制度和补充规则有冲突的，以补充规则的新版为准。

## 核算单位
- 项目 = 月份 + 客户编码 + 客户简称 + 产品型号。销售单里有两列都叫“客户简称”，项目和所有参考表用的是**最后一列**（与参考表里的客户简称写法一致的那一列）。
- 销售单上方有标题行，表头在标题行下面；“应收-回款备注”列里的 #N/A 是查不到，不影响核算。
- 销售金额为 0 的行（包括判断为“剔除”的行）不参与核算，也不算作有出货的月份。
- 只核算当月的项目；分配名单里有 1–9 月的行，只取核算月的，名单里夹着的重复表头行忽略。

## 数据匹配
- 所有参考表（预算、呆滞库存、逾期应收、毛利率、首发日期、欠款清单、分配名单）只跟销售单匹配。参考表里有、销售单里没有的客户、型号或项目，不参与核算。
- 呆滞库存、逾期应收、欠款清单按客户简称匹配；毛利率、首发日期按客户简称 + 产品型号匹配；预算按区域 + 月份匹配。

## 系数
- 达成率用区域当月实际销售额（当月该区域全部项目销售额之和）。查不到预算、或当月预算为 0，达成系数取 1。
- 月均销售额 = 客户当年 1 月到核算月的销售额合计 ÷ 这段时间里实际有出货的月数。
- 毛利率为负，取最低一档。
- 新老项目：核算月份与首发日期所在月份相差超过 18 个月的是老项目；查不到首发日期按新项目。
- 项目销售额为负（退货）照算：阶梯提成按第一档比例计算，结果为负。

## 分配
- 销售负责人、销售执行：一格写“姓名”的，这个角色的份额全部归他；写“姓名60%”的，他拿这个角色份额的 60%，其余 40% 记为“××未分配”（××为角色名）；空着的，这个角色的份额全部记为“××未分配”。
- 技术支持按新版补充规则的拆法；两列都空的记为“技术支持1未分配”（技术支持 2 空着不记未分配）。
- 分到每条记录的金额保留到分，尾差按最大余数法调整，各条合计等于项目可分配金额。
- “××未分配”和人一样列在分配结果里。
"""

SCENARIO = """id: syn_commission2
name: 销售提成（样例二）
description: 按月核算销售提成，先算到项目，再按名单分到人。
outputs: |
  结果固定两部分：
  1. 汇总核算：当月的提成总额、暂扣金额、可分配金额。
  2. 分配人员金额：每个人（含各角色的“未分配”）当月分到的可分配金额合计。
  项目一层的核算底稿要能看到每个项目的销售额、各项系数、提成总额、暂扣金额和可分配金额。
"""

ANSWER = "按场景说明（guide.md）和补充规则新版执行；制度和补充规则冲突的，以补充规则新版为准；旧版补充规则已作废。"


def write():
    d, blank, rnd = build_data()
    roster = roster_rows(d, rnd)
    # 参照算法按“查不到毛利率”处理空值：先从参照输入里去掉这些行，写进表里的仍然是空
    ref_in = dict(d, margin=[r for i, r in enumerate(d["margin"]) if i not in blank],
                  roster=[[MONTH, r[1], r[2], r[3], None, None, None] for r in roster if r[0] == MONTH])
    rows, _ = b1.reference(ref_in)
    people = allocate(rows, roster)

    inp, exp, scn = HERE / "inputs", HERE / "expected", HERE / "scenario" / "syn_commission2"
    for p in (inp, exp, scn):
        p.mkdir(parents=True, exist_ok=True)
    from pptx import Presentation
    prs = Presentation()
    for title, lines in PPT:
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = title
        s.placeholders[1].text = "\n".join(lines)
    prs.save(inp / "01_销售提成管理办法.pptx")
    import docx
    for name, text in (("02_补充规则说明（新版）.docx", NEW), ("03_补充规则说明（旧版）.docx", OLD)):
        doc = docx.Document()
        for line in text.strip().split("\n"):
            if line.strip():
                doc.add_paragraph(line)
        doc.save(inp / name)
    (scn / "guide.md").write_text(GUIDE, encoding="utf-8")
    (scn / "scenario.yaml").write_text(SCENARIO, encoding="utf-8")
    (HERE / "answer.txt").write_text(ANSWER, encoding="utf-8")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "销售单"
    ws.append(["销售单明细（集团口径，含剔除行）"])
    ws.append(["导出日期：2026-10-08；金额单位：元"])
    ws.append(["月份", "订单号", "客户编码", "客户简称", "区域", "产品型号", "数量", "销售金额", "判断", "备注", "应收-回款备注", "客户简称"])
    for r in d["sales"]:
        ws.append([*r[:10], "#N/A" if rnd.random() < 0.7 else "已回款", r[10]])
    ws.append(["合计", None, None, None, None, None, sum(r[6] for r in d["sales"]), sum(r[7] for r in d["sales"])])    # 表尾合计行
    for title, head, key in [("预算", ["区域", "月份", "预算金额"], "budget"), ("呆滞库存", ["客户简称", "呆滞金额"], "stock"),
                             ("逾期应收", ["客户简称", "逾期金额"], "overdue"), ("毛利率", ["客户简称", "产品型号", "毛利率"], "margin"),
                             ("首发日期", ["客户简称", "产品型号", "首发日期"], "first"), ("欠款清单", ["客户简称"], "debt")]:
        s = wb.create_sheet(title)
        s.append(head)
        for i, r in enumerate(d[key]):
            s.append([*r[:-1], None] if key == "margin" and i in blank else r)
    wb.save(inp / "04_销售单与参考数据.xlsx")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "分配名单"
    head = ["月份", "客户编码", "客户简称", "产品型号", "销售负责人", "销售执行", "技术支持1", "技术支持2", "技术支持比例"]
    ws.append(head)
    for i, r in enumerate(roster):
        if i and i % 150 == 0:
            ws.append(head)                                           # 夹在中间的重复表头
        t1, t2, q = r[6], r[7], r[8]
        txt = f"{t1}{round(q * 100)}%，{t2}{round((1 - q) * 100)}%" if t2 and t1 else None
        ws.append([*r[:8], txt])
    wb.save(inp / "05_分配名单.xlsx")

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
    print(f"销售单 {len(d['sales'])} 行；{MONTH} 项目 {len(rows)} 个；提成总额 {sum(r['提成总额'] for r in rows)}；"
          f"暂扣 {sum(r['暂扣金额'] for r in rows)}；可分配 {sum(r['可分配金额'] for r in rows)}；分配对象 {len(people)} 个；名单 {len(roster)} 行")


if __name__ == "__main__":
    write()
