"""Measure resolution of validation problems, rather than successful tool responses."""
from dataclasses import dataclass, field
import json

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
