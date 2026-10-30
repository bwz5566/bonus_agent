"""固定回归：对一组场景各跑 N 次，和期望结果逐分比对，出 JSON 汇总，用来判断每次改动是变好还是变坏。

每个场景按网页上的用法跑：智能体自己卡住（写法、原地打转等）时自动接着做，最多 auto_continue 次；
停下提问时，场景配了财务答复文件就把它作为答复接着跑下一轮，没配就记为“停下提问”。
期望结果只在算完后用来核对，不会交给智能体；坏数据用例等不该当材料的文件用 exclude 排除。

核心指标是“算错没拦住”：出了结果、金额却不对。宁可停下来问，也不能出错的结果。
"""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import yaml

from bonus_agent.failure import retryable

SUMMARY = "summary.json"
# 卡住原因分类：(分类, 原始信息里的特征)；按顺序取第一个匹配的
CAUSES = (
    ("输出格式不对", r"参数不是合法 JSON"),
    ("结束前自查没走完", r"结束前自查|条款清单还有没落实"),
    ("验算式写不出", r"验算式算不出来|验算式写错了|辅助变量"),
    ("验算对不上", r"对不上：引擎算出|独立验算还没全部通过"),
    ("程序检查没处理完", r"程序检查还有没处理的"),
    ("同一阻断改不掉", r"保存后仍有同样的阻断问题"),
    ("原地打转", r"反复重复相同的调用|原样调用|反复调用|反复提交相同|反复建了又删"),
    ("tokens 用完", r"tokens 用量"),
    ("步数用完", r"达到最大步数"),
    ("模型服务出错", r"(?i)timeout|timed out|connection|401|403|402|429|5\d\d|api key|quota|余额|欠费"),
    ("程序出错", r"object has no attribute|object is not|not subscriptable|unsupported operand|index out of range|"
                 r"KeyError|TypeError|AttributeError|NoneType|Traceback"),
)


def cause(error: str) -> str:
    for name, pat in CAUSES:
        if re.search(pat, error or ""):
            return name
    return "其他"


@dataclass
class Scenario:
    name: str
    inputs: list[Path]
    expected: Path | None = None
    expected_column: str | None = None
    expected_where: dict[str, str] = field(default_factory=dict)
    exclude: list[str] = field(default_factory=list)      # 不当材料的文件名（可用通配符）
    answers: Path | None = None                           # 停下提问时作为答复的文件
    max_answer_rounds: int = 2

    def materials(self) -> list[Path]:
        """实际交给智能体的文件：输入里去掉排除的和期望结果。"""
        files = []
        for p in self.inputs:
            files += sorted(x for x in p.rglob("*") if x.is_file()) if p.is_dir() else [p]
        skip = {self.expected.resolve()} if self.expected else set()
        if self.answers:
            skip.add(self.answers.resolve())
        return [f for f in files if f.resolve() not in skip and not any(f.match(pat) for pat in self.exclude)]


def load_suite(path: str | Path) -> list[Scenario]:
    """场景配置（YAML），路径相对配置文件。见 evals/regress.yaml。"""
    path = Path(path)
    spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = path.parent

    def rel(p):
        return (base / p).resolve() if p else None

    out = []
    for s in spec.get("scenarios") or []:
        ins = s["inputs"] if isinstance(s["inputs"], list) else [s["inputs"]]
        out.append(Scenario(name=str(s["name"]), inputs=[rel(p) for p in ins], expected=rel(s.get("expected")),
                            expected_column=s.get("expected_column"), expected_where=s.get("expected_where") or {},
                            exclude=list(s.get("exclude") or []), answers=rel(s.get("answers")),
                            max_answer_rounds=int(s.get("max_answer_rounds", 2))))
    return out


