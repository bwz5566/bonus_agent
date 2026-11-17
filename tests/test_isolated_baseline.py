import json

import pytest

from bonus_agent.regress import load_suite
from tools.prepare_september_regression import prepare, verify


def baseline(tmp_path):
    materials=tmp_path/'source';materials.mkdir()
    (materials/'policy.md').write_text('rules',encoding='utf-8')
    repaired=tmp_path/'repaired';repaired.mkdir()
    (repaired/'validation.json').write_text(json.dumps({'status':'PASS','exported_artifact_replay':'PASS'}))
    for name in ('rules.yaml','normalized.json','result.json'):
        (repaired/name).write_text('isolated acceptance artifact')
    private=tmp_path/'private';private.mkdir()
    expected=private/'expected.xlsx';expected.write_bytes(b'expected-only')
    history=private/'history.json'
    history.write_text(json.dumps({'steps':[{'tool':'read_document','args':{'file':'场景说明.md'},'result':'—— guide ——\n\nsource guide'}]}),encoding='utf-8')
    out=tmp_path/'regression'
    prepare(materials,repaired,expected,history,out)
    return out


def test_baseline_keeps_answers_out_of_model_inputs_and_detects_tampering(tmp_path):
    root=baseline(tmp_path)
    assert verify(root)
    scenario=load_suite(root/'suite.yaml')[0]
    files=scenario.materials()
    assert all(p.parent==root/'materials' for p in files)
    assert scenario.expected not in files
    assert not any(p.name in ('rules.yaml','normalized.json','result.json','expected.xlsx') for p in files)
    (root/'materials'/'policy.md').write_text('changed',encoding='utf-8')
    with pytest.raises(ValueError,match='fingerprint changed'):verify(root)


def test_baseline_rejects_unrecorded_material_and_existing_destination(tmp_path):
    root=baseline(tmp_path)
    (root/'materials'/'extra.md').write_text('unrecorded',encoding='utf-8')
    with pytest.raises(ValueError,match='population changed'):verify(root)
    with pytest.raises(ValueError,match='fresh regression'):
        prepare(root,root,root/'answer',root/'history',root)
