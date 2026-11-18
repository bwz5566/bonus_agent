"""Measure resolution of validation problems, rather than successful tool responses."""
from dataclasses import dataclass, field
import copy
import json
import re

from bonus_agent.recovery import fingerprint


def discover(step, seen):
    """Credit finite evidence or one first concrete compile diagnosis per phase.

    ``seen`` is persisted by RecoveryJournal. The diagnostic allowance is keyed
    by phase, deliberately not by issue wording, draft description or version:
    editing prose or restarting must not mint another recovery allowance.
    Actual fixes remain measured separately by ``ProgressWatch.observe``.
    """
    diagnostic = _compile_diagnostic(step)
    if diagnostic:
        key = fingerprint(('first_compile_diagnostic', diagnostic))
        if key in seen:
            return False
        seen.add(key)
        return True
    if not step.ok or not step.tool.startswith('read_'):
        return False
    keys = []
    if step.tool == 'read_workspace':
        try:
            value = json.loads(step.result)
        except (ValueError, TypeError):
            return False
        section = value.get('section')
        if section == 'checks':
            return _discover_checks(value, seen)
        for item in value.get('items', []):
            nested = item.get('value') if isinstance(item.get('value'), dict) else item
            name = nested.get('id') or nested.get('name') or nested.get('dataset') or nested.get('scheme')
            if name:
                keys.append((section, item.get('scheme'), item.get('field'), name))
        fragment = value.get('item_fragment', {})
        text = fragment.get('text', '')
        if text and (len(text) >= 1000 or fragment.get('next_detail_offset') is None):
            keys.append((section, step.args.get('scheme'), step.args.get('name'),
                         value.get('offset'), fragment.get('offset') // 1000))
    elif step.tool in ('read_document','read_parameter_table','read_sheet') and step.result:
        keys.append((step.tool, step.args.get('file'), step.args.get('page'), step.args.get('sheet')))
    fresh = {fingerprint(key) for key in keys} - seen
    seen.update(fresh)
    return bool(fresh)


def _discover_checks(page, seen):
    """Reading a new diagnostic record is finite evidence, not a repaired issue.

    The checks reader has no record name for legacy string diagnostics. Use
    their stable subject (parameter/cell/clause), never a page offset, report
    version, suggested fix or message body. Unknown formats get one credit per
    existing record slot instead of treating arbitrary prose as new evidence.
    """
    if page.get('status') in ('NOT_RUN', 'STALE', 'ERROR', 'DEFERRED'):
        return False
    discovered = False
    for item in page.get('items', []):
        if not isinstance(item, dict) or item.get('kind') not in ('issue', 'warning', 'preflight'):
            continue
        kind = item['kind']
        # New structured runtime diagnostics have machine-owned locations.
        location = {k: item[k] for k in ('code', 'where', 'node_id', 'rule_item', 'dataset', 'field', 'target', 'source', 'id')
                    if isinstance(item.get(k), str) and item[k]}
        slot = (fingerprint(('checks', kind, 'slot', item['index']))
                if isinstance(item.get('index'), int) and not isinstance(item['index'], bool) and item['index'] >= 0 else None)
        prose = False
        if location:
            key = ('checks', kind, location)
        else:
            message = item.get('message')
            if not isinstance(message, str) or not message.strip():
                continue
            message = message.strip()
            clause = re.match(r'^(C\d+)(?=[「：:\s])', message)
            # Single colons delimit subjects; double colons belong to stable
            # source-cell IDs or scheme-qualified rule paths.
            pieces = re.split(r'：|(?<!:):(?!:)', message, maxsplit=1)
            subject = clause.group(1) if clause else (pieces[0].strip() if len(pieces) == 2 else '')
            if subject:
                key = ('checks', kind, 'subject', subject)
                # Source-cell and clause IDs are stable records. Other legacy
                # headings are prose: changing a heading in an already-read
                # slot must not manufacture another discovery.
                prose = not clause and not re.search(r'::P\d+(?:\.\w+)+$', subject)
            elif slot:
                key = ('checks', kind, 'slot', item['index'])
            else:
                continue
        key = fingerprint(key)
        fresh = key not in seen and (not prose or (slot is not None and slot not in seen))
        discovered = discovered or fresh
        seen.add(key)
        if slot:
            seen.add(slot)
    # Partial JSON fragments cannot establish a new record identity. In
    # particular changing detail_offset or lengthening prose earns no credit.
    return discovered


def _compile_diagnostic(step):
    """Only executed compiler reports qualify; cached reads and tool errors do not."""
    automatic = (step.tool == '（自动复核）' and step.args.get('automatic') is True
                 and step.args.get('tool') == 'compile_formulas')
    if step.tool != 'compile_formulas' and not automatic:
        return None
    try:
        # Tool output can end with a program reminder after the JSON object.
        report, _ = json.JSONDecoder().raw_decode(step.result.lstrip())
    except (TypeError, ValueError, AttributeError):
        return None
    if not isinstance(report, dict) or report.get('status') != 'FAIL':
        return None
    phase = report.get('phase') or (step.args.get('phase') if automatic else None)
    if phase not in ('basis', 'all'):
        return None
    issues = report.get('issues')
    if not isinstance(issues, list) or not any(
        (isinstance(item, str) and item.strip())
        or (isinstance(item, dict) and isinstance(item.get('message'), str) and item['message'].strip())
        for item in issues
    ):
        return None
    return phase


def compile_objects(report, fact_names=()):
    """Stable program-owned objects, never diagnostic prose or error counts."""
    if not isinstance(report, dict) or report.get('phase') not in ('basis', 'all'):
        return {}
    phase, facts, out = report['phase'], set(fact_names), {}
    for issue in report.get('issues', []):
        message = issue.get('message', '') if isinstance(issue, dict) else str(issue)
        runtime = re.match(r'^方案「([^」]+)」基础求值预检[：:]', message)
        kind, scheme = 'static', ''
        if runtime:
            kind, scheme, identity = 'runtime', runtime.group(1), 'runtime:' + runtime.group(1)
        else:
            clause = re.match(r'^(C\d+)(?=[「：:\s])', message)
            subject = re.split(r'：|(?<!:):(?!:)', message, maxsplit=1)[0].strip()
            code = issue.get('code') if isinstance(issue, dict) else None
            target = (issue.get('target') or issue.get('where')) if isinstance(issue, dict) else None
            validator = re.match(r'^\[阻断\]\s+([A-Za-z_][\w.]*):.*\(([^()]+)\)\s*$', message, re.S)
            contract = re.match(r'^方案「([^」]+)」\s+((?:variables|tables)\.[A-Za-z_]\w*)[：:]\s*'
                                r'([A-Za-z_][\w.]*)(?=[，,：:\s]|$)', message)
            task = re.match(r'^construction\.task[：:]\s*(\S+?)未通过局部检查或已失效', message)
            if clause:
                identity = 'clause:' + clause.group(1)
            elif subject in facts or re.search(r'::P\d+(?:\.\w+)+$', subject):
                identity = 'fact:' + subject
            elif isinstance(code, str) and isinstance(target, str) and code and target:
                identity = 'check:' + json.dumps([issue.get('scheme', ''), code, target], ensure_ascii=False)
            elif validator:
                identity = 'check:' + json.dumps(list(validator.groups()), ensure_ascii=False)
            elif contract:
                identity = 'check:' + json.dumps([contract[1], contract[3], contract[2]], ensure_ascii=False)
            elif task:
                identity = 'task:' + task[1]
            else:
                # Legacy/free-text errors share one obligation. Rephrasing or
                # reducing their count is not proof of a resolved object.
                identity = 'unclassified'
        out[f'compile:{phase}:{identity}'] = {'phase': phase, 'kind': kind, 'scheme': scheme}
    return out


def compile_frontier(state, report, fact_names=()):
    """Keep masked obligations until their checker actually covers them again."""
    rows = state.setdefault('objects', {})
    if not isinstance(report, dict) or report.get('phase') not in ('basis', 'all') or report.get('status') not in ('PASS', 'FAIL'):
        return set(rows)
    phase = report['phase']
    current = compile_objects(report, fact_names)
    complete = (report.get('status') == 'PASS' or report.get('static_checks_complete') is True)
    complete = complete and report.get('issue_count', len(report.get('issues', []))) <= len(report.get('issues', []))
    for key, row in list(rows.items()):
        if row['phase'] != phase or key in current:
            continue
        if row['kind'] == 'runtime':
            covered = report.get('preflight', {}).get(row['scheme'], {}).get('status') == 'PASS'
        else:
            covered = complete
        if covered:
            del rows[key]
    rows.update(current)
    return set(rows)


@dataclass
class ProgressWatch:
    previous: set[str] = field(default_factory=set)
    stagnant: int = 0
    compile_state: dict = field(default_factory=dict)

    def observe(self, blockers, *, discovery=False, compile_report=None, fact_names=()):
        old_objects = set(self.compile_state.get('objects', {}))
        objects = compile_frontier(self.compile_state, compile_report, fact_names)
        current = {x for x in blockers if not x.startswith('compile:')} | objects
        resolved = old_objects - objects
        credited = set(self.compile_state.get('resolved', []))
        had_compile_history = bool(old_objects or credited)
        real_resolution = bool(resolved - credited)
        self.compile_state['resolved'] = sorted(credited | resolved)
        # While a compiler obligation exists, moving to another task or making
        # the report stale is not resolution. Only covered objects can clear it.
        ordinary_resolution = bool(self.previous - current) if not old_objects else False
        if discovery or real_resolution or ordinary_resolution or (
                not had_compile_history and (not current or not self.previous)):
            self.stagnant = 0
        elif current:
            self.stagnant += 1
        # An empty frontier permits later stages to proceed, but cannot refill
        # the allowance for reintroducing and fixing the same compiler object.
        self.previous = current
        return self.stagnant


def repair_scope(ws):
    """Identify only obligations explicitly omitted by the real basis projection.

    Unknown ownership, broken projections and shared basis dependencies remain
    active. This view schedules recovery; it never resolves an error or grants a
    receipt. Callers snapshot it at each tool boundary to preserve batch order.
    """
    if not ws.staged:
        return {'phase': 'all', 'deferred': []}
    from bonus_agent.construction import TaskBoard
    from bonus_agent.facts import target_parts
    try:
        board = TaskBoard(ws)
        phase = board.phase()
        if phase != 'basis':
            return {'phase': phase, 'deferred': []}
        view = ws._flow().basis_view()
        required = board.required('basis')
        active_facts = {fact.name for fact in view.facts}
        active_facts.update(row['fact']['name'] for row in view._phase_binding_evidence)
        deferred = {fact.name for fact in ws.facts} - active_facts
        deferred.update({clause.id for clause in ws.clauses} - {clause.id for clause in view.clauses})
        tasks = board.state['tasks']
        deferred.update(set(tasks) - required)
        # A target can be shared across schemes. Unqualified aliases are safe
        # only when every owner is outside the current dependency closure.
        owners = {}
        for ident, task in tasks.items():
            path = task['target']
            for alias in (path, '.'.join(target_parts(path)), task['scheme'] + '::' + path):
                owners.setdefault(alias, set()).add(ident)
        deferred.update(alias for alias, ids in owners.items() if not ids & required)
        return {'phase': phase, 'deferred': sorted(deferred)}
    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
        # Missing context is not evidence that an unresolved object may wait.
        return {'phase': 'basis', 'deferred': []}


def repair_deferred(scope, subject, phase=None):
    return bool(scope and scope.get('phase') == 'basis' and
                subject in scope.get('deferred', ()))


class RepairReadBudget:
    """Bound exploration of a concrete failed repair, not initial discovery.

    A new page is evidence, not resolution. Counts survive new queries, harmless
    writes and restarts; only a successful program check removes the obligation.
    Four reads leave room for the fact, source, target and mapping; eight allow
    follow-up evidence before stopping further model spending.
    """
    WARN = 4
    LIMIT = 8
    WRITES = {'set_rules', 'bind_facts', 'submit_formula_task', 'update_dataset',
              'define_dataset', 'record_facts', 'compile_table', 'compile_coefficient_table'}

    def __init__(self, state):
        self.state = state
        state.setdefault('records', {})
        state.setdefault('seen_compile', [])

    @property
    def active(self):
        return any(not row.get('deferred') for row in self.state['records'].values())

    @staticmethod
    def native_keys(native):
        return {fingerprint(('native', item['tool'], item.get('scheme'), obj))
                for item in native.values() if item['tool'] != 'compile_formulas'
                for obj in item.get('objects') or [item['tool']]}

    def validated_close(self, key, verification):
        """Archive a halted obligation only at an explicit successful check.

        Disappearing native entries still remove ordinary records in observe(),
        but absence alone never creates this proof or releases an old halt.
        """
        row = self.state['records'].get(key)
        if row is None or verification.get('status') != 'PASS':
            return
        marker = row.get('halt_marker') or {}
        if marker.get('id') and marker.get('record_key') == key and marker.get('subject') == row['subject']:
            self.state.setdefault('resolved_halts', {})[marker['id']] = {
                'halt': copy.deepcopy(marker), 'record': copy.deepcopy(row),
                'verification': copy.deepcopy(verification)}
        del self.state['records'][key]

    @staticmethod
    def _subject(message):
        # Keep source-cell IDs (which contain ::) intact. Do not key allowances
        # by query, description, generated remedy text or workspace version.
        return re.split(r'：|(?<!:):(?!:)', str(message), maxsplit=1)[0].strip()

    @staticmethod
    def _write_objects(step):
        if step.tool not in RepairReadBudget.WRITES or step.result.lstrip().startswith('no-op'):
            return set()
        args = step.args
        objects = {str(item.get('name') or item.get('id'))
                   for group in ('bindings', 'facts') for item in args.get(group, [])
                   if isinstance(item, dict) and (item.get('name') or item.get('id'))}
        for group in ('variables', 'tables'):
            objects.update(f'{group}.{name}' for name in args.get(group, {}) or {})
            objects.update(f'/{group}/{name}' for name in args.get(group, {}) or {})
        objects.update(key for key in ('pool', 'report', 'allocation', 'checks') if key in args)
        objects.update(str(args[key]) for key in ('name', 'task', 'dataset') if args.get(key))
        if step.tool == 'submit_formula_task':
            try:
                # The atomic tool reports its actual saved target; a task ID
                # alone does not match compiler diagnostics about that target.
                report, _ = json.JSONDecoder().raw_decode(step.result.lstrip())
                if isinstance(report, dict) and report.get('saved') is True and isinstance(report.get('target'), str):
                    from bonus_agent.facts import split_target, target_parts
                    _, path = split_target(report['target'])
                    objects.update((path, '.'.join(target_parts(path))))
            except (TypeError, ValueError):
                pass
        return objects

    @classmethod
    def message(cls, row):
        """Stable program-owned halt text, also used to identify legacy records."""
        action = ('bind_facts；目标公式缺分支时set_rules(bindings=...)；绑定已经一致时validate_parameters核验剩余缺口' if row['tool'] == 'bind_facts' else
                  'submit_formula_task（使用已有任务ID）/set_rules，然后程序局部检查')
        return (f'当前未解决对象「{row["subject"]}」已读取 {row["reads"]}/{cls.LIMIT} 次，'
                '换页面、换query、note、no-op或改描述不会增加额度。'
                f'程序反例：{row["issue"][:600]}\n'
                '已读位置：' + json.dumps(row['seen'], ensure_ascii=False) + '\n'
                f'下一步：仅提交该对象的{action}，按已核对原文修复并验证。'
                '需要补充证据时明确尚缺的原文位置；不能猜值、改无关公式或降低验证。')

    def observe(self, steps, native, report, read_tools, *, attempts=None, scope=None):
        records = self.state['records']
        native_keys = set()
        for item in native.values():
            # An initial compiler error is a first diagnosis, not a failed
            # repair attempt. The object-specific compile path below owns it.
            if item['tool'] == 'compile_formulas':
                continue
            for obj in item.get('objects') or [item['tool']]:
                key = fingerprint(('native', item['tool'], item.get('scheme'), obj))
                native_keys.add(key)
                row = records.setdefault(key, {'kind': 'native', 'reads': 0, 'seen': [], 'warned': False})
                row.update(subject=obj, issue=item['issue'], tool=item['tool'], phase=item.get('phase'))
        for key in list(records):
            if records[key]['kind'] == 'native' and key not in native_keys:
                del records[key]  # native feedback clears only validated successes

        phase = report.get('phase') if report else None
        if phase in ('basis', 'all') and report.get('status') in ('PASS', 'FAIL'):
            issues = {self._subject(message): str(message) for message in report.get('issues', [])}
            static_complete = report['status'] == 'PASS' or (
                report.get('static_checks_complete') is True
                and report.get('issue_count', len(issues)) <= len(report.get('issues', [])))
            for key, row in list(records.items()):
                if row['kind'] != 'compile' or (row['phase'] != phase and not
                                               (phase == 'all' and row['phase'] == 'basis')):
                    continue
                if repair_deferred(scope, row['subject'], row.get('phase')):
                    # Absence from a projected report is deferral, not proof of
                    # repair. Keep its earlier allowance for the full phase.
                    continue
                runtime = re.match(r'^方案「([^」]+)」基础求值预检[：:]', row['issue'])
                if runtime:
                    # Static errors can prevent the runtime check from running;
                    # even overall PASS can contain DEFERRED upstream-dependent
                    # preflights. Only this scheme's executed PASS resolves it.
                    if report.get('preflight', {}).get(runtime.group(1), {}).get('status') == 'PASS':
                        self.validated_close(key, {'status': 'PASS', 'kind': 'runtime', 'tool': 'compile_formulas',
                                                   'scheme': runtime.group(1), 'phase': phase,
                                                   'report_fingerprint': fingerprint(report)})
                elif static_complete and row['subject'] not in issues:
                    self.validated_close(key, {'status': 'PASS', 'kind': 'static', 'tool': 'compile_formulas',
                                               'phase': phase, 'report_fingerprint': fingerprint(report)})
            attempted_steps = steps if attempts is None else attempts
            attempted = set().union(*(self._write_objects(step) for step in attempted_steps)) if attempted_steps else set()
            if phase in self.state['seen_compile'] and report['status'] == 'FAIL' and attempted:
                for subject, message in issues.items():
                    if not any(obj == subject or obj in message for obj in attempted):
                        continue
                    key = fingerprint(('compile', phase, subject))
                    row = records.setdefault(key, {'kind': 'compile', 'phase': phase, 'reads': 0,
                                                   'seen': [], 'warned': False})
                    row.update(subject=subject, issue=message, tool='compile_formulas')
            if phase not in self.state['seen_compile']:
                self.state['seen_compile'].append(phase)

        for row in records.values():
            row['deferred'] = repair_deferred(scope, row['subject'], row.get('phase'))
        active = {key: row for key, row in records.items() if not row['deferred']}
        focus = self.state.get('focus')
        exhausted = next((key for key, row in active.items() if row['reads'] >= self.LIMIT), None)
        if exhausted is not None:
            focus = exhausted
        elif focus not in active:
            focus = next(iter(active), None)
        self.state['focus'] = focus
        if focus is None:
            return {'active': False, 'warn': False, 'halt': False, 'message': ''}
        row = records[focus]
        for step in steps:
            if step.tool not in read_tools:
                continue
            row['reads'] += 1
            args = step.args
            location = {'tool': step.tool, **{k: args[k] for k in ('section', 'name', 'query', 'file', 'page', 'sheet', 'detail_offset') if k in args}}
            if location not in row['seen']:
                row['seen'] = [*row['seen'][-5:], location]
        warn = row['reads'] >= self.WARN and not row['warned']
        row['warned'] = row['warned'] or warn
        return {'active': True, 'warn': warn, 'halt': row['reads'] >= self.LIMIT, 'message': self.message(row),
                'subject': row['subject'], 'reads': row['reads'], 'limit': self.LIMIT, 'record_key': focus}
