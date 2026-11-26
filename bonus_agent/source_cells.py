"""Read table coordinates from source documents; never locate a value by its amount."""
from decimal import Decimal
import re
import unicodedata


def ppt_cell(path, page, locator):
    from pptx import Presentation
    if not page or path.suffix.lower() != '.pptx':
        raise ValueError('source.cell：目前表格定位需要PPTX及明确page')
    prs = Presentation(path)
    try:
        tables = [s.table for s in prs.slides[page-1].shapes if s.has_table]
        table = tables[int(locator['table'])-1]
        row, col = int(locator['row']), int(locator['column'])
        if min(int(locator['table']), row, col) < 1:
            raise ValueError('坐标从1开始')
        if not (1 <= row <= len(table.rows) and 1 <= col <= len(table.columns)):
            raise IndexError
        cell = table.cell(row-1, col-1).text
        lines = [x.strip() for x in cell.splitlines() if x.strip()]
        if len(lines) > 1 and 'line' not in locator:
            raise ValueError(f'source.cell：R{row}C{col} 这一格有 {len(lines)} 行：'
                             + '；'.join(f'line {i}「{x}」' for i, x in enumerate(lines[:6], 1)) + '。用 line 指定登记哪一行')
        line = int(locator.get('line', 1))
        if not 1 <= line <= max(len(lines), 1):
            raise ValueError(f'source.cell：R{row}C{col} 只有 {len(lines)} 行，line 要在 1—{max(len(lines), 1)}')
        if not lines:
            raise ValueError(f'source.cell：R{row}C{col} 是空格，不是参数格')
        selected = lines[line-1]
        labels = [table.cell(row-1,0).text.strip(),table.cell(0,col-1).text.strip()]
        return selected, labels
    except (IndexError, KeyError, TypeError) as exc:
        try:
            size = '；'.join(f'表 {i} 有 {len(t.rows)} 行 {len(t.columns)} 列' for i, t in enumerate(tables, 1)) or '这一页没有表格'
        except NameError:
            size = '页码不存在'
        raise ValueError(f'source.cell：第 {page} 页没有这个坐标（{size}；行列从 1 数，含表头行）。'
                         '先 read_parameter_table 看清坐标') from exc


def scalar_tokens(text):
    return {Decimal(x) for x in re.findall(r'(?<![\d.])[-+]?\d+(?:\.\d+)?(?![\d.])', text)}


def read_ppt_tables(path, page):
    from pptx import Presentation
    if path.suffix.lower() != '.pptx' or page < 1:
        raise ValueError('需要PPTX和从1开始的页码')
    try:
        tables = [s.table for s in Presentation(path).slides[page-1].shapes if s.has_table]
    except IndexError as exc:
        raise ValueError('页码不存在') from exc
    return [{'table':i, 'rows':[{'row':j,'cells':[{'column':k,'lines':c.text.splitlines()}
               for k,c in enumerate(row.cells,1)]} for j,row in enumerate(table.rows,1)]}
             for i,table in enumerate(tables,1)]


def ppt_page_index(path):
    """Native page scope; captions after a table still belong to that same slide."""
    from pptx import Presentation
    if path.suffix.lower()!='.pptx':raise ValueError('需要PPTX文件')
    out=[]
    for page,slide in enumerate(Presentation(path).slides,1):
        texts=[s.text.strip() for s in slide.shapes if s.has_text_frame and s.text.strip()]
        out.append({'page':page,'context':texts,'table_count':sum(s.has_table for s in slide.shapes)})
    from bonus_agent.facts import norm
    for item in out:
        lines=[line.strip() for text in item['context'] for line in text.splitlines() if line.strip()]
        item['scope']=min(lines,key=lambda line:(sum(norm(line) in norm('\n'.join(p['context'])) for p in out),
                                                bool(re.search(r'\d',line)),len(line)),default='')
    return out


