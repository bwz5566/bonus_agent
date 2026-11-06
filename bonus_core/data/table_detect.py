"""识别格式不固定的 Excel：找表头行、展开合并单元格、剔除合计行。"""
from __future__ import annotations

import re

from dataclasses import dataclass, field
from pathlib import Path

import openpyxl

TOTAL_WORDS = ("合计", "小计", "总计", "总额")
SCAN_ROWS = 20


@dataclass
class SheetTable:
    file: str
    sheet: str
    header_row: int                         # Excel 行号
    headers: list[str]
    rows: list[tuple[int, list]] = field(default_factory=list)    # (Excel 行号, 值)
    totals: list[tuple[int, list]] = field(default_factory=list)  # 合计行，用于对账
    confidence: float = 1.0
    repeats: list[int] = field(default_factory=list)              # 数据中间重复出现的表头行（Excel 行号），不计入数据
    # 数据中间出现的另一段表头（几段数据拼在一起、列名有出入）：(Excel 行号, {列下标: (原列名, 这一段的列名)})。
    # 这一行不计入数据；数据仍按列的位置读（同一列），列名有出入的列由使用方提示核对两段含义是否一致
    segments: list[tuple[int, dict]] = field(default_factory=list)
    # 另一段只是把几列换了位置（列名都能在第一段表头里找到）：这一段的数据已按列名挪回第一段的位置。(Excel 行号, {原位置: 挪到的位置})
    remaps: list[tuple[int, dict]] = field(default_factory=list)
    # 另一段的列名对不上、又没法按列名挪回（几列都叫同一个名字等）：(Excel 行号, [涉及的列下标])，用到这些列时要先问清楚
    unsure: list[tuple[int, list]] = field(default_factory=list)

    def column(self, header: str) -> list:
        i = self.headers.index(header)
        return [vals[i] for _, vals in self.rows]

    def sample(self, n: int = 5) -> dict[str, list]:
        return {h: [vals[i] for _, vals in self.rows[:n]] for i, h in enumerate(self.headers) if h}


def _grid(ws) -> list[list]:
    """读出整个 sheet，并把合并单元格的值填到每个格子里。"""
    grid = [list(r) for r in ws.iter_rows(values_only=True)]
    for rng in ws.merged_cells.ranges:
        v = ws.cell(rng.min_row, rng.min_col).value
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                if r - 1 < len(grid) and c - 1 < len(grid[r - 1]):
                    grid[r - 1][c - 1] = v
    return grid


def _is_text(v) -> bool:
    if not isinstance(v, str):
        return False
    s = v.strip().replace(",", "").rstrip("%")
    try:
        float(s)
        return False
    except ValueError:
        return bool(s)


def _filled(row) -> int:
    return sum(1 for v in row if v not in (None, ""))


def _is_total(row) -> bool:
    return any(isinstance(v, str) and any(w in v for w in TOTAL_WORDS) for v in row[:3])


def detect_sheet(ws, file: str) -> SheetTable | None:
    grid = _grid(ws)
    if not grid:
        return None
    best, best_score = None, 0.0
    for i, row in enumerate(grid[:SCAN_ROWS]):
        n = _filled(row)
        # 合并单元格展开的标题行（如跨整表的表名、说明）所有格子是同一个值，不是表头
        if n < 2 or len({str(v).strip() for v in row if v not in (None, "")}) < 2:
            continue
        text = sum(1 for v in row if _is_text(v)) / n
        nxt = next((r for r in grid[i + 1:i + 4] if _filled(r) >= 2), None)
        data_like = 0.0
        if nxt:
            data_like = sum(1 for v in nxt if v not in (None, "") and not _is_text(v)) / max(_filled(nxt), 1)
        score = text * n * (0.5 + data_like)
        if score > best_score:
            best, best_score = i, score
    if best is None:
        return None
    rows = [best]
    # 两行表头：上一行是分组标题（如「金额（万元）」跨两列），下一行是具体列名
    # 只有上一行像分组标题（有合并展开后的重复值，或下一行有值处上一行为空）才算两行表头，
    # 避免把全是文字的数据行（如 部门编号 | 部门名称）当成表头
    if best + 1 < len(grid):
        head, nxt = grid[best], grid[best + 1]
        vals = [v for v in head if v not in (None, "")]
        grouped = len(set(vals)) < len(vals) or any(
            (head[i] if i < len(head) else None) in (None, "") and v not in (None, "") for i, v in enumerate(nxt))
        if grouped and _filled(nxt) >= 2 and all(_is_text(v) or v in (None, "") for v in nxt):
            rows.append(best + 1)
    t = _build(grid, file, ws.title, rows)
    t.confidence = min(1.0, best_score / max(len(t.headers), 1))
    return t


