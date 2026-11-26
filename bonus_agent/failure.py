"""把运行失败的技术信息翻译成财务看得懂的说明：先说原因，再说需要你做什么、想了解什么。

写法要求：每一项一句话，说具体的事；只给一个建议的操作（重试或发送），不罗列所有可能；不出现内部代号。
"""
from __future__ import annotations

import json
import re

TOOL_NAMES = {
    "read_document": "读文件", "read_sheet": "读表格", "view_image": "看图片", "record_facts": "登记参数",
    "remove_facts": "撤回参数登记", "define_dataset": "定义数据表", "remove_dataset": "删除数据表",
    "set_rules": "写计算规则", "trial_run": "试算", "explain": "查看计算过程", "evaluate": "验算公式",
    "ask_question": "提问", "answer_question": "记录答复", "note": "记笔记", "finish": "结束本轮", "reply": "回复",
    "record_clauses": "摘录条款", "cover_clauses": "标注条款位置", "verify": "独立验算",
}

RETRY = "直接点「重试」，会从停下的地方接着做"
SEND = "在下方写明怎么处理，再点「发送」"

DIAGNOSIS_FIELDS = {
    'item': '涉及项目或指标',
    'identified': '已识别内容',
    'missing': '缺少或待确认',
    'impact': '影响',
}


def vague(text: str) -> bool:
    parts = re.split(r'[。！？，、；.!?,;\s]+', text.strip())
    placeholders = {'无法判断', '不能判断', '无法确定', '不清楚', '看不出来', '材料不足', '资料不足',
                    '信息不足', '待确认', '需要确认', '请确认', '需要补充材料', '缺少材料', '未知', '无', '无法判断具体原因'}
    return all(re.sub(r'^(?:我|目前|暂时|当前|仍|尚)+', '', part) in placeholders for part in parts if part)


def question_diagnosis(value) -> dict[str, str] | None:
    if not isinstance(value, dict) or set(value) != set(DIAGNOSIS_FIELDS):
        return None
    if any(not isinstance(text, str) or vague(text) for text in value.values()):
        return None
    return {key: value[key].strip() for key in DIAGNOSIS_FIELDS}


def diagnosis_items(value):
    if isinstance(value, dict):
        return [(label, value[key]) for key, label in DIAGNOSIS_FIELDS.items() if value.get(key)]
    return []


def _recorded_feedback(error):
    start = error.find('{')
    if start < 0:
        return {}
    raw = error[start:]
    try:
        report, _ = json.JSONDecoder().raw_decode(raw)
        return report if isinstance(report, dict) and 'task' in report else {}
    except ValueError:
        # Older failure records kept a bounded prefix of the tool result.
        quoted = r'("(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*")'
        head = raw.split('"issues"', 1)[0]
        report = {}
        for key in ('task', 'scheme', 'target'):
            if match := re.search(r'"' + key + r'"\s*:\s*' + quoted, head):
                report[key] = json.loads(match[1])
        if first := re.search(r'"issues"\s*:\s*\[\s*' + quoted, raw):
            report['issues'] = [json.loads(first[1])]
        return report if 'task' in report else {}