def parameter_cell(path,page,table,row,column,line=None):
    """Construct one fact's immutable value/quote/identity from physical source coordinates."""
    if any(type(x) is not int or x<1 for x in (page,table,row,column,*([line] if line is not None else []))):
        raise ValueError('source.cell：页、表、行、列及line须为从1开始的整数')
    locator={'table':table,'row':row,'column':column}
    if line is not None:locator['line']=line
    selected,labels=ppt_cell(path,page,locator)
    # Extract only when the selected physical line has one unambiguous number/unit.
    from bonus_agent.facts import _TOKEN, interval, band_text
    matches=list(_TOKEN.finditer(selected))
    cleaned = band_text(selected)
    if interval(selected) is not None:
        # 阶梯表的档位格（如“200-1000万”）：整段区间作为一个参数，编译阶梯时自动取下界
        value=selected
    elif len(matches) > 1 and len(list(_TOKEN.finditer(cleaned))) == 1:
        match = next(_TOKEN.finditer(cleaned))
        value = match.group(1) + (match.group(2) or '')
    elif len(matches)!=1:
        raise ValueError(f'source.cell：R{row}C{column}' + (f'L{line}' if line else '') + f' 的内容「{selected}」'
                         + ('没有数值，不是参数格（可能是标题或文字说明），不要登记' if not matches else
                            f'里有 {len(matches)} 个数值，分不清登记哪一个；同一格分多行写的用 line 指定行，'
                            '一行里有多个数的，用 record_facts 逐个登记（quote 写这一行，value 写其中一个数）'))
    else:
        match=matches[0];value=match.group(1)+(match.group(2) or '')
    return {'locator':locator,'value':value,'quote':'\n'.join([*labels,selected]),
            'name':f'{path.name}::P{page}.T{table}.R{row}.C{column}.L{line or 1}',
            'selected':selected,'labels':labels}


_UNIT_HEADER = ['制度表标题', '适用行', '适用列', '裸数默认单位']
_HEADING_UNIT = re.compile(r'[(（]\s*(%|‰|万元|亿元|元|万|亿)\s*[）)]$')


def scenario_unit_defaults(guides, source_hashes):
    """Index explicit rows of the financial scenario guide, retaining their literal evidence."""
    from bonus_agent.facts import scope_norm

    rules = {}
    for path in guides:
        if path.suffix.lower() != '.md':
            continue
        in_table = False
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            quote = line.strip()
            if not quote.startswith('|'):
                in_table = False
                continue
            cells = [col.strip() for col in quote.strip('|').split('|')]
            if cells == _UNIT_HEADER:
                in_table = True
                continue
            if not in_table or all(re.fullmatch(r':?-+:?', col) for col in cells):
                continue
            if len(cells) != 4 or not all(cells) or cells[3] not in ('%', '‰', '元', '万元', '亿元', '万', '亿'):
                raise ValueError(f'《{path.name}》默认单位声明无效：{quote}')
            title, rows, columns, unit = cells
            basis = {'file': path.name, 'quote': quote, 'source_hash': source_hashes[path.name]}
            for row in rows.split('、'):
                for column in columns.split('、'):
                    key = (scope_norm(title), scope_norm(row), scope_norm(column))
                    if key in rules and rules[key][0] != unit:
                        raise ValueError(f'《{path.name}》{title} / {row} / {column} 的默认单位有冲突')
                    rules[key] = (unit, basis)
    return rules


