"""Locate report evidence errors in the full editable rules, including phase views."""
import json


def _pointer(*parts):
    return '/' + '/'.join(str(part).replace('~', '~0').replace('/', '~1') for part in parts)


def _same_item(item, raw):
    try:
        return type(item).model_validate(raw).model_dump(mode='json') == item.model_dump(mode='json')
    except (TypeError, ValueError):
        return False


def report_source_issue(ws, scheme, section, index, item, code, detail, *, source_index=None):
    """Never turn a projected list index into a pointer to a different item.

    Full views retain their real index. Phase views must uniquely match the
    complete original item; names alone are not enough. An uncertain location
    falls back to the containing section without suggesting an element index.
    """
    originals = getattr(ws, '_phase_source_drafts', None)
    original = originals.get(scheme) if isinstance(originals, dict) else ws._merged(scheme)
    rows = (original.get('report') or {}).get(section, []) if isinstance(original, dict) else []
    actual = None
    if isinstance(rows, list):
        if originals is None:
            if index < len(rows) and _same_item(item, rows[index]):
                actual = index
        else:
            matches = [i for i, candidate in enumerate(rows) if _same_item(item, candidate)]
            if len(matches) == 1:
                actual = matches[0]

    location = _pointer('report', section)
    read_path = location
    if actual is not None:
        read_path = _pointer('report', section, actual)
        location = _pointer('report', section, actual, 'sources')
        if source_index is not None:
            # The complete item comparison above includes ordered sources.
            location = _pointer('report', section, actual, 'sources', source_index)
    else:
        detail += '；无法唯一回溯到完整规则中的报告项，请核对该报告分组，不使用投影后的索引'

    call = {'tool': 'read_workspace', 'args': {'section': 'rules', 'name': scheme + '::' + read_path,
                                             'detail': True}}
    return (f'[阻断] {code}: 方案「{scheme}」报告「{item.name}」：{detail}；'
            f'next_call={json.dumps(call, ensure_ascii=False)} ({scheme}::{location})')