def _build(grid, file: str, title: str, header_idx: list[int]) -> SheetTable:
    """按指定的表头行（0 起的下标，可多行）组装表格；多行表头用“上级_下级”合并。"""
    header = [str(v).strip() if v is not None else "" for v in grid[header_idx[0]]]
    for i in header_idx[1:]:
        nxt = list(grid[i]) + [None] * max(0, len(header) - len(grid[i]))
        header = [f"{a}_{str(b).strip()}" if b not in (None, "") and str(b).strip() != a else a
                  for a, b in zip(header, nxt)]
    start = header_idx[-1] + 1
    width = len(header)
    body = [(j, (list(row) + [None] * width)[:width]) for j, row in enumerate(grid[start:], start=start + 1)]
    for i, h in enumerate(header):
        # 表头是空的、下面却有数据的列（如用公式拼出来的编号列）：起个固定的名字，才能被选用；否则只能拿别的列顶替
        if not h and any(row[i] not in (None, "") for _, row in body):
            header[i] = blank_header(i)
    active = list(header)              # 当前这一段的表头（原样，用来认重复表头和分段表头）
    base = list(header)                # 第一段的表头：别的段按列名对回这里
    header = dedupe_headers(header)
    t = SheetTable(file, title, header_idx[0] + 1, header)
    names = [h for h in header if h]
    perm: dict[int, int] = {}          # 当前这一段要挪位置的列：这一段的位置 -> 第一段的位置
    for j, row in body:
        if _filled(row) == 0:
            continue
        if names and _is_header_copy(row, active):      # 数据中间又夹了一行同样的表头（几段数据拼在一起时常见）：不是数据
            t.repeats.append(j)
            continue
        if names and _is_segment_header(row, active):  # 另一段的表头，有几列的列名和上面不同
            changed = {}
            for i, v in enumerate(row):
                new = str(v).strip() if v not in (None, "") else ""
                if new != active[i] and (new or active[i]):
                    changed[i] = (header[i], new)
                    if new:
                        active[i] = new
            t.segments.append((j, changed))
            perm, unsure = _segment_perm(row, base)
            if perm:
                t.remaps.append((j, dict(perm)))
            if unsure:
                t.unsure.append((j, unsure))
            continue
        if perm:          # 这一段换了列的位置：按列名挪回第一段的位置，同一个字段读到的是同一种数
            moved = list(row)
            for i, k in perm.items():
                moved[k] = row[i]
            row = moved
        (t.totals if _is_total(row) else t.rows).append((j, row))
    return t


def _segment_perm(row: list, base: list[str]) -> tuple[dict[int, int], list[int]]:
    """另一段的表头和第一段比：列名在第一段里唯一出现、位置不同的列，挪回第一段的位置。
    挪完必须是一一对应（不会两列挪到同一处、也不会把没挪走的列盖掉），否则不挪，返回拿不准的列。"""
    width = len(base)
    names = [str(v).strip() if v not in (None, "") else "" for v in list(row)[:width]] + [""] * max(0, width - len(row))
    target = list(range(width))
    unsure = []
    for i, n in enumerate(names):
        if not n or n == base[i]:
            continue
        hits = [k for k, h in enumerate(base) if h == n]
        if len(hits) == 1:
            target[i] = hits[0]
        elif len(hits) > 1:
            unsure.append(i)
    if len(set(target)) != width:      # 挪了会撞：哪几列撞了就算拿不准
        dup = {k for k in target if target.count(k) > 1}
        return {}, sorted(set(unsure) | {i for i, k in enumerate(target) if k in dup})
    return {i: k for i, k in enumerate(target) if k != i}, unsure


