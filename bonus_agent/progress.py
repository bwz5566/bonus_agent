"""Measure resolution of validation problems, rather than successful tool responses."""
from dataclasses import dataclass, field
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


@dataclass
class ProgressWatch:
    previous: set[str] = field(default_factory=set)
    stagnant: int = 0

    def observe(self, blockers, *, discovery=False):
        current = set(blockers)
        if discovery or not current or not self.previous or self.previous - current:
            self.stagnant = 0
        else:
            self.stagnant += 1
        self.previous = current
        return self.stagnant


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
        return bool(self.state['records'])

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
        return objects

    def observe(self, steps, native, report, read_tools, *, attempts=None):
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
                row.update(subject=obj, issue=item['issue'], tool=item['tool'])
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
                if row['kind'] != 'compile' or row['phase'] != phase:
                    continue
                runtime = re.match(r'^方案「([^」]+)」基础求值预检[：:]', row['issue'])
                if runtime:
                    # Static errors can prevent the runtime check from running;
                    # even overall PASS can contain DEFERRED upstream-dependent
                    # preflights. Only this scheme's executed PASS resolves it.
                    if report.get('preflight', {}).get(runtime.group(1), {}).get('status') == 'PASS':
                        del records[key]
                elif static_complete and row['subject'] not in issues:
                    del records[key]
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

        focus = self.state.get('focus')
        if focus not in records:
            focus = next(iter(records), None)
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
        action = ('bind_facts；目标公式缺分支时set_rules(bindings=...)；绑定已经一致时validate_parameters核验剩余缺口' if row['tool'] == 'bind_facts' else
                  'submit_formula_task（使用已有任务ID）/set_rules，然后程序局部检查')
        message = (f'当前未解决对象「{row["subject"]}」已读取 {row["reads"]}/{self.LIMIT} 次，'
                   '换页面、换query、note、no-op或改描述不会增加额度。'
                   f'程序反例：{row["issue"][:600]}\n'
                   '已读位置：' + json.dumps(row['seen'], ensure_ascii=False) + '\n'
                   f'下一步：仅提交该对象的{action}，按已核对原文修复并验证。'
                   '需要补充证据时明确尚缺的原文位置；不能猜值、改无关公式或降低验证。')
        return {'active': True, 'warn': warn, 'halt': row['reads'] >= self.LIMIT, 'message': message,
                'subject': row['subject'], 'reads': row['reads'], 'limit': self.LIMIT}
