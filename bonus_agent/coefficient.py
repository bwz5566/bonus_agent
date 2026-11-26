"""Compile interval coefficients from physical source cells, never from invented cutoffs."""
import copy
import json
import re
import unicodedata
from collections import defaultdict
from decimal import Decimal

from bonus_agent import facts as fx
from bonus_agent.source_cells import ppt_cell, ppt_page_index, read_ppt_tables


def _fail(message):
    from bonus_agent.agent import ToolError
    raise ToolError('coefficient.' + message)


def interval(text):
    """Bounds plus explicitly stated inclusivity. None means unspecified, not false."""
    raw = unicodedata.normalize('NFKC', text).replace(' ', '').replace('–', '-').replace('—', '-')
    bounds = fx.interval(raw)
    if bounds is not None:
        lo, hi = bounds
        left = right = None
    else:
        clean = fx.band_text(raw)
        tokens = list(fx._TOKEN.finditer(clean))
        if len(tokens) != 1:
            _fail(f'interval：无法唯一识别区间「{text}」，先查原文，不按相似数值猜测')
        value = fx.number(tokens[0].group())
        if value is None:
            _fail(f'interval：区间单位不明确「{text}」')
        if re.search(r'^(?:<=|≤|≦|不超过|至多)', raw):
            lo, hi, left, right = None, value, None, True
        elif re.search(r'^(?:<|＜|低于|不足|未满)', raw):
            lo, hi, left, right = None, value, None, False
        elif re.search(r'以下|以内', raw):
            lo, hi, left, right = None, value, None, None
        elif re.search(r'^(?:>=|≥|≧|至少)', raw):
            lo, hi, left, right = value, None, True, None
        elif re.search(r'^(?:>|＞|超过|大于)', raw):
            lo, hi, left, right = value, None, False, None
        elif re.search(r'以上|\+$', raw):
            lo, hi, left, right = value, None, None, None
        else:
            _fail(f'interval：「{text}」只有数值，没有区间关系；先核对档位表头')
    masked = re.sub(r'\([^()]*\)', lambda match: ' ' * len(match.group()), raw)
    endpoints = list(fx._TOKEN.finditer(masked))
    for note in re.finditer(r'\(([^()]*)\)', raw):
        annotation = note.group(1)
        if '含' not in annotation:
            continue
        included = not bool(re.search(r'不含|不包含', annotation))
        numbers = list(fx._TOKEN.finditer(annotation))
        if numbers:
            for match in numbers:
                token = match.group()
                if '%' in raw and '%' not in token:
                    token += '%'
                point = fx.number(token)
                if point == lo:
                    left = included
                elif point == hi:
                    right = included
                else:
                    _fail(f'boundary：「{text}」的含边界说明不对应其端点')
        elif lo is not None and hi is not None:
            if re.search(r'上下|两端|均', annotation):
                left = right = included
            elif endpoints and note.start() < endpoints[-1].start():
                left = included  # e.g. A%(含)-B% qualifies A, not B.
            else:
                right = included
        elif hi is not None:
            right = included
        else:
            left = included
    if lo is not None and hi is not None and lo >= hi:
        _fail(f'interval：区间不是严格递增「{text}」')
    return {'lower': lo, 'upper': hi, 'left': left, 'right': right, 'text': text}


def resolve(bands):
    bands.sort(key=lambda b: b['lower'] if b['lower'] is not None else Decimal('-Infinity'))
    if bands[0]['lower'] is not None or bands[-1]['upper'] is not None:
        _fail('coverage：原表未覆盖最低或最高区间，须先查明未命中政策；不能自行填极小/极大下界')
    for previous, current in zip(bands, bands[1:]):
        point = current['lower']
        if point is None or previous['upper'] != point:
            _fail(f'coverage：「{previous["text"]}」与「{current["text"]}」存在空档或重叠，先核对原文')
        upper, lower = previous['right'], current['left']
        if upper is None and lower is None:
            _fail(f'boundary：原文「{previous["text"]}」「{current["text"]}」未明确等于{point}归哪一档；'
                  f'该值取前档系数{previous.get("value_text", "未知")}或后档系数{current.get("value_text", "未知")}会得到不同结果。'
                  '先查完整说明，仍无依据再提问，不能默认选一档')
        if upper is None:
            previous['right'] = not lower
        elif lower is None:
            current['left'] = not upper
        elif lower == upper:
            _fail(f'boundary：等于{point}同时落入两档或两档均不接收；原文边界有冲突，不能默认选一档')
    return bands


