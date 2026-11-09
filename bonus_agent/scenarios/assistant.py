"""Bounded read-only conversation. It cannot create rules or start a calculation."""
import json

from bonus_agent.llm import parse_json
from bonus_agent.scenarios.releases import READ_TOOLS, definitions, load_release


def render_evidence(name, value):
    """Only program-owned wording and verified tool data reach the user."""
    if name == "explain_object" and "object" in value:
        row = value["object"]
        lines = [f"引擎结果｜{row.get('node_id', row.get('id'))}"]
        labels = {"final": "应得", "held": "暂缓", "cap": "可发上限", "offset": "已预发",
                  "deducted": "本次抵扣", "paid": "本次实发", "overpaid": "超发待核对"}
        lines += [f"{label}：{row[key]} 元" for key, label in labels.items() if key in row]
        if value.get("traces"):
            lines.append("计算链（引擎记录）：")
            for trace in value["traces"]:
                lines.append(f"{trace['rule_item']}：{trace['rendered']} = {trace['value']}")
                if trace.get("rows"):
                    lines.append("数据来源：" + "、".join(trace["rows"]))
        return "\n".join(lines)
    if name == "describe_scenario":
        return "已发布规则（直接读取批准版本）：\n" + json.dumps(value, ensure_ascii=False, indent=2)
    if name == "list_issues" and isinstance(value, list):
        return ("引擎异常提示：\n" + json.dumps(value, ensure_ascii=False, indent=2)
                if value else "本次引擎结果没有异常提示。")
    if isinstance(value, dict) and "status" in value:
        return "尚未计算，无法提供本批金额。"
    return "未找到对应结果，请核对结果文件中的对象编号。"


def answer(llm, scenario, version, message, *, releases=None, result=None, max_steps=5, should_stop=lambda: False):
    directory, spec, publication = load_release(scenario, version, releases)
    rs, _ = definitions(directory, spec)
    allowed = READ_TOOLS & set(spec.allowed_tools)

    def describe_scenario():
        return {"scenario": scenario, "version": version, "periods": spec.periods,
                "publication": publication["fingerprint"], "approved_by": publication["approved_by"],
                "rules": rs.model_dump(mode="json")}

    def list_issues():
        return [i.to_dict() for i in result.issues] if result else {"status": "尚未计算"}

    def explain_object(node_id):
        if result is None:
            return {"status": "尚未计算"}
        row = result.rollups.get(node_id) or result.payouts.get(node_id) or result.nodes.get(node_id)
        if row is None:
            return {"error": "结果中没有此编号"}
        from dataclasses import asdict
        return {"object": asdict(row), "traces": [t.to_dict() for t in result.traces.values()
                                                    if t.id.startswith(node_id + "/")]}

    handlers = {"describe_scenario": describe_scenario, "explain_object": explain_object, "list_issues": list_issues}
    schemas = [{"type": "function", "function": {"name": name, "description": "只读查询已发布规则或引擎结果",
        "parameters": {"type": "object", "properties": ({"node_id": {"type": "string"}} if name == "explain_object" else {}),
                       "required": ["node_id"] if name == "explain_object" else [], "additionalProperties": False}}}
        for name in sorted(allowed)]
    messages = [{"role": "system", "content":
        "你是固定奖金场景的只读查询助手。根据问题选择工具查询已发布规则或对象结果；必须先查询，不自行计算或编写金额。"
        "用户只会看到程序从工具结果生成的规则说明、金额卡片和计算链，你的自由文本不会展示。查询完成后停止调用。"
        "不能修改规则、数据、层级、参数、排除记录、解除阻断或开始计算。修改请求只能说明需要提交财务审批新版本，不能声称已经修改。"
        f"当前场景={scenario}，版本={version}。最多查询 {max_steps} 次。"},
        {"role": "user", "content": message}]
    used_queries = 0
    evidence = {}

    def finish(note=""):
        cards = list(evidence.values())
        if not cards:
            cards.append("尚未计算，无法提供本批金额。" if result is None else
                         "未取得可核验的查询结果。请提供结果文件中的对象编号，或查询已发布规则。")
        return "\n\n".join(cards + ([note] if note else []))

    for _ in range(max_steps):
        if should_stop():
            return "已停止只读查询；规则和计算结果未改变。"
        if getattr(llm, "tool_mode", "native") == "native":
            response = llm.chat_tools(messages, schemas, temperature=0)
            calls = response.get("tool_calls") or []
            if not calls:
                return finish()
            messages.append({"role": "assistant", "content": response.get("content") or "", "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]})
        else:
            prompt = "仅输出 JSON：{\"tool\":工具名,\"args\":{}}，或 {\"answer\":解释文字}。可用工具：" + json.dumps(schemas, ensure_ascii=False)
            response = parse_json(llm.chat(messages + [{"role": "user", "content": prompt}], json_out=True, temperature=0))
            if "answer" in response:
                return finish()
            calls = [{"name": response.get("tool"), "arguments": json.dumps(response.get("args") or {}), "id": "query"}]
        # Even a provider ignoring its tool schema cannot invoke a write handler.
        if len(calls) > min(3, max_steps - used_queries):
            return "查询数量超过限制，已停止；计算和规则未改变。"
        used_queries += len(calls)
        for c in calls:
            name = c.get("name")
            if name not in allowed:
                # The tool name is also model-authored text: never echo an
                # arbitrary name containing invented financial statements.
                label = "工具 set_rules" if name == "set_rules" else "未授权工具"
                return f"当前场景禁止{label}；规则和数据未改变。变更规则请提交财务审批新版本。"
            try:
                args = json.loads(c["arguments"])
                value = handlers[name](**args)
            except (ValueError, TypeError):
                return "查询参数无效，已停止；请核对结果文件中的对象编号。"
            text = json.dumps(value, ensure_ascii=False, default=str)
            if len(text) > 20000:
                text = json.dumps({"error": "查询结果超过本次上下文限额；请按单个对象或具体规则缩小问题范围"}, ensure_ascii=False)
                evidence[(name, json.dumps(args, sort_keys=True))] = "查询结果超过展示限额，请缩小问题范围。"
            else:
                evidence[(name, json.dumps(args, sort_keys=True))] = render_evidence(name, value)
            if getattr(llm, "tool_mode", "native") == "native":
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": text})
            else:
                messages.append({"role": "user", "content": "只读工具结果：" + text})
    return finish("已达到本次只读查询上限；计算和规则未改变，请缩小问题范围。")
