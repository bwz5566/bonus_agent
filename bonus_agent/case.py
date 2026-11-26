"""案卷：一笔奖金的计算往往要和财务来回几轮（提问 → 答复 → 补充材料 → 修改规则），每轮都接着上一轮做。

输出目录就是案卷，里面的 case.json 记录：
- 工作区快照：数据表定义和列映射、各方案规则、笔记、图片转写、问题和财务答复、方案概述；
- 每一轮：时间、输入文件指纹（sha256）、状态、问题、结果摘要（每个对象的金额），用于下一轮对比。

下一轮用同一个输出目录再运行，就从快照恢复工作区，只告诉智能体这一轮变了什么（答复、新增 / 修改 / 删除的文件），
不必从头读材料；算完后与上一轮逐个对象对比金额、列出规则改动。历次输出留在 history/第N轮/。
"""
from __future__ import annotations

import hashlib
import copy
import errno
import json
import os
import shutil
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from bonus_agent.extract_rules import Question
from bonus_agent.clauses import Clause
from bonus_agent.facts import Conflict, Fact
from bonus_core.data.mapping import DatasetMapping
from bonus_core.dsl.models import DatasetDef
from bonus_core.engine.run import Result

CASE_FILE = "case.json"


class CaseBusyError(RuntimeError):
    pass


@contextmanager
def case_lock(out: Path | None):
    """Hold one OS-owned writer lock for a case, released on close or process exit."""
    if out is None:
        yield
        return
    out.mkdir(parents=True, exist_ok=True)
    with (out / '.case.lock').open('a+b') as stream:
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            raise CaseBusyError('案卷正在计算或确认，完成当前操作后再提交。') from exc
        yield


def fingerprint(files: list[Path]) -> dict[str, str]:
    # 指纹按文件名记。不同文件夹里的同名文件会共用一条记录，其中一个改了也发现不了，所以先拒绝同名
    from bonus_core.data.files import require_unique_names
    require_unique_names(files)
    return {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in files}


def load_case(out: Path) -> dict | None:
    p = Path(out) / CASE_FILE
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            from bonus_agent.infra import checkpoints
            recovered = checkpoints.latest_case(out)
            if recovered is not None:
                recovered['_recovery']={'source':'checkpoint','damaged_path':str(p)}
                return recovered
            backup = p.with_suffix('.previous.json')
            if backup.exists():
                recovered=json.loads(backup.read_text(encoding='utf-8'))
                recovered['_recovery']={'source':'previous','damaged_path':str(p)}
                return recovered
            raise ValueError(f'案卷快照损坏且没有可恢复版本：{p}；保留损坏文件，不能从空工作区继续') from None
    # case.json 丢失（如写盘中途崩溃）：开启平台状态存储时从最近的检查点恢复
    from bonus_agent.infra import checkpoints
    return checkpoints.latest_case(out)


def save_case(out: Path, case: dict) -> Path:
    p = Path(out) / CASE_FILE
    from bonus_agent.infra.atomic import write_text
    if p.exists():
        try:
            prior = p.read_text(encoding='utf-8')
            json.loads(prior)
        except (ValueError,UnicodeError):
            pass  # do not replace a good backup with an already damaged snapshot
        else:
            write_text(p.with_suffix('.previous.json'),prior)
    write_text(p,json.dumps(case, ensure_ascii=False, indent=1))
    _checkpoint(out, case)
    return p


def _checkpoint(out: Path, case: dict) -> None:
    """每次存案卷后同步写一份检查点（仅在设置了 BONUS_AGENT_STATE_DIR 时）；失败不影响 case.json。"""
    from bonus_agent.infra import checkpoints
    try:
        checkpoints.save_round(out, case)
    except Exception as e:  # noqa: BLE001 - durability mirror must never break the round
        import warnings
        warnings.warn(f"检查点写入失败：{type(e).__name__}: {e}", RuntimeWarning, stacklevel=3)