def amount_tier_origin(ws, rows, source):
    """Use the structural origin when a complete source series gives only its first ceiling."""
    from bonus_agent.agent import ToolError

    if len(rows) < 2 or not rows[0].get('lower'):
        return rows
    try:
        endpoints = [_coordinate(ws, row['lower']) for row in rows]
        values = [_coordinate(ws, row['value']) for row in rows]
    except (KeyError, TypeError, ToolError):
        return rows
    pairs = list(zip(endpoints, values))
    # Pair each bound with its own rate cell. Read the selected series in its
    # submitted order and require adjacent physical bands in either orientation.
    paired = False
    for varying, fixed in (('row', 'column'), ('column', 'row')):
        positions = [cell[varying] for cell in endpoints]
        bound_lanes = {cell[fixed] for cell in endpoints}
        rate_lanes = {cell[fixed] for cell in values}
        if (all(a[varying] == b[varying] for a, b in pairs) and len(bound_lanes) == len(rate_lanes) == 1
                and bound_lanes != rate_lanes and {b - a for a, b in zip(positions, positions[1:])} in ({1}, {-1})):
            paired = True
            break
    if not paired:
        return rows
    ws._check_source_version()
    path = ws._file(source['file'])
    try:
        bands = [interval(ppt_cell(path, source['page'], {'table': source['table'], **cell})[0]) for cell in endpoints]
    except (ToolError, ValueError):
        return rows
    first = bands[0]
    if (first['lower'] is not None or first['upper'] is None or first['upper'] <= 0 or bands[-1]['upper'] is not None
            or any(b['lower'] is None or a['upper'] != b['lower'] for a, b in zip(bands, bands[1:]))):
        return rows
    return [{k: v for k, v in rows[0].items() if k != 'lower'}, *rows[1:]]


def _coordinate(ws, text):
    match = ws._CELL_REF.fullmatch(str(text))
    if not match:
        _fail(f'cell：须使用原始坐标而非自填数值：{text!r}')
    return {'row': int(match[1]), 'column': int(match[2]), **({'line': int(match[3])} if match[3] else {})}


def _achievement_label(subject, value):
    labels = {subject, subject + '销售任务', subject + '达成率', subject + '达成比例', subject + '达成系数'}
    text = fx.scope_norm(value)
    return text in labels or re.sub(r'^(?:该|本)', '', text) in labels


def checked_achievement_series(board, scheme, subject, statuses):
    """Find the already checked series whose bound physical row names the budget subject."""
    ws = board.ws
    rules = ws._ruleset(scheme)
    available = {}
    for spec in board.state['tasks'].values():
        parts = fx.target_parts(spec['target'])
        if spec['scheme'] != scheme or parts[:1] != ['tables'] or statuses[spec['id']]['status'] != 'PASS':
            continue
        table = rules.tables.get(parts[1])
        if getattr(table, 'purpose', None) == 'coefficient' and not table.applies_when:
            available[parts[1]] = spec['id'], table
    if not available:
        return None
    bindings = defaultdict(lambda: defaultdict(set))
    for fact in ws.facts:
        cell = fact.source_cell
        numeric = fx.numeric(fact)
        if (not cell or not fact.page or not fact.source_hash or not fact.file.lower().endswith('.pptx') or numeric is None
                or fact.source_hash != ws._source_version.get(fact.file)
                or any(type(cell.get(k)) is not int for k in ('table', 'row', 'column'))):
            continue
        for target in fact.targets:
            owner, path = fx.split_target(target)
            parts = fx.target_parts(path)
            if (owner == scheme and len(parts) == 5 and parts[0] == 'tables' and parts[1] in available
                    and parts[2] == 'steps' and parts[3].isdigit() and parts[4] == '1'):
                bindings[parts[1]][int(parts[3])].add(
                    (fact.file, fact.page, cell['table'], cell['row'], cell['column'], numeric, fact.value.strip()))
    checked = []
    for name, (task, table) in available.items():
        values = bindings.get(name, {})
        if set(values) != set(range(len(table.steps))) or any(len(pos) != 1 for pos in values.values()):
            continue
        ordered = [next(iter(values[i])) for i in range(len(table.steps))]
        locations = {pos[:4] for pos in ordered}
        expected = [Decimal(str(step[1])) for step in table.steps]
        if len(locations) != 1 or [pos[-2] for pos in ordered] != expected:
            continue
        file, page, table_no, row_no = next(iter(locations))
        try:
            source = next(t for t in read_ppt_tables(ws._file(file), page) if t['table'] == table_no)
            row = next(r for r in source['rows'] if r['row'] == row_no)
        except (KeyError, OSError, ValueError, StopIteration):
            continue
        cells = {c['column']: [s.strip() for s in c['lines'] if s.strip()] for c in row['cells']}
        label = cells.get(1, [])
        by_column = {pos[4]: pos[-1] for pos in ordered}
        body = {col: content for col, content in cells.items() if col > 1 and content}
        if (len(label) == 1 and _achievement_label(subject, label[0])
                and len(by_column) == len(ordered) and body == {col: [value] for col, value in by_column.items()}):
            checked.append((task, name))
    return checked[0] if len(checked) == 1 else None


