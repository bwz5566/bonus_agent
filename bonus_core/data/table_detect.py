"""识别格式不固定的 Excel：找表头行、展开合并单元格、剔除合计行。"""
from __future__ import annotations

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
        if n < 2:
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
    header = [str(v).strip() if v is not None else "" for v in grid[best]]
    start = best + 1
    # 两行表头：上一行是分组标题（如「金额（万元）」跨两列），下一行是具体列名
    if best + 1 < len(grid):
        nxt = grid[best + 1]
        if _filled(nxt) >= 2 and all(_is_text(v) or v in (None, "") for v in nxt):
            header = [
                f"{a}_{str(b).strip()}" if b not in (None, "") and str(b).strip() != a else a
                for a, b in zip(header, nxt + [None] * (len(header) - len(nxt)))
            ]
            start = best + 2
    width = len(header)
    t = SheetTable(file, ws.title, best + 1, header)
    for j, row in enumerate(grid[start:], start=start + 1):
        row = (list(row) + [None] * width)[:width]
        if _filled(row) == 0:
            continue
        (t.totals if _is_total(row) else t.rows).append((j, row))
    t.confidence = min(1.0, best_score / max(width, 1))
    return t


def detect(path: str | Path) -> list[SheetTable]:
    path = Path(path)
    wb = openpyxl.load_workbook(path, data_only=True)
    out = []
    for ws in wb.worksheets:
        t = detect_sheet(ws, path.name)
        if t is not None and t.rows:
            out.append(t)
    return out