def snapshot(ws) -> dict:
    flow = copy.deepcopy(ws.workflow)
    if ws.staged:
        flow['view_basis_version'] = ws._flow().version('basis')
    return {
        "datasets": {k: v.model_dump(mode="json", exclude_defaults=True) for k, v in ws.datasets.items()},
        "mapping": {k: v.model_dump(mode="json", exclude_defaults=True) for k, v in ws.mapping.items()},
        "drafts": ws.drafts,
        "notes": ws.notes,
        "transcripts": ws.transcripts,
        "summary": ws.summary,
        "ignored": ws.ignored,
        "questions": [q.model_dump() for q in ws.questions],
        "facts": [f.model_dump() for f in ws.facts],
        "clauses": [c.model_dump() for c in ws.clauses],
        "conflicts": {k: c.model_dump() for k, c in ws.conflicts.items()},
        "verified": ws.verified,
        "check_vars": ws.check_vars,
        "reread": ws.reread,
        "triage": ws.triage,
        "repairs": ws.repairs,
        "repair_attempts": ws.repair_attempts,
        "workflow": flow,
        "said": ws.said,
        "report_required": ws.report_required,
    }


def confirm_totals(out: Path, scheme: str, digest: str, by: str) -> dict:
    """Human-only boundary: recompute the exact pending basis before storing approval."""
    try:
        with case_lock(out):
            return _confirm_totals(out, scheme, digest, by)
    except CaseBusyError as exc:
        raise ConfirmError(str(exc)) from exc


def _confirm_totals(out: Path, scheme: str, digest: str, by: str) -> dict:
    from bonus_agent.agent import Workspace, ToolError
    from bonus_agent.auto import build_inventory
    from bonus_agent.llm import FakeLLM
    from bonus_core.data.period import Period
    from bonus_agent.workflow import Workflow
    from bonus_core.engine.issues import EngineError

    obj = load_case(out)
    if not obj or not obj.get('rounds'):
        raise ConfirmError('没有待确认的案卷')
    if obj.get('workspace',{}).get('workflow',{}).get('fixed_commission'):
        from bonus_agent.commission_chain import approve_fixed
        return approve_fixed(out,obj,scheme,digest,by)
    rnd = obj['rounds'][-1]
    paths = [Path(x['path']) for x in rnd.get('source_inputs', [])]
    if not paths or any(not p.is_file() for p in paths):
        raise ConfirmError('原轮输入缺失，须重新核算')
    if fingerprint(paths) != rnd.get('files'):
        raise ConfirmError('输入文件已变化，须重新核算')
    ws = Workspace(FakeLLM([]), build_inventory(paths))
    restore(ws, obj['workspace'])
    ws.period = Period.parse(rnd.get('period'))
    if not ws.staged:
        raise ConfirmError('该案卷不是分阶段核算')
    try:
        record = Workflow(ws).approve(scheme, by, digest, reviewed_snapshot=True)
    except (ToolError, EngineError, ValueError, KeyError) as exc:
        raise ConfirmError(str(exc)) from None
    if record.get('already_confirmed'):
        return record
    obj['workspace'] = snapshot(ws)
    obj.setdefault('total_confirmations', []).append(
        {**record, 'at': datetime.now().isoformat(timespec='seconds'), 'round': rnd['n']}
    )
    save_case(out, obj)
    return record