def stop_diagnosis(error, workspace=None, focus=None):
    """Describe a recorded stop from its check and task. Never infer a missing business rule."""
    error = (error or '').strip()
    workspace = workspace or {}
    construction = workspace.get('workflow', {}).get('construction', {})
    tasks = construction.get('tasks', {})
    feedback = _recorded_feedback(error)
    stopped = [ident for ident, task in tasks.items()
               if task.get('target') and f'construction:{ident}:{task["target"]}' in error]
    task_id = feedback.get('task')
    if not isinstance(task_id, str) or not task_id:
        task_id = stopped[0] if len(stopped) == 1 else (focus or {}).get('id')
    spec = tasks.get(task_id, {})
    execution = feedback.get('execution')
    repair = execution.get('repair') if isinstance(execution, dict) else None
    node = repair.get('node') if isinstance(repair, dict) else None
    item = ' / '.join(dict.fromkeys(str(part) for part in (
        spec.get('scheme') or feedback.get('scheme') or (focus or {}).get('scheme'),
        spec.get('title') or feedback.get('target') or task_id,
        task_id if spec.get('title') else None,
        node if isinstance(node, str) else None,
    ) if part))
    budget = re.search(r'已报告实际用量 (\d+) tokens，剩余额度 (\d+)，本请求估算输入 (\d+) \+ 输出上限 (\d+)', error)
    if budget:
        used, remaining, request, output = map(int, budget.groups())
        return {'item': item or '核算作业的模型请求',
                'identified': f'累计已报告 {used:,} tokens，剩余 {remaining:,}。本请求预估输入 {request:,}，输出上限 {output:,}。',
                'missing': f'按当前请求预估，至少还需 {request + output - remaining:,} tokens 的可用额度。',
                'impact': '本请求在发送前被限额拦截，本轮停在该任务。'}
    issues = [s for s in feedback.get('issues', ()) if isinstance(s, str) and s] if isinstance(feedback.get('issues'), list) else []
    issue = issues[0] if issues else _where(error)
    if not item and not issue:
        return {}
    cited = [c for c in workspace.get('clauses', ()) if c.get('id') in spec.get('clauses', ())]
    known = f'已登记《{cited[0]["file"]}》中的「{cited[0]["quote"]}」。' if cited else ''
    stalled = re.search(r'连续 (\d+) 次检查没有消除既有问题', error)
    if task_id in stopped and stalled:
        return {'item': item, 'identified': known + f'连续 {stalled[1]} 次检查后，该任务仍待验证。',
                'missing': f'「{spec.get("title") or spec["target"]}」缺少当前数据版本的通过回执。',
                'impact': '调度器已停止重复执行，依赖该构件的下游计算尚未完成。'}
    scope = re.search(r'scope_bad:.*?scope「([^」]+)」', issue)
    inputs = feedback.get('input_failures')
    inputs = inputs if isinstance(inputs, list) else []
    gap = next((x['detail'] for x in inputs if isinstance(x, dict) and isinstance(x.get('detail'), str)), '')
    return {'item': item or '本轮核算校验',
            'identified': known + (issue or '有任务位置的记录，未收到该项校验的具体结果。'),
            'missing': gap or (f'需要为「{scope[1]}」建立并验证实际核算分组，或把公式绑定到已登记的对应对象。' if scope else
                        '当前校验尚缺通过回执。具体应补的映射、数据或规则以该检查指出的项目为准。' if issue else
                        '缺少能定位到公式或数据行的校验明细。'),
            'impact': '该项校验未通过，后续金额计算或复核尚未完成。' if issue else '本轮停在该任务，后续计算尚未完成。'}


def explain(error: str, *, workspace=None, diagnosis=None) -> dict:
    """返回 {title 原因, where 具体卡在哪, advice 建议的操作, ask 想了解的信息, detail 原始信息}。"""
    err = (error or "").strip()
    x = _explain(err)
    x.setdefault("where", _where(err))
    x.setdefault("ask", "")
    x["detail"] = err
    x['diagnosis'] = diagnosis or stop_diagnosis(err, workspace)
    return x


