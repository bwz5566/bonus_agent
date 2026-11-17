"""Compile interval coefficients from physical source cells, never from invented cutoffs."""
import copy
import json
import re
import unicodedata
from decimal import Decimal

from bonus_agent import facts as fx
from bonus_agent.source_cells import ppt_cell, ppt_page_index


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


def _coordinate(ws, text):
    match = ws._CELL_REF.fullmatch(str(text))
    if not match:
        _fail(f'cell：须使用原始坐标而非自填数值：{text!r}')
    return {'row': int(match[1]), 'column': int(match[2]), **({'line': int(match[3])} if match[3] else {})}


def compile_coefficients(ws, name, rows, cells_from, applies_to, scheme=None):
    """All source registration and rule updates commit together, or leave the workspace intact."""
    if not isinstance(cells_from, dict) or set(cells_from) != {'file', 'page', 'table'} or not rows:
        _fail('source：须给非空rows及cells_from={file,page,table}')
    clone = copy.deepcopy(ws, {id(ws.llm): ws.llm})
    clone._check_source_version()
    path = clone._file(cells_from['file'])
    bands, cells = [], []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'range', 'value'}:
            _fail('rows：每档只给range与value坐标，边界、单位及系数由原文读取')
        bounds_cell = _coordinate(clone, row['range'])
        value_cell = _coordinate(clone, row['value'])
        raw, _ = ppt_cell(path, cells_from['page'], {'table': cells_from['table'], **bounds_cell})
        value_text, value_labels = ppt_cell(path, cells_from['page'], {'table': cells_from['table'], **value_cell})
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
    report = clone.record_parameter_cells(**cells_from, cells=list({json.dumps(c, sort_keys=True):c for c in cells}.values()), applies_to=applies_to)
    if report['status'] != 'PASS':
        _fail('source：原文参数登记未全部通过；本次未保存：' + json.dumps(report, ensure_ascii=False))

    def fact_for(locator):
        return next(f for f in clone.facts if f.file == cells_from['file'] and f.page == cells_from['page']
                    and f.source_cell and all(f.source_cell.get(k, 1) == v for k,v in {'table':cells_from['table'],**locator}.items()))

    scheme = clone._scheme(scheme, for_write=True)
    escape = lambda x: str(x).replace('~','~0').replace('/','~1')
    prefix = scheme + '::/tables/' + escape(name) + '/'
    for fact in clone.facts:
        fact.targets = [t for t in fact.targets if not t.startswith(prefix)]
    steps, inclusivity, bindings, sources = [], [], {}, []
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