def restore(ws, snap: dict) -> None:
    ws.datasets = {k: DatasetDef.model_validate(v) for k, v in snap.get("datasets", {}).items()}
    ws.mapping = {k: DatasetMapping.model_validate(v) for k, v in snap.get("mapping", {}).items()}
    ws.drafts = snap.get("drafts", {})
    ws.notes = snap.get("notes", {})
    ws.transcripts = snap.get("transcripts", {})
    ws.summary = snap.get("summary", "")
    ws.ignored = snap.get("ignored", [])
    ws.questions = [Question.model_validate(q) for q in snap.get("questions", [])]
    ws.facts = [Fact.model_validate(f) for f in snap.get("facts", [])]
    ws.clauses = [Clause.model_validate(c) for c in snap.get("clauses", [])]
    ws.conflicts = {k: Conflict.model_validate(c) for k, c in snap.get("conflicts", {}).items()}
    ws.check_vars = snap.get("check_vars", {})
    ws.reread = snap.get("reread", {})
    ws.triage = snap.get("triage", {})
    ws.repairs = snap.get("repairs", {})
    ws.repair_attempts = snap.get("repair_attempts", {})
    ws.workflow = snap.get('workflow',{})
    ws.said = snap.get('said','')
    ws.report_required = ws.report_required or snap.get('report_required') is True
    from bonus_agent.agent import _verify_key
    ws.verified = {}
    for key, record in snap.get("verified", {}).items():
        if key.startswith('['):
            try:
                parts = json.loads(key)
                if len(parts) == 3 and all(isinstance(x, str) for x in parts) and parts[2] in ('应得', '实发'):
                    ws.verified[_verify_key(*parts)] = record
            except (ValueError, TypeError):
                pass
            continue
        matches = [s for s in ws.schemes if key.startswith(s + '|')]
        if len(matches) != 1:  # ambiguous legacy keys require a new independent verification
            continue
        name = matches[0]
        amount = record.get('amount', '应得')
        node = key[len(name)+1:]
        if amount == '实发':
            node = node.removesuffix('|实发')
        ws.verified[_verify_key(name, node, amount)] = record
    ws.untested = set(ws.schemes)            # 数据或答复可能变了，每个方案都要重新试算


def restore_confirmed_basis(out: Path, case: dict, ws) -> None:
    """Recover older human confirmations using the reviewed round's saved inputs."""
    from bonus_agent.recovery import fingerprint as digest_of
    from bonus_agent.workflow import Workflow
    from bonus_core.data.period import Period

    if not case.get('total_confirmations') or not ws.staged:
        return
    archived = ws.workflow.setdefault('confirmed_basis', {})
    pending = set(ws.schemes) - archived.keys()
    snapshots = {}
    for record in reversed(case['total_confirmations']):
        if not isinstance(record, dict):
            continue
        name = record.get('scheme')
        if not isinstance(name, str) or name not in pending:
            continue
        pending.remove(name)
        n, digest, by = record.get('round'), record.get('digest'), record.get('by')
        if record.get('status') != 'PASS' or type(n) is not int or n < 1 or not digest or not by or not record.get('at'):
            continue
        if n not in snapshots:
            path = out / 'history' / f'第{n}轮' / CASE_FILE
            try:
                snapshots[n] = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                snapshots[n] = {}
        previous = snapshots[n]
        if not isinstance(previous, dict):
            continue
        rows = previous.get('rounds') or []
        if not rows or rows[-1].get('n') != n:
            continue
        inputs = rows[-1]
        try:
            period = Period.parse(inputs.get('period'))
        except ValueError:
            continue
        if inputs.get('files') != ws._source_version or period != ws.period:
            continue
        workspace = previous.get('workspace', {})
        flow = workspace.get('workflow', {})
        review = (flow.get('receipts', {}).get('review:' + name) or {}).get('payload', {})
        if (review.get('status') != 'PASS' or review.get('basis_digest') != digest
                or digest_of(flow.get('basis', {}).get(name)) != digest):
            continue
        try:
            questions = [Question.model_validate(q) for q in workspace.get('questions', [])]
        except ValueError:
            continue
        archived[name] = {
            'scope': Workflow.confirmation_scope(digest, ws._source_version, ws.period, questions),
            'by': by, 'at': record['at'],
        }


def apply_answers(questions: list[Question], answers: list[dict] | None) -> list[Question]:
    """把财务答复（questions.yaml 里填的 answer）对到问题上：先按编号，再按问题原文。返回本轮新答复的问题。"""
    fresh = []
    by_id = {a.get("id"): a for a in answers or [] if a.get("id")}
    by_text = {a.get("text"): a for a in answers or [] if a.get("text")}
    for q in questions:
        a = by_id.get(q.id) or by_text.get(q.text)
        if a is None or a.get("answer") in (None, ""):
            continue
        ans = a["answer"]
        if isinstance(ans, int) and not isinstance(ans, bool) and 0 <= ans < len(q.options):
            ans = q.options[ans]
        if q.answer != str(ans):
            q.answer = str(ans)
            q.answered_by, q.answered_at = str(a.get("answered_by") or ""), str(a.get("answered_at") or "")
            fresh.append(q)
    return fresh


