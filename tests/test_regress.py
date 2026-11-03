"""固定回归：场景配置、材料排除、提问后用答复接着跑、逐分比对和“算错没拦住”统计。"""
import json
import shutil

import openpyxl

from bonus_agent import regress as rg
from bonus_agent.llm import FakeLLM
from tests.test_syn_acceptance import CASE, EXPECTED_XLSX, _script, run_case


def _suite(tmp_path, **extra):
    inp = tmp_path / "inputs"
    inp.mkdir()
    shutil.copy(run_case.XLSX, inp)
    (inp / "negative_cases.json").write_text("[]", encoding="utf-8")
    shutil.copy(EXPECTED_XLSX, tmp_path / "期望.xlsx")
    spec = {"name": "S1", "inputs": "inputs", "exclude": ["negative_cases*.json"], "expected": "期望.xlsx", **extra}
    import yaml
    (tmp_path / "suite.yaml").write_text(yaml.safe_dump({"scenarios": [spec]}, allow_unicode=True), encoding="utf-8")
    return rg.load_suite(tmp_path / "suite.yaml")


def test_materials_exclude_answers_expected_and_patterns(tmp_path):
    (tmp_path / "答复.txt").write_text("按制度执行", encoding="utf-8")
    sc = _suite(tmp_path, answers="答复.txt")[0]
    sc.inputs.append(tmp_path)                     # 输入里混进了期望结果和答复文件
    names = [f.name for f in sc.materials()]
    assert run_case.XLSX.name in names and "negative_cases.json" not in names
    assert "期望.xlsx" not in names and "答复.txt" not in names


def test_suite_counts_correct_runs(tmp_path):
    sc = _suite(tmp_path)
    s = rg.run_suite(sc, tmp_path / "out", lambda: FakeLLM(_script()), runs=2, log=lambda _: None)
    assert s["总计"]["次数"] == 2 and s["总计"]["金额正确"] == 2 and s["总计"]["算错没拦住"] == 0
    r = s["明细"][0]
    assert r["status"] == "OK" and r["correct"] and r["checked"] > 100 and r["wrong"] == 0 and r["review_rows"] > 0
    assert json.loads((tmp_path / "out" / rg.SUMMARY).read_text(encoding="utf-8"))["场景"]["S1"]["正确率"] == 1


def test_wrong_amount_is_missed(tmp_path):
    sc = _suite(tmp_path)
    wb = openpyxl.load_workbook(tmp_path / "期望.xlsx")
    ws = wb["个人结果"]
    head = next(r for r in ws.iter_rows() if r[0].value == "方案")
    col = next(c.column for c in head if c.value == "应得金额")
    ws.cell(head[0].row + 1, col).value += 1                 # 改坏一个期望金额：相当于结果算错了
    wb.save(tmp_path / "期望.xlsx")
    s = rg.run_suite(sc, tmp_path / "out", lambda: FakeLLM(_script()), runs=1, log=lambda _: None)
    r = s["明细"][0]
    assert r["status"] == "OK" and r["missed"] and r["wrong"] == 1 and s["总计"]["算错没拦住"] == 1
    assert "不一致" in (tmp_path / "out" / "S1" / "第1次" / "expected_check.txt").read_text(encoding="utf-8")


def test_questions_answered_from_file_then_continue(tmp_path):
    (tmp_path / "答复.txt").write_text("年度奖扣减本年度已发的季度奖和过程奖", encoding="utf-8")
    sc = _suite(tmp_path, answers="答复.txt")
    s = rg.run_suite(sc, tmp_path / "out", lambda: FakeLLM(_script(blocking=True) + _script()), runs=1,
                     log=lambda _: None)
    r = s["明细"][0]
    assert r["answered_rounds"] == 1 and r["rounds"] == 2 and r["questions"] == ["跨期抵扣口径尚未确认：年度奖扣减哪些已发金额？"]
    assert r["status"] == "OK" and r["correct"]
    # 没配答复：记为停下提问，不算算错
    sc[0].answers = None
    s = rg.run_suite(sc, tmp_path / "out2", lambda: FakeLLM(_script(blocking=True)), runs=1, log=lambda _: None)
    assert s["明细"][0]["status"] == "NEEDS_CONFIRMATION" and s["场景"]["S1"]["卡住原因"] == {"等财务答复": 1}
    assert s["总计"]["算错没拦住"] == 0 and s["明细"][0]["correct"] is None


def test_cause_and_answer_text(tmp_path):
    assert rg.cause("智能体卡住了：verify 连续 3 次失败，最后一次：对不上：引擎算出 1") == "验算对不上"
    assert rg.cause("达到最大步数 150") == "步数用完" and rg.cause("别的") == "其他"
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"answers": [{"question_id": "Q1", "answer": "6%"}]}, ensure_ascii=False), encoding="utf-8")
    assert rg.answer_text(p) == "(Q1) 6%"


def test_default_suite_files_exist():
    for sc in rg.load_suite(CASE.parent.parent / "regress.yaml"):
        if "bonus_fixture_v" in str(sc.inputs[0]) and "examples" not in str(sc.inputs[0]):
            continue                    # 放在仓库外的样例（Q6、Q7），本机可能没有
        assert sc.expected.exists() and sc.materials(), sc.name
        assert not any(f.name.startswith("negative_cases") for f in sc.materials())


