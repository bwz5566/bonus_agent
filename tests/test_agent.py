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
    assert LLM.from_config("qwen3-vl-8b", cfg).api_key == "sk-from-env"
    assert resolve_models(None, cfg) == cfg["candidates"]
    assert resolve_models("next,qwen3-vl-8b", cfg)[-1] == "qwen3-vl-8b"
    with pytest.raises(LLMError):
        resolve_models("no-such-model", cfg)
