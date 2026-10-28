import json
from pathlib import Path

import pytest

from bonus_agent.extract_rules import check_quotes, extract
from bonus_agent.llm import FakeLLM, LLMError, parse_json
from bonus_agent.parsing.docs import parse_any
from bonus_core.dsl.models import RuleSet
from evals.run_eval import fake_llm_for, load_case, main as eval_main, run_one

ROOT = Path(__file__).parent.parent
CASE = ROOT / "evals" / "cases" / "sales_q3"
RULES = RuleSet.load(ROOT / "examples" / "sales_q3" / "rules.yaml")


def test_parse_json_tolerant():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('好的，结果如下：{"a": {"b": "}"}} 以上') == {"a": {"b": "}"}}
    with pytest.raises(LLMError):
        parse_json("没有 JSON")


def test_parse_case_documents():
    blocks = [b for f in ("激励方案.docx", "系数表.pptx") for b in parse_any(CASE / f).blocks]
    kinds = [b.kind for b in blocks]
    assert kinds.count("image") == 2 and "table" in kinds
    assert any("1千万" in b.text for b in blocks)


def _answer(**override):
    base = {
        "tables": {"t": {"kind": "tier", "mode": "whole", "steps": [[0, 0], [10000000, 0.1]],
                         "sources": [{"doc": "激励方案.docx", "quote": "以部门本季度合同销售额合计为基数"}]}},
        "variables": {"total": {"scope": "global", "expr": "sum(sales_amount)"}},
        "pool": {"expr": "tier(t, total)"},
        "allocation": [{"level": "小组", "key": "group_id", "weight": "group_score"},
                       {"level": "个人", "key": "employee_id", "parent_key": "group_id", "weight": "share(seniority)"}],
        "payout": [],
        "questions": [{"text": "净利润是全额还是超额？", "options": ["全额", "超额"]}],
    }
    base.update(override)
    return json.dumps(base, ensure_ascii=False)


def test_extract_repairs_invalid_output():
    bad = _answer(pool={"expr": "tier(t, total) + unknown_name"})
    llm = FakeLLM(["图片转写", "图片转写", bad, _answer()])
    res = extract(llm, [CASE / "激励方案.docx", CASE / "系数表.pptx"], RULES.datasets)
    assert res.ok and res.attempts == 2
    assert "unknown_name" in json.dumps(llm.calls[-1][-1]["content"], ensure_ascii=False)
    assert res.quote_hits == 1 and res.quote_total == 1
    assert len(res.transcripts) == 2


def test_hallucinated_quote_is_flagged():
    ans = _answer(tables={"t": {"kind": "tier", "mode": "whole", "steps": [[0, 0], [10000000, 0.1]],
                                "sources": [{"doc": "激励方案.docx", "quote": "文档里根本没有这句话"}]}})
    res = extract(FakeLLM(["转写", "转写", ans]), [CASE / "激励方案.docx", CASE / "系数表.pptx"], RULES.datasets)
    assert res.quote_hits == 0 and any(i.code == "quote_not_found" for i in res.issues)


def test_eval_pipeline_with_fake_model(tmp_path):
    case = load_case(CASE)
    row = run_one(fake_llm_for(case), case, "two_step")
    assert row["ok"] and row["check_score"] == 1.0 and row["leaf_match_rate"] == 1.0
    assert eval_main(["--fake", "--out", str(tmp_path)]) == 0
    assert list(tmp_path.glob("*.md"))


def test_secrets_file_and_model_groups(tmp_path, monkeypatch):
    from bonus_agent.llm import LLM, load_config, resolve_models
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    (tmp_path / "models.yaml").write_text((ROOT / "config" / "models.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "secrets.yaml").write_text("providers:\n  dashscope:\n    api_key: sk-from-file\n", encoding="utf-8")
    cfg = load_config(tmp_path / "models.yaml")
    llm = LLM.from_config("qwen3.6-35b-a3b", cfg)
    assert llm.api_key == "sk-from-file" and llm.extra_body == {"enable_thinking": False}
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-from-env")
    assert LLM.from_config("qwen3.5-27b", cfg).api_key == "sk-from-env"
    assert resolve_models(None, cfg) == resolve_models("candidates", cfg) == cfg["candidates"]
    assert resolve_models("candidates,open", cfg) == cfg["groups"]["open"]
    assert resolve_models("reference,qwen3.6-27b", cfg) == ["qwen3-vl-plus", "qwen3.6-flash", "qwen3.6-27b"]
    with pytest.raises(LLMError):
        resolve_models("no-such-model", cfg)


ANNUAL = ROOT / "evals" / "cases" / "annual_2026"


def test_annual_reference_golden():
    from bonus_core.golden import run_golden
    rs = RuleSet.load(ANNUAL / "rules.yaml")
    assert all(r.passed for r in run_golden(rs))


def test_auto_end_to_end_and_rerun(tmp_path):
    import openpyxl
    import yaml as _yaml
    from bonus_agent.auto import run_auto
    from bonus_core.cli import main as bonus_main

    case = load_case(ANNUAL)
    ar = run_auto(fake_llm_for(case), [ANNUAL], tmp_path)   # 传文件夹，自动区分规则和数据
    assert ar.ok, ar.error
    assert ar.result.pool == case.ref_result.pool
    wb = openpyxl.load_workbook(ar.outputs["excel"])
    assert {"待确认问题", "智能体规划", "汇总"} <= set(wb.sheetnames)
    qs = _yaml.safe_load(ar.outputs["questions"].read_text(encoding="utf-8"))
    assert qs and qs[0]["answer"] is None
    # 输出的规则和映射可以直接用计算引擎复算
    out = tmp_path / "rerun.xlsx"
    assert bonus_main(["run", "--rules", str(ar.outputs["rules"]), "--mapping", str(ar.outputs["mapping"]),
                       "--out", str(out), str(ANNUAL / "年度数据.xlsx")]) == 0
    # 带答复重跑：答复进入提示词
    qs[0]["answer"] = 1
    llm = fake_llm_for(case)
    run_auto(llm, [ANNUAL], tmp_path / "2", answers=qs)
    assert any("财务已确认" in json.dumps(m, ensure_ascii=False) for c in llm.calls for m in c)


def test_plan_errors_are_repaired():
    from bonus_agent.auto import build_inventory, collect_files, make_plan
    from evals.run_eval import fake_plan
    case = load_case(ANNUAL)
    good = fake_plan(case)
    bad = json.loads(json.dumps(good, ensure_ascii=False))
    bad["datasets"]["员工"]["fields"]["monthly_salary"]["column"] = "工资"          # 不存在的列
    bad["datasets"]["员工"]["fields"]["name"]["type"] = "money"                    # 类型与数据不符
    bad["datasets"]["员工"]["fields"]["months"]["column"] = "本年在职 月数"         # 空格差异，应自动更正
    inv = build_inventory(collect_files([ANNUAL]))
    llm = FakeLLM([json.dumps(bad, ensure_ascii=False), json.dumps(good, ensure_ascii=False)])
    po = make_plan(llm, inv)
    assert po.attempts == 2 and not po.errors
    feedback = llm.calls[1][-1]["content"]
    assert "工资" in feedback and "name" in feedback and "本年在职 月数" not in feedback
