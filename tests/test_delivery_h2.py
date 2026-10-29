"""交付结算奖回归（不调用大模型）：按一串固定的工具调用回放，验证规则写法能完整表达材料要求，
V1 / V2（补充资料并入、长表更正、评分修订、迟延规则修订）/ V3（在 V2 上锁定）逐个对象与独立手算一致。
"""
import importlib.util
from pathlib import Path

from bonus_agent.agent import run_agent
from bonus_agent.compare import compare_expected, report
from bonus_agent.llm import FakeLLM

_spec = importlib.util.spec_from_file_location("delivery_make", Path(__file__).parent.parent / "evals/cases/delivery_h2/make.py")
mk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mk)

F1, F4 = "02_首轮业务数据.xlsx", "04_补充业务数据.xlsx"


def ds(name, file, sheet, row, fields, key=None, link=None):
    return {"tool": "define_dataset", "args": {"name": name, "file": file, "sheet": sheet, "header_rows": [row],
                                               "key": key, "link": link, "fields": fields}}


def f(col, t, **kw):
    return {"column": col, "type": t, **kw}


DATASETS = [
    ds("项目", F1, "项目台账", 4, {"project_id": f("项目编号", "id"), "contract_amount": f("不含税合同额（元）", "money", unit="元"),
                                  "accept_approved": f("验收已批准", "text"), "actual_accept_date": f("实际验收日", "date"),
                                  "plan_accept_date": f("计划验收日", "date"), "grade": f("交付评级", "text"),
                                  "warranty_done": f("质保回访完成", "text")}, key="project_id"),
    ds("人员", F1, "人员档案", 4, {"emp_id": f("工号", "id"), "emp_name": f("姓名", "text"), "employ_type": f("用工性质", "text")},
       key="emp_id"),
    ds("参与分配", F1, "参与分配", 4, {"part_id": f("参与编号", "id"), "project_id": f("项目编号", "id"), "emp_id": f("工号", "id"),
                                     "role": f("角色", "text"), "contrib_score": f("负责人确认贡献分", "number"),
                                     "part_qualify": f("参与资格", "text")}, key="part_id"),
    ds("工时明细", F1, "工时明细", 4, {"timesheet_id": f("工时单号", "id"), "part_id": f("参与编号", "id"),
                                     "work_date": f("工作日期", "date"), "hours": f("小时数", "number"),
                                     "approved": f("审批通过", "text")}, link="part_id"),
    ds("收款流水", F1, "收款流水", 4, {"receipt_id": f("收款单号", "id"), "project_id": f("项目编号", "id"),
                                     "receipt_date": f("入账日期", "date"), "receipt_amount": f("收款金额（元）", "money", unit="元"),
                                     "approved": f("审批通过", "text")}, link="project_id"),
    ds("成本费用", F1, "成本费用", 4, {"expense_id": f("费用单号", "id"), "project_id": f("项目编号", "id"),
                                     "expense_date": f("发生日期", "date"), "expense_amount": f("费用金额（万元）", "money", unit="万元"),
                                     "approved": f("审批通过", "text")}, link="project_id"),
    ds("已预发", F1, "已预发", 4, {"emp_id": f("工号", "id"), "prepaid": f("本批已预发（元）", "money", unit="元")}, link="emp_id"),
    # 补充资料：表头在第 5 行，列名、单位、取值写法都不同，按原表的字段名定义
    ds("补充收款", F4, "补充收款", 5, {"receipt_id": f("流水编号", "id"), "project_id": f("合同项目号", "id"),
                                     "receipt_date": f("到账日", "date"), "receipt_amount": f("到账数（元）", "money", unit="元"),
                                     "approved": f("复核结果", "text")}),
    ds("补充费用", F4, "补充费用", 5, {"expense_id": f("凭证编号", "id"), "project_id": f("合同项目号", "id"),
                                     "expense_date": f("业务日期", "date"), "expense_amount": f("补记费用（元）", "money", unit="元"),
                                     "approved": f("复核结果", "text")}),
    ds("项目更正", F4, "项目更正", 5, {"project_id": f("项目编号", "id"), "field_name": f("更正字段", "text"),
                                     "new_value": f("新值", "text"), "scope_note": f("生效范围", "text")}),
    ds("评分修订", F4, "评分修订", 5, {"part_id": f("参与编号", "id"), "old_score": f("原贡献分", "number"),
                                     "new_score": f("新贡献分", "number")}),
]

COMMON = {"scheme": "共用", "tables": {
    "contrib_tier": {"kind": "tier", "mode": "excess", "steps": [[0, 0.05], [100000, 0.08], [300000, 0.10]]},
    "grade_coef": {"kind": "lookup", "mapping": {"优": 1.15, "合格": 1.0, "待改进": 0.8}},
    "role_coef": {"kind": "lookup", "mapping": {"项目经理": 1.2, "实施工程师": 1.0, "顾问": 1.1, "质量工程师": 0.9}}},
    "variables": {
        "net_receipt": {"scope": "项目", "expr": 'max(min(sum(receipt_amount, of: "收款流水", where: approved == "是" and '
                                                 'receipt_date <= "2026-12-31"), contract_amount), 0)'},
        "cost": {"scope": "项目", "expr": 'sum(expense_amount, of: "成本费用", where: approved == "是" and expense_date <= "2026-12-31")'},
        "margin": {"scope": "项目", "expr": "max(net_receipt - cost, 0)"},
        "delay_days": {"scope": "项目", "expr": "max(days_between(plan_accept_date, actual_accept_date), 0)"},
        "eligible": {"scope": "项目", "expr": 'accept_approved == "是" and between(actual_accept_date, "2026-07-01", "2026-12-31")'},
        "valid_hours": {"scope": "参与记录", "expr": 'sum(hours, of: "工时明细", where: approved == "是" and '
                                                    'between(work_date, "2026-07-01", "2026-12-31"))'},
        "raw_weight": {"scope": "参与记录", "expr": 'if(employ_type == "正式" and part_qualify == "参与", '
                                                   'valid_hours * lookup(role_coef, role) * contrib_score / 100, 0)'}},
    "allocation": [{"level": "项目", "key": "project_id"},
                   {"level": "参与记录", "key": "part_id", "parent_key": "project_id", "weight": "raw_weight"}],
    "payout": [{"id": "warranty", "scope": "参与记录", "pay_ratio": 'if(warranty_done == "是", 1, 0.8)'}],
    "offsets": [{"id": "prepaid", "scope": "人员", "expr": 'sum(prepaid, of: "已预发")'}]}


