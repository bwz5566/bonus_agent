"""不用模型的参照做法：按场景说明手写一版数据表和规则，算出来要和期望结果一致。

用途：改引擎、读表或工具后先跑这一版（tests/test_syn_commission2.py），确认"规则写对就能算对"；
真实模型的跑法见 syn_run.py。这里的写法只是参照，不会交给智能体。
"""
from __future__ import annotations

from pathlib import Path

from bonus_agent.agent import Workspace
from bonus_agent.auto import build_inventory, collect_files
from bonus_agent.llm import FakeLLM
from bonus_core.data.period import Period

C = Path(__file__).resolve().parent
X, R = "04_销售单与参考数据.xlsx", "05_分配名单.xlsx"
B = {"file": "guide.md", "quote": "参考表里有、销售单里没有的客户、型号或项目，不参与核算"}
NZ = {"销售金额": {"not": ["0"]}}


def build() -> Workspace:
    w = Workspace(FakeLLM(["x"]), build_inventory(collect_files([C / "inputs", C / "scenario/syn_commission2/guide.md"])))
    w.period = Period.parse("2026-09")
    w.report_required = True

    def step(_name, fn, *a, **k):
        if len(a) > 3 and a[2] == "销售单":
            k["header_rows"] = [3]          # 销售单上方有两行标题
        return fn(*a, **k)

    step("项目", w.define_dataset, "项目", X, "销售单", {
        "pid": {"column": "项目编号", "type": "id"}, "cust": {"column": "客户简称(L列)", "type": "id"},
        "region": {"column": "区域", "type": "id"}, "cm": {"column": "客户型号", "type": "id"},
        "p_month": {"column": "月份", "type": "date"}}, key="pid", distinct=True, period="p_month", filter=NZ,
        combine={"项目编号": ["月份", "客户编码", "客户简称(L列)", "产品型号"], "客户型号": ["客户简称(L列)", "产品型号"]})
    step("销售", w.define_dataset, "销售", X, "销售单", {
        "pid": {"column": "项目编号", "type": "id"}, "amt": {"column": "销售金额", "type": "money", "unit": "元"},
        "s_month": {"column": "月份", "type": "date"}}, link="pid", period="s_month", filter=NZ,
        combine={"项目编号": ["月份", "客户编码", "客户简称(L列)", "产品型号"]})
    step("客户", w.define_dataset, "客户", X, "销售单", {
        "cust": {"column": "客户简称(L列)", "type": "id"}}, key="cust", distinct=True, filter=NZ)
    step("客户累计", w.define_dataset, "客户累计", X, "销售单", {
        "cust": {"column": "客户简称(L列)", "type": "id"}, "y_amt": {"column": "销售金额", "type": "money", "unit": "元"},
        "y_month": {"column": "月份", "type": "date"}}, link="cust", period="y_month", window="year_to_date", filter=NZ)
    step("区域", w.define_dataset, "区域", X, "销售单", {"region": {"column": "区域", "type": "id"}}, key="region", distinct=True, filter=NZ)
    step("区域销售", w.define_dataset, "区域销售", X, "销售单", {
        "region": {"column": "区域", "type": "id"}, "r_amt": {"column": "销售金额", "type": "money", "unit": "元"},
        "r_month": {"column": "月份", "type": "date"}}, link="region", period="r_month", filter=NZ)
    step("预算", w.define_dataset, "预算", X, "预算", {
        "region": {"column": "区域", "type": "id"}, "budget": {"column": "预算金额", "type": "money", "unit": "元"},
        "b_month": {"column": "月份", "type": "date"}}, link="region", period="b_month")
    step("客户型号", w.define_dataset, "客户型号", X, "销售单", {"cm": {"column": "客户型号", "type": "id"}}, key="cm", distinct=True,
         filter=NZ, combine={"客户型号": ["客户简称(L列)", "产品型号"]})
    step("毛利率", w.define_dataset, "毛利率", X, "毛利率", {"cm": {"column": "客户型号", "type": "id"}, "margin": {"column": "毛利率", "type": "ratio"}},
         link="cm", combine={"客户型号": ["客户简称", "产品型号"]}, unmatched="ignore", unmatched_basis=B,
         filter={"毛利率": {"not": [""]}})
    step("首发", w.define_dataset, "首发", X, "首发日期", {"cm": {"column": "客户型号", "type": "id"}, "first_date": {"column": "首发日期", "type": "date"}},
         link="cm", combine={"客户型号": ["客户简称", "产品型号"]}, unmatched="ignore", unmatched_basis=B)
    step("呆滞", w.define_dataset, "呆滞", X, "呆滞库存", {"cust": {"column": "客户简称", "type": "id"}, "stock": {"column": "呆滞金额", "type": "money", "unit": "元"}}, link="cust", unmatched="ignore", unmatched_basis=B)
    step("逾期", w.define_dataset, "逾期", X, "逾期应收", {"cust": {"column": "客户简称", "type": "id"}, "overdue": {"column": "逾期金额", "type": "money", "unit": "元"}}, link="cust", unmatched="ignore", unmatched_basis=B)
    step("欠款", w.define_dataset, "欠款", X, "欠款清单", {"cust": {"column": "客户简称", "type": "id"}}, link="cust", unmatched="ignore", unmatched_basis=B)
    step("名单", w.define_dataset, "名单", R, "分配名单", {
        "rid": {"column": "（行号）", "type": "id"}, "pid": {"column": "项目编号", "type": "id"}, "person": {"column": "人员", "type": "text"},
        "role": {"column": "角色", "type": "text"}, "pct": {"column": "比例", "type": "ratio"}, "n_month": {"column": "月份", "type": "date"}},
        key="rid", period="n_month", unmatched="ignore", unmatched_basis=B,
        unpivot={"columns": ["销售负责人", "销售执行", "技术支持1", "技术支持2"], "value": "人员", "label": "角色", "share": "比例",
                 "remainder": "{label}未分配", "blank": "{label}未分配", "share_from": "技术支持比例", "blank_except": ["技术支持2"], "share_from_columns": ["技术支持1", "技术支持2"]},
        combine={"项目编号": ["月份", "客户编码", "客户简称", "产品型号"]})

    step("rules", w.set_rules,
      tables={"sale_tier": {"kind": "tier", "mode": "excess", "steps": [[0, 0.01], [100000, 0.015], [500000, 0.02]], "below_first": "first"},
              "reach_t": {"kind": "tier", "mode": "whole", "steps": [[0, 0.8], [0.8, 0.9], [1, 1.0], [1.2, 1.1]], "below_first": "first"},
              "scale_t": {"kind": "tier", "mode": "whole", "steps": [[0, 0.9], [50000, 1.0], [200000, 1.05]], "below_first": "first"},
              "margin_t": {"kind": "tier", "mode": "whole", "steps": [[0, 0.8], [0.1, 0.9], [0.2, 1.0]], "below_first": "first"},
              "stock_t": {"kind": "tier", "mode": "whole", "steps": [[0, 0.95], [100000, 0.9]]},
              "overdue_t": {"kind": "tier", "mode": "whole", "steps": [[0, 0.95], [50000, 0.85]]},
              "role_t": {"kind": "lookup", "mapping": {"销售负责人": 0.5, "销售执行": 0.3, "技术支持1": 0.2, "技术支持2": 0.2}, "default": None}},
      variables={
        "proj_amt": {"scope": "项目", "expr": 'sum(amt, of: "销售")'},
        "base_amt": {"scope": "项目", "expr": "tier(sale_tier, proj_amt)"},
        "region_real": {"scope": "区域", "expr": 'sum(r_amt, of: "区域销售")'},
        "region_budget": {"scope": "区域", "expr": 'sum(budget, of: "预算")'},
        "reach_c": {"scope": "区域", "expr": "if(region_budget > 0, tier_rate(reach_t, region_real / region_budget), 1)"},
        "cust_avg": {"scope": "客户", "expr": 'ratio(sum(y_amt, of: "客户累计"), count_distinct(y_month, of: "客户累计"), zero: 0)'},
        "scale_c": {"scope": "客户", "expr": "tier_rate(scale_t, cust_avg)"},
        "margin_c": {"scope": "客户型号", "expr": 'if(count(of: "毛利率") > 0, tier_rate(margin_t, max(margin, of: "毛利率")), 1)'},
        "old_c": {"scope": "客户型号", "expr": 'if(count(of: "首发") > 0, if((year(period_end()) * 12 + month(period_end())) - (year(max(first_date, of: "首发")) * 12 + month(max(first_date, of: "首发"))) > 18, 0.8, 1), 1)'},
        "total": {"scope": "项目", "expr": "round(base_amt * reach_c * scale_c * margin_c * old_c, 2)"},
        "stock_c": {"scope": "客户", "expr": 'if(sum(stock, of: "呆滞") > 0, tier_rate(stock_t, sum(stock, of: "呆滞")), 1)'},
        "overdue_c": {"scope": "客户", "expr": 'if(sum(overdue, of: "逾期") > 0, tier_rate(overdue_t, sum(overdue, of: "逾期")), 1)'},
        "in_debt": {"scope": "客户", "expr": 'count(of: "欠款") > 0'},
        "hold": {"scope": "项目", "expr": "if(in_debt, total, round(total * (1 - stock_c) + total * (1 - overdue_c), 2))"},
        "payable": {"scope": "项目", "expr": "total - hold"}},
      pool={"scope": "项目", "expr": "payable", "negative": "allow"},
      allocation=[{"level": "项目", "key": "pid"}, {"level": "分配记录", "key": "rid", "parent_key": "pid", "weight": "lookup(role_t, role) * pct"}],
      report={"totals": [{"name": "提成总额", "level": "项目", "expr": "total"}, {"name": "暂扣金额", "level": "项目", "expr": "hold"},
                         {"name": "可分配金额", "level": "项目", "expr": "payable"}], "people": "person"})
    step("trial", w.trial_run)
    return w