def registered_coefficient_call(ws, clause, name, scheme):
    """Use one complete physical coefficient row uniquely assigned by the cited rule."""
    title, separator, _ = unicodedata.normalize('NFKC', clause.quote).partition('|')
    role = re.fullmatch(r'(.+?)达成(?:率|比例|系数)', fx.scope_norm(title))
    if not separator or not role:
        return None
    return achievement_series_call(ws, role[1], name, scheme, require_recorded_label=True)


def achievement_series_call(ws, subject, name, scheme, *, require_recorded_label=False):
    """Prepare a complete registered series, with the role confirmed in its original cells."""
    from bonus_agent.agent import ToolError

    rows = {}
    for fact in ws.facts:
        cell = fact.source_cell or {}
        if (not fact.page or not fact.context or not fact.source_hash or fact.source_hash != ws._source_version.get(fact.file)
                or not fact.file.lower().endswith('.pptx') or cell.get('line', 1) != 1
                or any(type(cell.get(k)) is not int or cell[k] < 1 for k in ('table', 'row', 'column'))
                or fact.component != 'literal' or fx.numeric(fact) is None):
            continue
        if require_recorded_label and not _achievement_label(subject, (fact.quote.splitlines() or [''])[0]):
            continue
        key = (fact.file, fact.page, cell['table'], cell['row'])
        rows.setdefault(key, []).append(fact)

    candidates = []
    for (file, page, table, row), entries in rows.items():
        cols = sorted({f.source_cell['column'] for f in entries})
        contexts = {f.context for f in entries}
        if len(cols) < 2 or min(cols) <= 1 or len(contexts) != 1:
            continue
        try:
            source = read_ppt_tables(ws._file(file), page)[table - 1]['rows']
            physical = source[row - 1]
        except (IndexError, KeyError, OSError, ValueError, ToolError):
            continue
        cells = {cell['column']: [s.strip() for s in cell['lines'] if s.strip()] for cell in physical['cells']}
        label = cells.get(1) or []
        values = {col: lines for col, lines in cells.items() if col != 1 and lines}
        if (len(label) != 1 or not _achievement_label(subject, label[0]) or sorted(values) != cols
                or any(values[f.source_cell['column']] != [f.value.strip()] for f in entries)):
            continue
        for header in source[:row - 1]:
            headings = {cell['column']: [s.strip() for s in cell['lines'] if s.strip()] for cell in header['cells']}
            if any(len(headings.get(col, ())) != 1 or not re.search(r'[%％‰]', headings[col][0]) for col in cols):
                continue
            try:
                resolve([interval(headings[col][0]) for col in cols])
            except ToolError:
                continue
            candidates.append({
                'name': name, 'scheme': scheme, 'applies_to': next(iter(contexts)) if require_recorded_label else label[0],
                'cells_from': {'file': file, 'page': page, 'table': table},
                'rows': [{'range': f'R{header["row"]}C{col}', 'value': f'R{row}C{col}'} for col in cols],
            })
    return candidates[0] if len(candidates) == 1 else None


def planned_coefficient_call(board, spec):
    """Recover a complete ratio-to-coefficient series from one declared source row or column."""
    from bonus_agent.clauses import check_quote
    from bonus_agent.semantics import stated_modes

    ws = board.ws
    parts = fx.target_parts(spec['target'])
    if parts[0] != 'tables' or not 2 <= len(spec['facts']) <= 32 or board.value(spec) is not None:
        return None
    corpus = ws._corpus()
    cited = [c for c in ws.clauses if c.id in spec['clauses']]
    if any(stated_modes(c.quote) for c in cited if not check_quote(c.file, c.quote, corpus)):
        return None
    by_name = defaultdict(list)
    for fact in ws.facts:
        if fact.name in spec['facts']:
            by_name[fact.name].append(fact)
    if set(by_name) != set(spec['facts']) or any(len(matches) != 1 for matches in by_name.values()):
        return None
    facts = [matches[0] for matches in by_name.values()]
    if any(not f.source_cell or not f.page or not f.file.lower().endswith('.pptx') or f.component != 'literal'
           or f.source_cell.get('line', 1) != 1 or not f.source_hash or f.source_hash != ws._source_version.get(f.file)
           or fx.numeric(f) is None or check_quote(f.file, f.quote, corpus) for f in facts):
        return None
    locations = {(f.file, f.page, f.source_cell['table']) for f in facts}
    if len(locations) != 1:
        return None
    file, page, table = next(iter(locations))
    try:
        raw = read_ppt_tables(ws._file(file), page)[table - 1]['rows']
    except (OSError, IndexError, ValueError):
        return None
    text = {(r['row'], c['column']): [v.strip() for v in c['lines'] if v.strip()] for r in raw for c in r['cells']}
    positions = {(f.source_cell['row'], f.source_cell['column']): f for f in facts}
    if len(positions) != len(facts) or any(min(pos) <= 1 for pos in positions):
        return None
    row_ids = {r for r, _ in positions}
    col_ids = {c for _, c in positions}
    if len(row_ids) == 1:
        line = next(iter(row_ids))
        selected = {pos for pos, lines in text.items() if pos[0] == line and pos[1] > 1 and lines}
        bounds = {pos: (1, pos[1]) for pos in positions}
        heading = text.get((line, 1), ())
    elif len(col_ids) == 1:
        line = next(iter(col_ids))
        selected = {pos for pos, lines in text.items() if pos[1] == line and pos[0] > 1 and lines}
        bounds = {pos: (pos[0], 1) for pos in positions}
        heading = text.get((1, line), ())
    else:
        return None
    if not heading or selected != set(positions):
        return None
    rows = []
    for pos, bound in sorted(bounds.items()):
        value, ratio = text.get(pos, ()), text.get(bound, ())
        if (len(value) != 1 or len(ratio) != 1 or not re.search(r'[%％‰]', ratio[0])
                or fx.numeric(positions[pos]) != fx.numeric(positions[pos].model_copy(update={'value': value[0]}))):
            return None
        rows.append({'range': f'R{bound[0]}C{bound[1]}', 'value': f'R{pos[0]}C{pos[1]}'})
    return {'name': parts[1], 'scheme': spec['scheme'], 'cells_from': {'file': file, 'page': page, 'table': table},
            'applies_to': ' '.join(heading), 'rows': rows}


def prepare_planned_coefficient(board, spec):
    """Commit a coefficient table only after its normal source and task checks pass."""
    from bonus_agent.agent import ToolError
    from bonus_agent.construction import TaskBoard

    call = planned_coefficient_call(board, spec)
    if not call:
        return None
    ws = board.ws
    candidate = copy.deepcopy(ws, {id(ws.llm): ws.llm})
    result = {'task': spec['id'], 'scheme': spec['scheme'], 'target': spec['target'],
              'source': call['cells_from'], 'applies_to': call['applies_to']}
    try:
        report = compile_coefficients(candidate, **call)
        check = TaskBoard(candidate).check(spec['id'], include_next=False)
    except (ToolError, ValueError, KeyError, StopIteration) as exc:
        return {**result, 'status': 'FAIL', 'saved': False, 'issues': [str(exc)]}
    if check['status'] != 'PASS':
        return {**result, 'status': 'FAIL', 'saved': False, 'issues': check['issues']}
    candidate._check_source_version()
    ws.__dict__.update(candidate.__dict__)
    return {**result, 'status': 'PASS', 'saved': True, 'intervals': report['intervals'],
            'checked': [{'task': spec['id'], 'target': spec['target'], 'status': 'PASS'}]}


