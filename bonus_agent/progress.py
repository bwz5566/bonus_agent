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
