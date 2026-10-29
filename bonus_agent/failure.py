"""把运行失败的技术信息翻译成财务看得懂的说明：卡在哪一步、为什么停、接下来该做什么。"""
from __future__ import annotations

import re

TOOL_NAMES = {
    "read_document": "读文件", "read_sheet": "读表格", "view_image": "看图片", "record_facts": "登记参数",
    "remove_facts": "撤回参数登记", "define_dataset": "定义数据表", "remove_dataset": "删除数据表",
    "set_rules": "写计算规则", "trial_run": "试算", "explain": "查看计算过程", "evaluate": "验算公式",
    "ask_question": "提问", "answer_question": "记录答复", "note": "记笔记", "finish": "结束本轮", "reply": "回复",
}

RETRY = "点「重试」会从上一轮已完成的地方接着做"
TELL = "也可以在对话里告诉智能体怎么处理"


def explain(error: str) -> dict:
    """返回 {title, advice, detail}；detail 保留原始信息给开发排查。"""
    err = (error or "").strip()
    m = re.search(r"：(\w+) 连续 (\d+) 次失败，最后一次：(.*)", err, re.S)
    if m:
        tool, n, last = TOOL_NAMES.get(m.group(1), m.group(1)), m.group(2), m.group(3)
        why = _why(last)
        return {"title": f"智能体在「{tool}」这一步连续失败 {n} 次，已自动停下，避免继续消耗 tokens。",
                "advice": (f"{why}{RETRY}；如果重试还卡在同一处，{TELL}（例如“没有回款的销售单按 0 处理”），"
                           "或把下面的详细信息发给开发。"),
                "detail": err}
    if "反复重复相同的调用" in err or "原样调用" in err:
        return {"title": "智能体在原地打转（反复做同一件事），已自动停下。",
                "advice": f"{RETRY}；{TELL}，比如指出它下一步该做什么。", "detail": err}
    m = re.search(r"tokens 用量 (\d+) 达到上限 (\d+)", err)
    if m:
        return {"title": f"本轮用量达到上限（{int(m.group(2)):,} tokens），已停止。",
                "advice": f"{RETRY}。经常出现时，说明材料太多或模型反复返工：可以调大上限，或换更强的模型。", "detail": err}
    if "达到最大步数" in err:
        return {"title": "本轮步数用完了还没做完。", "advice": f"{RETRY}。", "detail": err}
    low = err.lower()
    if any(k in low for k in ("401", "403", "unauthorized", "invalid api key", "api key", "authentication")):
        return {"title": "模型服务拒绝访问（API Key 不对或没有权限）。",
                "advice": "请管理员检查 API Key 配置（环境变量或 config/secrets.yaml），改好后点「重试」。", "detail": err}
    if any(k in low for k in ("arrearage", "insufficient", "402", "欠费", "余额", "quota")):
        return {"title": "模型服务欠费或额度用完。", "advice": "请管理员充值或换一个模型，然后点「重试」。", "detail": err}
    if any(k in low for k in ("timeout", "timed out", "connection", "连接", "无法连接", "name resolution", "502", "503", "504")):
        return {"title": "连不上模型服务（网络或服务暂时不可用）。",
                "advice": "稍后点「重试」；一直连不上请管理员检查网络和服务地址（本地部署的看 vLLM 是否在运行）。", "detail": err}
    return {"title": "本轮没有完成。", "advice": f"{RETRY}；{TELL}；还不行就把下面的详细信息发给开发。", "detail": err}


def _why(last: str) -> str:
    if "参数不是合法 JSON" in last:
        return "原因是模型几次写出的内容格式不对（通常是一次写得太长）。"
    if "没有可计算的明细行" in last:
        return "原因是有的对象没有对应的明细行（比如销售单还没有回款），规则没说明这种情况怎么算。"
    if "需要条件（是/否）" in last:
        return "原因是把文字字段当成了是/否条件。"
    if "主键" in last and "重复" in last:
        return "原因是数据表的编号有重复，请先核对数据。"
    if "找不到" in last and "「" in last:
        return "原因是数据之间关联不上（编号在另一张表里找不到），请先核对数据。"
    if "校验" in last and "未通过" in last:
        return "原因是数据没有通过规则要求的校验，请先核对数据是否有误。"
    return ""


def to_text(error: str) -> str:
    x = explain(error)
    return f"{x['title']}\n{x['advice']}"
