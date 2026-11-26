"""Identify a rejected tier boundary after the owning task moves to a different table."""
import re
from decimal import Decimal, InvalidOperation

from bonus_agent import facts as fx


def rejected_bound(args, issue, owner):
    match = re.match(r'parameter\.ref：第 1 档的 lower 用了 (R\d+C\d+(?:L\d+)?)「[^」]*」，这是上限写法', issue)
    source = args.get('cells_from')
    rows = args.get('rows')
    if not (match and owner and args.get('kind', 'tier') == 'tier'
            and isinstance(source, dict) and isinstance(source.get('file'), str)
            and type(source.get('page')) is int and type(source.get('table')) is int
            and isinstance(rows, list) and rows and isinstance(rows[0], dict)):
        return None
    lower, value = rows[0].get('lower'), rows[0].get('value')
    if (lower != match[1] or not isinstance(value, str) or not re.fullmatch(r'R\d+C\d+(?:L\d+)?', value)
            or owner.get('scheme') != args.get('scheme') or fx.target_parts(owner.get('target', '')) != ['tables', args.get('name')]):
        return None
    row, column = re.match(r'R(\d+)C(\d+)', lower).groups()
    rate_row, rate_column = re.match(r'R(\d+)C(\d+)', value).groups()
    if ((row, column) == (rate_row, rate_column)
            or not (column == '1' and row == rate_row or row == '1' and column == rate_column)):
        return None
    return {'task': owner['id'], 'source': {k: source[k] for k in ('file', 'page', 'table')},
            'lower': lower, 'value': value, 'mode': args.get('mode', 'excess'),
            'clauses': owner.get('clauses', []), 'facts': owner.get('facts', [])}


def replacement_bound(board, item):
    """Require the same owner, original rate cell, upper limit, and carried evidence."""
    origin = item.get('rejected_bound') or {}
    specs = board.state['tasks']
    owner = specs.get(origin.get('task'))
    if not owner or owner['scheme'] != item['scheme'] or len(item['objects']) != 1:
        return None
    scheme = owner['scheme']
    old = item['objects'][0]
    parts = fx.target_parts(owner['target'])
    if len(parts) != 2 or parts[0] != 'tables' or parts[1] == old:
        return None
    tables = [t for t in specs.values() if t['scheme'] == scheme and t['target'].startswith('/tables/')]
    if old in board.ws._merged(scheme).get('tables', {}) or any(fx.target_parts(t['target']) == ['tables', old] for t in tables):
        return None
    for field in ('clauses', 'facts'):
        if set(origin[field]) - {name for t in tables for name in t[field]}:
            return None
    table = board.value(owner)
    if not isinstance(table, dict) or table.get('kind') != 'tier' or table.get('mode') != origin['mode']:
        return None
    source = origin['source']
    bands = {text for cell, text, _, upper, _ in board.ws._band_cells(source) if upper and cell == origin['lower']}
    if len(bands) != 1:
        return None
    upper = fx.number(board.ws._UPPER_BAND.sub('', fx.band_text(bands.pop())).strip().lstrip('='))
    try:
        first, second = table['steps'][:2]
        if Decimal(str(first[0])) != 0 or Decimal(str(second[0])) != upper:
            return None
        rate = Decimal(str(first[1]))
    except (InvalidOperation, IndexError, KeyError, TypeError, ValueError):
        return None
    for fact in board.ws.facts:
        cell = fact.source_cell or {}
        loc = f"R{cell.get('row')}C{cell.get('column')}" + (f"L{cell['line']}" if cell.get('line', 1) != 1 else '')
        if (fact.file == source['file'] and fact.source_hash and fact.source_hash == board.ws._source_version.get(fact.file)
                and fact.page == source['page'] and cell.get('table') == source['table']
                and loc == origin['value'] and fx.numeric(fact) == rate and any(
                    fx.split_target(t)[0] == scheme and fx.target_parts(fx.split_target(t)[1]) == parts + ['steps', '0', '1']
                    for t in fact.targets)):
            return owner
    return None
