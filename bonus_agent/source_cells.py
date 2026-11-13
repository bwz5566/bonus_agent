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
        cell = table.cell(row-1, col-1).text
        lines = [x.strip() for x in cell.splitlines() if x.strip()]
        if len(lines) > 1 and 'line' not in locator:
            raise ValueError('单元格有多行适用条件，必须指定line')
        line = int(locator.get('line', 1))
        if line < 1:
            raise ValueError('line从1开始')
        selected = lines[line-1]
        labels = [table.cell(row-1,0).text.strip(),table.cell(0,col-1).text.strip()]
        return selected, labels
    except (IndexError, KeyError, TypeError) as exc:
        raise ValueError('source.cell：表格坐标不存在') from exc


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
