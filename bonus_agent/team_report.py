"""Read-only comparisons; never infer business correctness from process success."""
import json
from pathlib import Path

from bonus_agent.recovery import fingerprint


def compare_runs(left, right):
    def read(path):
        p = Path(path)
        obj = json.loads((p / 'case.json' if p.is_dir() else p).read_text(encoding='utf-8'))
        rounds = obj.get('rounds', [])
        latest = rounds[-1] if rounds else {}
        workspace = obj.get('workspace', {})
        signature = {k:latest.get(k) for k in ('files','period','model','checker','limits')}
        signature['context'] = fingerprint([workspace.get('said'),
            [(q.get('text'),q.get('answer')) for q in workspace.get('questions', []) if q.get('answer') is not None]])
        usage = {k:sum(r.get('usage',{}).get(k,0) for r in rounds)
                 for k in ('calls','prompt_tokens','completion_tokens','cached_tokens','secs','steps')}
        return {'mode':latest.get('execution_mode','staged'), 'status':latest.get('status'),
                'error':latest.get('error'), 'usage':usage, 'signature':signature,
                'financial_digest':latest.get('financial_digest'),
                'task_counts':{s:sum(t.get('status') == s for t in workspace.get('workflow',{}).get('team',{}).get('tasks',{}).values())
                               for s in ('PASSED','BLOCKED','STALE','RUNNING','PENDING','READY')}}
    a,b = read(left),read(right)
    missing = [k for k in ('files','model','limits') if not a['signature'].get(k) or not b['signature'].get(k)]
    differences = [k for k in a['signature'] if a['signature'][k] != b['signature'][k]]
    both = a['financial_digest'] is not None and b['financial_digest'] is not None
    equal = a['financial_digest'] == b['financial_digest'] if both else None
    return {'left':a,'right':b,'comparable':not missing and not differences,
            'missing_comparison_metadata':missing,'different_conditions':differences,
            'financial_outputs_equal':equal,
            'business_acceptance':'UNVERIFIED',
            'conclusion':'同结果不证明业务正确；须另行全量验收。单组比较不证明稳定性提升。'}