def compile_coefficients(ws, name, rows, cells_from, applies_to, scheme=None):
    """All source registration and rule updates commit together, or leave the workspace intact."""
    from bonus_agent.agent import ToolError

    if not isinstance(cells_from, dict) or set(cells_from) != {'file', 'page', 'table'} or not rows:
        _fail('source：须给非空rows及cells_from={file,page,table}')
    clone = copy.deepcopy(ws, {id(ws.llm): ws.llm})
    clone._check_source_version()
    path = clone._file(cells_from['file'])
    bands, cells, row_labels = [], [], []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'range', 'value'}:
            _fail('rows：每档只给range与value坐标，边界、单位及系数由原文读取')
        bounds_cell = _coordinate(clone, row['range'])
        value_cell = _coordinate(clone, row['value'])
        raw, _ = ppt_cell(path, cells_from['page'], {'table': cells_from['table'], **bounds_cell})
        value_text, value_labels = ppt_cell(path, cells_from['page'], {'table': cells_from['table'], **value_cell})
        row_labels.append({label.strip() for label in value_labels if label.strip()})
        # Refuse explicit monetary algorithms rather than relabeling them as coefficients.
        from bonus_agent.semantics import stated_mode
        if stated_mode(raw) == 'excess':
            _fail('purpose：原文要求金额超额累进，不能编为区间取系数')
        band = interval(raw)
        bands.append({**band, 'range_cell': bounds_cell, 'value_cell': value_cell, 'value_text': value_text})
        labels = unicodedata.normalize('NFKC', '\n'.join(value_labels))
        # Percentages inside an input interval are not the output value's unit.
        # Only an independent percent header (e.g. 系数(%)) applies to a bare value.
        header = re.sub(r'[-+]?\d+(?:\.\d+)?\s*[%‰万亿元]+', '', labels)
        value_registration = dict(value_cell)
        if '%' in header and '%' not in value_text and fx.number(value_text) is not None:
            value_registration['unit'] = '%'
        cells.extend([bounds_cell, value_registration])
    resolve(bands)
    try:
        report = clone.record_parameter_cells(**cells_from, cells=list({json.dumps(c, sort_keys=True):c for c in cells}.values()),
                                              applies_to=applies_to)
    except ToolError as exc:
        labels = set.intersection(*row_labels)
        if str(exc).startswith('source.scope') and len(labels) == 1:
            physical, = labels
            next_call = {'tool': 'compile_coefficient_table', 'args': {'name': name, 'rows': rows, 'cells_from': cells_from,
                                                                       'applies_to': physical, 'scheme': scheme}}
            raise ToolError(str(exc) + f'\n当前所选系数格共同的原文标题是「{physical}」。'
                            '核对适用对象后使用该标题及原坐标。'
                            '\nnext_call=' + json.dumps(next_call, ensure_ascii=False)) from exc
        raise
    if report['status'] != 'PASS':
        _fail('source：原文参数登记未全部通过；本次未保存：' + json.dumps(report, ensure_ascii=False))

    def fact_for(locator):
        return next(f for f in clone.facts if f.file == cells_from['file'] and f.page == cells_from['page']
                    and f.source_cell and all(f.source_cell.get(k, 1) == v for k,v in {'table':cells_from['table'],**locator}.items()))

    scheme = clone._scheme(scheme, for_write=True)
    from bonus_agent.construction import guard_table_target
    guard_table_target(clone, scheme, name, (fact_for(b['value_cell']).name for b in bands))
    prior = clone._merged(scheme).get('tables', {}).get(name, {})
    prior = prior if prior.get('kind') == 'tier' else {}
    escape = lambda x: str(x).replace('~','~0').replace('/','~1')
    prefix = scheme + '::/tables/' + escape(name) + '/'
    for fact in clone.facts:
        fact.targets = [t for t in fact.targets if not t.startswith((prefix + 'steps/', prefix + 'mapping/'))]
    steps, inclusivity, bindings, sources = [], [], {}, copy.deepcopy(prior.get('sources', []))
    def bind(fact, target):
        bindings.setdefault(fact.name, set(fact.targets)).add(target)
        source = {'doc':fact.file, 'page':fact.page, 'quote':fact.quote}
        if source not in sources:
            sources.append(source)

    def endpoint(fact, component, point):
        if fx.interval(fact.value) is None:
            if fx.numeric(fact) != point:
                _fail('unit：区间端点与来源单位不一致')
            return fact
        spec = fact.model_dump()
        spec.update(name=fact.name+'::'+component, component=component, targets=[])
        prior = next((f for f in clone.facts if f.name == spec['name'] and f.source_hash == fact.source_hash
                      and f.component == component and f.quote == fact.quote and fx.numeric(f) == point), None)
        if prior is not None:
            return prior
        clone.record_facts([{k:spec[k] for k in ('name','file','page','context','value','quote','source_cell','component','unit')}])
        found = next(f for f in clone.facts if f.name == spec['name'])
        if fx.numeric(found) != point:
            _fail('unit：原文端点投影失败，不能自行换算')
        return found

    for i, band in enumerate(bands):
        value_fact = fact_for(band['value_cell'])
        value = fx.numeric(value_fact)
        if value is None:
            _fail('value：原文取值不是明确系数，先核对单位')
        lower = band['lower'] if band['lower'] is not None else Decimal(0)
        steps.append([str(lower), str(value)])
        inclusivity.append(bool(band['left']) if i else True)
        bind(value_fact, prefix+f'steps/{i}/1')
        range_fact = fact_for(band['range_cell'])
        if band['lower'] is not None:
            bind(endpoint(range_fact, 'lower', band['lower']), prefix+f'steps/{i}/0')
        if band['upper'] is not None:
            bind(endpoint(range_fact, 'upper', band['upper']), prefix+f'steps/{i+1}/0')
    # A zero structural origin must precede the remaining cutoffs; a negative first cutoff
    # needs a source-backed origin instead of silently changing the original intervals.
    if any(Decimal(a[0]) >= Decimal(b[0]) for a,b in zip(steps,steps[1:])):
        _fail('origin：原文含非正分界点，不能用隐式0起点表示；请按原文明确定义表及未命中政策')
    table = {'kind':'tier','purpose':'coefficient','mode':'whole','steps':steps,
             'below_first':'first','step_inclusive':inclusivity,'sources':sources}
    # Interval boundaries are regenerated from source cells, while a separate
    # applicability condition must not disappear just because rows were rebuilt.
    table.update({k: copy.deepcopy(prior[k]) for k in ('applies_when', 'desc') if k in prior})
    clone.set_rules(scheme=scheme, tables={name:table}, bindings=[{'name':n,'targets':sorted(ts)} for n,ts in bindings.items()])
    ws.__dict__.update(clone.__dict__)
    return {'status':'PASS','table':name,'scheme':scheme,'scope':'仅区间系数表编译；整案仍须完整编译和独立核算',
            'usage':f'tier_rate({name}, 实际指标)，只返回系数；不能用tier乘指标计算金额',
            'intervals':[{k:str(v) if isinstance(v,Decimal) else v for k,v in b.items() if k in ('text','lower','upper','left','right')} for b in bands]}


