"""场景的规则版本：编制案卷经财务确认后冻结成计算模板，发布到场景下成为 v1、v2……，以后每期换数据直接按版本算。

<home>/<场景>/releases/<版本>/ 里是计算模板的全部文件（manifest.yaml、规则、映射、修订）和 release.yaml（谁、什么时候、
从哪个案卷发布、验收结果）。版本只增不改：同名版本不能覆盖，文件被改过拒绝计算（模板自带指纹核对）。
场景配了验收用例时，发布前按模板逐个复算，与财务的期望结果逐分一致才能发布。
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from bonus_agent.scenarios import Registry, Scenario, ScenarioError
from bonus_agent.scenarios.inputs import InputReport, check_inputs

RELEASE_FILE = "release.yaml"
VERSION = re.compile(r"v(\d{1,4})\Z")


class PublishError(ScenarioError):
    pass


class InputError(ScenarioError):
    def __init__(self, report: InputReport):
        self.report = report
        super().__init__("本期数据不符合输入模板：\n" + "\n".join(f.line() for f in report.errors))


@dataclass
class Release:
    scenario: str
    version: str
    dir: Path
    info: dict

    def template(self):
        from bonus_core.template import Template
        return Template.load(self.dir)

    @property
    def summary(self) -> str:
        i = self.info
        return f"{self.version}（{i.get('published_by', '')} {str(i.get('published_at', ''))[:16]} 发布" + (
            f"，{i['note']}" if i.get("note") else "") + "）"


def list_releases(reg: Registry, scenario_id: str) -> list[Release]:
    root = reg.data_dir(scenario_id) / "releases"
    if not root.is_dir():
        return []
    out = []
    for d in root.iterdir():
        if VERSION.fullmatch(d.name) and (d / RELEASE_FILE).is_file():
            info = yaml.safe_load((d / RELEASE_FILE).read_text(encoding="utf-8")) or {}
            out.append(Release(scenario_id, d.name, d, info))
    return sorted(out, key=lambda r: int(VERSION.fullmatch(r.version).group(1)))


def get_release(reg: Registry, scenario_id: str, version: str | None = None) -> Release:
    rels = list_releases(reg, scenario_id)
    if not rels:
        raise ScenarioError(f"场景 {scenario_id} 还没有发布的规则版本：先在编制模式下让智能体起草规则，财务确认后发布")
    if version in (None, "", "latest"):
        return rels[-1]
    for r in rels:
        if r.version == version:
            return r
    raise ScenarioError(f"场景 {scenario_id} 没有版本 {version}（有：{'、'.join(r.version for r in rels)}）")


def run_acceptance(sc: Scenario, template_dir: Path) -> list[dict]:
    """按模板复算场景的每个验收用例，与期望结果逐分比对。返回 [{id, ok, lines}]。"""
    from bonus_agent.compare import compare_expected, report
    from bonus_core.template import Template
    out = []
    for case in sc.acceptance:
        files = [sc.file(x) for x in case.data]
        expected = sc.file(case.expected)
        missing = [str(p) for p in files + [expected] if not p.exists()]
        if missing:
            out.append({"id": case.id, "ok": False, "lines": ["文件不存在：" + "、".join(missing)]})
            continue
        try:
            results, _ = Template.load(template_dir).run(files, case.period)
        except Exception as e:
            out.append({"id": case.id, "ok": False, "lines": [f"复算失败：{e}"]})
            continue
        ms = compare_expected(results, expected)
        found = [m for m in ms if m.total]
        ok = bool(found) and all(m.ok for m in ms if m.total or m.ambiguous)
        out.append({"id": case.id, "ok": ok, "lines": report(ms) or ["期望结果里没有找到对应的金额"]})
    return out


def publish(reg: Registry, scenario_id: str, template_dir: str | Path, by: str, note: str = "", *,
            source: str = "", questions: list[dict] | None = None) -> Release:
    """把已确认的计算模板发布成场景的下一个规则版本。验收用例没全部对上时拒绝发布。"""
    from bonus_core.template import Template, TemplateError
    sc = reg.get(scenario_id)
    if not by.strip():
        raise PublishError("要写明发布人")
    src = Path(template_dir)
    try:
        tpl = Template.load(src)
    except TemplateError as e:
        raise PublishError(str(e)) from None
    acc = run_acceptance(sc, src)
    bad = [a for a in acc if not a["ok"]]
    if bad:
        raise PublishError("验收没通过，不能发布：\n" + "\n".join(f"「{a['id']}」" + "；".join(a["lines"]) for a in bad))
    root = reg.data_dir(scenario_id) / "releases"
    root.mkdir(parents=True, exist_ok=True)
    have = list_releases(reg, scenario_id)
    version = f"v{(int(VERSION.fullmatch(have[-1].version).group(1)) if have else 0) + 1}"
    dest, tmp = root / version, root / f".{version}.new"
    if dest.exists():
        raise PublishError(f"版本 {version} 已存在，不能覆盖")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    for name in ["manifest.yaml", *(tpl.manifest.get("files") or {})]:
        (tmp / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / name, tmp / name)
    info = {
        "scenario": scenario_id, "version": version, "name": sc.name,
        "published_by": by, "published_at": datetime.now().isoformat(timespec="seconds"),
        "note": note, "source": source, "confirmed": tpl.manifest.get("confirmed"),
        "acceptance": acc,
    }
    (tmp / RELEASE_FILE).write_text(yaml.safe_dump(info, allow_unicode=True, sort_keys=False), encoding="utf-8")
    Template.load(tmp)          # 复制后的文件指纹仍一致
    tmp.rename(dest)
    if sc.memory and questions:
        reg.memory(scenario_id).add_questions(questions, version=version, source=source, by=by)
    return Release(scenario_id, version, dest, info)


@dataclass
class Computed:
    release: Release
    report: InputReport
    results: dict
    warnings: list
    written: list[Path]
    period: object = None


def compute(reg: Registry, scenario_id: str, data_files: list[str | Path], out: str | Path,
            version: str | None = None, period: str | None = None) -> Computed:
    """计算模式：按已发布的规则版本算本期数据，不调用大模型。先按输入模板检查，有错误就不算。
    period：核算期间（如 2026-10、2026Q4、2026-07~2026-09）；不写用发布时的期间。"""
    sc = reg.get(scenario_id)
    rel = get_release(reg, scenario_id, version)
    files = [Path(f) for f in data_files]
    rep = check_inputs(sc, files)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "输入检查.txt").write_text(rep.text() + "\n", encoding="utf-8")
    if not rep.ok:
        raise InputError(rep)
    tpl = rel.template()
    results, warnings = tpl.run(files, period)
    written = [p for p, _ in tpl.write(results, out)]
    from bonus_core.template import sha256
    (out / "run.json").write_text(json.dumps({
        "scenario": scenario_id, "version": rel.version, "at": datetime.now().isoformat(timespec="seconds"),
        "period": str(tpl.last_period) if tpl.last_period else None,
        "inputs": {p.name: sha256(p) for p in files}, "outputs": [p.name for p in written],
        "warnings": [str(w) for w in warnings]}, ensure_ascii=False, indent=1), encoding="utf-8")
    return Computed(rel, rep, results, warnings, written, tpl.last_period)
