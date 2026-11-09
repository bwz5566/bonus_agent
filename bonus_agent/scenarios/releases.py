"""Immutable, finance-approved versions. Publication runs independent acceptance cases.

No model participates in publication or calculation. A pending scenario can receive
a release once finance supplies its actual rules, mapping and expected results.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml

from bonus_agent.infra.storage import atomic_write, canonical, fingerprint
from bonus_core.data.mapping import DatasetMapping, apply_mapping, rebind_files
from bonus_core.data.table_detect import table_at
from bonus_core.dsl.models import RuleSet
from bonus_core.dsl.validator import validate
from bonus_core.engine.issues import EngineError
from bonus_core.engine.run import Adjustment, Engine, Result

READ_TOOLS = frozenset({"describe_scenario", "explain_object", "list_issues"})
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
METRICS = ("final", "held", "cap", "offset", "deducted", "paid", "overpaid")


class ReleaseError(ValueError):
    pass


def root_dir(value=None) -> Path:
    return Path(value or os.environ.get("BONUS_SCENARIO_RELEASES_DIR") or "scenario_releases").resolve()


def safe_id(value: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value) or value in {".", ".."}:
        raise ReleaseError("场景、版本和用例编号必须是安全的字母数字标识")
    return value


def inside(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if not name or Path(name).is_absolute() or not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ReleaseError(f"规则包内文件不存在或越界：{name}")
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Acceptance(Strict):
    id: str
    data: list[str] = Field(min_length=1)
    expected: str | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def check(self):
        safe_id(self.id)
        if bool(self.expected) == bool(self.error_code):
            raise ValueError("用例必须且只能提供 expected 或 error_code")
        return self


class ReleaseSpec(Strict):
    scenario: str
    version: str
    periods: list[str] = Field(min_length=1)
    rules: str
    mapping: str
    cases: list[Acceptance] = Field(min_length=2)
    allowed_tools: list[str] = Field(default_factory=lambda: sorted(READ_TOOLS))

    @model_validator(mode="after")
    def check(self):
        safe_id(self.scenario); safe_id(self.version)
        if len({c.id for c in self.cases}) != len(self.cases):
            raise ValueError("验收用例编号重复")
        if not any(c.expected for c in self.cases) or not any(c.error_code for c in self.cases):
            raise ValueError("发布至少需要一个独立期望结果用例和一个异常阻断用例")
        if any(not p.strip() for p in self.periods) or len(set(self.periods)) != len(self.periods):
            raise ValueError("核算期间不能为空或重复")
        if not set(self.allowed_tools) <= READ_TOOLS:
            raise ValueError("运行智能体只能使用只读工具")
        names = [self.rules, self.mapping, *[n for c in self.cases for n in c.data],
                 *[c.expected for c in self.cases if c.expected]]
        for name in names:
            if (Path(name).is_absolute() or ".." in Path(name).parts or "\\" in name
                    or name in {"release.yaml", "publication.json", "acceptance.json"}):
                raise ValueError("规则包路径非法或使用了保留文件名")
        return self


def read_spec(directory: Path) -> ReleaseSpec:
    try:
        return ReleaseSpec.model_validate(yaml.safe_load(inside(directory, "release.yaml").read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"无效的发布包：{exc}") from exc


def definitions(directory: Path, spec: ReleaseSpec):
    rs = RuleSet.load(inside(directory, spec.rules))
    issues = [i for i in validate(rs) if i.severity == "blocking"]
    if issues:
        raise EngineError(issues)
    raw = yaml.safe_load(inside(directory, spec.mapping).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != set(rs.datasets):
        raise ReleaseError("固定映射必须逐一覆盖规则中的数据表")
    mapping = {name: DatasetMapping.model_validate(value) for name, value in raw.items()}
    for name, m in mapping.items():
        if not m.file or not m.sheet or not m.header_rows or len(m.header_rows) != 1:
            raise ReleaseError(f"{name} 必须指定固定工作表和单行表头行号")
        if m.unpivot or set(m.columns) != set(rs.datasets[name].fields):
            raise ReleaseError(f"{name} 必须显式映射全部字段；发布版暂不允许动态展开")
        if len(set(m.columns.values())) != len(m.columns):
            raise ReleaseError(f"{name} 的多个字段不能映射到同一列")
        if rs.datasets[name].unmatched != "block":
            raise ReleaseError(f"{name} 不能静默忽略关联失败的记录")
    return rs, mapping


class InputContractError(ReleaseError):
    code = "input_contract"


def map_fixed(rs, mapping, files):
    """Exact single-header contracts, then the existing typed/foreign-key checks."""
    files = [Path(p) for p in files]
    if not files or len({p.name for p in files}) != len(files):
        raise InputContractError("输入不能为空，且文件名不能重复")
    mapping = rebind_files(mapping, files)
    by_name = {p.name: p for p in files}
    used = set()
    for name, m in mapping.items():
        if m.file not in by_name:
            raise InputContractError(f"{name} 找不到唯一的固定输入文件")
        used.add(m.file)
        st = table_at(by_name[m.file], m.sheet, m.header_rows)
        if (st.header_row != m.header_rows[0] or st.headers != list(m.columns.values())
                or st.segments or st.repeats or st.totals):
            raise InputContractError(f"{m.file}/{m.sheet} 表头、顺序或数据区域不符合已发布模板；禁止自动猜测或跳过记录")
    if used != set(by_name):
        raise InputContractError("输入包含规则版本未声明的文件")
    data, issues = apply_mapping(rs, files, mapping)
    bad = [i for i in issues if i.severity == "blocking"]
    if bad:
        raise EngineError(bad)
    # Never guess identifiers by case/width/spacing in a fixed contract. The old
    # engine's normalization warnings must become blockers on this entry point.
    keys = {d.key: {str(row.values[d.key]) for row in data[name].rows}
            for name, d in rs.datasets.items() if d.key}
    for name, definition in rs.datasets.items():
        for field in definition.fields:
            if field in keys and field != definition.key:
                bad = [str(row.src) for row in data[name].rows if str(row.values[field]) not in keys[field]]
                if bad:
                    raise InputContractError(f"{name}.{field} 必须精确关联，不能猜测或忽略：{bad[:5]}")
    return data, issues


def reconcile(result: Result):
    for row in result.payout_rows():
        v = {k: Decimal(getattr(row, k)) for k in METRICS}
        if any(not n.is_finite() or n < 0 for n in v.values()):
            raise ReleaseError(f"非法金额：{row.node_id}")
        if (v["final"] != v["held"] + v["cap"] or v["cap"] != v["deducted"] + v["paid"]
                or v["offset"] != v["deducted"] + v["overpaid"]):
            raise ReleaseError(f"发放金额勾稽失败：{row.node_id}")


def figures(result: Result):
    return {"nodes": {k: v.final for k, v in result.nodes.items()},
            "payouts": {k: {m: getattr(v, m) for m in METRICS} for k, v in result.payouts.items()},
            "rollups": {k: {m: getattr(v, m) for m in METRICS} for k, v in result.rollups.items()}}


def check_expected(results: dict[str, Result], expected):
    def compare(a, b, loc):
        if isinstance(a, dict):
            if not isinstance(b, dict) or set(a) != set(b):
                raise ReleaseError(f"期望结果对象或指标不完整：{loc}")
            for key in a:
                compare(a[key], b[key], f"{loc}/{key}")
        else:
            try:
                value = Decimal(str(b))
                if isinstance(b, bool) or not value.is_finite() or Decimal(a) != value:
                    raise ReleaseError(f"期望结果不一致：{loc} 引擎={a} 期望={b}")
            except (InvalidOperation, TypeError):
                raise ReleaseError(f"期望结果必须是有效金额：{loc}") from None
    compare({rev: figures(r) for rev, r in results.items()}, expected, "结果")


def publish(source, releases, approver):
    """Administrative publication: all fixtures pass before an immutable version appears."""
    from bonus_agent.scenarios import scenario_metadata
    if not str(approver).strip():
        raise ReleaseError("财务审批人不能为空")
    source, releases = Path(source).resolve(), root_dir(releases)
    spec = read_spec(source)
    scenario_metadata(spec.scenario)  # registration, not the old migration fixture
    dest = releases / spec.scenario / spec.version
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise ReleaseError("版本已发布，不可覆盖；请使用新版本号")
    stage = Path(tempfile.mkdtemp(prefix=".publishing-", dir=dest.parent))
    try:
        names = {"release.yaml", spec.rules, spec.mapping}
        for c in spec.cases:
            names.update(c.data)
            if c.expected:
                names.add(c.expected)
        for name in names:
            src = inside(source, name)
            dst = stage / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        spec = read_spec(stage)
        rs, mapping = definitions(stage, spec)
        checks = []
        for case in spec.cases:
            try:
                data, _ = map_fixed(rs, mapping, [inside(stage, n) for n in case.data])
                result = Engine(rs, data).compute()
                reconcile(result)
            except (EngineError, InputContractError) as exc:
                codes = {i.code for i in exc.issues} if isinstance(exc, EngineError) else {exc.code}
                if not case.error_code or case.error_code not in codes:
                    raise ReleaseError(f"验收用例 {case.id} 非预期失败：{exc}") from exc
                checks.append({"id": case.id, "status": "passed", "error_code": case.error_code})
                continue
            if case.error_code:
                raise ReleaseError(f"验收用例 {case.id} 未拦住指定异常 {case.error_code}")
            check_expected({"R0": result}, json.loads(inside(stage, case.expected).read_text(encoding="utf-8")))
            checks.append({"id": case.id, "status": "passed"})
        report = {"cases": checks, "status": "passed"}
        atomic_write(stage / "acceptance.json", canonical(report))
        names.add("acceptance.json")
        from bonus_agent.scenarios.execution import engine_code
        from bonus_core.engine.run import ENGINE_VERSION
        manifest = {"status": "published", "scenario": spec.scenario, "version": spec.version,
                    "approved_by": str(approver).strip(), "approved_at": datetime.now(timezone.utc).isoformat(),
                    "engine_version": ENGINE_VERSION, "engine_code": engine_code(),
                    "files": {name: sha(stage / name) for name in sorted(names)}}
        manifest["fingerprint"] = fingerprint(manifest)
        atomic_write(stage / "publication.json", canonical(manifest))
        # mkdir is the publication lease: concurrent publishers cannot overwrite.
        lock = dest.parent / f".{spec.version}.lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise ReleaseError("此版本正在发布") from None
        try:
            os.close(fd)
            if dest.exists():
                raise ReleaseError("版本已发布，不可覆盖")
            stage.rename(dest)
        finally:
            lock.unlink(missing_ok=True)
        return load_release(spec.scenario, spec.version, releases)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def load_release(scenario, version, releases=None):
    from bonus_agent.scenarios import scenario_metadata
    scenario_metadata(scenario)
    directory = root_dir(releases) / safe_id(scenario) / safe_id(version)
    try:
        manifest = json.loads((directory / "publication.json").read_text(encoding="utf-8"))
        body = {k: v for k, v in manifest.items() if k != "fingerprint"}
        if (manifest["status"] != "published" or manifest["scenario"] != scenario or manifest["version"] != version
                or fingerprint(body) != manifest["fingerprint"]):
            raise ReleaseError("发布记录无效")
        from bonus_agent.scenarios.execution import engine_code
        from bonus_core.engine.run import ENGINE_VERSION
        if manifest.get("engine_version") != ENGINE_VERSION or manifest.get("engine_code") != engine_code():
            raise ReleaseError("计算程序已变更，须在当前程序上重新验收并发布新版本")
        for name, digest in manifest["files"].items():
            if sha(inside(directory, name)) != digest:
                raise ReleaseError(f"已发布文件被修改：{name}")
        spec = read_spec(directory)
        if spec.scenario != scenario or spec.version != version:
            raise ReleaseError("版本与发布记录不一致")
        definitions(directory, spec)
        return directory, spec, manifest
    except (OSError, ValueError, KeyError) as exc:
        raise ReleaseError(f"场景 {scenario}/{version} 未发布或已损坏：{exc}") from exc


def versions(scenario, releases=None):
    directory = root_dir(releases) / safe_id(scenario)
    out = []
    for p in sorted(directory.iterdir()) if directory.is_dir() else []:
        if p.is_dir() and not p.name.startswith("."):
            try:
                load_release(scenario, p.name, releases)
                out.append(p.name)
            except ReleaseError:
                pass
    return out
