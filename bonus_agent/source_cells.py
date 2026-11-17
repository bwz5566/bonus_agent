"""Read table coordinates from source documents; never locate a value by its amount."""
from decimal import Decimal
import re


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