def file_changes(before: dict[str, str], now: dict[str, str]) -> dict[str, list[str]]:
    return {"新增": sorted(set(now) - set(before)), "删除": sorted(set(before) - set(now)),
            "修改": sorted(n for n in set(now) & set(before) if now[n] != before[n])}


def digest(results: dict[str, dict[str, Result]]) -> dict:
    """结果摘要：方案 -> 修订 -> 对象 -> [名称, 应得, 本次实发]。"""
    out = {}
    for s, revs in results.items():
        for rev, r in revs.items():
            out.setdefault(s, {})[rev] = {
                nid: [n.label, n.final, r.payouts[nid].paid if nid in r.payouts else None]
                for nid, n in r.nodes.items() if n.key}
            out[s][rev].update({nid: [x.label, x.final, x.paid] for nid, x in r.rollups.items()})
    return out


def financial_digest(results: dict) -> dict:
    """Freeze all monetary views and their object sets, not only earned amounts."""
    from dataclasses import asdict, is_dataclass
    fields = ('nodes','payouts','rollups','people','totals','details','rounding','pool','pool_exact')
    out={}
    for scheme,revs in results.items():
        out[scheme]={}
        for rev,res in revs.items():
            payload={k:getattr(res,k,None) for k in fields}
            out[scheme][rev]=json.loads(json.dumps(payload,ensure_ascii=False,
                         default=lambda value:asdict(value) if is_dataclass(value) else str(value)))
    return out


def diff_results(before: dict, results: dict[str, dict[str, Result]]) -> list[list]:
    """与上一轮逐个对象对比：[方案, 修订, 对象, 名称, 上一轮应得, 本轮应得, 差额, 上一轮实发, 本轮实发]，只列有变化的。"""
    now = digest(results)
    rows = []
    for s in sorted(set(before) | set(now)):
        for rev in sorted(set(before.get(s, {})) | set(now.get(s, {}))):
            b, n = before.get(s, {}).get(rev, {}), now.get(s, {}).get(rev, {})
            for nid in sorted(set(b) | set(n)):
                (lb, fb, pb), (ln, fn, pn) = b.get(nid, [None] * 3), n.get(nid, [None] * 3)
                if fb == fn and pb == pn:
                    continue
                delta = (Decimal(fn or 0) - Decimal(fb or 0)) if (fb or fn) else None
                rows.append([s, rev, nid, ln or lb, _f(fb), _f(fn), float(delta) if delta is not None else None,
                             _f(pb), _f(pn)])
    return rows


def _f(v):
    return None if v is None else float(Decimal(v))


def diff_rules(before: dict, now: dict) -> list[str]:
    """规则草稿的改动：每个方案的参数表、变量按名字比，其余部分整体比。"""
    lines = []
    for s in sorted(set(before) | set(now)):
        b, n = before.get(s, {}), now.get(s, {})
        if s not in before:
            lines.append(f"新增方案「{s}」")
            continue
        if s not in now:
            lines.append(f"删除方案「{s}」")
            continue
        parts = []
        for k in ("tables", "variables"):
            bk, nk = b.get(k) or {}, n.get(k) or {}
            add = [x for x in nk if x not in bk]
            rm = [x for x in bk if x not in nk]
            mod = [x for x in nk if x in bk and _strip(nk[x]) != _strip(bk[x])]
            label = "参数表" if k == "tables" else "变量"
            parts += ([f"{label}新增 {'、'.join(add)}"] if add else []) + ([f"{label}删除 {'、'.join(rm)}"] if rm else []) \
                + ([f"{label}修改 {'、'.join(mod)}"] if mod else [])
        for k, label in (("pool", "奖金池"), ("allocation", "分配层级与权重"), ("payout", "发放条件"), ("offsets", "已发抵扣"),
                         ("checks", "数据校验"), ("revisions", "修订")):
            if _strip(b.get(k)) != _strip(n.get(k)):
                parts.append(f"{label}修改")
        if parts:
            lines.append(f"「{s}」：" + "；".join(parts))
    return lines