def apply_cell_unit(fact, path, defaults, pages, corpus):
    """Apply a declared default only at the physical title, row and column that own it."""
    from bonus_agent import facts as fx

    loc = fact.source_cell or {}
    if not defaults or path.suffix.lower() != '.pptx' or not fact.page or fact.component != 'literal':
        return fact
    key = (fact.file, fact.page)
    if key not in pages:
        if path not in pages:
            pages[path] = ppt_page_index(path)
        index = pages[path]
        if fact.page > len(index):
            raise ValueError(f'source.page：第 {fact.page} 页不存在')
        entry = index[fact.page - 1]
        titles = [line.strip() for text in entry['context'] for line in text.splitlines()
                  if any(fx.scope_norm(line) == k[0] for k in defaults)]
        # A slide-level title cannot identify which of several tables it describes.
        pages[key] = (titles, read_ppt_tables(path, fact.page)[0]['rows']) if titles and entry['table_count'] == 1 else ([], [])
    titles, rows = pages[key]
    if not titles:
        return fact
    if not loc:
        table_rows = [fx.norm(' '.join(v for cell in r['cells'] for v in cell['lines'])) for r in rows[1:]]
        quote = fx.norm(fact.quote)
        if fx.number(fact.value) is not None and (quote == fx.norm(fact.value) or
                any(quote and quote in row for row in table_rows) or
                any(fx.scope_norm(fact.context) == fx.scope_norm(title) for title in titles)):
            raise ValueError('source.cell：本页声明了按行列适用的默认单位，请提供原表 source_cell 或使用 record_parameter_cells')
        return fact
    row, col, line = loc.get('row'), loc.get('column'), loc.get('line', 1)
    if (loc.get('table') != 1 or any(type(n) is not int or n < 1 for n in (row, col, line))
            or row <= 1 or col <= 1 or row > len(rows) or col > len(rows[0]['cells'])):
        return fact
    labels = [' '.join(c['lines']).strip() for c in (rows[row-1]['cells'][0], rows[0]['cells'][col-1])]
    bare_labels = [fx.scope_norm(_HEADING_UNIT.sub('', label)) for label in labels]
    matches = [(t, defaults[(fx.scope_norm(t), *bare_labels)]) for t in titles
               if (fx.scope_norm(t), *bare_labels) in defaults]
    if not matches:
        return fact
    if len({entry[1][0] for entry in matches}) != 1:
        raise ValueError('parameter.unit：同一物理单元格有多种适用单位，请核对制度表标题')
    title, (unit, basis) = matches[0]
    physical = [v.strip() for v in rows[row-1]['cells'][col-1]['lines'] if v.strip()]
    if line > len(physical) or (len(physical) > 1 and 'line' not in loc):
        raise ValueError('source.cell：请按表内实际行号指定 line')
    selected = physical[line-1]
    if fact.context and fx.scope_norm(fact.context) not in fx.scope_norm(title):
        raise ValueError(f'source.scope：本单元格属于「{title}」，请按实际表标题登记 context')
    if fx.check_fact(fact.model_copy(update={'quote': selected, 'unit': None}), {fact.file: fx.norm(selected)}):
        raise ValueError(f'source.cell：单元格原文「{selected}」与登记值「{fact.value}」不符')
    native_quote = '\n'.join([*labels, selected])
    if fx.norm(fact.quote) == fx.norm(' '.join([labels[0], *physical])):
        # A row label plus one complete cell may be non-contiguous in the rendered Markdown row.
        fact = fact.model_copy(update={'quote': native_quote})
    fact = fact.model_copy(update={'context': title})
    headings = {m[1] for label in labels if (m := _HEADING_UNIT.search(label))}
    scalar = fx._NUM.fullmatch(unicodedata.normalize('NFKC', fact.value).strip())
    explicit = scalar.group(2) if scalar else None
    if len(headings | ({explicit} if explicit else set())) > 1:
        raise ValueError('parameter.unit：单元格与行列表头的明示单位冲突')
    if explicit or headings:
        native_unit = explicit or next(iter(headings))
        if fx.check_fact(fact.model_copy(update={'unit': None}), corpus):
            return fact  # a real header cannot repair an invented or wrong value quotation
        # Units such as 年 already belong to the literal and are not conversion hints.
        return fact.model_copy(update={'unit': None if explicit else native_unit, 'unit_basis': None, 'quote': native_quote})
    if fact.unit_basis and fact.unit_basis.get('file') != fact.file:
        if not fx.unit_basis_matches(fact, corpus):
            raise ValueError(f'parameter.unit：所引单位依据不适用于「{title}」，基本规则的单位为 {unit}')
        return fact
    if fact.unit_basis and fx.unit_basis_matches(fact, corpus):
        return fact  # an explicit policy in the source takes precedence over its default
    return fact.model_copy(update={'unit': unit, 'unit_basis': basis})
