"""Bind exact declared evidence and account for thresholds used as source labels."""
from collections import defaultdict
import re

from bonus_agent import facts as fx
from bonus_agent.clauses import check_quote


def _literal_path(target):
    scheme, path = fx.split_target(target)
    return scheme, fx.target_parts(path)


def covered_range_labels(ws, active, scopes):
    """A physical first-band upper label corroborates the adjacent bound lower endpoint."""
    scoped = defaultdict(list)
    for fact in active:
        if not fact.source_cell or not fact.targets:
            continue
        for target in fact.targets:
            scheme, parts = _literal_path(target)
            if (len(parts) == 5 and parts[0] == 'tables' and parts[2] == 'steps'
                    and parts[3:] in (['0', '1'], ['1', '0']) and scheme in scopes):
                slot = (scheme, parts[1], fact.file, fact.page, fact.source_cell.get('table'))
                scoped[slot].append((parts[3:], fact))
    verified = set()
    for fact in active:
        locator = fact.source_cell
        label = (fact.quote or fact.value).splitlines()[-1]
        if fact.targets or not locator or locator.get('column') != 1 or not ws._UPPER_BAND.search(label):
            continue
        for (scheme, name, file, page, table), positions in scoped.items():
            if (fact.file, fact.page, locator.get('table')) != (file, page, table):
                continue
            applied = scopes[scheme].get('tables', {}).get(name, {}).get('steps', [])
            if len(applied) < 2:
                continue
            if (not any(s == ['0', '1'] and f.source_cell.get('row') == locator.get('row') for s, f in positions)
                    or not any(s == ['1', '0'] and fx.numeric(f) == fx.numeric(fact) for s, f in positions)):
                continue
            if str(applied[0][0]) not in ('0', '0.0') or fx.number(str(applied[1][0])) != fx.numeric(fact):
                continue
            verified.add((fact.name, fact.file, fact.quote, scheme, name))
    return verified


def _normalized(text):
    return fx.scope_norm(text)


def declared_parameter_repairs(ws, task_id=None):
    """Find a binding only when the physical quote and the owning formula are already declared."""
    if not ws.staged:
        return []
    from bonus_agent.construction import TaskBoard

    board = TaskBoard(ws)
    allowed = {task_id} if task_id is not None else (
        board.required('basis') if board.phase() == 'basis' else set(board.state['tasks']))
    corpus = ws._corpus()
    quotes = {c.id: (c.file, _normalized(c.quote)) for c in ws.clauses
              if not check_quote(c.file, c.quote, corpus)}
    candidates = defaultdict(list)
    for task, spec in board.state['tasks'].items():
        if task not in allowed or not spec['target'].startswith('/variables/'):
            continue
        definition = board.value(spec)
        if not definition or not isinstance(definition.get('expr'), str):
            continue
        target = f'{spec["scheme"]}::{spec["target"]}/expr'
        grounds = [quotes[c] for c in spec['clauses'] if c in quotes]
        grounds.extend((source['doc'], _normalized(source['quote'])) for source in definition.get('sources', [])
                       if source.get('doc') and source.get('quote')
                       and not check_quote(source['doc'], source['quote'], corpus))
        candidates[spec['scheme']].append((target, grounds, definition['expr']))

    groups = defaultdict(list)
    for fact in ws.facts:
        groups[fact.name].append(fact)
    repairs = []
    for name, group in groups.items():
        if not any(not f.targets for f in group):
            continue
        supported = []
        for fact in group:
            value, quote = fx.numeric(fact), _normalized(fact.quote)
            if value is None or len(quote) < 6 or fact.source_cell:
                break
            matching = set(fact.targets)
            for items in candidates.values():
                for target, grounds, expr in items:
                    if (any(doc == fact.file and quote in ground for doc, ground in grounds)
                            and not fx._binding_target_problem(fact, value, expr, target)):
                        matching.add(target)
            supported.append(matching)
        if len(supported) != len(group):
            continue
        targets = set.intersection(*supported)
        if not targets or any(set(f.targets) - targets for f in group):
            continue
        if not any(targets - set(f.targets) for f in group):
            continue
        repairs.append({'name': name, 'targets': sorted(targets)})
    return repairs