def answer_text(path: Path) -> str:
    """财务答复文件的内容：JSON / YAML（answers 列表，每项 answer 字段）或纯文本。"""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() not in (".json", ".yaml", ".yml"):
        return text.strip()
    data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    items = data.get("answers", data) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return text.strip()
    return "\n".join(f"({a.get('question_id') or a.get('id') or i + 1}) {a.get('answer', '')}"
                     for i, a in enumerate(items) if isinstance(a, dict))


def _answer_pending(out: Path, text: str) -> int:
    """把答复填进案卷里还没答复的问题，返回填了几个。"""
    p = out / "questions.yaml"
    qs = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else []
    n = 0
    for q in qs or []:
        if q.get("answer") in (None, "") and not q.get("withdrawn"):
            q["answer"], q["answered_by"] = text, "回归脚本"
            n += 1
    if n:
        p.write_text(yaml.safe_dump(qs, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return n


@dataclass
class RunRecord:
    scenario: str
    run: int
    status: str = ""                 # OK / NEEDS_CONFIRMATION / FAILED / ERROR
    correct: bool | None = None      # 出了结果时金额是否全部逐分一致；没出结果为 None
    missed: bool = False             # 算错没拦住：出了结果、金额不对
    cause: str = ""                  # 没出结果的原因分类
    error: str = ""
    rounds: int = 0
    auto_continued: int = 0
    answered_rounds: int = 0
    questions: list[str] = field(default_factory=list)
    checked: int = 0                 # 核对的金额个数
    wrong: int = 0                   # 不一致的金额个数
    check: list[str] = field(default_factory=list)   # 核对明细（与 expected_check.txt 相同）
    review_rows: int = 0             # 口径单行数
    review_flagged: int = 0          # 口径单上标了疑点的行数
    steps: int = 0
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    out: str = ""


def run_once(sc: Scenario, i: int, out: Path, llm_factory: Callable[[], object], *, auto_continue: int = 2,
             max_steps: int = 150, max_tokens: int = 4_000_000, max_history_chars: int = 40000,
             log: Callable[[str], None] = print) -> RunRecord:
    from bonus_agent import review as rv
    from bonus_agent.agent import run_agent
    from bonus_agent.compare import compare_expected, report
    rec = RunRecord(sc.name, i, out=str(out))
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    files = sc.materials()
    llm = llm_factory()
    t0 = time.time()
    steps = 0
    ar = None
    try:
        new, auto, answered = True, 0, 0
        while True:
            ar = run_agent(llm, files, out, resume=not new, max_steps=max_steps, max_tokens=max_tokens,
                           max_history_chars=max_history_chars)
            new = False
            steps += len(ar.steps)
            rec.rounds += 1
            log(f"  {sc.name} 第 {i} 次 · 第 {ar.round} 轮：{ar.status}" + (f"（{ar.error[:80]}）" if ar.error else ""))
            rec.questions += [q.text for q in ar.ws.pending if q.text not in rec.questions]
            if ar.status == "FAILED" and retryable(ar.error) and auto < auto_continue:
                auto += 1
                continue
            if ar.status == "NEEDS_CONFIRMATION" and sc.answers and answered < sc.max_answer_rounds \
                    and _answer_pending(out, answer_text(sc.answers)):
                answered += 1
                continue
            break
        rec.auto_continued, rec.answered_rounds = auto, answered
        rec.status = ar.status
        if ar.status == "NEEDS_CONFIRMATION":
            rec.cause = "等财务答复"
        elif ar.status != "OK":
            rec.error, rec.cause = ar.error, cause(ar.error)
    except Exception as e:  # 一次运行出错不影响其他场景，如实记录
        rec.status, rec.error = "ERROR", f"{type(e).__name__}: {e}"
        rec.cause = cause(rec.error)
    rec.seconds = round(time.time() - t0, 1)
    u = getattr(llm, "usage", None)
    if u is not None:
        rec.calls, rec.prompt_tokens, rec.completion_tokens = u.calls, u.prompt_tokens, u.completion_tokens
    rec.steps = steps
    if ar is not None and ar.status == "OK" and ar.results:
        rows = rv.load(out)
        rec.review_rows, rec.review_flagged = len(rows), sum(1 for r in rows if r["flag"])
        if sc.expected:
            ms = compare_expected(ar.results, sc.expected, sc.expected_column, sc.expected_where)
            rec.check = report(ms)
            rec.checked = sum(m.total for m in ms)
            rec.wrong = sum(len(m.diffs) for m in ms)
            rec.correct = rec.checked > 0 and all(m.ok for m in ms if m.total or m.ambiguous) and \
                all(m.total or m.ambiguous for m in ms if m.metric == "应得")
            rec.missed = not rec.correct
            (out / "expected_check.txt").write_text("\n".join(rec.check) + "\n", encoding="utf-8")
    return rec


def summarize(records: list[RunRecord], meta: dict) -> dict:
    by: dict[str, list[RunRecord]] = {}
    for r in records:
        by.setdefault(r.scenario, []).append(r)

    def stats(rs: list[RunRecord]) -> dict:
        n = len(rs)
        done = [r for r in rs if r.status == "OK"]
        causes: dict[str, int] = {}
        for r in rs:
            if r.cause:
                causes[r.cause] = causes.get(r.cause, 0) + 1
        return {"次数": n, "出结果": len(done), "金额正确": sum(1 for r in rs if r.correct),
                "算错没拦住": sum(1 for r in rs if r.missed),
                "自动接续过": sum(1 for r in rs if r.auto_continued),     # 中途卡住、自动接着做才完成的次数（稳定性）
                "完成率": round(len(done) / n, 3) if n else 0,
                "正确率": round(sum(1 for r in rs if r.correct) / n, 3) if n else 0,
                "卡住原因": causes,
                "平均耗时秒": round(sum(r.seconds for r in rs) / n, 1) if n else 0,
                "平均输入tokens": sum(r.prompt_tokens for r in rs) // n if n else 0}

    return {"meta": meta, "总计": stats(records), "场景": {k: stats(v) for k, v in by.items()},
            "明细": [asdict(r) for r in records]}


def lines(summary: dict) -> list[str]:
    out = [f"{'场景':<8}{'次数':>4}{'出结果':>6}{'正确':>6}{'算错没拦住':>10}{'自动接续过':>10}  卡住原因"]
    for name, s in list(summary["场景"].items()) + [("总计", summary["总计"])]:
        why = "、".join(f"{k} {v}" for k, v in s["卡住原因"].items()) or "—"
        out.append(f"{name:<8}{s['次数']:>4}{s['出结果']:>6}{s['金额正确']:>6}{s['算错没拦住']:>10}{s['自动接续过']:>10}  {why}")
    return out


def run_suite(scenarios: list[Scenario], out: Path, llm_factory: Callable[[], object], *, runs: int = 3,
              meta: dict | None = None, log: Callable[[str], None] = print, **kw) -> dict:
    """每个场景跑 runs 次；每跑完一次就更新 summary.json（中途停掉也有已跑完的部分）。"""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    meta = {"开始": datetime.now().isoformat(timespec="seconds"), "每个场景次数": runs, **(meta or {})}
    records: list[RunRecord] = []
    summary = summarize(records, meta)
    for sc in scenarios:
        for i in range(1, runs + 1):
            log(f"{sc.name} 第 {i}/{runs} 次 ...")
            rec = run_once(sc, i, out / sc.name / f"第{i}次", llm_factory, log=log, **kw)
            records.append(rec)
            verdict = ("金额全部正确" if rec.correct else f"算错没拦住（{rec.wrong}/{rec.checked} 个金额不一致）"
                       if rec.missed else "已出结果（没有期望结果，未核对）" if rec.status == "OK" else f"没出结果：{rec.cause}")
            log(f"  → {verdict}，{rec.rounds} 轮，{rec.seconds:.0f} 秒")
            summary = summarize(records, meta)
            (out / SUMMARY).write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary
