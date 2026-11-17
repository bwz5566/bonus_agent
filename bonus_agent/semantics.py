"""Check explicit algorithm statements against the rules that claim to implement them."""
import re

from bonus_agent import clauses as cl, facts as fx
from bonus_core.dsl.parser import Call, Name, ExprError, parse, walk


def stated_mode(text):
    text = fx.scope_norm(text)
    found = set()
    for mode, pattern in [('excess', r'超额累进|excess|marginal'), ('whole', r'全额累进|整额累进|wholeamount')]:
        for match in re.finditer(pattern, text):
            if re.search(r'不|非|取消|禁止', text[max(0, match.start()-3):match.start()]):
                continue
            found.add(mode)
    return next(iter(found)) if len(found) == 1 else None


def preserves_negative(text):
    text = fx.scope_norm(text)
    return bool(re.search(r'负数|负金额|负奖金|负提成', text) and
                re.search(r'照算|(?<!不)保留负|不归零|不得归零|不记为0|不能记为0', text))


def mode_basis(ws, basis):
    from bonus_agent.agent import ToolError
    error = cl.check_quote(basis.get('file', ''), basis.get('quote', ''), ws._corpus())
    if error:
        raise ToolError('algorithm.source：' + error)
    if basis.get('page') is not None and basis['page'] not in ws._quote_pages(basis['quote'], ws._page_texts(basis['file'])):
        raise ToolError('algorithm.source：算法引用不在所填页码')
    mode = stated_mode(basis['quote'])
    if mode is None:
        raise ToolError('algorithm.source：引用须明确且不矛盾地说明超额累进或整额/全额分档算法；不能从数字或表名猜测')
    return mode


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            if k not in ('sources', 'desc'):
                yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def target_mode_issues(ws, scheme, target, clause_ids):
    draft = ws._merged(scheme)
    try:
        owner = fx.target_value(draft, target)
    except (KeyError, IndexError, TypeError, ValueError):
        return []
    issues = []
    for clause in ws.clauses:
        if clause.id not in clause_ids:
            continue
        expected = stated_mode(clause.quote)  # use the verified source, not a model paraphrase
        negative = preserves_negative(clause.quote)
        if expected is None and not negative:
            continue
        error = cl.check_quote(clause.file, clause.quote, ws._corpus())
        if error:
            issues.append(f'algorithm.source：{clause.id} {error}')
            continue
        parts = fx.target_parts(target)
        tables = {parts[1]} if len(parts) >= 2 and parts[0] == 'tables' else set()
        todo, seen = list(_strings(owner)), set()
        while todo:
            text = todo.pop()
            if text in seen:
                continue
            seen.add(text)
            try:
                ast = parse(text)
            except ExprError:
                continue
            for node in walk(ast):
                if isinstance(node, Call) and node.fn == 'tier' and node.args and isinstance(node.args[0], Name):
                    tables.add(node.args[0].id)
                if isinstance(node, Name) and node.id in draft.get('variables', {}):
                    todo.append(draft['variables'][node.id]['expr'])
        if not tables and expected:
            issues.append(f'algorithm.scope：{clause.id}明确规定{expected}，但{scheme}::{target}没有对应的tier金额阶梯；'
                          '把该条款关联到金额公式或具体阶梯表，不能用整额费率乘总量替代超额分段')
        for name in sorted(tables):
            table = draft.get('tables', {}).get(name, {})
            if expected and table.get('mode', 'whole') != expected:
                issues.append(f'algorithm.mode：{scheme}::/tables/{name}使用{table.get("mode", "whole")}，'
                              f'{clause.id}《{clause.file}》原文「{clause.quote}」要求{expected}；'
                              '用compile_table按原始参数重编并提供algorithm_basis，不能只改说明')
            if negative and table.get('below_first', 'zero') != 'first':
                issues.append(f'algorithm.negative：{scheme}::/tables/{name}低于首档时归零，'
                              f'{clause.id}原文「{clause.quote}」要求保留负值；按原文核对below_first，不能静默截断')
    return issues


def semantic_gates(ws):
    issues = []
    for scheme in ws.schemes:
        for name, table in ws._merged(scheme).get('tables', {}).items():
            for source in table.get('sources', []):
                expected = stated_mode(source.get('quote', ''))
                if expected and table.get('mode', 'whole') != expected:
                    issues.append(f'algorithm.mode：{scheme}::/tables/{name}与其原文算法依据{expected}矛盾，按来源重编')
    for clause in ws.clauses:
        if stated_mode(clause.quote) is None and not preserves_negative(clause.quote):
            continue
        if clause.where.startswith(cl.BUILTIN):
            issues.append(f'algorithm.scope：{clause.id}明确规定阶梯算法，必须关联具体表或金额公式；引擎支持多种算法，不能用“引擎自带”代替选择')
        for ref in cl.refs(clause.where):
            if ref == 'pool' or ref.startswith(('variables.', 'tables.')):
                target = '/' + '/'.join(p.replace('~', '~0').replace('/', '~1') for p in ref.split('.', 1))
                for scheme in ws.schemes:
                    issues += target_mode_issues(ws, scheme, target, [clause.id])
    return list(dict.fromkeys(issues))