def _strip(v):
    """比较时忽略出处、说明、置信度这些不影响计算的字段。"""
    if isinstance(v, dict):
        return {k: _strip(x) for k, x in v.items() if k not in ("sources", "desc", "confidence")}
    if isinstance(v, list):
        return [_strip(x) for x in v]
    return v


def archive(out: Path, n: int, paths: dict[str, Path]) -> Path:
    """把本轮输出复制到 history/第N轮/。"""
    d = Path(out) / "history" / f"第{n}轮"
    d.mkdir(parents=True, exist_ok=True)
    for p in paths.values():
        if p.exists() and p.is_file():
            shutil.copy2(p, d / p.name)
    return d


def new_round(case: dict | None, files: dict[str, str]) -> dict:
    rounds = (case or {}).get("rounds", [])
    return {"n": len(rounds) + 1, "at": datetime.now().isoformat(timespec="seconds"), "files": files}


def sha256(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class ConfirmError(ValueError):
    pass


TEMPLATE_NOTICE = "请勿修改-说明.txt"
PROCESSING_NOTICE = "正在处理中-请勿修改.txt"


def _revision_keys(outs: dict) -> dict[str, str]:
    """本轮输出里的人工调整修订：{"方案/修订名": 输出键}。"""
    return {"/".join(k.split(":", 2)[1:]): k for k in outs if k.startswith("adjust:")}


def _required_revisions(out: Path, outs: dict) -> dict[str, list[str]]:
    """被其他方案的规则引用（scheme_paid("方案/修订")）的修订：{修订: [引用它的方案]}。
    不录入它，引用方在模板里就算不出来，所以不是可选项。"""
    import re
    # scheme_paid("方案", "修订")；YAML/JSON 里引号可能带转义
    ref = re.compile(r"""scheme_paid\(\s*\\?["']([^"'\\]+)\\?["']\s*(?:,\s*\\?["']([^"'\\]+)\\?["'])?""")
    need: dict[str, list[str]] = {}
    for k, v in outs.items():
        if (k == "rules" or k.startswith("rules:")) and (out / v[0]).exists():
            for scheme, rev in ref.findall((out / v[0]).read_text(encoding="utf-8")):
                need.setdefault(f"{scheme}/{rev or 'R0'}", []).append("默认" if k == "rules" else k.split(":", 1)[1])
    return {r: sorted(set(s)) for r, s in need.items() if r in _revision_keys(outs)}


def confirm_options(out: Path) -> dict:
    """确认前要问财务的事（接口/MCP 先调它，把问题交给人选，再带着选择调用 confirm）：
    - 是否把本轮录入模板（录入后以后每期换数据直接按模板算）；
    - 本轮相对已录入模板改了哪些规则（规则整体录入，不能挑着录：模板必须能复算出本轮已确认的金额）；
    - 有哪些人工调整修订，逐个选录不录（录入的调整以后每期都会自动带上）。"""
    import yaml
    out = Path(out)
    case = load_case(out) or {}
    rnd = (case.get("rounds") or [{}])[-1]
    outs = rnd.get("outputs") or {}

    def load(p: Path):
        return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}

    now = {("默认" if k == "rules" else k.split(":", 1)[1]): load(out / v[0])
           for k, v in outs.items() if k == "rules" or k.startswith("rules:")}
    manifest = load(out / "template" / "manifest.yaml")
    before = {s["name"]: load(out / "template" / s["rules"]) for s in manifest.get("schemes") or []}
    saved = {f"{s['name']}/{r['name']}" for s in manifest.get("schemes") or [] for r in s.get("revisions") or []}
    revisions = []
    required = _required_revisions(out, outs)
    for key, k in _revision_keys(outs).items():
        items = yaml.safe_load((out / outs[k][0]).read_text(encoding="utf-8")) or [] if (out / outs[k][0]).exists() else []
        revisions.append({"key": key, "new": key not in saved,
                          # 被其他方案引用：必须录入（引用方要用它的结果），不是可选项
                          "required_by": required.get(key, []),
                          "adjustments": [{"node": a.get("node_id"), "amount": str(a.get("amount")),
                                           "reason": a.get("reason", "")} for a in items]})
    return {
        "round": rnd.get("n"), "status": rnd.get("status"), "has_template": bool(manifest),
        "rule_changes": diff_rules(before, now) if manifest else [f"首次录入方案「{s}」的全部规则" for s in now],
        "revisions": revisions,
        "ask": "是否把本轮结果录入模板？录入后以后每期换数据直接按模板计算。"
               + ("另有人工调整修订，请逐个选择是否录入：录入的调整以后每期都会自动带上。" if revisions else ""),
    }


