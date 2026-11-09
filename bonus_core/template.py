"""已确认的计算模板：财务确认某一轮结果后，把规则、列映射和人工调整冻结成模板，以后每期换数据直接算，不再调用大模型。

模板目录里有 manifest.yaml：
  confirmed: {by, at, round, note}           谁在什么时候确认了第几轮
  mapping: mapping.yaml
  schemes:                                    按计算顺序（被引用的方案在前）
    - {name, rules, revisions: [{name, adjust}]}
  files: {文件名: sha256}                     模板文件的指纹，运行前核对，被改过就拒绝计算
  data: {文件名: sha256}                      确认时所用数据的指纹（只作记录，换数据是正常用法）
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from bonus_core.data.mapping import apply_mapping, load_mapping, rebind_files
from bonus_core.dsl.models import RuleSet
from bonus_core.engine.issues import EngineError, Issue
from bonus_core.engine.run import Adjustment, Engine, Result

MANIFEST = "manifest.yaml"
SCHEME_REF = re.compile(r"""scheme_paid\(\s*\\?["']([^"'\\]+)""")    # 也匹配 JSON 里转义的引号


class TemplateError(ValueError):
    pass


def sha256(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def scheme_deps(rs: RuleSet) -> set[str]:
    """规则里 scheme_paid 引用的其他方案。"""
    return set(SCHEME_REF.findall(rs.model_dump_json()))


def order_schemes(rulesets: dict[str, RuleSet]) -> list[str]:
    out: list[str] = []

    def visit(n, path):
        if n in out:
            return
        if n in path:
            raise TemplateError(f"方案之间循环引用：{' → '.join(path + [n])}")
        for d in sorted(scheme_deps(rulesets[n])):
            if d in rulesets:
                visit(d, path + [n])
        out.append(n)
    for n in rulesets:
        visit(n, [])
    return out


@dataclass
class Template:
    dir: Path
    manifest: dict

    @classmethod
    def load(cls, d: str | Path, verify: bool = True) -> "Template":
        d = Path(d)
        p = d / MANIFEST
        if not p.exists():
            raise TemplateError(f"{d} 不是计算模板（没有 {MANIFEST}）")
        m = yaml.safe_load(p.read_text(encoding="utf-8"))
        t = cls(d, m)
        if verify:
            t.verify()
        return t

    def verify(self) -> None:
        """模板文件与确认时的指纹一致，否则拒绝计算（规则被改过就不是财务确认的那一版）。"""
        bad = []
        for name, h in (self.manifest.get("files") or {}).items():
            f = self.dir / name
            if not f.exists():
                bad.append(f"{name} 不存在")
            elif sha256(f) != h:
                bad.append(f"{name} 在确认后被修改过")
        if bad:
            raise TemplateError("模板与确认时不一致，拒绝计算：" + "；".join(bad) + "。如需修改规则，请走一轮智能体处理并重新确认")

    def period(self, period=None):
        """本次的核算期间：调用方给的优先，没给用确认时的期间（manifest 的 period）。"""
        from bonus_core.data.period import Period
        if period is None:
            return Period.parse(self.manifest.get("period"))
        return period if isinstance(period, Period) else Period.parse(period)

    def run(self, data_files: list[str | Path], period=None) -> tuple[dict[str, dict[str, Result]], list[Issue]]:
        """按顺序计算每个方案的基准结果和各修订，被引用的方案结果传给引用它的方案。
        period：核算期间（如 "2026-10"），不给时用确认时的期间；写了 period 字段的数据表只取期间里的行。"""
        period = self.period(period)
        self.last_period = period
        mapping = rebind_files(load_mapping(self.dir / self.manifest["mapping"]), data_files)
        out: dict[str, dict[str, Result]] = {}
        refs: dict[str, Result] = {}
        warnings: list[Issue] = []
        for s in self.manifest["schemes"]:
            rs = RuleSet.load(self.dir / s["rules"])
            data, issues = apply_mapping(rs, data_files, mapping, period)
            blocking = [i for i in issues if i.severity == "blocking"]
            if blocking:
                raise EngineError(blocking)
            warnings += issues
            eng = Engine(rs, data, refs, period)
            res = {"R0": eng.compute()}
            for rev in s.get("revisions") or []:
                adj = yaml.safe_load((self.dir / rev["adjust"]).read_text(encoding="utf-8")) or []
                res[rev["name"]] = eng.compute([Adjustment(**a) for a in adj])
            out[s["name"]] = res
            refs.update({f"{s['name']}/{k}": v for k, v in res.items()})
        return out, warnings

    def write(self, results: dict[str, dict[str, Result]], out: str | Path) -> list[tuple[Path, tuple[str, str, Result]]]:
        """每个方案（修订）写一个结果 Excel，附「确认信息」。返回 [(文件, (方案, 修订, 结果))]。"""
        from bonus_core.engine.ledger import ledger_entries
        from bonus_core.report.excel import write_excel
        c = self.manifest["confirmed"]
        info = [("确认信息", ["项目", "值"], [["确认人", c["by"]], ["确认时间", c["at"]], ["确认的轮次", f"第 {c['round']} 轮"],
                                           ["说明", c.get("note", "")], ["模板目录", str(self.dir)],
                                           ["核算期间", str(getattr(self, "last_period", None) or self.manifest.get("period") or "未指定")]],
                 {"项目": 14, "值": 70})]
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        rules = {s["name"]: RuleSet.load(self.dir / s["rules"]) for s in self.manifest["schemes"]}
        written = []
        for name, revs in results.items():
            for rev, res in revs.items():
                f = out / (f"result_{name}.xlsx" if rev == "R0" else f"result_{name}_{rev}.xlsx")
                write_excel(rules[name], res, f, ledger_entries(res), extra_sheets=info)
                written.append((f, (name, rev, res)))
        return written