def pool(rate, floor):
    return {"scope": "项目", "expr": f"if(eligible, round(min(tier(contrib_tier, margin) * lookup(grade_coef, grade) * "
                                    f"max({floor}, 1 - delay_days * {rate}), contract_amount * 0.04), 2), 0)"}


V1 = {"scheme": "V1", "pool": pool("0.02", "0.70")}
V2 = {"scheme": "V2", "pool": pool("0.01", "0.80"), "patches": [
    {"kind": "append", "source": "补充收款", "into": "收款流水", "unique": "receipt_id",
     "values": {"approved": {"通过": "是", "驳回": "否"}}},
    {"kind": "append", "source": "补充费用", "into": "成本费用", "unique": "expense_id",
     "values": {"approved": {"通过": "是", "驳回": "否"}}},
    {"kind": "correct", "source": "项目更正", "target": "项目", "key": "project_id", "field": "field_name", "value": "new_value",
     "labels": {"交付评级": "grade", "计划验收日": "plan_accept_date"}},
    {"kind": "correct", "source": "评分修订", "target": "参与分配", "key": "part_id",
     "set": {"contrib_score": "new_score"}, "expect": {"contrib_score": "old_score"}}],
    "revisions": [{"name": "R1", "adjust": [{"node": "参与记录:J01-U002", "amount": 6000},
                                            {"node": "参与记录:J03-U008", "amount": 12000}]}]}


def script():
    clauses = [{"point": "按参与记录分配后按工号汇总抵扣预发", "file": "01_交付奖金规则.md",
                "quote": "同一员工可以参与多个项目，先按参与编号分配，再按工号汇总，不得去重。"},
               {"point": "超额累进 5% / 8% / 10%", "file": "01_交付奖金规则.md", "quote": "前100,000元（含）部分5%"},
               {"point": "补充收款按流水编号并入、同号只算一次", "file": "03_补充通知.md", "quote": "同号重复提交只算一次。"},
               {"point": "评分修订先核对原贡献分", "file": "03_补充通知.md", "quote": "先核对原贡献分，对不上要报错。"},
               {"point": "V3 锁定两条参与记录", "file": "03_补充通知.md", "quote": "J01项目下U002的应得锁定为6,000.00元"}]
    cover = {"C1": "allocation.参与记录、offsets.prepaid", "C2": "tables.contrib_tier", "C3": "patches.补充收款",
             "C4": "patches.评分修订", "C5": "revisions.R1"}
    facts = [{"name": "贡献毛利计提率-第1档", "value": "5%", "file": "01_交付奖金规则.md", "quote": "前100,000元（含）部分5%"},
             {"name": "V2迟延每日扣减", "value": "0.01", "file": "03_补充通知.md", "quote": "V2迟延系数=max(0.80,1-迟延天数×0.01)"}]
    return ([{"tool": "record_facts", "args": {"facts": facts}}, {"tool": "record_clauses", "args": {"clauses": clauses}}] + DATASETS
            + [{"tool": "set_rules", "args": x} for x in (COMMON, V1, V2)]
            + [{"tool": "cover_clauses", "args": {"items": cover}}, {"tool": "trial_run"},
               {"tool": "finish", "args": {"summary": "交付结算奖 V1 / V2 / V3"}}])


def test_delivery_versions_match_hand_calculation(tmp_path):
    case = mk.make(tmp_path / "case")
    ar = run_agent(FakeLLM(script()), [case / "materials"], tmp_path / "out")
    bad = [(s.tool, s.result[:300]) for s in ar.steps if not s.ok]
    assert ar.status == "OK", (ar.error, bad)
    ms = compare_expected(ar.results, case / "expected.xlsx")
    lines = report(ms)
    assert all(m.ok for m in ms if m.total), lines
    got = {(m.scheme, m.revision, m.metric): m.total for m in ms}
    for sch, rev in (("V1", "R0"), ("V2", "R0"), ("V2", "R1")):           # 项目 5 + 参与记录 14 + 个人 9；发放类指标按人
        assert got[(sch, rev, "应得")] == 28 and got[(sch, rev, "本次可发")] == 9 and got[(sch, rev, "实际抵扣")] == 9, lines
    trial = next(s.result for s in ar.steps if s.tool == "trial_run")
    assert "数据修订：「补充收款」3 行并入「收款流水」，1 行重复已跳过" in trial
    assert "J02 grade：合格 → 优" in trial and "J03 plan_accept_date：2026-10-01 → 2026-11-20" in trial and "J01-U003 contrib_score：76 → 86" in trial
