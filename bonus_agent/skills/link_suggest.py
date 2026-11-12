"""挂接建议：一张参考表（预算、名单、各类系数和标记表）按哪几列和主数据表（如销售明细）对得上、该挂在哪一层。

只读、确定性：只比较“编号类”的列（月份、客户、型号、区域），逐列、逐组合算参考表的行有多少在主表里找得到，
给出建议的挂法和没对上的例子，让模型据此写 define_dataset 的 combine / link / key，而不是凭表头名字猜。"""
from __future__ import annotations

import itertools
import re
import unicodedata

from bonus_core.data.table_detect import SheetTable, key_part

MIN_OVERLAP = 0.5           # 两列取值重合到这个比例才算同一个意思
MIN_OVERLAP_SAME_KIND = 0.2  # 同一类列（如都是客户列）放宽：参考表常多出主表里没有的客户
MAX_COMBO = 4


def _norm(v) -> str:
    s = unicodedata.normalize("NFKC", key_part(v)).strip().casefold()
    s = re.sub(r"\s+", "", s)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):       # 日期按年月比
        s = s[:4] + s[5:7]
    return s


def _base_name(h: str) -> str:
    """去掉同名列的列号后缀，如“客户简称(AE列)” → “客户简称”。"""
    return re.sub(r"\([A-Z]{1,3}列\)'*$", "", str(h or ""))


def kind(h: str) -> str:
    h = _base_name(h)
    if re.search(r"系列|类型|金额|数量|汇率|比例|预算|收入", h):
        return ""
    if re.search(r"日期|月份|年月|期间", h):
        return "月份"
    if re.search(r"型号|物料编号|SKU", h, re.I):
        return "型号"
    if re.search(r"区域|大区|地区", h):
        return "区域"
    if re.search(r"客户|简称|编码", h):
        return "客户"
    if re.search(r"匹配|项目编号|项目$|编号", h):
        return "编号"
    return ""


def _cols(st: SheetTable, only_kind: bool) -> dict[str, list[str]]:
    out = {}
    for j, h in enumerate(st.headers):
        if not h or (only_kind and not kind(h)):
            continue
        vals = [_norm(v[j]) if j < len(v) else "" for _, v in st.rows]
        if any(vals):
            out[h] = vals
    return out


def _level(kinds: set[str]) -> str:
    if {"客户", "型号"} <= kinds:
        return "项目"
    if kinds <= {"客户", "月份"} and "客户" in kinds:
        return "客户"
    if "区域" in kinds:
        return "区域"
    if "型号" in kinds:
        return "型号"
    return ""