def _checked_round(out: Path) -> tuple[dict, dict, dict]:
    case = load_case(out)
    if not case or not case.get("rounds"):
        raise ConfirmError(f"{out} 不是案卷（没有 {CASE_FILE}），先用 bonus-agent auto --out {out} 计算")
    rnd = case["rounds"][-1]
    if rnd.get("status") != "OK":
        raise ConfirmError(f"第 {rnd['n']} 轮状态是 {rnd.get('status')}，没有正式结果，不能确认"
                           + ("（还有问题等财务答复）" if rnd.get("pending") else ""))
    if case.get('workspace',{}).get('workflow',{}).get('fixed_commission'):
        from bonus_agent.commission_revision import validate_current
        validate_current(out,case,final=True)
    outs = rnd.get("outputs") or {}
    changed = [name for name, h in outs.values() if not (out / name).is_file() or sha256(out / name) != h]
    if changed:
        raise ConfirmError(f"这些文件在第 {rnd['n']} 轮输出之后被改过或删除：{'、'.join(changed)}。"
                           "改规则请走一轮智能体处理，不要直接改文件")
    return case, rnd, outs


def _template_plan(out: Path, rnd: dict, outs: dict, data: list[Path] | None,
                   revisions: list[str] | None) -> tuple[dict, list[Path], dict]:
    from bonus_core.dsl.models import RuleSet
    from bonus_core.template import order_schemes

    available = _revision_keys(outs)
    required = _required_revisions(out, outs)
    optional = [k for k in available if k not in required]
    if revisions is None and optional:
        raise ConfirmError("本轮有人工调整修订：" + "、".join(optional) + "。录入模板后，以后每期按模板计算都会自动带上这些调整，"
                           "请逐个选择是否录入（都不录入也要明确选择），或只确认结果、不录入模板")
    unknown = sorted(set(revisions or []) - set(available))
    if unknown:
        raise ConfirmError(f"没有这些修订：{'、'.join(unknown)}；本轮可选：{'、'.join(optional) or '无'}")
    selected = sorted(set(revisions or []) | set(required))
    kept = {available[k] for k in selected}
    outs = {k: v for k, v in outs.items() if not k.startswith("adjust:") or k in kept}
    names = {("默认" if k == "rules" else k.split(":", 1)[1]): v[0]
             for k, v in outs.items() if k == "rules" or k.startswith("rules:")}
    if not names or "mapping" not in outs:
        raise ConfirmError("本轮缺少计算规则或数据映射，无法按模板复算")
    revs = {n: [{"name": k.split(":")[2], "adjust": v[0]} for k, v in outs.items()
                if k.startswith(f"adjust:{n}:")] for n in names}
    inputs = [Path(p) for p in (data or rnd.get("inputs") or [])]
    missing = [str(p) for p in inputs if not p.is_file()]
    if missing or not inputs:
        raise ConfirmError("找不到确认时要复算用的数据文件：" + ("、".join(missing) or "无") + "，请在命令后面给出数据文件")
    original_names = {Path(p).name for p in rnd.get("inputs") or []}
    input_names = {p.name for p in inputs}
    if original_names and input_names != original_names:
        raise ConfirmError("复算用的数据文件必须与原轮文件名一致：" + "、".join(sorted(input_names ^ original_names)))
    try:
        hashes = fingerprint(inputs)
        ordered = order_schemes({n: RuleSet.load(out / f) for n, f in names.items()})
    except ValueError as exc:
        raise ConfirmError(f"模板准备失败：{exc}") from exc
    expected = rnd.get("files") or {}
    changed_inputs = [name for name, h in hashes.items() if expected.get(name) != h]
    changed_inputs += [item["name"] for item in rnd.get("source_inputs", [])
                       if not Path(item["path"]).is_file() or sha256(Path(item["path"])) != item["hash"]]
    if changed_inputs:
        raise ConfirmError("确认输入版本已变化或缺少原轮指纹，请重新核算：" + "、".join(sorted(set(changed_inputs))))
    manifest = {
        "period": rnd.get("period"),
        "mapping": outs["mapping"][0],
        "schemes": [{"name": n, "rules": names[n], "revisions": revs[n]} for n in ordered],
        "files": {name: h for name, h in outs.values()},
        "data": hashes,
    }
    selection = {"revisions": selected, "revisions_required": dict(sorted(required.items())),
                 "revisions_not_saved": sorted(set(available) - set(selected))}
    return manifest, inputs, selection