def test_scenario_outside_repo_is_skipped_when_missing(tmp_path):
    sc = rg.Scenario(name="X", inputs=[tmp_path / "nope"], expected=tmp_path / "nope.xlsx")
    logs = []
    summary = rg.run_suite([sc], tmp_path / "out", lambda: FakeLLM([]), runs=1, log=logs.append)
    assert summary["总计"]["次数"] == 0 and any("跳过" in x for x in logs)


def test_long_run_skips_failures_and_resumes(tmp_path, monkeypatch):
    """跑几小时的回归：一次超时、一次出错都只记在那一次，接着跑下一个；中途断了用 resume 接着跑，已跑完的不重跑。"""
    sc = _suite(tmp_path)
    clock = iter(range(0, 10 ** 6, 600))                 # 每看一次表过 10 分钟：第一次就超过 5 分钟上限
    with monkeypatch.context() as m:
        m.setattr(rg.time, "time", lambda: next(clock))
        s = rg.run_suite(sc, tmp_path / "out", lambda: FakeLLM(_script()), runs=1, log=lambda _: None, max_minutes=5)
    r = s["明细"][0]
    assert r["status"] == "FAILED" and r["cause"] == "超时跳过" and "超过 5 分钟" in r["error"]

    def broken():
        raise RuntimeError("模型配置坏了")
    s = rg.run_suite(sc, tmp_path / "out2", broken, runs=2, log=lambda _: None)
    assert [r["status"] for r in s["明细"]] == ["ERROR", "ERROR"]          # 出错照样跑完两次、写进汇总
    # 第 1 次跑完后中断：续跑只跑第 2 次，汇总里两次都在
    rg.run_suite(sc, tmp_path / "out3", lambda: FakeLLM(_script()), runs=1, log=lambda _: None)
    seen = []
    s = rg.run_suite(sc, tmp_path / "out3", lambda: FakeLLM(_script()), runs=2, log=seen.append, resume=True)
    assert [r["run"] for r in s["明细"]] == [1, 2] and s["总计"]["金额正确"] == 2
    assert any("上次已跑完，跳过" in x for x in seen)


def test_preflight_checks_main_and_checker_models():
    """开跑前确认模型能调度：主模型不通就不开跑；交叉验证模型不通只提示（会退回主模型）；上下文太小也算不通。"""
    from bonus_agent.llm import LLMError
    good = FakeLLM(lambda m: '{"ok": true}', name="qwen-b")
    ok, lines = rg.preflight(good)
    assert ok and "主模型 qwen-b：可以调用" in lines[0]

    def down(m):
        raise LLMError("Connection error.")
    good.checker_llm = FakeLLM(down, name="qwen-a")
    ok, lines = rg.preflight(good)
    assert ok and "交叉验证模型 qwen-a：调用失败" in lines[1] and "退回主模型" in lines[1]
    small = FakeLLM(lambda m: '{"ok": true}', name="tiny")
    small.ctx = 4096
    ok, lines = rg.preflight(small)
    assert not ok and "4096" in lines[0]
    ok, _ = rg.preflight(FakeLLM(lambda m: "好的", name="chatty"))
    assert not ok


def test_compare_flags_scenarios_that_got_worse(tmp_path):
    # 每次修复后和上一次回归对比：修好一个场景、弄坏另一个，要能一眼看出来
    import json
    from bonus_agent.regress import RunRecord, summarize, compare, load_summary
    from bonus_agent.cli import main
    old = summarize([RunRecord("Q1", 1, status="OK", correct=False, missed=True, wrong=3),
                     RunRecord("Q2", 1, status="OK", correct=True)], {})
    new = summarize([RunRecord("Q1", 1, status="OK", correct=True),
                     RunRecord("Q2", 1, status="FAILED", cause="卡住"),
                     RunRecord("Q3", 1, status="OK", correct=True)], {})
    text = "\n".join(compare(new, old))
    q1, q2, q3 = (next(l for l in text.splitlines() if l.startswith(q)) for q in ("Q1", "Q2", "Q3"))
    assert "变好" in q1 and "变差：正确率下降、完成率下降" in q2 and "上次没跑" in q3
    assert "共 1 个场景变差" in text and "--runs 3" in text
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "summary.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
    assert load_summary(tmp_path / "a")["场景"]["Q1"]["不一致金额"] == 3
    assert main(["regress-compare", str(tmp_path / "a"), str(tmp_path / "b.json")]) == 0
    # 以前算错了没拦住、现在拦下来停下提问：完成率降了也不算变差（1103 的 Q1、Q4）
    old = summarize([RunRecord("Q1", 1, status="OK", correct=False, missed=True)], {})
    new = summarize([RunRecord("Q1", 1, status="NEEDS_CONFIRMATION", cause="等财务答复")], {})
    q1 = next(l for l in compare(new, old) if l.startswith("Q1"))
    assert "变好：算错没拦住变少（改成停下提问）" in q1