def _explain(err: str) -> dict:
    if 'recovery.json' in err and re.search(r'\[WinError (?:5|32|33)\]', err):
        return {"title": "Windows 未能写入本轮恢复记录，核算已停止。",
                "where": "案卷恢复记录写入被拒绝", "advice": "从最近一次成功保存的案卷续跑。"}
    if "案卷累计 tokens 用量" in err and "达到总上限" in err:
        return {"title": "案卷模型用量已达到上限。",
                "where": err, "advice": "本次已在发送新请求前停止。"}
    if err.startswith("纠错预算耗尽"):
        from bonus_agent.stuck_question import program_issue
        if program_issue(err):
            # 能问财务的卡点已在本轮改成具体问题（见 stuck_question），这里是程序或模型自身的问题：说清不需要回答
            return {"title": "智能体同一处多次出错，已停止自动接续并保存工作区。这是程序或模型自身的问题，不需要你回答或补充材料。",
                    "where": err.split('：', 1)[-1][:350],
                    "advice": "请把这条消息发给开发；修复更新后点「运行下一轮」会自动接着做（不必新建案卷）。"
                              "也可以在对话框里直接告诉智能体下一步该做什么，发送后它会换个做法接着做。"}
        return {"title": "同一问题多次纠错仍未解决，已停止自动接续并保存工作区。",
                "where": err.split('：', 1)[-1][:350],
                "advice": "请先回答「问题」里列出的待确认问题，再点「运行下一轮」；没有待确认问题时，"
                          "在对话框里写明这一步该怎么处理再点「发送」，回答后会从停下的地方接着做。"}
    if err.startswith("作业") and "上限" in err:
        return {"title": "本次作业达到总用量或时间上限，已停止并保存工作区。",
                "where": err, "advice": "请先检查未解决的校验问题，再决定是否继续。"}
    m = re.search(r"：(\w+) 连续 (\d+) 次失败，最后一次：(.*)", err, re.S)
    if m:
        tool, n, last = TOOL_NAMES.get(m.group(1), m.group(1)), m.group(2), m.group(3)
        if "参数不是合法 JSON" in last:      # 模型输出格式的问题，不是材料或规则的问题
            return {"title": f"智能体在「{tool}」这一步连续 {n} 次写出的内容格式不对（通常是一次写得太长），已自动停下。"
                             "这是模型输出格式的问题，不需要你回答。",
                    "advice": f"{RETRY}；下一轮会告诉它上次错在哪、要分小段写。连续几轮都卡在这里，请把这条消息发给开发。",
                    "where": ""}
        if "结束前自查" in last or "条款清单还有没落实" in last:     # 程序自己的核对没走完，不是要财务回答的问题
            return {"title": f"智能体在结束前的自查（规则逐条对照材料）连续 {n} 次没走完，已自动停下。"
                             "这是程序内部的核对，不需要你回答。",
                    "advice": f"{RETRY}；重试后还停在这里，请把这条消息发给开发。", "where": ""}
        if "验算式算不出来" in last or "验算式写错了" in last or "辅助变量" in last:   # 模型的验算式写法问题，不是金额对不上
            return {"title": f"智能体写的独立验算式连续 {n} 次算不出来（写法不对，还没比到金额），已自动停下。"
                             "这是模型写法的问题，不需要你回答。",
                    "advice": f"{RETRY}；下一轮会带上这次的原因。连续几轮都卡在这里，请把这条消息发给开发。", "where": ""}
        if "没有可计算的明细行" in last:
            return {"title": f"智能体在「{tool}」这一步连续失败 {n} 次，已自动停下。部分对象没有对应的明细行，"
                             "须先核对核算范围、关联、期间和筛选，尚不能据此判定材料缺少规则。",
                    "advice": f"{RETRY}；先核对已有材料和数据映射，保留计算所需历史数据。"
                              "只有原文仍未明确缺行处理时，再提出带具体对象例子的确认问题。", "ask": ""}
        from bonus_agent.stuck_question import _PROGRAM
        if _PROGRAM.search(last):          # 参数没绑到规则位置、报表声明没写这类，是智能体自己的核对没做完
            return {"title": f"智能体在「{tool}」这一步连续 {n} 次没通过程序检查，已自动停下。"
                             "卡住的是它自己的登记、绑定或写法，不是材料没写清，不需要你回答或补充材料。",
                    "advice": f"{RETRY}（下一轮会带上这次卡在哪）；连续几轮都卡在同一处，请把这条消息发给开发。"}
        why, ask = _why(last)
        if "对不上：引擎算出" in last or "验算式" in last:       # 可能是规则错了，也可能是验算式没写对
            return {"title": f"智能体在「独立验算」这一步连续 {n} 次没对上，已自动停下，{why}。",
                    "advice": f"先{RETRY}（下一轮会带上这次的原因）；同一个对象还对不上，再在下方写明它按材料该怎么算，点「发送」。",
                    "ask": ask}
        return {"title": f"智能体在「{tool}」这一步连续失败 {n} 次，已自动停下" + (f"，{why}" if why else "") + "。",
                "advice": f"{SEND}（直接重试多半还会卡在同一处）。",
                "ask": ask or "材料里这条规则具体怎么算？"}
    m = re.search(r"set_rules 连续 (\d+) 次保存后仍有同样的阻断问题", err)
    if m:
        return {"title": f"智能体连续 {m.group(1)} 次写计算规则，同一个问题一直没改掉，已自动停下，"
                         "多半是没理解材料要求的算法。",
                "advice": f"{SEND}。",
                "ask": "奖金是按什么层级算的（如每人独立算、先到项目再到个人）？卡住的这条规则是什么意思？"}
    m = re.search(r"「([^」]+)」反复建了又删", err)
    if m:
        return {"title": f"智能体对「{m.group(1)}」反复建了又删，拿不准这张表怎么定义，已自动停下。",
                "advice": f"{SEND}。", "ask": f"「{m.group(1)}」的每一行代表什么？用哪一列和其他表关联？"}
    if "反复重复相同的调用" in err or "原样调用" in err or "反复调用" in err or "反复提交相同" in err:
        # 原样重复同一个调用是模型自己没看懂程序的回复，不是材料没写清：不再笼统地问“材料里是怎么规定的”
        what = re.search(r"卡住了[^：]*：(.*?)。已用 \d+ 步", err, re.S)
        return {"title": "智能体在原地打转（反复提交同一个操作），已自动停下。这是模型自身的问题，不需要你回答或补充材料。",
                "where": (what.group(1) if what else err)[:300],
                "advice": f"{RETRY}（下一轮会带上这次卡在哪）；连续几轮都卡在同一处，请把这条消息发给开发。"}
    if err.startswith("已手动停止"):
        return {"title": f"你{err.removeprefix('已')}，已做的部分都保存了。".replace("（", "，停在").replace("）", ""),
                "advice": f"只是暂停的话，{RETRY}；如果是发现它方向不对，{SEND}。", "where": ""}
    m = re.search(r"tokens 用量 (\d+) 达到上限 (\d+)", err)
    if m:
        return {"title": f"本轮用量达到上限（{int(m.group(2)):,} tokens），已停止，通常是材料太多或反复返工。",
                "advice": f"{RETRY}；经常出现时请管理员调大上限或换更强的模型。", "where": ""}
    if err.startswith("本轮没有进展"):
        return {"title": "本轮步数用完了，而且整轮只在读取、没有任何登记或改动，已停下，没有自动接续。",
                "advice": f"{SEND}，指出它该先做哪一步（如先登记哪份材料的参数、先定义哪张表）。", "where": ""}
    if "达到最大步数" in err:
        return {"title": "本轮步数用完了，还没做完。", "advice": f"{RETRY}。", "where": ""}
    low = err.lower()
    if any(k in low for k in ("401", "403", "unauthorized", "invalid api key", "api key", "authentication")):
        return {"title": "模型服务拒绝访问（API Key 不对或没有权限）。",
                "advice": "请管理员检查 API Key 配置，改好后点「重试」。", "where": ""}
    if any(k in low for k in ("arrearage", "insufficient", "402", "欠费", "余额", "quota")):
        return {"title": "模型服务欠费或额度用完。", "advice": "请管理员充值或换一个模型，然后点「重试」。", "where": ""}
    if "max_model_len" in low or "max-model-len" in low or "maximum context length" in low or "上下文长度" in err:
        return {"title": "模型服务的上下文长度不够，放不下智能体每一步的输入。",
                "advice": "请管理员把模型服务的上下文长度调到至少 65536（建议 131072，vLLM 启动参数 --max-model-len），然后点「重试」。",
                "where": ""}
    if any(k in low for k in ("timeout", "timed out", "connection", "连接", "无法连接", "name resolution", "502", "503", "504")):
        return {"title": "连不上模型服务（网络或服务暂时不可用）。",
                "advice": "稍后点「重试」；一直连不上请管理员检查网络和服务地址。", "where": ""}
    if _is_bug(err):
        return {"title": "程序内部出错，本轮中断，已做的部分都保存了。", "where": err[:200],
                "advice": f"{RETRY}，通常能继续；同样的错误再出现，请把这条消息发给开发。"}
    return {"title": "本轮没有完成。", "advice": f"{RETRY}；需要补充说明时，{SEND}。"}


