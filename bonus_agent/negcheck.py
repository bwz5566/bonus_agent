"""坏数据回归：把智能体做好的规则（不调用大模型）在一批改坏的数据上重算，看每种坏数据能不能被拦住。

坏数据用例的格式（negative_cases.json）：
  [{"case_id": "N001", "name": "销售主键重复",
    "patch": {"target": "data", "op": "append_copy" | "set", "table": "sales", "index": 0, "field": "...", "value": ...}}]
table 是数据表名；Excel 里的工作表名不一样时用 sheets 对照（如 {"sales": "销售明细"}）。
target 不是 data 的用例（如政策未确认）不在这里测，由智能体的阻断问题处理。
"""
from __future__ import annotations

import copy
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import openpyxl


@dataclass
class NegResult:
    case_id: str
    name: str
    blocked: bool
    detail: str


def patch_workbook(src: Path, dst: Path, patch: dict, sheets: dict[str, str] | None = None,
                   header_rows: list[int] | None = None, columns: dict[str, str] | None = None) -> None:
    """在复制的工作簿上改一处。行号按表头位置换算（表头不一定在第 1 行）：header_rows 是案卷里保存的表头行，
    没有时按程序识别的表头；index 是第几条数据行（从 0 开始，不含表头、空行和合计行）。
    field 可以写 Excel 列名，也可以写标准字段名（columns：标准字段 -> 列名）。"""
    from bonus_core.data.table_detect import detect_sheet, table_at

    sheet = (sheets or {}).get(patch["table"], patch["table"])
    st = table_at(src, sheet, header_rows) if header_rows else detect_sheet(openpyxl.load_workbook(src, data_only=True)[sheet], src.name)
    if st is None or not st.rows:
        raise ValueError(f"工作表「{sheet}」里没有识别出数据行")
    if not 0 <= patch["index"] < len(st.rows):
        raise ValueError(f"「{sheet}」只有 {len(st.rows)} 条数据行，没有第 {patch['index']} 条")
    r = st.rows[patch["index"]][0]
    # 按值读入再保存：公式单元格换成它的计算值。直接带公式保存会丢掉计算值，程序读回来是空，用例会因为别的原因被拦下
    wb = openpyxl.load_workbook(src, data_only=True)
    ws = wb[sheet]
    if patch["op"] == "append_copy":         # 复制一条数据行，插在最后一条数据行后面（合计行之前）
        last = st.rows[-1][0]
        vals = [c.value for c in ws[r]]
        ws.insert_rows(last + 1)
        for i, v in enumerate(vals, start=1):
            ws.cell(last + 1, i).value = v
    elif patch["op"] == "set":
        name = patch["field"]
        col = name if name in st.headers else (columns or {}).get(name)
        if col not in st.headers:
            raise ValueError(f"「{sheet}」的表头（第 {st.header_row} 行）里没有列「{name}」：{[h for h in st.headers if h]}")
        v = patch["value"]
        if isinstance(v, str):
            try:
                v = float(v)
            except ValueError:
                pass
        ws.cell(r, st.headers.index(col) + 1).value = v
    else:
        raise ValueError(f"不支持的 op：{patch['op']}")
    wb.save(dst)


def _layout(snapshot: dict, sheet: str) -> tuple[list[int] | None, dict[str, str] | None]:
    """案卷里这张工作表保存的表头行和列对照（标准字段 -> 列名）。"""
    for m in (snapshot.get("mapping") or {}).values():
        if isinstance(m, dict) and m.get("sheet") == sheet:
            return m.get("header_rows"), m.get("columns")
    return None, None


def _target(files: list[Path], patch: dict, sheets: dict[str, str] | None) -> Path:
    sheet = (sheets or {}).get(patch["table"], patch["table"])
    for f in files:
        if f.suffix.lower() in (".xlsx", ".xlsm"):
            if sheet in openpyxl.load_workbook(f, read_only=True).sheetnames:
                return f
    raise ValueError(f"材料里没有工作表「{sheet}」")


def replay(snapshot: dict, files: list[Path], cases: list[dict], sheets: dict[str, str] | None = None) -> list[NegResult]:
    """snapshot 是案卷 case.json 里的 workspace。每个用例都在一份复制的材料上改一处、重算全部方案。"""
    from bonus_agent import case as cs
    from bonus_agent.agent import ToolError, Workspace
    from bonus_agent.auto import build_inventory
    from bonus_agent.llm import FakeLLM

    if not any(s != "共用" and d.get("pool") for s, d in (snapshot.get("drafts") or {}).items()):
        raise ValueError("这个案卷还没有写好的方案（智能体没跑完或失败了），没法做坏数据回归")
    base = Workspace(FakeLLM([]), build_inventory(files))      # 先在原始数据上算一遍：本身就算不出来，后面的“拦住”都不作数
    cs.restore(base, copy.deepcopy(snapshot))
    try:
        for s in base.schemes:
            base._trial(s)
    except ToolError as e:
        raise ValueError("这个案卷的规则在原始数据上就算不出结果，没法做坏数据回归：" + " ".join(str(e).split())[:200]) from None
    out = []
    for c in cases:
        p = c.get("patch") or {}
        if p.get("target") != "data":
            continue
        with tempfile.TemporaryDirectory() as tmp:
            src = _target(files, p, sheets)
            copies = []
            for f in files:
                dst = Path(tmp) / f.name
                if f == src:
                    patch_workbook(f, dst, p, sheets, *_layout(snapshot, (sheets or {}).get(p["table"], p["table"])))
                else:
                    shutil.copy(f, dst)
                copies.append(dst)
            ws = Workspace(FakeLLM([]), build_inventory(copies))
            cs.restore(ws, copy.deepcopy(snapshot))
            detail = ""
            try:
                for s in ws.schemes:
                    ws._trial(s)
            except ToolError as e:
                detail = " ".join(str(e).split())[:300]
            out.append(NegResult(c.get("case_id", ""), c.get("name", ""), bool(detail), detail or "没有拦住，照常算出了结果"))
    return out


def report(results: list[NegResult]) -> list[str]:
    n = sum(r.blocked for r in results)
    lines = [f"坏数据拦截 {n}/{len(results)}"]
    lines += [f"{'✓' if r.blocked else '✗'} {r.case_id} {r.name}：{r.detail}" for r in results]
    return lines
