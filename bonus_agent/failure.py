"""把运行失败的技术信息翻译成财务看得懂的说明：先说原因，再说需要你做什么、想了解什么。

写法要求：每一项一句话，说具体的事；只给一个建议的操作（重试或发送），不罗列所有可能；不出现内部代号。
"""
from __future__ import annotations

import re

TOOL_NAMES = {
    "read_document": "读文件", "read_sheet": "读表格", "view_image": "看图片", "record_facts": "登记参数",
    "remove_facts": "撤回参数登记", "define_dataset": "定义数据表", "remove_dataset": "删除数据表",
    "set_rules": "写计算规则", "trial_run": "试算", "explain": "查看计算过程", "evaluate": "验算公式",
    "ask_question": "提问", "answer_question": "记录答复", "note": "记笔记", "finish": "结束本轮", "reply": "回复",
    "record_clauses": "摘录条款", "cover_clauses": "标注条款位置",
}

RETRY = "直接点「重试」，会从停下的地方接着做"
SEND = "在下方写明怎么处理，再点「发送」"


def explain(error: str) -> dict:
    """返回 {title 原因, where 具体卡在哪, advice 建议的操作, ask 想了解的信息, detail 原始信息}。"""
    err = (error or "").strip()
    x = _explain(err)
    x.setdefault("where", _where(err))
    x.setdefault("ask", "")
    x["detail"] = err
    return x


def _explain(err: str) -> dict:
    m = re.search(r"：(\w+) 连续 (\d+) 次失败，最后一次：(.*)", err, re.S)
    if m:
        tool, n, last = TOOL_NAMES.get(m.group(1), m.group(1)), m.group(2), m.group(3)
        why, ask = _why(last)
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
        return {"title": "智能体在原地打转（反复做同一件事），已自动停下。",
                "advice": f"{SEND}，指出它下一步该做什么。", "ask": "它卡住的这一步，材料里是怎么规定的？"}
    if err.startswith("已手动停止"):
        return {"title": f"你{err.removeprefix('已')}，已做的部分都保存了。".replace("（", "，停在").replace("）", ""),
                "advice": f"只是暂停的话，{RETRY}；如果是发现它方向不对，{SEND}。", "where": ""}
    m = re.search(r"tokens 用量 (\d+) 达到上限 (\d+)", err)
    if m:
        return {"title": f"本轮用量达到上限（{int(m.group(2)):,} tokens），已停止，通常是材料太多或反复返工。",
                "advice": f"{RETRY}；经常出现时请管理员调大上限或换更强的模型。", "where": ""}
    if "达到最大步数" in err:
        return {"title": "本轮步数用完了，还没做完。", "advice": f"{RETRY}。", "where": ""}
    low = err.lower()
    if any(k in low for k in ("401", "403", "unauthorized", "invalid api key", "api key", "authentication")):
        return {"title": "模型服务拒绝访问（API Key 不对或没有权限）。",
                "advice": "请管理员检查 API Key 配置，改好后点「重试」。", "where": ""}
    if any(k in low for k in ("arrearage", "insufficient", "402", "欠费", "余额", "quota")):
        return {"title": "模型服务欠费或额度用完。", "advice": "请管理员充值或换一个模型，然后点「重试」。", "where": ""}
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
    if "没有可计算的明细行" in last:
        return ("原因是有的对象没有对应的明细行（比如销售单还没有回款），规则没说这种情况怎么算",
                "没有明细行的对象按 0 处理，还是应该报错拦下？")
    if "需要条件（是/否）" in last:
        return "原因是把文字字段当成了是/否条件", "这个字段有哪些取值？哪个取值算“是”？"
    if "主键" in last and "重复" in last:
        return "原因是数据表的编号有重复", "重复的编号是录入错误，还是一个编号本来就对应多行（如一人参与多个项目）？"
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


def to_text(error: str) -> str:
    """对话框里的说明：原因 → 卡在哪 → 想了解什么 → 怎么继续。每项一行。"""
    x = explain(error)
    lines = [x["title"]]
    if x["where"]:
        lines.append(f"卡在：{x['where']}")
    if x["ask"]:
        lines.append(f"想请你确认：{x['ask']}")
    lines.append(f"怎么继续：{x['advice']}")
    return "\n".join(lines)
