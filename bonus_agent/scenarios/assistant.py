"""Bounded read-only conversation. It cannot create rules or start a calculation."""
import json

from bonus_agent.llm import parse_json
from bonus_agent.scenarios.releases import READ_TOOLS, definitions, load_release


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
        "你是固定奖金场景的只读解释助手。仅解释当前已发布版本和工具返回的事实。金额必须来自引擎结果；没有结果就说明尚未计算。"
        "不能修改规则、数据、层级、参数、排除记录、解除阻断或开始计算。修改请求只能说明需要提交财务审批新版本，不能声称已经修改。"
        f"当前场景={scenario}，版本={version}。最多查询 {max_steps} 次。"},
        {"role": "user", "content": message}]
    used_queries = 0
    for _ in range(max_steps):
        if should_stop():
            return "已停止只读查询；规则和计算结果未改变。"
        if getattr(llm, "tool_mode", "native") == "native":
            response = llm.chat_tools(messages, schemas, temperature=0)
            calls = response.get("tool_calls") or []
            if not calls:
                return response.get("content") or "没有可补充的说明。"
            messages.append({"role": "assistant", "content": response.get("content") or "", "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]})
        else:
            prompt = "仅输出 JSON：{\"tool\":工具名,\"args\":{}}，或 {\"answer\":解释文字}。可用工具：" + json.dumps(schemas, ensure_ascii=False)
            response = parse_json(llm.chat(messages + [{"role": "user", "content": prompt}], json_out=True, temperature=0))
            if "answer" in response:
                return str(response["answer"])
            calls = [{"name": response.get("tool"), "arguments": json.dumps(response.get("args") or {}), "id": "query"}]
        # Even a provider ignoring its tool schema cannot invoke a write handler.
        if len(calls) > min(3, max_steps - used_queries):
            return "查询数量超过限制，已停止；计算和规则未改变。"
        used_queries += len(calls)
        for c in calls:
            name = c.get("name")
            if name not in allowed:
                return f"当前场景禁止工具 {name}；规则和数据未改变。变更规则请提交财务审批新版本。"
            try:
                args = json.loads(c["arguments"])
                value = handlers[name](**args)
            except (ValueError, TypeError) as exc:
                return f"查询参数无效，已停止：{exc}"
            text = json.dumps(value, ensure_ascii=False, default=str)
            if len(text) > 20000:
                text = json.dumps({"error": "查询结果超过本次上下文限额；请按单个对象或具体规则缩小问题范围"}, ensure_ascii=False)
            if getattr(llm, "tool_mode", "native") == "native":
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": text})
            else:
                messages.append({"role": "user", "content": "只读工具结果：" + text})
    return "已达到本次只读查询上限；计算和规则未改变，请缩小问题范围。"