def _is_bug(err: str) -> bool:
    """Python 异常（不是智能体或模型服务的问题），如 'list' object has no attribute 'items'。"""
    return bool(re.search(r"object has no attribute|object is not|not subscriptable|unsupported operand|"
                          r"index out of range|KeyError|TypeError|AttributeError|NoneType", err))


def _why(last: str) -> tuple[str, str]:
    """(原因, 想了解的信息)"""
    if "参数不是合法 JSON" in last:
        return "原因是模型几次写出的内容格式不对（通常是一次写得太长）", ""
    if "需要条件（是/否）" in last:
        return "原因是把文字字段当成了是/否条件", "这个字段有哪些取值？哪个取值算“是”？"
    if "主键" in last and "重复" in last:
        return "原因是数据表的编号有重复", "重复的编号是录入错误，还是一个编号本来就对应多行（如一人参与多个项目）？"
    if "独立验算还没全部通过" in last or "对不上：引擎算出" in last:
        return ("原因是引擎算出的金额和按材料原文独立验算的结果对不上，或还没验算完",
                "下面这个对象的金额按材料应该怎么算？")
    if "程序检查还有没处理的" in last:
        return ("原因是规则里有数在材料里找不到出处，或写的取值和数据对不上",
                "下面列出的数或取值应该是多少、怎么处理？（例如“挂起的工单不计入”）")
    if "找不到" in last and "「" in last:
        return "原因是数据之间关联不上（编号在另一张表里找不到）", "缺的编号是漏录了，还是这类记录应该排除？"
    if "校验" in last and "未通过" in last:
        return "原因是数据没通过规则要求的校验", "这些数据有误吗？数据没错的话，是不是校验写得不对？"
    return "", ""


