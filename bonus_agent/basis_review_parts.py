"""Build a blind review declaration in independently validated, resumable pieces."""

import copy
import json

from bonus_agent import reread
from bonus_agent.agent import ToolError
from bonus_agent.clauses import check_quote
from bonus_agent.facts import MAX_QUOTE, norm
from bonus_agent.llm import LLMError, RunBudgetError, parse_json
from bonus_agent.recovery import fingerprint
from bonus_agent.review_population import check_populations
from bonus_core.engine.evaluator import EvalError
from bonus_core.engine.issues import EngineError


def groups(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def source_catalog(ws, evidence=None):
    """Expose only verified original excerpts and sheet headers, never ledger explanations or calculated rows."""
    corpus = ws._corpus()
    visible = norm(evidence if evidence is not None else reread.material(ws))
    sources = {}
    def add(doc, text):
        quote = text.strip()
        if not 3 <= len(quote) <= MAX_QUOTE or len(sources.get(doc, {})) >= 48:
            return
        literal = norm(quote)
        if literal not in visible or literal not in corpus.get(doc, '') or check_quote(doc, quote, corpus):
            return
        ref = {'doc': doc, 'quote': quote}
        sources.setdefault(doc, {})['src_' + fingerprint(ref)[:16]] = ref

    for doc in sorted(ws.inv.docs, key=lambda d: d.name):
        for line in ws._lines(doc.name):
            if len(sources.get(doc.name, {})) >= 48:
                break
            if not line.strip().startswith('#'):
                add(doc.name, line)
    for sheet in sorted(ws.inv.sheets, key=lambda s: (s.file, s.sheet)):
        # Synthetic or combined headers which don't occur in the workbook itself are excluded by add().
        add(sheet.file, ' | '.join(str(h) for h in sheet.headers[:8]))
    authorized = {p.name for p in ws.inv.files}
    for doc, quote in sorted({(c.file, c.quote) for c in ws.clauses if c.file in authorized}):
        add(doc, quote)
    catalog = {}
    for offset in range(48):
        for doc in sorted(sources):
            entries = list(sources[doc].items())
            if offset < len(entries) and len(catalog) < 120:
                key, ref = entries[offset]
                catalog[key] = ref
    return catalog


class PopulationDifference(Exception):
    def __init__(self, report, selection):
        self.report = report
        self.selection = selection


class InvalidPopulations(ValueError):
    def __init__(self, accepted, errors):
        self.accepted, self.errors = accepted, errors
        super().__init__('范围声明未通过合同校验：' + _json(errors))


class BasisReviewParts:
    def __init__(self, flow, name, rs, evidence, structure, signature):
        self.ws, self.flow, self.name, self.rs = flow.ws, flow, name, rs
        self.evidence, self.structure, self.signature = evidence, structure, signature
        self.records = flow.state.setdefault('basis_review_parts', {}).setdefault(name, {})
        self.base = structure['base']

    @staticmethod
    def visible_structure(flow, name, structure, evidence=None):
        base = {k: structure[k] for k in ('scheme', 'pool_scope', 'period', 'allocation', 'columns',
                                         'source_mapping', 'data_scope')}
        base['upstream_schemes'] = []
        for scheme, revision in sorted(flow.ws._deps(name)):
            if scheme in flow.ws.schemes:
                upstream = flow.ws._ruleset(scheme)
                base['upstream_schemes'].append({'scheme': scheme, 'revision': revision,
                    'pool_scope': upstream.pool.scope, 'levels': [{'level': lv.level, 'key': lv.key} for lv in upstream.allocation],
                    'approved': flow.current('approved:' + scheme)})
        return {'base': base, 'population_requirements': structure['population_requirements'],
                'dataset_names': sorted(flow.ws.datasets), 'source_catalog': source_catalog(flow.ws, evidence),
                'reports': [(field, row[:2]) for field in ('details', 'totals') for row in structure[field]]}

    def _part(self, section, targets, instruction, contract, validate):
        sys = ('【独立总额分段复核】按原材料独立重建本段声明。只输出本段的 JSON 对象，其他段由程序处理。'
               '已列出的映射、范围、期间和分类是待核事实。核对当期对象、历史累计窗口、空值和真实分类。'
               'unknown、values_truncated 或 fields_complete=false 均不证明未展示的值不存在。'
               '业务口径无法从原文或财务答复确定时用 unclear 字符串数组具体指出问题；程序计算金额。' + instruction)
        prefix = self.evidence + '\n独立复核结构：' + _json(self.base)
        if section == 'populations':
            prefix += '\n允许的标准数据集名称：' + _json(self.structure['dataset_names'])
            prefix += '\n可选来源目录 source_catalog：' + _json(self.structure['source_catalog'])
        prefix += '\n本段输入：'
        messages = [{'role': 'system', 'content': sys}, {'role': 'user', 'content': prefix + _json(contract)}]
        checker = self.ws.llm.checker_llm
        sig = fingerprint((self.signature, section, messages, getattr(checker, 'basis_review_reasoning_effort', None)))
        row = self.records.setdefault(sig, {'section': section, 'targets': targets})
        if row.get('state') in ('valid', 'unresolved'):
            if section == 'populations' and row['state'] == 'valid':
                try:
                    validate(row['value'])  # Reconcile the frozen selection against the current production scope.
                except ValueError as exc:
                    raise ToolError(f'已保存的独立范围声明无法验证：{exc}') from None
            return copy.deepcopy(row['value'])
        key = f'{self.name}:part:{sig}'
        attempts = self.flow.state['attempts']
        before = attempts.get(key, 0)
        accepted = row.get('accepted', {})
        if accepted:
            self._validate_populations({'populations': accepted}, list(accepted))
            if self.population_report['status'] != 'PASS':
                raise PopulationDifference(self.population_report, accepted)
            pending = [label for label in targets if label not in accepted]
            messages[1]['content'] = prefix + _json({k: {n: values[n] for n in pending} for k, values in contract.items()})
        if before >= 2:
            raise ToolError(f'独立复核分段 {section} {targets} 已达两次尝试，诊断：{row.get("diagnostic", "请求未完成")}')
        if row.get('diagnostic'):
            messages.append({'role': 'user', 'content': '上一份本段声明的程序诊断：' + row['diagnostic'] +
                             '。请仅按原材料修复本段。'})
        with reread.basis_part_request_policy(self.ws):
            model = reread.guard_request(self.ws, messages, checker_only=True)
            attempts[key] = before + 1
            try:
                raw = reread._ask(self.ws, messages, checker_only=True)
            except RunBudgetError as exc:
                call = getattr(model, 'last_call', {})
                if getattr(exc, 'request_not_sent', False) or (call.get('status') == 'budget_blocked' and
                        call.get('request_attempts') == [] and not call.get('budget_request_sequence') and
                        not call.get('response_received')):
                    attempts[key] = before
                raise
            except LLMError as exc:
                row.update(state='error', checker_error=str(exc)[:300])
                raise
            finally:
                call = getattr(model, 'last_call', {})
                row['request'] = {k: call[k] for k in ('model', 'status', 'finish_reason', 'response_received') if k in call}
        try:
            try:
                value = parse_json(raw)
            except (LLMError, ValueError):
                raise ValueError('未输出可解析的完整 JSON 对象') from None
            if not isinstance(value, dict) or not isinstance(value.get('unclear', []), list) or any(
                    not isinstance(q, str) or not q.strip() for q in value.get('unclear', [])):
                raise ValueError('须输出 JSON 对象，unclear 如有内容必须是非空字符串数组')
            if value.get('unclear'):
                row.update(state='unresolved', value={'unclear': value['unclear']})
                return copy.deepcopy(row['value'])
            if accepted:
                returned = value.get('populations')
                if not isinstance(returned, dict) or set(returned) - set(pending):
                    raise ValueError('补充声明只能包含本次剩余的范围名，禁止重写已保存的独立范围')
                value['populations'] = {**accepted, **returned}
            validate(value)
        except PopulationDifference as exc:
            row.update(state='partial', accepted=copy.deepcopy(exc.selection))
            raise
        except InvalidPopulations as exc:
            row.update(state='invalid', accepted=copy.deepcopy(exc.accepted), contract_errors=exc.errors, diagnostic=str(exc))
            raise ToolError(f'独立复核分段 {section} {targets} 未通过：{exc}') from None
        except ValueError as exc:
            row.update(state='invalid', diagnostic=str(exc))
            raise ToolError(f'独立复核分段 {section} {targets} 未通过：{exc}') from None
        row.update(state='valid', value=copy.deepcopy(value))
        row.pop('diagnostic', None)
        row.pop('accepted', None)
        row.pop('contract_errors', None)
        return value

    @staticmethod
    def _shape(value, fields):
        if set(value) - {'unclear'} != set(fields):
            raise ValueError('只允许并须完整填写本段字段：' + '、'.join(fields))
        for field in fields:
            if field != 'pool' and not isinstance(value[field], dict):
                raise ValueError(f'{field} 须为 JSON 对象')

    def _validate_populations(self, value, labels):
        self._shape(value, ['populations'])
        if set(value['populations']) - set(labels):
            raise ValueError('populations 须且仅须覆盖：' + '、'.join(labels))
        invalid_ids = set()
        for label, cfg in value['populations'].items():
            if not isinstance(cfg, dict) or not isinstance(cfg.get('sources'), list):
                continue
            for i, source in enumerate(cfg['sources']):
                if isinstance(source, dict) and 'source_id' in source:
                    ident = source['source_id']
                    ref = self.structure['source_catalog'].get(ident) if isinstance(ident, str) else None
                    if set(source) == {'source_id'} and ref:
                        cfg['sources'][i] = copy.deepcopy(ref)
                    else:
                        invalid_ids.add(label)
                        cfg['sources'][i] = {}  # Cannot masquerade as a literal citation using extra fields.
        _, root, levels, _ = self.ws._independent_env(self.name, {}, basis=True)
        report = check_populations(self.ws, self.rs, value, root, levels, scope_names=labels)
        if not report['contract_valid']:
            valid = {r['scope']: value['populations'][r['scope']] for r in report['checks']}
            if any(r['status'] == 'FAIL' for r in report['checks']):
                checked = check_populations(self.ws, self.rs, {'populations': valid}, root, levels, scope_names=valid)
                raise PopulationDifference(checked, valid)
            errors = [{**e, 'code': 'invalid_source_catalog_id'} if e['scope'] in invalid_ids else e
                      for e in report['contract_errors']]
            raise InvalidPopulations(valid, errors)
        self.population_report = report

    def _validate_formulas(self, value, pool=None, items=()):
        self._shape(value, ['details', 'totals'] if pool else ['pool', 'helpers', 'tables'])
        if pool:
            for field in ('details', 'totals'):
                if set(value[field]) != {r[1][0] for r in items if r[0] == field}:
                    raise ValueError(f'{field} 须且仅须覆盖本段指定的报告名')
        expressions = ([(row[0], row[1], value[field][row[0]]) for field, row in items] if pool else
                       [('pool', self.rs.pool.scope, value['pool'])])
        if any(not isinstance(expr, str) or not expr.strip() for _, _, expr in expressions):
            raise ValueError('每个独立算式必须为非空字符串')
        current = 'helpers/tables'
        try:
            _, root, levels, ev = self.ws._independent_env(self.name, pool or value, basis=True)
            for current, level, expr in expressions:
                nodes = [root] if level == 'global' else levels.get(level) or list((root.index or {}).get(level, {}).values())
                if not nodes:
                    raise ValueError('核算层级没有对象')
                self.ws._independent_eval(ev, expr, nodes, current)
            self.ws._independent_holes(ev.rs, ev)
        except (ToolError, EvalError, EngineError, ValueError):
            # Provider input may contain only source data and independent declarations. Engine exceptions
            # can contain evaluated amounts or internal node values and must stay on this side of the check.
            raise ValueError(f'独立算式 {current} 未通过 DSL 或求值校验。请按字段、层级、分档和语法检查') from None

    def generate(self):
        result = {'details': {}, 'totals': {}, 'populations': {}, 'unclear': []}
        required = self.structure['population_requirements']
        for labels in groups(list(required), 4):
            template = {k: {'dataset': required[k].get('dataset', '<能据原文识别合格对象的标准数据集>'),
                            **({'key': '<该来源的标准对象标识>'} if required[k]['kind'] == 'objects' else {}),
                            'filter': '<原始列名构成的JSON筛选对象>', 'period': '<标准行日期字段或JSON null>',
                            'window': '<根据原文选择 period/year_to_date/to_date>',
                            'sources': [{'doc': '<材料名>', 'quote': '<支持该范围的逐字引文>'}]} for k in labels}
            part = self._part('populations', labels,
                '输出 {"populations":{"范围名":{"dataset":"来源表","key":"对象标识字段",'
                '"filter":{},"period":null,"window":"period","sources":[{"doc":"文件","quote":"逐字引文"}]}},"unclear":[]}。'
                'kind=objects 时范围名是核算层级。按原文选择能直接识别合格对象的来源数据集；'
                '可以是业务明细或实体清单，不要求来源与层级同名或自身按该 key 唯一。'
                '例如原文要求“当期有合格交易的门店”时，可选包含门店标识、交易日期和资格列的交易明细；'
                'key 填该明细中的门店标识标准字段，period 填交易日期标准字段，按原文填写 filter/window。'
                '程序从原表取行，按筛选和期间窗口取出合格行，再按 key 去重比对对象集合。'
                'population_from 是待核生产映射，实现上述声明无需复制该字段，也不应仅因输出没有该字段就报 unclear。'
                'kind=rows 时 dataset 必须为要求核对的来源表，按物理行比对、不去重，无需 key。'
                'filter 用原表列名；period 填标准日期字段或明确 null，window 为 period/year_to_date/to_date。'
                '月度横表若原材料以月份列选择当期、没有行日期，则 period 可填 JSON null；'
                '选当月金额列与筛选业务行分别判定，金额列不能充当行日期字段。'
                '占位或小计行是否参与须有独立材料依据；原列排除可写 {"列名":{"not":["占位标签"]}}。'
                'output_template 按本次请求逐个给出 JSON 键；所有尖括号占位符都要据原文换成正确类型的声明。'
                'sources 引用支持本次对象、资格或期间口径的原文。材料确有歧义、原表缺少关键字段，'
                '或无法用以上方式表示时，说明具体缺口并保留 unclear；示例条件不能替代本批依据。'
                'dataset 必须从允许的标准数据集名称中逐字选取，文件名和工作表名用于引用材料。'
                '每条 sources 可采用 {"source_id":"目录中的完整ID"} 或 {"doc":"材料名","quote":"原文"}；'
                '程序将 ID 展开后逐字核验。目录仅帮助定位；表头只可证明表结构，只有原文确实支持本范围时才引用。',
                {'population_requirements': {k: required[k] for k in labels}, 'output_template': template},
                lambda value: self._validate_populations(value, labels))
            if part.get('unclear'):
                return part
            result['populations'].update(part['populations'])
            if self.population_report['status'] != 'PASS':
                raise PopulationDifference(self.population_report, part['populations'])
        # Descriptions are free-form production draft text and may include its formula or result.
        reports = self.structure['reports']
        pool = self._part('pool', ['pool'],
            '输出 {"pool":"独立表达式","helpers":{"变量":{"scope":"层级或global","expr":"表达式"}},"tables":{},"unclear":[]}。'
            '独立制定奖金池和后续报告共用的变量、档位表。报告列表用于规划共用定义，此段不输出报告算式。'
            '材料要求引用上游已确认方案时，upstream_schemes 仅提供可引用的方案/修订、层级与标识；'
            '按原文决定是否使用 scheme_paid(方案名,修订名)，现场不提供上游金额或生产公式。',
            {'reports': reports, 'dsl': self.flow._dsl()}, self._validate_formulas)
        if pool.get('unclear'):
            return pool
        result.update(pool)
        for items in groups(reports, 3):
            part = self._part('reports', [(field, row[0]) for field, row in items],
                '输出 {"details":{"本段明细名":"独立算式"},"totals":{"本段汇总名":"独立算式"},"unclear":[]}。'
                '两组均须提供，空组填 {}。可引用本次独立复核上段已建立的变量和表，严禁改写其定义。',
                {'reports': items, 'independent_pool': pool, 'dsl': self.flow._dsl()},
                lambda value: self._validate_formulas(value, pool, items))
            if part.get('unclear'):
                return part
            for field in ('details', 'totals'):
                result[field].update(part[field])
        return result
