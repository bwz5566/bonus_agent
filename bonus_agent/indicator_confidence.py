"""Summarize current indicator evidence for repair triage."""

from collections.abc import Iterable, Mapping


MISSING_EVIDENCE = {
    'rule_source': '缺少当前指标的规则原文核验记录',
    'grain': '缺少按业务对象、关联键和取数粒度的核验记录',
    'source_period': '缺少当前指标的原始来源和实际取数期间的核验记录',
    'execution': '缺少当前版本在实际对象上的求值记录',
    'independent': '缺少按当前对象和口径进行的独立核验记录',
}

_STATES = {'VERIFIED', 'PARTIAL', 'CONFLICT', 'UNKNOWN'}
_REPAIR_ORDER = ('CONFLICT', 'UNKNOWN', 'LOW', 'SUPPORTED', 'HIGH')


def assess_indicator(
    target: str,
    evidence: Mapping[str, Mapping[str, str]] | None = None,
    *,
    counterexamples: Iterable[str] = (),
) -> dict:
    """Evaluate records selected by the caller for this target and version.

    VERIFIED requires an applicable check with an identifiable supporting record.
    A rule declaration, a local task PASS, or an unverified model assertion does
    not constitute observed execution or independent verification.
    """
    evidence = {} if evidence is None else evidence
    if not isinstance(target, str) or not target.strip():
        raise ValueError('可信度记录需要有效的指标目标')
    extra = set(evidence).difference(MISSING_EVIDENCE)
    if extra:
        raise ValueError(f'未知可信度维度 {sorted(extra)}')

    dimensions = {}
    for name, missing_reason in MISSING_EVIDENCE.items():
        record = evidence.get(name, {})
        state = record.get('state', 'UNKNOWN')
        if state not in _STATES:
            raise ValueError(f'{name} 的证据状态无效 {state!r}')
        reason = record.get('reason', '')
        if not isinstance(reason, str) or (state != 'UNKNOWN' and not reason.strip()):
            raise ValueError(f'{name} 的 {state} 结论缺少证据说明')
        dimensions[name] = {'state': state, 'reason': reason.strip() or missing_reason}

    if isinstance(counterexamples, str):
        counterexamples = (counterexamples,)
    examples = list(dict.fromkeys(item.strip() for item in counterexamples if item.strip()))
    gaps = [name for name, record in dimensions.items() if record['state'] != 'VERIFIED']
    states = {record['state'] for record in dimensions.values()}
    if examples or 'CONFLICT' in states:
        level = 'CONFLICT'
    elif states == {'UNKNOWN'}:
        level = 'UNKNOWN'
    elif not gaps:
        level = 'HIGH'
    elif gaps == ['independent']:
        level = 'SUPPORTED'
    else:
        level = 'LOW'
    return {'target': target, 'level': level, 'dimensions': dimensions,
            'counterexamples': examples, 'gaps': gaps}


def repair_priority_key(assessment: Mapping) -> tuple[int, int, int]:
    """Lower keys are inspected first within an affected dependency closure."""
    states = [record['state'] for record in assessment['dimensions'].values()]
    return (_REPAIR_ORDER.index(assessment['level']),
            states.count('VERIFIED'), states.count('PARTIAL'))