def _verify_template_replay(template, rnd: dict, inputs: list[Path]) -> list:
    chosen = {s["name"]: {"R0", *(r["name"] for r in s["revisions"])} for s in template.manifest["schemes"]}

    def only(values):
        return {s: {r: v for r, v in (rs or {}).items() if r in chosen.get(s, {"R0"})}
                for s, rs in (values or {}).items()}

    try:
        template.verify()
        results, warnings = template.run(inputs)
    except Exception as exc:
        raise ConfirmError(f"按模板复算失败：{exc}") from exc
    differences = []
    if digest(results) != only(rnd.get("digest")):
        differences.append("对象集合或应得/实发改变")
    frozen = rnd.get("financial_digest")
    if frozen is None:
        differences.append("缺少完整金额基线，旧案卷须重新核算")
    elif financial_digest(results) != only(frozen):
        differences.append("完整金额及报告对账与原轮不一致")
    if differences:
        raise ConfirmError(f"按模板复算与第 {rnd['n']} 轮结果不一致：{'、'.join(differences)}")
    return warnings


def check_template(out: Path, data: list[Path] | None = None, *, revisions: list[str] | None = None) -> dict:
    """复算最新完成轮次并核对原始指纹及完整金额基线。"""
    from collections import Counter

    from bonus_core.template import Template

    out = Path(out)
    if not out.is_dir():
        raise ConfirmError(f"案卷目录不存在：{out}")
    with case_lock(out):
        _, rnd, outs = _checked_round(out)
        manifest, inputs, selection = _template_plan(out, rnd, outs, data, revisions)
        warnings = _verify_template_replay(Template(out, manifest), rnd, inputs)
        sources = sorted((x["name"], x["hash"]) for x in rnd.get("source_inputs", []))
        encoded = json.dumps({"manifest": manifest, "source": sources}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return {"status": "PASS", "round": rnd["n"], "period": manifest["period"],
                "fingerprint": hashlib.sha256(encoded).hexdigest(),
                "schemes": [s["name"] for s in manifest["schemes"]], "revisions": selection["revisions"],
                "data_files": len(inputs), "source_files": len(sources),
                "warnings": dict(sorted(Counter(w.code for w in warnings).items()))}


def confirm(out: Path, by: str, note: str = "", data: list[Path] | None = None, review: bool = True, *,
            save_template: bool = True, revisions: list[str] | None = None) -> Path | None:
    """财务确认最新一轮。按需用选定修订复算并冻结模板，供场景发布后每期使用。

    review=False 表示口径单已在线下核对。save_template=False 表示只确认本轮结果。
    有可选人工调整修订时，必须明确指定 revisions。空列表表示只录入必选修订。
    """
    out = Path(out)
    if not out.is_dir():
        raise ConfirmError(f"案卷目录不存在：{out}")
    with case_lock(out):
        return _confirm(out, by, note, data, review, save_template, revisions)


def _confirm(out: Path, by: str, note: str, data: list[Path] | None, review: bool,
             save_template: bool, revisions: list[str] | None) -> Path | None:
    import yaml

    from bonus_core.template import MANIFEST, Template

    if not by.strip():
        raise ConfirmError("要写明确认人")
    case, rnd, outs = _checked_round(out)
    if case.get('workspace',{}).get('workflow',{}).get('fixed_commission') and save_template:
        raise ConfirmError('固定提成链路每期直接重新绑定材料；当前结果可单独确认，不录入旧版通用DSL模板')
    if review and "review" in outs:
        from bonus_agent import review as rv
        st = rv.status(out)
        if st["wrong"]:
            raise ConfirmError(f"口径单有 {len(st['wrong'])} 行标了有误（{'、'.join(r['id'] for r in st['wrong'][:10])}），"
                               "先让智能体改正（运行下一轮），改完重新确认这些行")
        if st["pending"]:
            raise ConfirmError(f"口径单还有 {len(st['pending'])} / {st['total']} 行没确认"
                               f"（{'、'.join(r['id'] for r in st['pending'][:10])}{'……' if len(st['pending']) > 10 else ''}），"
                               "逐条确认后才能确认结果")
    at = datetime.now().isoformat(timespec="seconds")
    record = {"by": by, "at": at, "round": rnd["n"], "note": note, "summary": rnd.get("summary", ""), "template": None}
    if not save_template:
        case.setdefault("confirmations", []).append(record)
        case["confirmed"] = record
        rnd["confirmed"] = record
        save_case(out, case)
        return None
    manifest, inputs, selection = _template_plan(out, rnd, outs, data, revisions)
    manifest["confirmed"] = {k: v for k, v in record.items() if k != "template"} | selection
    final = out / "template"
    tpl = out / "template.new"
    if tpl.exists():
        shutil.rmtree(tpl)
    tpl.mkdir(parents=True)
    files = list(manifest["files"])
    banner = (f"# 已确认冻结的计算模板：第 {rnd['n']} 轮，{by} 于 {at} 确认。\n"
              "# 请勿直接修改本目录里的任何文件。改动后按模板计算会被拒绝；需要改规则，请走一轮智能体处理并重新确认。\n")
    for f in files:
        if Path(f).suffix.lower() in (".yaml", ".yml"):
            (tpl / f).write_text(banner + (out / f).read_text(encoding="utf-8"), encoding="utf-8")
        else:
            shutil.copy2(out / f, tpl / f)
    (tpl / TEMPLATE_NOTICE).write_text(
        f"本目录是第 {rnd['n']} 轮结果确认后冻结的计算模板（确认人：{by}，确认时间：{at}）。\n\n"
        "请勿修改、替换或删除这里的任何文件：\n"
        "- 每次按模板计算前，程序会核对 manifest.yaml 里记的文件指纹，任何改动都会让计算被拒绝；\n"
        "- 需要修改规则、参数或人工调整，请在案卷里走一轮智能体处理，由财务重新确认后生成新模板；\n"
        "- 手工改文件并同步改指纹可以绕过检查，但这样算出的结果没有经过确认，不能作为正式结果。\n",
        encoding="utf-8")
    manifest["files"] = {f: sha256(tpl / f) for f in files}
    (tpl / MANIFEST).write_text(banner + yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    try:
        _verify_template_replay(Template.load(tpl, verify=False), rnd, inputs)
    except ConfirmError:
        shutil.rmtree(tpl)
        raise
    old = out / "template.old"
    if old.exists():
        shutil.rmtree(old)
    if final.exists():
        final.rename(old)
    try:
        tpl.rename(final)
    except OSError:
        if old.exists():
            old.rename(final)
        raise
    if old.exists():
        shutil.rmtree(old)
    record = {**manifest["confirmed"], "template": str(final)}
    case.setdefault("confirmations", []).append(record)
    case["confirmed"] = record
    rnd["confirmed"] = record
    save_case(out, case)
    return final
