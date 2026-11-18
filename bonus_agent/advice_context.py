"""Bounded, read-only evidence for an optional adviser; never a policy decision."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


MAX_CONTEXT_CHARS = 9500


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _dump(value):
    return value.model_dump(mode='json', exclude_defaults=True) if hasattr(value, 'model_dump') else value


def build_advice_context(ws, steps, report=None):
    """Use the current validated report and already-read physical source material.

    No tools, data preparation, source discovery, approvals or writes are invoked.
    A read receipt proves that content was available, not that its interpretation
    or the accounting scope has been approved.
    """
    ws._check_source_version()
    saved = ws.workflow.get('formula_report', {})
    phase = (report or saved.get('report', {})).get('phase', 'basis')
    current = ws._flow().compile_feedback(phase) if ws.staged else None
    valid = current is not None and (report is None or current == report)
    packet = {
        'status': 'CURRENT' if valid else 'NO_CURRENT_REPORT',
        'phase': phase, 'period': str(ws.period),
        'version': saved.get('phase_version') or saved.get('version'),
        'current_failure': [], 'scope_observations': [], 'current_nodes': [],
        'source_excerpts': [], 'source_coverage': [], 'read_receipts': [], 'omitted': [],
        'limits': {
            'max_chars': MAX_CONTEXT_CHARS, 'truncated': False,
            'note': '仅为有界相关证据；省略或未读不等于材料缺失。读取回执不代表业务批准。'},
        'interpretation': (
            '先按已核对原文判断反例对象是否属于本期核算范围，再判断是否真的缺数据。'
            '有明确原文时可以修正population_from、期间或关系；同时验证历史依赖仍可达，不可为消除报错任意删行。'
            '覆盖数量不能批准范围；未证明对象应在本期时，不得要求该历史反例永远保留。'
            '对象显示文本或截断摘要不能证明拼接键格式错误；必须比较实际完整键、映射及程序匹配证据。'
            '缺少这些证据时只指出下一条证据，不编造根因、缺失政策或强制验收口径。'),
    }
    if not valid:
        return packet
    packet['report_summary'] = {
        'status': current.get('status'), 'issue_count': len(current.get('issues', [])),
        'static_checks_complete': current.get('static_checks_complete') is True,
        'selection': '仅聚焦首个失败方案的第一个完整反例；其他诊断不代表已通过',
    }

    omitted = []

    def add(section, value, *, reserve=0, address=None):
        packet[section].append(value)
        if len(_json(packet)) + reserve <= MAX_CONTEXT_CHARS - 600:
            return True
        packet[section].pop()
        omitted.append({'section': section, **({'read_call': address} if address else {})})
        return False

    focus = []
    for scheme, preflight in current.get('preflight', {}).items():
        if preflight.get('status') != 'FAIL':
            continue
        call = {'tool': 'read_workspace', 'args': {'section': 'checks', 'name': 'preflight:' + scheme}}
        for issue in preflight.get('issues', [])[:1]:
            # The first actual counterexample is complete, not the tool summary's
            # truncated embedded JSON. Scope has its own bounded slot below.
            failure = {key: value for key, value in issue.items() if key != 'scope_context'}
            add('current_failure', {'scheme': scheme, 'diagnostic': failure, 'read_call': call}, reserve=4300, address=call)
            scope = issue.get('scope_context') or preflight.get('scope_context') or {}
            focus.append((scheme, issue, scope))
        if focus:
            break
    if not focus:
        # Static-only diagnostics remain explicit; no runtime or scope inference.
        for issue in current.get('issues', [])[:2]:
            add('current_failure', {'diagnostic': issue}, reserve=4300,
                address={'tool': 'read_workspace', 'args': {'section': 'checks'}})

    targets, datasets, terms, source_names, mapped_files = [], [], set(), set(), set()
    drafts = {scheme: ws._merged(scheme) for scheme in ws.schemes}
    from bonus_core.dsl.parser import ExprError, names, parse

    def references(value):
        try:
            return names(parse(value.get('expr') or value.get('applies_when') or '0'))
        except ExprError:
            return set()

    def target(scheme, item):
        # Runtime frames use a plain variable name; static diagnostics use a
        # canonical path. Neither source text nor prose supplies a node identity.
        if not isinstance(item, str):
            return
        draft = drafts.get(scheme, {})
        for kind in ('variables', 'tables'):
            for name in draft.get(kind, {}):
                if item == name or item == f'{kind}.{name}' or item.startswith(f'{kind}.{name}.'):
                    identity = (scheme, kind, name)
                    if identity not in targets:
                        targets.append(identity)

    if not focus:
        for issue in current.get('issues', [])[:4]:
            if isinstance(issue, dict):
                item = issue.get('where') or issue.get('target') or ''
                schemes = [issue['scheme']] if issue.get('scheme') in drafts else list(drafts)
            else:
                match = re.search(r'\(((?:variables|tables)\.[^()]+)\)\s*$', str(issue))
                item = match.group(1) if match else ''
                named = re.match(r'^方案「([^」]+)」', str(issue))
                schemes = [named.group(1)] if named and named.group(1) in drafts else list(drafts)
            for scheme in schemes:
                target(scheme, item)
            if targets:
                # One concrete failure plus its actual dependencies is more
                # useful than several neighboring tables from the same report.
                break

    def dataset(name):
        if name and name in ws.datasets and name not in datasets:
            datasets.append(name)

    for scheme, issue, scope in focus:
        if issue.get('rule_item'):
            target(scheme, issue['rule_item'])
        elif issue.get('dependency_chain'):
            target(scheme, issue['dependency_chain'][0].get('rule_item'))
        for coverage in issue.get('coverage', []):
            dataset(coverage.get('dataset'))
        for comparison in scope.get('comparisons', [])[:1]:
            dataset(comparison.get('parent'))
            dataset(comparison.get('detail'))
            impact = comparison.get('hypothetical_population_from', {})
            for item in impact.get('other_details_losing_parent', []):
                dataset(item.get('dataset'))
            add('scope_observations', {'scheme': scheme, 'status': scope.get('status'),
                'comparison': comparison, 'notes': scope.get('notes', []),
                'comparison_count': scope.get('comparison_count'), 'omitted': scope.get('omitted', 0)},
                reserve=2600, address={'tool': 'read_workspace', 'args': {'section': 'checks', 'name': 'preflight:' + scheme}})

    seen_targets = set()
    for scheme, kind, name in targets:
        if (scheme, kind, name) in seen_targets:
            continue
        seen_targets.add((scheme, kind, name))
        draft = drafts[scheme]
        value = draft.get(kind, {}).get(name)
        if value is None:
            continue
        value = _dump(value)
        path = scheme + '::/' + kind + '/' + name
        add('current_nodes', {'target': path, 'definition': value}, reserve=2200,
            address={'tool': 'read_workspace', 'args': {'section': 'rules', 'name': path}})
        terms.add(name)
        dataset(value.get('scope'))
        referenced = references(value)
        if kind == 'tables':
            for caller, definition in draft.get('variables', {}).items():
                if name in references(_dump(definition)):
                    target(scheme, 'variables.' + caller)
        for referenced_name in sorted(referenced, key=lambda n: (n not in draft.get('variables', {}), n)):
            target(scheme, referenced_name)
            for owner, definition in ws.datasets.items():
                if referenced_name in definition.fields:
                    dataset(owner)
        for source in value.get('sources', []):
            if source.get('doc'):
                source_names.add(source['doc'])
        if len(seen_targets) >= 3:
            break
    # Scope names may be allocation aliases, so use their exact declared keys.
    referenced_scopes = {(_dump(drafts[scheme].get(kind, {}).get(name, {}))).get('scope')
                         for scheme, kind, name in seen_targets}
    for scheme in dict.fromkeys(item[0] for item in targets):
        for level in drafts[scheme].get('allocation', []):
            if level.get('level') not in referenced_scopes:
                continue
            for name, spec in ws.datasets.items():
                if getattr(spec, 'key', None) == level.get('key'):
                    dataset(name)
    for name in datasets[:4]:
        spec, mapping = ws.datasets[name], ws.mapping.get(name)
        if mapping is None:
            continue
        definition = _dump(spec)
        mapped = _dump(mapping)
        mapped_files.add(Path(str(mapped.get('file', ''))).name)
        terms.add(name)
        terms.update(definition.get('fields', {}))
        terms.update(str(v) for v in mapped.get('columns', {}).values())
        add('current_nodes', {'dataset': name, 'definition': definition, 'mapping': mapped}, reserve=1600,
            address={'tool': 'read_workspace', 'args': {'section': 'mapping', 'name': name}})

    # Keep successful, relevant reads across the entire available history. Their
    # exact arguments and content hash survive the four-step recency window.
    receipts, source_reads = [], {}
    for index, step in enumerate(steps, 1):
        if not step.ok:
            continue
        args = step.args
        name = args.get('name', '')
        file = Path(str(args.get('file', ''))).name
        related_target = any(
            name in (scheme + '::/' + kind + '/' + target, scheme + '::/' + kind + '/' + target + '/expr',
                     scheme + '::/' + kind + '/' + target + '/applies_when')
            or (args.get('section') == kind and name == target and args.get('scheme') == scheme)
            for scheme, kind, target in seen_targets)
        relevant = (args.get('dataset') in datasets or name in datasets or related_target
                    or step.tool == 'read_sheet' and file in mapped_files)
        if step.tool == 'read_document' and file:
            source_reads.setdefault(file, []).append((index, step))
            if file in source_names:
                relevant = True
        if step.tool in ('read_workspace', 'read_document', 'read_sheet', 'query_dataset') and relevant:
            record = {'step': index, 'tool': step.tool, 'args': args, 'result_hash': _hash(step.result),
                      'meaning': '已成功读取；旧读取不代替上方当前定义或完整预检'}
            try:
                parsed = json.loads(step.result)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                record['page_complete'] = parsed.get('next_call') is None and parsed.get('next_offset') is None
                if step.tool == 'query_dataset':
                    record['observed_query'] = {k: parsed[k] for k in ('rows', 'value', 'scope') if k in parsed}
            receipts.append(record)

    docs = {doc.name: doc for doc in ws.inv.docs}
    for name in sorted(source_names)[:3]:
        add('source_coverage', {'file': name, 'loaded_material': name in docs,
                              'successful_read_recorded': name in source_reads,
                              'pinned_in_material': name in ws.pinned,
                              'meaning': '未读或不在此包中不代表缺文件；不读取虚拟财务答复或外部结果'})
    for doc in ws.inv.docs:
        file = doc.name
        pinned = file in ws.pinned
        reads = source_reads.get(file, [])
        if not pinned and not reads:
            continue
        if source_names and file not in source_names:
            continue
        # No new files, result artifacts, arbitrary paths or tool calls. A folded
        # page is eligible only when it was actually read or pinned as content.
        material = ws._render_doc(doc, folded=True).replace('\r\n', '\n')
        pinned_material = ws._render_doc(doc).replace('\r\n', '\n') if pinned else ''
        blocks = [block.strip() for block in re.split(r'\n\s*\n', material) if block.strip()]
        eligible = []
        for order, block in enumerate(blocks):
            read = next(((index, step) for index, step in reversed(reads)
                         if block in step.result.replace('\r\n', '\n')), None)
            pinned_block = pinned and block in pinned_material
            if not pinned_block and read is None:
                continue
            score = sum(term in block for term in terms if len(term) >= 2)
            if score or file in source_names:
                eligible.append((-score, order, block, read, pinned_block))
        budget = 2000
        for _, order, block, read, pinned_block in sorted(eligible)[:4]:
            if len(block) > budget:
                omitted.append({'section': 'source_excerpts', 'file': file,
                                'reason': '完整段落超过剩余空间，未截断其条件'})
                continue
            index, step = read if read else (None, None)
            record = {'file': file, 'paragraph': order + 1, 'text': block,
                      'current_material_hash': _hash(material), 'verified_in_current_material': True,
                      'read_receipt': {'pinned_in_material': bool(pinned_block), 'step': index,
                                       'args': step.args if step else None},
                      'is_excerpt': True, 'not_a_policy_approval': True}
            if add('source_excerpts', record, reserve=300):
                budget -= len(block)

    seen = set()
    for receipt in reversed(receipts):
        identity = _json([receipt['tool'], receipt['args']])
        if identity in seen:
            continue
        seen.add(identity)
        if len(packet['read_receipts']) >= 8:
            omitted.append({'section': 'read_receipts', 'reason': '其余已读记录省略'})
            break
        add('read_receipts', receipt)
    packet['omitted'] = omitted[:4]
    packet['limits']['truncated'] = bool(omitted)
    packet['limits']['omitted_count'] = len(omitted)
    # The allowance above includes this bounded omission ledger.
    if len(_json(packet)) > MAX_CONTEXT_CHARS:
        raise ValueError('顾问证据包超过上限；未调用模型，不能静默截断当前诊断')
    return packet