def suggest(base: SheetTable, ref: SheetTable, top: int = 5) -> str:
    """base：主数据表（如销售单）；ref：要挂上去的参考表。返回给模型看的文字建议。"""
    bc, rc = _cols(base, True), _cols(ref, True)
    lines = [f"主表「{base.sheet}」{len(base.rows)} 行；参考表「{ref.sheet}」{len(ref.rows)} 行。"]
    if not bc or not rc:
        return "\n".join(lines + ["两张表里没有可比较的编号类列（月份、客户、型号、区域），先用 read_sheet 看取值写法。"])
    bsets = {h: set(v) - {""} for h, v in bc.items()}
    keep = list(range(len(ref.rows)))
    # 参考表有月份列、只有一部分月份在主表里：先按主表的月份筛，再比其他列
    for h, vals in rc.items():
        if kind(h) != "月份":
            continue
        for b, bs in bsets.items():
            if kind(b) != "月份":
                continue
            rs = set(vals) - {""}
            inter = rs & bs
            if inter and inter != rs:
                keep = [i for i in keep if vals[i] in inter]
                lines.append(f"参考表「{h}」有 {len(rs)} 个不同取值，只有 {sorted(inter)[:6]} 在主表「{b}」里：这张表跨期间，"
                             f"定义时按核算期间筛选（filter 或 period），筛完剩 {len(keep)} 行，下面按筛完的行比。")
            break
    best: dict[str, list[tuple[str, float]]] = {}
    for h, vals in rc.items():
        rs = {vals[i] for i in keep} - {""}
        if not rs:
            continue
        for b, bs in bsets.items():
            ov = len(rs & bs) / len(rs)
            same_kind = kind(h) == kind(b) and kind(h) != "编号"
            if ov >= MIN_OVERLAP or (same_kind and ov >= MIN_OVERLAP_SAME_KIND):
                best.setdefault(h, []).append((b, ov))
    for h in best:
        best[h].sort(key=lambda x: -x[1])
    if best:
        lines.append("单列对应（参考表列 → 主表列：参考表取值在主表里出现的比例）：")
        for h, cand in best.items():
            lines.append(f"  · {h} → " + "；".join(f"{b} {ov:.0%}" for b, ov in cand[:3]))
            names = [b for b, _ in cand]
            if len({_base_name(b) for b in names}) < len(names):
                lines.append("    主表有同名列：选对得上比例高的那一列，define_dataset 里写明列号；两列一样高时看哪一列和其他参考表写法一致，拿不准就提问")
    # 每个参考表列取对得上比例最高的主表列；主表同名列（如 客户简称 / 客户简称(AE列)）比例差不多时两列都试
    matched = []
    for h, cand in best.items():
        if kind(h) == "编号":
            continue
        top_ov = cand[0][1]
        for b, ov in cand:
            if b == cand[0][0] or (_base_name(b) == _base_name(cand[0][0]) and ov >= top_ov - 0.05):
                matched.append((h, b))
    combos = []
    cache: dict[tuple, set] = {}
    for n in range(1, min(MAX_COMBO, len(matched)) + 1):
        for combo in itertools.combinations(matched, n):
            rcols, bcols = [c[0] for c in combo], [c[1] for c in combo]
            kinds = [kind(b) for b in bcols]
            if len(set(rcols)) < n or len(set(bcols)) < n or len(set(kinds)) + max(0, kinds.count("客户") - 1) < n \
                    or kinds.count("客户") > 2:
                continue
            if tuple(bcols) not in cache:
                cache[tuple(bcols)] = {tuple(bc[b][i] for b in bcols) for i in range(len(base.rows))}
            bset = cache[tuple(bcols)]
            rt = [tuple(rc[r][i] for r in rcols) for i in keep]
            rt = [t for t in rt if all(t)]
            if rt:
                miss = [t for t in rt if t not in bset]
                rset = set(rt)
                # 反过来看：主表这几列的组合（同一期间）有多少在参考表里找不到；挂错列时这里会多出来
                rev = [t for t in bset if all(t) and t not in rset] if "月份" in kinds else []
                combos.append((rcols, bcols, len(rt) - len(miss), len(rt), miss[:3], rev))
    # 参考表现成的编号列（如“匹配列”）= 主表几列直接拼起来
    bk = [b for b in bc if kind(b) in ("月份", "客户", "型号")]
    for h, vals in rc.items():
        if kind(h) != "编号":
            continue
        rs = {vals[i] for i in keep} - {""}
        for n in (3, 4):
            for combo in itertools.combinations(bk, n):
                joined = {"".join(bc[b][i] for b in combo) for i in range(len(base.rows))}
                hit = len(rs & joined)
                if rs and hit / len(rs) >= MIN_OVERLAP:
                    combos.append(([h], ["+".join(combo) + "（直接拼接，无分隔符）"], hit, len(rs), [], []))
    combos.sort(key=lambda c: (-round(c[2] / c[3], 2), -len(c[0]), len(c[5])))
    if combos:
        lines.append("可选挂法（参考表这几列合起来，有多少行在主表里找得到；带月份的再看主表同期的组合有多少在参考表里没有）：")
        for rcols, bcols, hit, tot, miss, rev in combos[:top]:
            lv = _level({kind(b.split("（")[0].split("+")[0]) if "（" in b else kind(b) for b in bcols}
                        | ({"客户", "型号"} if any("（" in b for b in bcols) else set()))
            lines.append(f"  · {'+'.join(rcols)} ↔ {'+'.join(bcols)}：{hit}/{tot} 行对上（{hit / tot:.0%}）"
                         + (f"，挂在「{lv}」这一层" if lv else "")
                         + (f"；没对上的如 {['|'.join(m) for m in miss]}" if miss else "")
                         + (f"；主表同期有 {len(rev)} 个组合在参考表里没有，如 {['|'.join(m) for m in rev[:3]]}" if rev else ""))
        lines.append("怎么用：能按 月份+客户+型号 对上的是项目级的表，用 combine 合成和主表一样的编号再 link；"
                     "只能按客户对上的表挂到「客户」这一层（客户实体用主表的同一列客户列），按区域对上的挂到「区域」；"
                     "不要把参考表挂到另一张参考表下面。没对上的行先看写法、期间，再决定 unmatched 或提问。")
    else:
        lines.append("没找到对得上的列：先用 read_sheet 看两张表的取值写法。")
    return "\n".join(lines)
