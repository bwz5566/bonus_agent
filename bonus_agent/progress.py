"""Measure resolution of validation problems, rather than successful tool responses."""
from dataclasses import dataclass, field
import json

from bonus_agent.recovery import fingerprint


def discover(step, seen):
    """Credit new finite source records, never another spelling of the same query or success flag."""
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