def _is_segment_header(row: list, header: list[str]) -> bool:
    """这一行是另一段表的表头：全是文字，至少一半有名字的列都填了，其中至少 2 格、三成以上和当前表头相同（同一类表），
    但又有和当前表头不同的列名。全是文字的普通数据行（如 部门编号 | 部门名称）很少会和表头有两格以上相同。"""
    cells = [(str(v).strip(), h) for v, h in zip(row, header) if v not in (None, "")]
    if not cells or any(not _is_text(v) for v in row if v not in (None, "")):
        return False
    named = sum(1 for h in header if h and not h.endswith("(无表头)"))
    same = sum(1 for v, h in cells if v == h)
    differ = sum(1 for v, h in cells if v != h)
    if differ and len(cells) >= 2:     # 同样的列名换了位置（如“收入、成本”对调）：几乎每格都是第一段的某个列名
        known = {h for h in header if h}
        inside = sum(1 for v, _ in cells if v in known)
        if inside >= max(2, 0.8 * named) and inside >= 0.8 * len(cells):
            return True
    return len(cells) >= max(2, 0.5 * named) and same >= 2 and same >= 0.3 * len(cells) and differ >= 1


_NAME_SHARE = re.compile(r"^(?P<name>.*?)\s*[(（]?\s*(?P<pct>\d+(?:\.\d+)?)\s*[%％]\s*[)）]?$")


def split_name_share(text) -> tuple[str, "Decimal | None"]:
    """“张三50%”“张三(50%)”拆成 (张三, 0.5)；没有百分比（或前面没有名字）原样返回、比例是 None。"""
    from decimal import Decimal
    if not isinstance(text, str):
        return text, None
    m = _NAME_SHARE.match(text.strip())
    if not m or not m.group("name").strip() or not re.search(r"[^\d\s%％.]", m.group("name")):
        return text, None
    return m.group("name").strip(), Decimal(m.group("pct")) / 100


def unpivot_sheet(st: SheetTable, columns: list[str], value: str, label: str = "", share: str = "", remainder: str = "") -> SheetTable:
    """把几列同一类的值（如销售负责人、执行人、客户经理各一列人名）展开成每个值一行：其余列照抄，
    展开的值放在 value 列，label 列记它原来在哪一列（如 角色）。空格子不展开；合计行不要。
    share 非空时：格子里“名字+百分比”（如 张三50%）拆开，名字放 value、比例放 share 列（小数，没写百分比的是 1）；
    remainder 非空时：比例不足 1 的，余下的部分另记一行（名字用 remainder，其中 {label} 换成原列名，比例是余下的部分）。"""
    from decimal import Decimal
    idx = [st.headers.index(c) for c in columns]
    keep = [i for i in range(len(st.headers)) if i not in idx]
    headers = [st.headers[i] for i in keep] + [value] + ([label] if label else []) + ([share] if share else [])
    rows = []
    for j, vals in st.rows:
        base = [vals[i] for i in keep]
        for c, i in zip(columns, idx):
            if vals[i] in (None, "") or (isinstance(vals[i], str) and not vals[i].strip()):
                continue
            v, pct = split_name_share(vals[i]) if share else (vals[i], None)
            rows.append((j, base + [v] + ([c] if label else []) + ([pct if pct is not None else Decimal(1)] if share else [])))
            if share and remainder and pct is not None and pct < 1:
                rows.append((j, base + [remainder.replace("{label}", c)] + ([c] if label else []) + [Decimal(1) - pct]))
    return SheetTable(st.file, st.sheet, st.header_row, headers, rows, [], st.confidence, list(st.repeats), list(st.segments),
                      list(st.remaps), list(st.unsure))