def suggestion(ws, name, rows, cells_from, scheme):
    """Offer a source-coordinate action, never silently change the table's business purpose."""
    if not cells_from or not rows:
        return ''
    from bonus_agent.agent import ToolError
    try:
        values = [_coordinate(ws, row['value']) for row in rows]
        horizontal = len({c['row'] for c in values}) == 1
        vertical = len({c['column'] for c in values}) == 1
        if horizontal == vertical:
            return ''
        new_rows=[]
        for value in values:
            bound = {'row':1,'column':value['column']} if horizontal else {'row':value['row'],'column':1}
            text,_=ppt_cell(ws._file(cells_from['file']),cells_from['page'],{'table':cells_from['table'],**bound})
            interval(text)
            new_rows.append({'range':f'R{bound["row"]}C{bound["column"]}',
                             'value':f'R{value["row"]}C{value["column"]}'+(f'L{value["line"]}' if 'line' in value else '')})
        index=ppt_page_index(ws._file(cells_from['file']))
        args={'name':name,'scheme':scheme,'cells_from':cells_from,
              'applies_to':index[cells_from['page']-1]['scope'],'rows':new_rows}
        return ('。若原表用途是按指标区间取系数，核对用途后改用compile_coefficient_table；它直接读取区间及系数、补齐坐标证据，'
                '不用人造负下界，也不要求原文出现“金额累进”字样。若确为金额阶梯，仍须补明确算法依据。\nnext_call='
                +json.dumps({'tool':'compile_coefficient_table','args':args},ensure_ascii=False))
    except (ValueError, KeyError, TypeError, ToolError):
        return ''
