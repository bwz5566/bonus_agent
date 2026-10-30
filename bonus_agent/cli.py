"""智能体命令行：python -m bonus_agent.cli <命令> ...

  models [--remote]                        列出已配置的模型；--remote 用 Key 查询服务商实际可用的模型
  extract --datasets D.yaml [--model M] --out draft.yaml 文档...
                                           从规则文档抽取规则草稿和问题卡
  map --rules R.yaml [--model M] 数据.xlsx 推荐字段映射（启发式 + 大模型）
  auto [--model M] [--new] [--expected 期望结果.xlsx] [--pipeline] --out 案卷目录 文件或文件夹...
                                           全自动：大模型自主调用工具完成建模和试算，引擎计算，输出 Excel
                                           --out 是案卷目录：有问题时本轮停下等答复；在 questions.yaml 填答复、
                                           补充材料后用同一个目录再运行，从上一轮接着做（--new 从头开始）
                                           （--pipeline 改用固定流程：规划 → 抽取 → 计算）
  confirm --out 案卷目录 --by 确认人 [--note 说明] [--no-review] [数据文件...]
                                           财务确认最新一轮结果：复算核对后把规则、映射和调整冻结为 案卷目录/template/，
                                           以后每期用 bonus run-template 计算，不再调用大模型
  negcheck --out 案卷目录 --cases negative_cases.json [--sheet 表名=工作表 ...] 材料...
                                           坏数据回归：用该案卷的规则（不调用大模型）在逐个改坏的数据上重算，
                                           列出每种坏数据有没有被拦住
  serve --data 数据目录 [--host 127.0.0.1] [--port 8080] [--model M]
                                           内网网页服务：看问题、填答复、传材料、运行下一轮、确认、按模板计算
  user add --data 数据目录 用户名 --roles editor,approver [--display 姓名]
                                           添加或修改网页用户（口令从终端输入，只存哈希）
"""
from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal
import sys
from pathlib import Path

import yaml

