"""Prepare isolated model inputs and a frozen acceptance baseline. No network calls."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path

import yaml


CONFIRMATIONS='''# 本批范围与财务确认
仅处理2026年9月，不验收1—8月；部分历史材料已经截断移除。历史数据仅用于本期所需累计指标。
补充说明文档是最新规则材料，以其为主。输入与参考材料都可能有错误，不猜测修正。
来源冲突按最新业务版本，不按复制时间。区域以1112版底稿中的原始数据为主。
平均出货按实际有出货的月份平均，不按总月数。
库存与逾期余额业务日期目前无法确认，先保留核验草稿，不得声明正式可发放。
客户预算全空行不参与核算，这是已答复的问题，无需重复询问。
未明确的新口径保持待确认，不自动确认总额，不绕过独立复核、来源或数据检查。
'''


def verify(destination):
    manifest=json.loads((destination/'baseline_manifest.json').read_text(encoding='utf-8'))
    actual={str(p.relative_to(destination)) for directory in ('materials','acceptance')
            for p in (destination/directory).iterdir() if p.is_file()}
    if actual!=set(manifest):
        raise ValueError('Baseline file population changed')
    for relative,digest in manifest.items():
        path=(destination/relative).resolve()
        if destination.resolve() not in path.parents:
            raise ValueError('Manifest path leaves the baseline directory')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise ValueError(f'Baseline fingerprint changed: {relative}')
    return True


def prepare(materials, repaired, expected, history_log, destination):
    if destination.exists():
        raise ValueError('Use a fresh regression directory; never overwrite a previous baseline')
    # No parent/child overlap with any source directory.
    target=destination.resolve()
    for source in (materials,repaired,expected.parent,history_log.parent):
        origin=source.resolve()
        if target==origin or origin in target.parents or target in origin.parents:
            raise ValueError('Regression directory must be separate from sources')
    validation=json.loads((repaired/'validation.json').read_text(encoding='utf-8'))
    if validation.get('status')!='PASS' or validation.get('exported_artifact_replay')!='PASS':
        raise ValueError('Baseline must have passed acceptance and artifact replay')
    acceptance=destination/'acceptance'
    model_inputs=destination/'materials'
    acceptance.mkdir(parents=True)
    model_inputs.mkdir()
    for file in materials.iterdir():
        if file.is_file():
            shutil.copy2(file,model_inputs/file.name)
    log=json.loads(history_log.read_text(encoding='utf-8'))
    guide=next(s for s in log['steps'] if s['tool']=='read_document' and '场景说明' in str(s['args']))
    content=guide['result']
    if content.startswith('——'):
        content=content.split('\n',2)[2]
    (model_inputs/'场景说明（提成奖金计算）.md').write_text(content,encoding='utf-8')
    (model_inputs/'本批范围与财务确认.md').write_text(CONFIRMATIONS,encoding='utf-8')
    for name in ('rules.yaml','normalized.json','result.json','validation.json'):
        shutil.copy2(repaired/name,acceptance/name)
    shutil.copy2(expected,acceptance/'expected.xlsx')
    suite={'scenarios':[{'name':'september_isolated','inputs':'materials','expected':'acceptance/expected.xlsx'}]}
    (destination/'suite.yaml').write_text(yaml.safe_dump(suite,allow_unicode=True,sort_keys=False),encoding='utf-8')
    manifest={str(p.relative_to(destination)):hashlib.sha256(p.read_bytes()).hexdigest()
              for base in (model_inputs,acceptance) for p in sorted(base.iterdir()) if p.is_file()}
    (destination/'baseline_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'materials':len(list(model_inputs.iterdir())),'baseline_files':len(list(acceptance.iterdir())),
            'business_status':'DRAFT','manifest':str(destination/'baseline_manifest.json')}


if __name__=='__main__':
    p=argparse.ArgumentParser()
    for name in ('materials','repaired','expected','history-log','destination'):
        p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args()
    print(json.dumps(prepare(a.materials,a.repaired,a.expected,a.history_log,a.destination),ensure_ascii=False))
