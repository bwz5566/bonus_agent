"""Physical source catalogue for the fixed commission compiler.

Models select coordinates, not copied numeric values or executable code. All
rows and parameter cells are re-read locally. Node prompts see only selected
source blocks; neither prior case drafts nor acceptance amounts are inputs.
"""
from __future__ import annotations

import re
import csv
import io
import json
import hashlib
from dataclasses import dataclass
from pathlib import Path
from collections import Counter
from decimal import Decimal
from functools import cached_property

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
    formats: dict[tuple[int,int],str] | None = None

    def literal(self, row, col, *, display=False):
        """Read a real coordinate. Preserve styled IDs and percentage units.

        Raw numeric data stay numeric. Only an explicitly requested display
        cell (parameter evidence or an ID field) uses the source number format.
        """
        if type(row) is not int or type(col) is not int or row<1 or col<1 or row>len(self.rows) or col>len(self.rows[row-1]):
            raise ChainError(f'{self.file}/{self.name}：不存在的单元格 R{row}C{col}')
        value=self.rows[row-1][col-1]
        if display and isinstance(value,(int,float,Decimal)) and not isinstance(value,bool):
            fmt=(self.formats or {}).get((row,col),'')
            if '%' in fmt:
                return format(Decimal(str(value))*100,'f')+'%'
            if re.fullmatch(r'0{2,}',fmt) and Decimal(str(value))==int(value):
                return str(int(value)).zfill(len(fmt))
        return value

    @cached_property
    def _ranked_headers(self):
        """Content-based headers at actual rows, independent of file names.

        A title/blank prefix must not hide the actual headers. Prefer text
        labels followed by populated data, but keep alternative candidates
        rather than silently deciding an ambiguous multi-header workbook.
        """
        scores=[]
        def label(x):
            return isinstance(x,str) and bool(x.strip()) and len(x.strip())<=120 and not re.fullmatch(r'[+-]?[\d.,]+\s*%?',x.strip())
        for i,row in enumerate(self.rows[:-1]):
            filled=[j for j,x in enumerate(row) if x not in (None,'')]
            if len(filled)<2:continue
            labels=[string(row[j]) for j in filled if label(row[j])]
            if len(labels)<max(2,(len(filled)+1)//2):continue
            next_rows=self.rows[i+1:i+4]
            following=max((sum(j<len(n) and n[j] not in (None,'') for j in filled) for n in next_rows),default=0)
            if following<2:continue
            numeric=max((sum(j<len(n) and n[j] not in (None,'') and not label(n[j]) for j in filled) for n in next_rows),default=0)
            score=(len(labels)/len(filled)*4 + len(set(labels))/len(labels)*2 + following/len(filled)*2 + numeric/len(filled)*3)
            scores.append((score,i+1))
        # The earliest plausible header is also exposed; later repeated
        # sections or all-text records cannot permanently hide it.
        first=scores[0][1] if scores else next((i+1 for i,r in enumerate(self.rows[:-1]) if any(x not in (None,'') for x in r)),1)
        ranked=[i for _,i in sorted(scores,key=lambda t:(-t[0],t[1]))[:8]]
        return tuple(dict.fromkeys([first]+ranked))

    def header_candidates(self, limit=4):
        return list(self._ranked_headers[:limit])

    def profile(self, sample=2, *, compact=False):
        def preview(i):
            return {'row':i,'cells':{get_column_letter(j):string(self.literal(i,j,display=True))[:240]
                    for j,x in enumerate(self.rows[i-1],1) if x is not None}}
        candidates=self.header_candidates(2 if compact else 4)
        # Index all physical headers, plus limited sample rows below them.
        visible=list(dict.fromkeys([i for h in candidates for i in range(h,min(len(self.rows),h+(1 if compact else sample))+1)]))
        if not visible:visible=list(range(1,min(len(self.rows),sample+1)+1))
        return dict(id=self.id, file=self.file, sheet=self.name, rows=len(self.rows),
                    header_candidates=[preview(i) for i in candidates if 1<=i<=len(self.rows)],
                    preview=[preview(i) for i in visible])

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
    sheet_name: str = ''
    row_offset: int = 0
    col_offset: int = 0

    def cell(self, row, col):
        if type(row) is not int or type(col) is not int or not (1 <= row <= len(self.rows)) or not (1 <= col <= len(self.rows[row-1])):
            raise ChainError(f'{self.id}：不存在的参数格 R{row}C{col}')
        return self.rows[row-1][col-1]

    def location(self, row, col):
        if self.sheet_name:
            return f'{self.file}/{self.sheet_name}!{get_column_letter(col+self.col_offset)}{row+self.row_offset}'
        return f'{self.file}/P{self.page}.T{self.table}.R{row}.C{col}'

    def profile(self):
        return dict(id=self.id, file=self.file, page=self.page, title=self.title,
                    sheet=self.sheet_name or None, origin=self.location(1,1),
                    rows=[{'row':i, 'cells':row} for i,row in enumerate(self.rows,1)])


class Catalog:
    def __init__(self, paths):
        self.sheets, self.tables, self.blocks = {}, {}, {}
        self.unreadable_blocks=[]
        self.decorative_blocks=[]
        self.authorized_image_exclusions={}
        deferred_tables=[]
        deferred_blocks=[]
        self.paths = [Path(p) for p in paths]
        for path in self.paths:
            suffix = path.suffix.lower()
            if suffix in ('.xlsx', '.xlsm'):
                book = load_workbook(path, read_only=True, data_only=True)
                try:
                    for ws in book:
                        rows=[];formats={}
                        for r,cells in enumerate(ws.iter_rows(),1):
                            rows.append(tuple(x.value for x in cells))
                            for j,cell in enumerate(cells,1):
                                fmt=getattr(cell,'number_format',None)
                                if fmt and ('%' in fmt or re.fullmatch(r'0{2,}',fmt)):
                                    formats[(r,j)]=fmt
                        if not rows:
                            continue
                        sid = f'S{len(self.sheets)+1}'
                        self.sheets[sid] = Sheet(sid, path.name, ws.title, rows,formats)
                        # Rule instruction sheets are identified by shape and prose,
                        # not filename. Large business ledgers are not prompt text.
                        if len(rows) <= 100 and max(map(len, rows), default=0) <= 5:
                            rich = [r for r in rows if len(r)>1 and isinstance(r[1],str) and len(r[1])>=12]
                            if len(rich)>=3:
                                for i,row in enumerate(rows,1):
                                    self._block(path.name,f'{ws.title}!{i}', ' | '.join(string(x) for x in row if x is not None))
                finally:
                    book.close()
            elif suffix in ('.csv','.tsv'):
                raw=path.read_bytes()
                try:decoded=raw.decode('utf-8-sig')
                except UnicodeDecodeError:decoded=raw.decode('gb18030')
                try:dialect=csv.Sniffer().sniff(decoded[:8192],delimiters=',;\t')
                except csv.Error:dialect=csv.excel
                if suffix=='.tsv':dialect=csv.excel_tab
                rows=[tuple(r) for r in csv.reader(io.StringIO(decoded,newline=''),dialect)]
                if rows:
                    sid=f'S{len(self.sheets)+1}'
                    self.sheets[sid]=Sheet(sid,path.name,path.stem,rows)
            elif suffix=='.json':
                raw=path.read_text(encoding='utf-8-sig')
                try:value=json.loads(raw)
                except json.JSONDecodeError as exc:raise ChainError(f'{path.name} JSON 数据不合法：{exc}') from exc
                # Structured record arrays are a business data source, not a
                # prompt blob. Other JSON documents remain literal text.
                arrays={'records':value} if isinstance(value,list) else {k:v for k,v in value.items() if isinstance(v,list)} if isinstance(value,dict) else {}
                tables={k:v for k,v in arrays.items() if v and all(isinstance(x,dict) and all(not isinstance(z,(list,dict)) for z in x.values()) for x in v)}
                if tables and (isinstance(value,list) or set(tables)==set(value)):
                    for name,records in tables.items():
                        headers=list(dict.fromkeys(k for row in records for k in row))
                        rows=[tuple(headers),*[tuple(row.get(k) for k in headers) for row in records]]
                        sid=f'S{len(self.sheets)+1}';self.sheets[sid]=Sheet(sid,path.name,str(name),rows)
                else:
                    self._block(path.name,'JSON原文',raw)
            elif suffix == '.pptx':
                from pptx import Presentation
                from pptx.enum.shapes import MSO_SHAPE_TYPE
                prs=Presentation(path)
                pictures=[]
                def visit_nested(shape,page,title,native):
                    if shape.shape_type==MSO_SHAPE_TYPE.GROUP:
                        for child in shape.shapes:visit_nested(child,page,title,native)
                    elif shape.has_table:
                        deferred_tables.append((path.name,page,shape.shape_id,title,[[c.text for c in r.cells] for r in shape.table.rows]))
                    elif getattr(shape,'has_chart',False):
                        chart=shape.chart
                        cats=list(chart.plots[0].categories) if chart.plots else []
                        rows=[['类别']+[s.name for s in chart.series]]+[[str(c)]+[string(s.values[i]) for s in chart.series] for i,c in enumerate(cats)]
                        deferred_tables.append((path.name,page,shape.shape_id,title,rows))
                    elif shape.shape_type==MSO_SHAPE_TYPE.PICTURE:
                        pictures.append({'file':path.name,'where':f'第{page}页/{shape.name}/{shape.shape_id}','page':page,
                            'digest':hashlib.sha256(shape.image.blob).hexdigest(),'area':float(shape.width*shape.height/(prs.slide_width*prs.slide_height)),'native_content':native})
                    elif shape.has_text_frame and shape.text.strip():deferred_blocks.append((path.name,f'第{page}页/{shape.name}',shape.text.strip()))
                for page,sl in enumerate(prs.slides,1):
                    prose = [s.text.strip() for s in sl.shapes if s.has_text_frame and s.text.strip()]
                    title = ' | '.join(prose)
                    for s in prose:
                        self._block(path.name, f'第{page}页', s)
                    for index, s in enumerate((s for s in sl.shapes if s.has_table),1):
                        tid = f'T{len(self.tables)+1}'
                        self.tables[tid] = ParameterTable(tid,path.name,page,index,title,[[c.text for c in r.cells] for r in s.table.rows])
                    native=bool(prose or any(s.has_table for s in sl.shapes))
                    for shape in sl.shapes:
                        if shape.shape_type in (MSO_SHAPE_TYPE.PICTURE,MSO_SHAPE_TYPE.GROUP) or getattr(shape,'has_chart',False):
                            visit_nested(shape,page,title,native)
                    # Speaker notes can carry an operative rule. Append after
                    # original block IDs for compatibility with saved v1 contracts.
                    if sl.has_notes_slide:
                        notes=sl.notes_slide.notes_text_frame.text.strip()
                        if notes:deferred_blocks.append((path.name,f'第{page}页备注',notes))
                counts=Counter(v['digest'] for v in pictures)
                for item in pictures:
                    item['repeat_count']=counts[item['digest']]
                    if item['area']<=.02 and item['repeat_count']>=2 and item['native_content']:
                        # Small identical branding element repeated beside
                        # native material; keep the structural evidence.
                        self.decorative_blocks.append({**item,'reason':'多页同一图像指纹、占版面不超过2%、所在页有独立原生文字/表格'})
                    else:self.unreadable_blocks.append({**item,'reason':'图像可能含未转写业务规则；需可核对原文或真实排除确认'})
            elif suffix in ('.md','.txt'):
                # Keep headings on their own, preserving line locations.
                for n, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(),1):
                    self._block(path.name,f'第{n}行',line)
            else:
                from bonus_agent.parsing.docs import parse_any
                doc = parse_any(path)
                title=''
                for idx, block in enumerate(doc.blocks,1):
                    place=str(block.page if block.page is not None else idx)
                    self._block(path.name,place,str(block.text or ''))
                    if block.kind=='text':title=block.text[:240]
                    if block.kind in ('table','chart') and block.cells:
                        deferred_tables.append((path.name,block.page or idx,idx,title,[[string(x) for x in r] for r in block.cells]))
                    if block.kind=='image':self.unreadable_blocks.append({'file':path.name,'where':place,'reason':'图像型材料未提供可核对的文字或物理表格'})
        # Every spreadsheet is addressable as a data source; smaller matrices
        # also get rule-table IDs. Large rule matrices remain addressable via
        # physical sheet cells in the adaptive compiler. Append T IDs after the
        # original PPT tables so old frozen contracts keep their coordinates.
        for sheet in self.sheets.values():
            if len(sheet.rows)>200 or max(map(len,sheet.rows),default=0)>32:continue
            if not sheet.rows:continue
            for h in sheet.header_candidates(1):
                if len(sheet.rows)-h<1:continue
                rows=[[string(sheet.literal(i,j,display=True)) for j in range(1,len(sheet.rows[i-1])+1)]
                      for i in range(h,len(sheet.rows)+1)]
                tid=f'T{len(self.tables)+1}'
                self.tables[tid]=ParameterTable(tid,sheet.file,0,1,f'{sheet.file} / {sheet.name}',rows,sheet.name,h-1)
        for file,page,index,title,rows in deferred_tables:
            tid=f'T{len(self.tables)+1}';self.tables[tid]=ParameterTable(tid,file,page,index,title,rows)
        for file,place,text in deferred_blocks:self._block(file,place,text)

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
        def preferred(s):
            headers=' '.join(str(s.rows[i-1]) for i in s.header_candidates() if 1<=i<=len(s.rows))
            return not keywords or any(k.casefold() in (s.file+' '+s.name+' '+headers).casefold() for k in keywords)
        matches={s.id:preferred(s) for s in self.sheets.values()}
        ranked=sorted(self.sheets.values(),key=lambda s:not matches[s.id])
        # Keywords rank, never hide. Unfamiliar names still expose their true
        # header coordinates and a sample, so a new source can be bound.
        return dict(index=[dict(id=s.id,file=s.file,sheet=s.name,rows=len(s.rows)) for s in self.sheets.values()],
                    previews=[dict(s.profile(sample,compact=not matches[s.id]),preferred=matches[s.id]) for s in ranked])

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