def unpivot_by(st: SheetTable, spec: dict) -> SheetTable:
    """按 define_dataset / 映射里存的 unpivot 说明展开。"""
    return unpivot_sheet(st, spec["columns"], spec["value"], spec.get("label") or "", spec.get("share") or "",
                         spec.get("remainder") or "")


ROW_ID = "（行号）"      # 虚拟列：每行的 Excel 行号（展开后同一行的几条加 -1、-2），给没有唯一编号列的明细表当编号


def add_row_ids(st: SheetTable) -> SheetTable:
    """在表的最后加一列 ROW_ID：没有每行唯一编号的明细表（如展开后的分配名单）也能当最末层、写 key。"""
    if ROW_ID in st.headers:
        return st
    from collections import Counter
    many = Counter(j for j, _ in st.rows)
    seq: dict[int, int] = {}
    rows = []
    for j, vals in st.rows:
        seq[j] = seq.get(j, 0) + 1
        rows.append((j, list(vals) + [f"{j}-{seq[j]}" if many[j] > 1 else str(j)]))
    return SheetTable(st.file, st.sheet, st.header_row, list(st.headers) + [ROW_ID], rows, [], st.confidence,
                      list(st.repeats), list(st.segments), list(st.remaps), list(st.unsure))


def dedupe_headers(header: list[str]) -> list[str]:
    """同一张表里几列同名（如三列都叫“区域”）：第一列保持原名，后面的加上 Excel 列字母，如“区域(Z列)”，才能分别选用。"""
    from openpyxl.utils import get_column_letter
    seen, out = set(header), []
    first: set[str] = set()
    for i, h in enumerate(header):
        if h and h in first:
            new = f"{h}({get_column_letter(i + 1)}列)"
            while new in seen:
                new += "'"
            seen.add(new)
            out.append(new)
        else:
            first.add(h)
            out.append(h)
    return out


def blank_header(i: int) -> str:
    """没有表头的第 i 列（0 起）的名字，如 “第1列(无表头)”。"""
    return f"第{i + 1}列(无表头)"


def _is_header_copy(row: list, header: list[str]) -> bool:
    """这一行和表头基本一样（有表头的列里八成以上相同，且至少 2 列）：是重复的表头行。"""
    pairs = [(str(v).strip(), h) for v, h in zip(row, header) if h and not h.endswith("(无表头)") and v not in (None, "")]
    same = sum(1 for v, h in pairs if v == h)
    named = sum(1 for h in header if h and not h.endswith("(无表头)"))
    return same >= 2 and same >= 0.8 * named


def table_at(path: str | Path, sheet: str, header_rows: list[int]) -> SheetTable:
    """按人工（或智能体）指定的表头行读取工作表，header_rows 为 Excel 行号，如 [3] 或 [3, 4]。"""
    path = Path(path)
    wb = openpyxl.load_workbook(path, data_only=True)
    if sheet not in wb.sheetnames:
        raise KeyError(f"{path.name} 中没有工作表「{sheet}」")
    grid = _grid(wb[sheet])
    if not header_rows or max(header_rows) > len(grid) or min(header_rows) < 1:
        raise ValueError(f"表头行 {header_rows} 超出范围（工作表共 {len(grid)} 行）")
    return _build(grid, path.name, sheet, [r - 1 for r in sorted(header_rows)])


def detect(path: str | Path) -> list[SheetTable]:
    path = Path(path)
    wb = openpyxl.load_workbook(path, data_only=True)
    out = []
    for ws in wb.worksheets:
        t = detect_sheet(ws, path.name)
        if t is not None and t.rows:
            out.append(t)
    return out
