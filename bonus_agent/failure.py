"""把运行失败的技术信息翻译成财务看得懂的说明：先说原因，再说需要什么帮助、需要了解什么信息。"""
from __future__ import annotations

import re

TOOL_NAMES = {
    "read_document": "读文件", "read_sheet": "读表格", "view_image": "看图片", "record_facts": "登记参数",
    "remove_facts": "撤回参数登记", "define_dataset": "定义数据表", "remove_dataset": "删除数据表",
    "set_rules": "写计算规则", "trial_run": "试算", "explain": "查看计算过程", "evaluate": "验算公式",
    "ask_question": "提问", "answer_question": "记录答复", "note": "记笔记", "finish": "结束本轮", "reply": "回复",
}

RETRY = "点「重试」：不补充信息，从上一轮做到的地方接着做"
TELL = "在对话框写明怎么处理后点「发送」：智能体带着你的说明接着做这一轮"


def explain(error: str) -> dict:
    """返回 {title 原因, where 具体卡在哪, advice 需要的帮助, ask 想了解的信息, detail 原始信息}。"""
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
        return {"title": f"智能体在「{tool}」这一步连续失败 {n} 次，已自动停下，避免继续消耗 tokens。" + (f"{why}" if why else ""),
                "advice": f"不建议直接重试（多半还会卡在同一处）。{TELL}，例如“没有回款的销售单按 0 处理”；缺材料就附上文件再发送。",
                "ask": ask or "材料里这条规则具体怎么算？上面卡住的地方应该怎么处理？"}
    m = re.search(r"set_rules 连续 (\d+) 次保存后仍有同样的阻断问题", err)
    if m:
        return {"title": f"智能体连续 {m.group(1)} 次写计算规则，同一个问题一直没改掉，已自动停下。"
                         "多半是材料要求的算法和它选的写法对不上。",
                "advice": f"{TELL}，说明这部分怎么算；还不行就把详细信息发给开发。",
                "ask": "奖金分配到哪一层（如项目 → 参与记录）？哪些数按人或按项目汇总？卡住的这条规则在材料里是什么意思？"}
    m = re.search(r"「([^」]+)」反复建了又删", err)
    if m:
        return {"title": f"智能体对「{m.group(1)}」反复建了又删，没有进展，已自动停下。多半是拿不准这张表或这个参数该怎么定义。",
                "advice": f"{TELL}。",
                "ask": f"「{m.group(1)}」的每一行代表什么？用哪一列和其他表关联？"}
    if "反复重复相同的调用" in err or "原样调用" in err or "反复调用" in err or "反复提交相同" in err:
        return {"title": "智能体在原地打转（反复做同一件事），已自动停下。",
                "advice": f"{TELL}，指出它下一步该做什么；{RETRY}也可以，但可能还会打转。", "ask": "它卡住的这一步，材料里是怎么规定的？"}
    if err.startswith("已手动停止"):
        return {"title": f"本轮{err}。已做的部分保存在过程记录里。",
                "advice": f"只是想继续：{RETRY}。发现它方向不对才停的：{TELL}，写清正确做法。",
                "ask": "停下的原因是什么？哪一步做得不对？"}
    m = re.search(r"tokens 用量 (\d+) 达到上限 (\d+)", err)
    if m:
        return {"title": f"本轮用量达到上限（{int(m.group(2)):,} tokens），已停止。通常是材料太多，或模型反复返工。",
                "advice": f"{RETRY}。经常出现时，可以调大上限或换更强的模型。"}
    if "达到最大步数" in err:
        return {"title": "本轮步数用完了还没做完。", "advice": f"{RETRY}。"}
    low = err.lower()
    if any(k in low for k in ("401", "403", "unauthorized", "invalid api key", "api key", "authentication")):
        return {"title": "模型服务拒绝访问（API Key 不对或没有权限）。",
                "advice": "请管理员检查 API Key 配置（环境变量或 config/secrets.yaml），改好后点「重试」。"}
    if any(k in low for k in ("arrearage", "insufficient", "402", "欠费", "余额", "quota")):
        return {"title": "模型服务欠费或额度用完。", "advice": "请管理员充值或换一个模型，然后点「重试」。"}
    if any(k in low for k in ("timeout", "timed out", "connection", "连接", "无法连接", "name resolution", "502", "503", "504")):
        return {"title": "连不上模型服务（网络或服务暂时不可用）。",
                "advice": "稍后点「重试」；一直连不上请管理员检查网络和服务地址（本地部署的看 vLLM 是否在运行）。"}
    return {"title": "本轮没有完成。", "advice": f"没有要补充的：{RETRY}；需要补充或纠正：{TELL}；还不行就把详细信息发给开发。"}


def _why(last: str) -> tuple[str, str]:
    """(原因, 想了解的信息)"""
    if "程序检查还有没处理的" in last:
        return ("原因是程序检查发现规则里有数在材料里找不到出处，或者规则里写的取值和数据对不上，模型没能自己处理。",
                "上面列出的数或取值应该是多少、怎么处理？（例如“挂起的工单不计入”）")
    if "参数不是合法 JSON" in last:
        return "原因是模型几次写出的内容格式不对（通常是一次写得太长）。", ""
    if "没有可计算的明细行" in last:
        return ("原因是有的对象没有对应的明细行（比如销售单还没有回款），规则没说明这种情况怎么算。",
                "没有明细行的对象应该按 0 处理，还是应该报错拦下？")
    if "需要条件（是/否）" in last:
        return "原因是把文字字段当成了是/否条件。", "这个字段有哪些取值？哪个取值算“是”？"
    if "主键" in last and "重复" in last:
        return "原因是数据表的编号有重复。", "重复的编号是数据录入错误，还是一个编号本来就对应多行（如一人参与多个项目）？"
    if "找不到" in last and "「" in last:
        return "原因是数据之间关联不上（编号在另一张表里找不到）。", "缺的编号是漏录了，还是这类记录应该排除？"
    if "校验" in last and "未通过" in last:
        return "原因是数据没有通过规则要求的校验。", "这些数据是否有误？如果数据没错，是不是校验写得不对（比如应该按项目判断，而不是逐条判断）？"
    return "", ""


def _where(err: str) -> str:
    """从原始信息里摘出最具体的一句（第一条阻断问题或最后一次的错误），去掉内部代号。"""
    m = re.search(r"\[阻断\]\s*\w+:\s*([^\n]+)", err)
    s = m.group(1) if m else ""
    if not s:
        m = re.search(r"最后一次：(?:错误：)?([^\n]+)", err)
        s = m.group(1) if m else ""
    s = re.sub(r"\s*\((?:variables|checks|pool|payout|offsets|allocation|tables)[^)]*\)\s*$", "", s.strip())
    return s[:240] + ("……" if len(s) > 240 else "")


def to_text(error: str) -> str:
    """对话框里的说明：先说原因，再说需要什么帮助、需要了解什么信息。"""
    x = explain(error)
    lines = [f"原因：{x['title']}"]
    if x["where"]:
        lines.append(f"具体卡在：{x['where']}")
    lines.append(f"需要你帮忙：{x['advice']}")
    if x["ask"]:
        lines.append(f"想了解：{x['ask']}")
    return "\n".join(lines)
