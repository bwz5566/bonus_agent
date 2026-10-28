"""字段映射推荐：先用启发式，置信度不足的字段再问大模型，结果都要财务确认（H2）。"""
from __future__ import annotations

import json

from bonus_agent.extract_rules import prompt
from bonus_agent.llm import LLM, LLMError
from bonus_core.data.mapping import DatasetMapping, Mapping, suggest_heuristic
from bonus_core.data.table_detect import SheetTable
from bonus_core.dsl.models import RuleSet

THRESHOLD = 0.8


def suggest(rs: RuleSet, sheets: list[SheetTable], llm: LLM | None = None, history: Mapping | None = None) -> Mapping:
    base = suggest_heuristic(rs, sheets, history)
    if llm is None:
        return base
    for ds_name, m in base.items():
        ds = rs.datasets[ds_name]
        weak = [f for f in ds.fields if m.confidence.get(f, 0) < THRESHOLD]
        st = next((s for s in sheets if s.sheet == m.sheet), None)
        if not weak or st is None:
            continue
        fields = "\n".join(f"- {f}: {ds.fields[f].type}，{ds.fields[f].desc}" + (f"，单位 {ds.fields[f].unit}" if ds.fields[f].unit else "")
                           for f in weak)
        cols = json.dumps(st.sample(5), ensure_ascii=False, default=str)
        msg = prompt("mapping").replace("{fields}", fields).replace("{sheet}", st.sheet).replace("{columns}", cols)
        msg = msg.replace("{{", "{").replace("}}", "}")
        try:
            out = llm.chat_json([{"role": "user", "content": msg}], temperature=0)
        except LLMError:
            continue
        for f, v in (out.get("mapping") or {}).items():
            if f in weak and isinstance(v, dict) and v.get("column") in st.headers:
                conf = float(v.get("confidence") or 0)
                if conf > m.confidence.get(f, 0):
                    m.columns[f], m.confidence[f] = v["column"], round(conf, 2)
        base[ds_name] = DatasetMapping(sheet=m.sheet, columns=m.columns, confidence=m.confidence)
    return base