from bonus_agent.extract_rules import extract, load_datasets
from bonus_agent.llm import LLM, LLMError, list_remote, load_config
from bonus_agent.suggest_mapping import suggest
from bonus_core.data.table_detect import detect
from bonus_core.dsl.models import RuleSet


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bonus-agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("models")
    p.add_argument("--remote", action="store_true", help="调用服务商 /models 接口，核对已配置模型能否调用")
    p = sub.add_parser("extract")
    p.add_argument("--datasets", required=True, help="数据字典 YAML，或任意一份含 datasets 的规则文件")
    p.add_argument("--model")
    p.add_argument("--name", default="规则草稿")
    p.add_argument("--version", default="draft")
    p.add_argument("--direct-images", action="store_true", help="不单独转写图片，直接看图抽取")
    p.add_argument("--out", required=True)
    p.add_argument("docs", nargs="+")
    p = sub.add_parser("map")
    p.add_argument("--rules", required=True)
    p.add_argument("--model")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("data", nargs="+")
    p = sub.add_parser("confirm")
    p.add_argument("--out", required=True, help="案卷目录")
    p.add_argument("--by", required=True, help="确认人")
    p.add_argument("--note", default="", help="确认说明")
    p.add_argument("--no-review", action="store_true", help="口径单（口径单.json）已在线下逐条核对，不检查网页上的确认记录")
    p.add_argument("data", nargs="*", help="复算用的数据文件（默认用该轮的输入文件）")
    p = sub.add_parser("negcheck")
    p.add_argument("--out", required=True, help="案卷目录（读其中 case.json 的规则）")
    p.add_argument("--cases", required=True, help="坏数据用例 JSON")
    p.add_argument("--sheet", action="append", default=[], help="数据表名=工作表名，可写多次")
    p.add_argument("files", nargs="+", help="原始材料文件或文件夹")
    p = sub.add_parser("serve")
    p.add_argument("--data", required=True, help="数据目录：用户、案卷、材料和结果都在这里")
    p.add_argument("--host", default="127.0.0.1", help="默认只监听本机；内网访问请放在 HTTPS 反向代理后面")
    p.add_argument("--port", type=int, default=8080, help="默认 8080（8000 留给本地 vLLM）")
    p.add_argument("--model")
    p.add_argument("--max-parallel", type=int, default=2, help="同时最多运行几个案卷")
    p = sub.add_parser("user")
    p.add_argument("action", choices=["add"])
    p.add_argument("--data", required=True)
    p.add_argument("name")
    p.add_argument("--roles", default="editor", help="逗号分隔：editor（传材料、答复、运行）、approver（确认结果）")
    p.add_argument("--display", default="", help="显示的姓名（记入答复人、确认人）")
    p.add_argument("--password-stdin", action="store_true", help="从标准输入读口令（脚本用）")
    p = sub.add_parser("auto")
    p.add_argument("--model")
    p.add_argument("--answers", help="财务答复文件（默认读案卷目录里的 questions.yaml）")
    p.add_argument("--out", required=True, help="案卷目录：每轮的输出、问题和工作区都在这里，下一轮接着用")
    p.add_argument("--new", action="store_true", help="忽略案卷目录里上一轮的工作，从头开始")
    p.add_argument("--max-files", type=int, default=30, help="一批最多处理的文件数，防止误传整个目录")
    p.add_argument("--max-steps", type=int, default=150, help="安全上限：智能体最多调用工具的轮数，完成即停，正常用不满")
    p.add_argument("--max-tokens", type=int, default=4_000_000, help="安全上限：本次最多消耗的 tokens，超过即停止")
    p.add_argument("--expected", help="期望结果 Excel，只在算完后用来逐个对象核对，不会交给智能体；不要放在输入文件夹里")
    p.add_argument("--expected-column", help="期望结果中应得金额的列名（自动识别不出或有几列时指定）")
    p.add_argument("--pipeline", action="store_true", help="使用固定流程（规划 → 抽取 → 计算），不让模型自主调度")
    p.add_argument("paths", nargs="+", help="本批次的规则文档、Excel 数据，或只包含它们的文件夹")
    a = ap.parse_args(argv)

    if a.cmd == "user":
        import getpass

        from bonus_agent.web import Users
        if a.password_stdin:
            pw = sys.stdin.readline().rstrip("\n")
        else:
            pw = getpass.getpass("口令（至少 8 位）：")
            if pw != getpass.getpass("再输一次："):
                print("两次输入不一致")
                return 1
        try:
            Users(Path(a.data) / "users.yaml").add(a.name, pw, [r for r in a.roles.split(",") if r], a.display)
        except ValueError as e:
            print(f"失败：{e}")
            return 1
        print(f"已保存用户 {a.name}（{a.roles}）")
        return 0
    if a.cmd == "negcheck":
        from bonus_agent import case as cs
        from bonus_agent.auto import collect_files
        from bonus_agent.negcheck import replay, report
        case = cs.load_case(Path(a.out))
        if not case or not case.get("workspace"):
            print(f"{a.out} 里没有案卷记录（case.json）")
            return 1
        sheets = dict(s.split("=", 1) for s in a.sheet)
        try:
            res = replay(case["workspace"], collect_files(a.files), json.loads(Path(a.cases).read_text(encoding="utf-8")), sheets)
        except ValueError as e:
            print(f"失败：{e}")
            return 1
        print("\n".join(report(res)))
        return 0 if all(r.blocked for r in res) else 3
    if a.cmd == "serve":
        from bonus_agent.web import serve
        serve(a.data, a.host, a.port, a.model, max_parallel=a.max_parallel)
        return 0
    cfg = load_config()
    if a.cmd == "models":
        remote: dict[str, set[str] | str] = {}
        if a.remote:
            for prov in sorted({m["provider"] for m in cfg["models"].values()}):
                if prov == "local_vllm":   # 选型阶段还没部署，查不到是正常的
                    try:
                        remote[prov] = set(list_remote(prov, cfg))
                    except LLMError:
                        remote[prov] = "本地 vLLM 还未部署（选型完成、部署后才需要），已跳过"
                    continue
                try:
                    remote[prov] = set(list_remote(prov, cfg))
                except LLMError as e:
                    remote[prov] = str(e)
        for name, m in cfg["models"].items():
            mark = "（默认）" if name == cfg["default"] else ""
            tags = [g for g, ms in [("候选", cfg.get("candidates", []))] + list(cfg.get("groups", {}).items())
                    if name in ms]
            line = f"{name}{mark}: {m['provider']} / {m['model']}" + (f"  [{', '.join(tags)}]" if tags else "")
            if a.remote:
                r = remote[m["provider"]]
                line += "  无法查询" if isinstance(r, str) else ("  可用" if m["model"] in r else "  不可用")
            print(line)
        for prov, r in remote.items():
            if isinstance(r, str):
                print(f"\n{r}")
            else:
                print(f"\n{prov} 共返回 {len(r)} 个模型，其中名字含 vl / 3.5 / 3.6 的：")
                print("  " + ", ".join(x for x in sorted(r) if any(k in x for k in ("vl", "3.5", "3.6", "omni"))))
        return 0

    if a.cmd == "extract":
        llm = LLM.from_config(a.model, cfg)
        res = extract(llm, a.docs, load_datasets(a.datasets), name=a.name, version=a.version,
                      direct_images=a.direct_images)
        out = Path(a.out)
        if res.ruleset is not None:
            out.write_text(res.ruleset.dump_yaml(), encoding="utf-8")
            print(f"规则草稿已写出：{out}")
        qpath = out.with_suffix(".questions.json")
        qpath.write_text(json.dumps({
            "questions": [q.model_dump() for q in res.questions],
            "issues": [i.to_dict() for i in res.issues],
            "transcripts": res.transcripts,
            "usage": vars(res.usage),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"问题卡与校验结果：{qpath}")
        for q in res.questions:
            print(f"  ? {q.text}  选项：{' / '.join(q.options)}")
        for i in res.issues:
            print(f"  {i}")
        if res.error:
            print(f"失败：{res.error}")
        return 0 if res.ok else 1

    if a.cmd == "map":
        rs = RuleSet.load(a.rules)
        llm = None if a.no_llm else LLM.from_config(a.model, cfg)
        m = suggest(rs, [s for f in a.data for s in detect(f)], llm)
        print(yaml.safe_dump({k: v.model_dump(exclude_none=True) for k, v in m.items()}, allow_unicode=True, sort_keys=False))
        return 0
    if a.cmd == "confirm":
        from bonus_agent.case import ConfirmError, confirm
        try:
            tpl = confirm(Path(a.out), a.by, a.note, [Path(x) for x in a.data] or None, review=not a.no_review)
        except ConfirmError as e:
            print(f"不能确认：{e}")
            return 1
        print(f"已确认并冻结为模板：{tpl}（按模板复算，与该轮结果逐分一致）")
        print(f"以后每期：bonus run-template --template {tpl} --out 结果目录 数据文件...")
        return 0
    if a.cmd == "auto" and not a.pipeline:
        return _agent(a, cfg)
    if a.cmd == "auto":
        from bonus_agent.auto import run_auto
        answers = yaml.safe_load(Path(a.answers).read_text(encoding="utf-8")) if a.answers else None
        llm = LLM.from_config(a.model, cfg)
        print(f"使用模型 {llm.name}（固定流程）", flush=True)

        def show(files):
            print(f"本批次共 {len(files)} 个文件：", flush=True)
            for f in files:
                print(f"  {f}", flush=True)
            print("统筹规划中 ...", flush=True)

        ar = run_auto(llm, a.paths, a.out, answers=answers, max_files=a.max_files, on_files=show)
        po = ar.plan
        if po.plan:
            print(f"\n【规划】{po.plan.summary}")
            print("  分配层级：" + " → ".join(lv.level for lv in po.plan.hierarchy))
            for n, m in po.mapping.items():
                print(f"  数据表 {n} ← {m.file}/{m.sheet}（{len(m.columns)} 个字段）")
            for w in po.warnings:
                print(f"  注意：{w}")
        if ar.result is not None:
            print(f"\n【结果】奖金池 {Decimal(ar.result.pool):,}，结果已写入 {ar.outputs.get('excel')}")
        if ar.questions:
            print(f"\n【待确认问题】{len(ar.questions)} 个（已按建议理解先算，答复后用 --answers 重跑）：")
            for stage, q in ar.questions:
                print(f"  [{stage}] {q.text}  选项：{' / '.join(q.options)}")
        for i in ar.issues:
            if i.severity == "warning":
                print(f"  {i}")
        if ar.error:
            print(f"\n失败：{ar.error}")
            for e in po.errors:
                print(f"  - {e}")
        print(f"\n输出目录：{a.out}（规则 rules.yaml、映射 mapping.yaml、问题 questions.yaml、日志 agent_log.json）")
        u = llm.usage
        print(f"调用 {u.calls} 次，输入 {u.prompt_tokens} tokens，输出 {u.completion_tokens} tokens，耗时 {u.latency_s:.0f} 秒")
        return 0 if ar.ok else 1
    return 2


def _agent(a, cfg) -> int:
    from bonus_agent.agent import ToolError, run_agent
    answers = yaml.safe_load(Path(a.answers).read_text(encoding="utf-8")) if a.answers else None
    llm = LLM.from_config(a.model, cfg)
    hist = cfg["models"][llm.name].get("max_history_chars", 40000)
    print(f"使用模型 {llm.name}，智能体开始工作 ...", flush=True)

    trace = os.environ.get("BONUS_AGENT_TRACE")     # 调试用：每一步的完整参数和结果逐行写入这个文件（JSON Lines）

    def show(n, step):
        args = json.dumps(step.args, ensure_ascii=False)
        if trace:
            with open(trace, "a", encoding="utf-8") as f:
                f.write(json.dumps({"n": n, "tool": step.tool, "args": step.args, "ok": step.ok, "result": step.result},
                                   ensure_ascii=False) + "\n")
        print(f"  [{n:02d}] {step.tool} {args[:100]}{'…' if len(args) > 100 else ''}", flush=True)
        lines = step.result.splitlines() or [""]
        if step.ok:
            print(f"       ✓ {lines[0][:150]}", flush=True)
        else:   # 失败时显示完整原因，方便判断是模型的问题还是规则表达不了
            print(f"       ✗ {lines[0][:300]}", flush=True)
            for ln in lines[1:20]:
                print(f"         {ln[:300]}", flush=True)
            if len(lines) > 20:
                print(f"         ……共 {len(lines)} 行，完整内容见 agent_log.json", flush=True)

    try:
        ar = run_agent(llm, a.paths, a.out, answers=answers, max_steps=a.max_steps, max_tokens=a.max_tokens,
                       exclude=[a.expected] if a.expected else [],
                       max_history_chars=hist, resume=not a.new,
                       max_files=a.max_files, on_step=show)
    except ToolError as e:
        print(f"失败：{e}")
        return 1
    if ar.ws.summary:
        print(f"\n【方案】{ar.ws.summary}")
    for name, revs in ar.results.items():
        for rev, res in revs.items():
            label = "" if ar.single else f"「{name}{'' if rev == 'R0' else ' ' + rev}」"
            print(f"【结果】{label}奖金池 {Decimal(res.pool):,}")
    if ar.results:
        print(f"结果已写入 {ar.outputs.get('excel')}" + ("" if ar.single else "（各方案明细见同目录 result_*.xlsx）"))
        print(f"【复算核对】{ar.recheck}")
    if a.expected and ar.results:
        from bonus_agent.compare import compare_expected, report
        lines = report(compare_expected(ar.results, a.expected, a.expected_column))
        (Path(a.out) / "expected_check.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n【与期望结果核对】（已写入 expected_check.txt）")
        for ln in lines:
            print(f"  {ln}")
    if ar.round > 1:
        ch = "；".join(f"{k} {'、'.join(v)}" for k, v in (ar.changes or {}).items() if v) or "无"
        print(f"\n【第 {ar.round} 轮】文件变化：{ch}；规则改动：{'；'.join(ar.rule_changes) or '无'}")
        if ar.compared:
            print(f"  与上一轮相比金额变化的对象 {len(ar.diff_rows)} 个（见 result.xlsx「与上一轮对比」）")
    if ar.questions:
        print(f"\n【问题】{len(ar.questions)} 个：")
        for q in ar.questions:
            state = (f"已答复：{q.answer}" if q.answer else f"已撤回：{q.withdrawn}" if q.withdrawn
                     else "等待答复" if q.blocking else "提示")
            print(f"  {q.id}. {q.text}  选项：{' / '.join(q.options) or '无'}  [{state}]")
    if ar.status == "NEEDS_CONFIRMATION":
        print(f"\n【状态】NEEDS_CONFIRMATION：{len(ar.pending)} 个问题等财务答复，本轮不出结果。")
        print(f"  下一步：在 {Path(a.out) / 'questions.yaml'} 的 answer 填写答复（有补充材料放进材料文件夹），"
              f"然后用同一个 --out 再运行一次，会从这一轮接着做。")
    if ar.error:
        from bonus_agent.failure import to_text
        print(f"\n未完成：{to_text(ar.error).replace('点「重试」', '用同一个 --out 再运行一次')}\n  详细信息：{ar.error}")
    print(f"\n输出目录：{a.out}（result*.xlsx、rules*.yaml、mapping.yaml、questions.yaml、agent_log.json）")
    u = llm.usage
    cached = f"（其中命中缓存 {u.cached_tokens}）" if u.cached_tokens else ""
    print(f"共 {len(ar.steps)} 步，调用模型 {u.calls} 次，输入 {u.prompt_tokens} tokens{cached}，输出 {u.completion_tokens} tokens，"
          f"平均每次输入 {u.prompt_tokens // max(u.calls, 1)}，耗时 {u.latency_s:.0f} 秒")
    return {"OK": 0, "NEEDS_CONFIRMATION": 3}.get(ar.status, 1)


if __name__ == "__main__":
    sys.exit(main())
