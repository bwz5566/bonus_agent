"""Physical source catalogue for the fixed commission compiler.

Models select coordinates, not copied numeric values or executable code. All
rows and parameter cells are re-read locally. Node prompts see only selected
source blocks; neither prior case drafts nor acceptance amounts are inputs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from collections import Counter
from decimal import Decimal

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter, column_index_from_string
from bonus_core.data.table import convert, to_date
from bonus_core.dsl.models import FieldDef


class ChainError(ValueError):
    """A specific, actionable failure of one fixed node."""

    def __init__(self, message, *, finance=False, examples=None, owner=None):
        super().__init__(message)
        self.finance = finance
        self.examples = examples or []
        self.owner=owner


def string(value):
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def number(value, where, *, blank=False):
    if value is None or string(value) == '':
        if blank:
            return None
        raise ChainError(f'{where}：金额/比例为空，需要原材料或空值规则', finance=True)
    try:
        return convert(value, FieldDef(type='number'))
    except ValueError as exc:
        raise ChainError(f'{where}：不是合法数值 {value!r}：{exc}', finance=True) from exc


def month(value, where):
    try:
        return to_date(value).strftime('%Y%m')
    except (ValueError, TypeError) as exc:
        raise ChainError(f'{where}：不能识别月份 {value!r}') from exc


@dataclass
class Sheet:
    id: str
    file: str
    name: str
    rows: list[tuple]

    def profile(self, sample=2):
        # Include first rows so a changed header row is still selectable.
        return dict(id=self.id, file=self.file, sheet=self.name, rows=len(self.rows),
                    preview=[{'row': i, 'cells': {get_column_letter(j): string(x)[:240]
                             for j, x in enumerate(row, 1) if x is not None}}
                             for i, row in enumerate(self.rows[:sample+1], 1)])

    def validate(self, spec, fields):
        if not isinstance(spec, dict) or spec.get('sheet') != self.id:
            raise ChainError(f'{self.id}：映射必须指向实际工作表 ID')
        header = spec.get('header_row', 1)
        if type(header) is not int or not 1 <= header < len(self.rows):
            raise ChainError(f'{self.id}：无效表头行 {header}')
        cols = spec.get('columns') or {}
        for field in fields:
            if field not in cols or cols[field] in ('', None):
                raise ChainError(f'{self.file}/{self.name}：缺少字段 {field}')
        for field, col in cols.items():
            if col in ('', None):
                continue
            if not isinstance(col, str) or not re.fullmatch(r'[A-Z]{1,3}', col):
                raise ChainError(f'{self.id}.{field}：列应使用物理列字母，不是虚构字段 {col!r}')
            i = column_index_from_string(col)
            if i > len(self.rows[header-1]) or self.rows[header-1][i-1] in (None, ''):
                raise ChainError(f'{self.id}.{field}：{col}{header} 不存在或无表头')
        required_cols = [cols[f] for f in fields]
        if len(set(required_cols)) != len(required_cols):
            raise ChainError(f'{self.id}：不同必需字段绑定到同一列 {required_cols}')
        return spec

    def records(self, spec, fields):
        self.validate(spec, fields)
        h = spec.get('header_row', 1)
        indices = {k: column_index_from_string(v)-1 for k, v in spec['columns'].items() if v}
        for n, row in enumerate(self.rows[h:], h+1):
            if all(x in ('', None) for x in row):
                continue
            yield {k: row[i] if i < len(row) else None for k, i in indices.items()}, f'{self.file}/{self.name}!{n}', row


@dataclass
class ParameterTable:
    id: str
    file: str
    page: int
    table: int
    title: str
    rows: list[list[str]]

    def cell(self, row, col):
        if type(row) is not int or type(col) is not int or not (1 <= row <= len(self.rows)) or not (1 <= col <= len(self.rows[row-1])):
            raise ChainError(f'{self.id}：不存在的参数格 R{row}C{col}')
        return self.rows[row-1][col-1]

    def location(self, row, col):
        return f'{self.file}/P{self.page}.T{self.table}.R{row}.C{col}'

    def profile(self):
        return dict(id=self.id, file=self.file, page=self.page, title=self.title,
                    rows=[{'row':i, 'cells':row} for i,row in enumerate(self.rows,1)])


class Catalog:
    def __init__(self, paths):
        self.sheets, self.tables, self.blocks = {}, {}, {}
        self.paths = [Path(p) for p in paths]
        for path in self.paths:
            suffix = path.suffix.lower()
            if suffix in ('.xlsx', '.xlsm'):
                book = load_workbook(path, read_only=True, data_only=True)
                try:
                    for ws in book:
                        rows = [tuple(x) for x in ws.values]
                        if not rows:
                            continue
                        sid = f'S{len(self.sheets)+1}'
                        self.sheets[sid] = Sheet(sid, path.name, ws.title, rows)
                        # Rule instruction sheets are identified by shape and prose,
                        # not filename. Large business ledgers are not prompt text.
                        if len(rows) <= 100 and max(map(len, rows), default=0) <= 5:
                            rich = [r for r in rows if len(r)>1 and isinstance(r[1],str) and len(r[1])>=12]
                            if len(rich)>=3:
                                for i,row in enumerate(rows,1):
                                    self._block(path.name,f'{ws.title}!{i}', ' | '.join(string(x) for x in row if x is not None))
                finally:
                    book.close()
            elif suffix == '.pptx':
                from pptx import Presentation
                prs=Presentation(path)
                for page,sl in enumerate(prs.slides,1):
                    prose = [s.text.strip() for s in sl.shapes if s.has_text_frame and s.text.strip()]
                    title = ' | '.join(prose)
                    for s in prose:
                        self._block(path.name, f'第{page}页', s)
                    for index, s in enumerate((s for s in sl.shapes if s.has_table),1):
                        tid = f'T{len(self.tables)+1}'
                        self.tables[tid] = ParameterTable(tid,path.name,page,index,title,[[c.text for c in r.cells] for r in s.table.rows])
            elif suffix in ('.md','.txt'):
                # Keep headings on their own, preserving line locations.
                for n, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(),1):
                    self._block(path.name,f'第{n}行',line)
            else:
                from bonus_agent.parsing.docs import parse_any
                doc = parse_any(path)
                for idx, block in enumerate(doc.blocks,1):
                    self._block(path.name,str(getattr(block,'page',idx)),str(getattr(block,'text','')))

    def _block(self, file, place, text):
        text = str(text).strip()
        if text:
            self.blocks[f'R{len(self.blocks)+1}'] = dict(file=file, where=place, text=text)

    def table(self, id):
        if id not in self.tables:
            raise ChainError(f'原始制度中不存在表格 {id!r}，请使用当前节点给出的 ID')
        return self.tables[id]

    def sheet(self, spec):
        sid = spec.get('sheet') if isinstance(spec,dict) else None
        if sid not in self.sheets:
            raise ChainError(f'原始材料中不存在工作表 {sid!r}')
        return self.sheets[sid]

    def profiles(self, keywords=(), sample=2):
        selected = [v for v in self.sheets.values() if not keywords or any(k.casefold() in (v.file+' '+v.name+' '+str(v.rows[:1])).casefold() for k in keywords)]
        # A compact index of all sheets makes retrieval misses visible; only
        # the relevant sheets get their physical row/column previews.
        return dict(index=[dict(id=s.id,file=s.file,sheet=s.name,rows=len(s.rows)) for s in self.sheets.values()],
                    previews=[s.profile(sample) for s in selected])

    def sources(self, keywords=(), *, table_keywords=()):
        # Original documents only. Authorized amendments and exact feedback
        # excerpts are attached explicitly to their responsible node.
        blocks={k:v for k,v in self.blocks.items() if k.startswith('R') and (not keywords or any(w.casefold() in v['text'].casefold() for w in keywords))}
        tables=[v.profile() for v in self.tables.values() if table_keywords and any(w.casefold() in (v.title+' '+str(v.rows[0])).casefold() for w in table_keywords)]
        return dict(text=blocks,tables=tables)

    def evidence(self, contract, required, allowed):
        ev=contract.get('evidence') or {}
        for key in required:
            source=ev.get(key)
            if not isinstance(source,dict) or source.get('id') not in allowed:
                raise ChainError(f'{key} 缺少本节点提供的实际原文 ID')
            raw=self.blocks[source['id']]['text']
            quote=source.get('quote')
            norm=lambda x:re.sub(r'\s+','',str(x))
            if not quote or norm(quote) not in norm(raw):
                raise ChainError(f'{key} 的引用不在 {source["id"]} 原文：{raw[:300]}')
        return {k:dict(**self.blocks[v['id']],quote=v['quote']) for k,v in ev.items() if isinstance(v,dict) and v.get('id') in self.blocks and 'quote' in v}


def project_key(row, where):
    key=(month(row.get('month'),where), string(row.get('code')),string(row.get('customer')),string(row.get('product')))
    if any(not p for p in key) or any('|' in p for p in key):
        raise ChainError(f'{where}：项目四键不完整或包含分隔符 {key}')
    return '|'.join(key)


def pivot_label(value):
    return string(value) in ('总计','合计','Grand Total','(空白)','（空白）')


def population(catalog, mapping, period, policy):
    """Only sales create projects. Reference tables never create entities."""
    sheet=catalog.sheet(mapping['sales']); rows=[]; excluded=Counter()
    for row, where, raw in sheet.records(mapping['sales'], ('month','code','customer','product','region','amount','currency','fx','series')):
        if pivot_label(row.get('month')):
            excluded['透视汇总/占位']+=1;continue
        if month(row['month'],where)!=period:
            excluded['非本期']+=1;continue
        if string(row.get('exclude')) in policy['filter']['exclude_values']:
            excluded['书面排除标记']+=1;continue
        amt=number(row['amount'],where)
        if policy['filter']['exclude_zero'] and amt==0:
            excluded['零销售']+=1;continue
        key=project_key(row,where)
        if not string(row.get('region')):
            raise ChainError(f'{where}：项目区域为空',finance=True)
        rows.append(dict(key=key,amount=amt,region=string(row['region']),customer=string(row['customer']),
                         code=string(row['code']),product=string(row['product']),currency=string(row['currency']),
                         fx=number(row['fx'],where,blank=True),series=string(row['series']),where=where))
    if not rows:
        raise ChainError('当期有效销售集合为空，核对期间及销售表/排除列')
    return rows,dict(excluded)
