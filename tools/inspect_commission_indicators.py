"""Inspect saved formula tasks locally, without updating the case or calling a model."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

from bonus_agent import case
from bonus_agent.agent import ToolError, Workspace
from bonus_agent.auto import build_inventory
from bonus_agent.construction import TaskBoard
from bonus_agent.llm import FakeLLM
from bonus_core.data.period import Period


def _originals(rnd):
    manifest = rnd.get('files', {})
    sources = rnd.get('source_inputs', [])
    if not manifest or not sources:
        raise ValueError('原轮缺 files 或 source_inputs，无法核对全部材料指纹')
    paths = []
    for source in sources:
        path = Path(source['path'])
        name = source['name']
        if path.name != name or not source.get('hash') or manifest.get(name) != source['hash']:
            raise ValueError(f'{name} 在原轮来源清单和指纹表中的记录不一致')
        if not path.is_file():
            raise FileNotFoundError(f'缺少原轮材料 {path}')
        paths.append(path)
    changes = case.file_changes(manifest, case.fingerprint(paths))
    if any(changes.values()):
        raise ValueError(f'原轮材料指纹不匹配 {changes}')
    return paths


def inspect_case(out):
    out = Path(out)
    case_file = out / case.CASE_FILE
    if not case_file.is_file():
        return {'status': 'CASE_UNAVAILABLE', 'issues': [f'缺少案卷文件 {case_file}']}
    obj = case.load_case(out)
    if not obj or obj.get('_recovery') or not obj.get('rounds') or not obj.get('workspace'):
        return {'status': 'CASE_UNAVAILABLE', 'issues': ['缺少完整的原轮工作区或已转入历史快照恢复']}
    rnd = obj['rounds'][-1]
    try:
        paths = _originals(rnd)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        return {'status': 'INPUT_INVALID', 'issues': [str(exc)]}
    ws = Workspace(FakeLLM([]), build_inventory(paths))
    if ws._source_version != rnd['files']:
        return {'status': 'INPUT_INVALID', 'issues': [f'读入材料期间指纹变化 {case.file_changes(rnd["files"], ws._source_version)}']}
    case.restore(ws, obj['workspace'])
    ws.period = Period.parse(rnd.get('period'))
    board = TaskBoard(ws)
    info = {'round': rnd.get('n'), 'period': rnd.get('period'), 'source_files': len(paths)}
    if not ws._flow().has_plan() or not board.state['tasks']:
        return {**info, 'status': 'PLAN_UNAVAILABLE', 'issues': ['缺少与当前材料一致的核算计划或公式任务']}
    try:
        preparation = ws.prepare_data()
    except (ToolError, ValueError) as exc:
        return {**info, 'status': 'DATA_UNAVAILABLE', 'issues': [str(exc)]}
    if preparation['status'] != 'PASS':
        return {**info, 'status': 'DATA_UNAVAILABLE', 'issues': preparation.get('issues', [])}

    statuses = board.refresh()
    for scheme in dict.fromkeys(spec['scheme'] for spec in board.state['tasks'].values()):
        board.inspect_indicators(scheme, statuses)
    statuses = board.statuses()
    indicators, levels, failed = [], Counter(), False
    for task, status in statuses.items():
        current = board.state['reports'].get(task, {})
        current = current if current.get('version') == status['version'] else {}
        confidence = status['confidence']
        levels[confidence['level']] += 1
        row = {'task': task, 'target': confidence['target'], 'version': status['version'],
               'task_status': 'FAIL' if current.get('status') == 'FAIL' else status['status'], 'confidence': confidence,
               'findings': status.get('indicator_findings', []), 'issues': current.get('issues', [])}
        if current.get('status') == 'FAIL' or confidence['level'] == 'CONFLICT':
            owner = current.get('execution', {}).get('repair', {}).get('task', task)
            row['repair_order'] = board.repair_order(owner, statuses)
            failed = True
        indicators.append(row)
    _originals(rnd)
    return {**info, 'status': 'FINDINGS' if failed else 'INSPECTED',
            'confidence_distribution': dict(sorted(levels.items())), 'indicators': indicators}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('case_dir', type=Path, help='包含 case.json 的案卷输出目录')
    args = parser.parse_args(argv)
    try:
        result = inspect_case(args.case_dir)
    except Exception as exc:
        # A corrupt workbook, case snapshot, or engine error still needs a machine-readable diagnostic.
        result = {'status': 'INSPECTION_FAILED', 'issues': [f'{type(exc).__name__}: {exc}']}
    output = json.dumps(result, ensure_ascii=False, indent=2)
    sys.stdout.buffer.write((output + '\n').encode('utf-8'))
    return 0 if result['status'] == 'INSPECTED' else 1


if __name__ == '__main__':
    raise SystemExit(main())
