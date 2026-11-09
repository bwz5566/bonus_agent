"""核算期间：这次算哪段时间（某个月、某个季度、某一年或自定义起止）。

规则里不写死月份：数据表用 period 标出记日期/月份的字段，计算时按这次选的期间筛选行；表达式里要用“当期”时写
period_start() / period_end()。同一版规则换任何期间都能算。
"""
from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime

_YM = re.compile(r"^(\d{4})\s*[-/.年]?\s*(\d{1,2})\s*月?$")
_YMD = re.compile(r"^(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?$")
_Q = re.compile(r"^(\d{4})\s*[-年]?\s*[QqＱ第]\s*([1-4])\s*季?度?$")
_Y = re.compile(r"^(\d{4})\s*年?$")


class PeriodError(ValueError):
    pass


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def _norm(s) -> str:
    return unicodedata.normalize("NFKC", str(s)).strip()


def _one(text: str, end: bool) -> tuple[date, date]:
    """一个期间写法 -> (起, 止)。"""
    t = _norm(text)
    if m := _YMD.match(t):
        d = date(int(m[1]), int(m[2]), int(m[3]))
        return d, d
    if m := _Q.match(t):
        y, q = int(m[1]), int(m[2])
        return date(y, 3 * q - 2, 1), _month_end(y, 3 * q)
    if m := _YM.match(t):
        y, mo = int(m[1]), int(m[2])
        if 1 <= mo <= 12:
            return date(y, mo, 1), _month_end(y, mo)
    if m := _Y.match(t):
        y = int(m[1])
        return date(y, 1, 1), date(y, 12, 31)
    raise PeriodError(f"看不懂期间「{text}」：写成 2026-09（月）、2026Q3（季度）、2026（年）、"
                      "2026-07~2026-09 或 2026-09-01~2026-09-30（起止）")


@dataclass(frozen=True)
class Period:
    start: date
    end: date          # 含当天

    @classmethod
    def parse(cls, text: str | None) -> "Period | None":
        if text is None or not _norm(text):
            return None
        parts = re.split(r"\s*(?:~|～|至|到|—|–|\s-\s)\s*", _norm(text))
        if len(parts) == 1:
            s, e = _one(parts[0], False)
        elif len(parts) == 2:
            s, e = _one(parts[0], False)[0], _one(parts[1], True)[1]
        else:
            raise PeriodError(f"看不懂期间「{text}」")
        if s > e:
            raise PeriodError(f"期间「{text}」的开始晚于结束")
        return cls(s, e)

    @property
    def label(self) -> str:
        s, e = self.start, self.end
        if s.day == 1 and e == _month_end(e.year, e.month):
            if (s.year, s.month) == (e.year, e.month):
                return f"{s.year}-{s.month:02d}"
            if s.month == 1 and e.month == 12 and s.year == e.year:
                return f"{s.year}"
            if s.year == e.year and s.month % 3 == 1 and e.month == s.month + 2:
                return f"{s.year}Q{(s.month + 2) // 3}"
            return f"{s.year}-{s.month:02d}~{e.year}-{e.month:02d}"
        return f"{s.isoformat()}~{e.isoformat()}"

    @property
    def months(self) -> int:
        return (self.end.year - self.start.year) * 12 + self.end.month - self.start.month + 1

    def __str__(self) -> str:
        return self.label

    def span_of(self, v) -> tuple[date, date] | None:
        """单元格里的日期 / 月份 / 年份 -> 它覆盖的日期范围；认不出返回 None。"""
        if isinstance(v, datetime):
            return v.date(), v.date()
        if isinstance(v, date):
            return v, v
        if isinstance(v, bool) or v is None:
            return None
        if isinstance(v, (int, float)):
            if float(v) != int(v):
                return None
            v = str(int(v))
        t = _norm(v)
        if re.fullmatch(r"\d{8}", t):
            t = f"{t[:4]}-{t[4:6]}-{t[6:]}"
        elif re.fullmatch(r"\d{6}", t):
            t = f"{t[:4]}-{t[4:]}"
        t = re.sub(r"\s+00:00(:00)?$", "", t)
        try:
            return _one(t, False)
        except (PeriodError, ValueError):
            return None

    def contains(self, v) -> bool | None:
        """这一行是否属于本期间。按日记的看日期；按月（年）记的，整月（整年）落在期间里才算；只覆盖一部分时返回 None（说不清）。"""
        sp = self.span_of(v)
        if sp is None:
            return None
        a, b = sp
        if self.start <= a and b <= self.end:
            return True
        if b < self.start or a > self.end:
            return False
        return None
