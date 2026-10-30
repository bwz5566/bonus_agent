"""变异检查：故意改坏关键代码，看测试能不能发现（python tools/mutation_check.py，约 10 分钟）。
新加防错关时在 M 里加一条：(名称, 文件, 原文, 改坏后的写法)。
每个变异：在仓库副本里改一处，跑全量测试（-x 遇到失败即停）。"""
import json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path
REPO = Path(__file__).resolve().parents[1]
M = [
 ("引擎：舍入改成银行家舍入", "bonus_core/engine/evaluator.py", "r = self._num(a, ctx.where).quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP)", "r = self._num(a, ctx.where).quantize(Decimal(1).scaleb(-places))"),
 ("引擎：尾差不按最大余数分", "bonus_core/engine/allocation.py", "order = sorted(range(len(free)), key=lambda i: (-(exacts[i] - floor[i]), free[i].id))", "order = sorted(range(len(free)), key=lambda i: free[i].id)"),
 ("引擎：中间层没人分到钱不报错", "bonus_core/engine/run.py", "        self._check_stranded()", "        pass"),
 ("引擎：关联实体读不到参数表", "bonus_core/engine/evaluator.py", "            if n.attrs:", "            if n.attrs and not n.aux:"),
 ("引擎：补发不先应用数据修订", "bonus_core/engine/ledger.py", "    updated, patched = apply_patches(rs_then, updated)", "    patched = []"),
 ("模板：下期文件改名对不上", "bonus_core/template.py", "mapping = rebind_files(load_mapping(self.dir / self.manifest[\"mapping\"]), data_files)", "mapping = load_mapping(self.dir / self.manifest[\"mapping\"])"),
 ("检查：预发扣在奖金池不拦", "bonus_agent/lint.py", "    paid = _paid_in_pool(rs, headers or {})", "    paid = []"),
 ("检查：照抄参数表不拦", "bonus_agent/lint.py", "    copied = _copied(rs, data)", "    copied = {}"),
 ("检查：所有检查都能写进 ignored", "bonus_agent/agent.py", 'NO_IGNORE = ("查找表 ", "抵扣 ")', "NO_IGNORE = ()"),
 ("检查：预发检查能靠提问放过", "bonus_agent/agent.py", 'MUST_FIX = ("抵扣 ",)', "MUST_FIX = ()"),
 ("检查：参数没用上不拦", "bonus_agent/agent.py", "        if miss:\n            raise ToolError(f\"台账里这些参数", "        if False:\n            raise ToolError(f\"台账里这些参数"),
 ("验算：finish 不查独立验算", "bonus_agent/agent.py", "        left = self._verify_gaps()\n        if left:\n            raise ToolError(\"独立验算还没全部通过", "        left = []\n        if left:\n            raise ToolError(\"独立验算还没全部通过"),
 ("验算：允许把结果写成数字", "bonus_agent/agent.py", "        if loose:", "        if False:"),
 ("验算：尾差放宽到 1 元", "bonus_agent/agent.py", "if has_tail and abs(mine - final) <= Decimal(\"0.01\")", "if abs(mine - final) <= Decimal(\"1\")"),
 ("Excel：查找键不转文本", "bonus_core/dsl/excel_formula.py", "out = f\"IF(({x}&\\\"\\\")={kc},{vc},{out})\"", "out = f\"IF({x}={kc},{vc},{out})\""),
 ("Excel：缓存值舍入用 Python round", "bonus_core/report/xlcalc.py", "    return float(Decimal(format(x, \".15g\")).quantize(q, ROUND_HALF_UP))", "    return round(x, n)"),
 ("Excel：不写缓存值", "bonus_core/report/excel.py", "    xlcalc.fill_cached(path)", "    pass"),
 ("JSON：不修拆开的对象", "bonus_agent/agent.py", "                       *_split_objects(raw)):", "                       ):"),
 ("失败后下一轮不说原因", "bonus_agent/agent.py", "    if last and last.get(\"status\") == \"FAILED\" and last.get(\"error\"):", "    if False:"),
]
out = []
for name, f, old, new in M:
    work = Path(tempfile.gettempdir()) / "bonus_mutation_repo"
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(REPO, work, ignore=shutil.ignore_patterns(".git", "__pycache__", "out", "*.pyc"))
    p = work / f
    s = p.read_text(encoding="utf-8")
    if s.count(old) != 1:
        out.append((name, "锚点不唯一/找不到", s.count(old))); print(out[-1], flush=True); continue
    p.write_text(s.replace(old, new), encoding="utf-8")
    t = time.time()
    r = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"], cwd=work,
                       capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(work)})
    caught = r.returncode != 0
    first = next((l for l in r.stdout.splitlines() if l.startswith("FAILED")), "")
    out.append((name, "发现" if caught else "没发现", first[:150], round(time.time() - t)))
    print(out[-1], flush=True)
print(f"发现 {sum(1 for o in out if o[1] == '发现')}/{len(out)}")
