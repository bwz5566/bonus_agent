"""Locate report evidence errors in the full editable rules, including phase views."""
import json


def explicit_sources(value, path=()):
    """Enumerate explicit DSL references, also in nested reports and lists."""
    if isinstance(value, dict):
        for key, part in value.items():
            if key == 'sources' and isinstance(part, list):
                for index, source in enumerate(part):
                    if isinstance(source, dict):
                        yield (*path, key, index), source
            else:
                yield from explicit_sources(part, (*path, key))
    elif isinstance(value, list):
        for index, part in enumerate(value):
            yield from explicit_sources(part, (*path, index))


def source_reference_error(ws, source, corpus):
    """A quoted document and any declared page must both contain the quotation."""
    from bonus_agent.clauses import check_quote

    doc, quote = source.get('doc', ''), source.get('quote', '')
    if gap := check_quote(doc, quote, corpus):
        return gap
    page = source.get('page')
    if page is not None and page not in ws._quote_pages(quote, ws._page_texts(doc)):
        return f'原文在「{doc}」第 {page} 页找不到，须核对实际所在页。'
    return None


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