def _where(err: str) -> str:
    """从原始信息里摘出最具体的一句（第一条阻断问题、程序检查的第一项或最后一次的错误），去掉内部代号。"""
    for pat in (r"\[阻断\]\s*\w+:\s*([^\n]+)", r"程序检查还有没处理的[^\n]*\n-\s*(?:方案「[^」]*」：)?([^\n]+)",
                r"最后一次：(?:错误：)?([^\n]+)"):
        m = re.search(pat, err)
        if m:
            s = m.group(1)
            break
    else:
        s = ""
    s = re.sub(r"\s*\((?:variables|checks|pool|payout|offsets|allocation|tables)[^)]*\)\s*$", "", s.strip())
    s = re.sub(r"。已用 \d+ 步.*$", "", s)
    return s[:200] + ("……" if len(s) > 200 else "")


def retryable(error: str | None) -> bool:
    """Only an exhausted step allowance can continue; stagnation never restarts itself."""
    if not error or _is_bug(error):
        return False
    # Stagnation is a stop condition, not permission to restart the same repair loop.
    return error.startswith("达到最大步数")


def to_text(error: str, *, diagnosis=None) -> str:
    """对话框里的说明：原因 → 卡在哪 → 想了解什么 → 怎么继续。每项一行。"""
    x = explain(error, diagnosis=diagnosis)
    lines = [x["title"]]
    if x['diagnosis']:
        lines.extend(f'{label}\n{value}' for label, value in diagnosis_items(x['diagnosis']))
    elif x["where"]:
        lines.append(f"卡在：{x['where']}")
    if x["ask"]:
        lines.append(f"想请你确认：{x['ask']}")
    lines.append(f"怎么继续：{x['advice']}")
    return "\n".join(lines)
