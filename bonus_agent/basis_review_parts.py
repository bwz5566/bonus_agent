"""Build a blind review declaration in independently validated, resumable pieces."""

import copy
import json
import re
from collections import Counter
from decimal import Decimal
from functools import cached_property
from pathlib import Path

from bonus_agent import reread
from bonus_agent.agent import CHAT_SOURCE, ToolError
from bonus_agent.clauses import check_quote
from bonus_agent.facts import MAX_QUOTE, norm, number, scope_norm
from bonus_agent.llm import LLMError, RunBudgetError, parse_json
from bonus_agent.question_review import _known_length
from bonus_agent.recovery import fingerprint
from bonus_agent.review_population import blank_scope_witnesses, check_populations, header_locations, material_scope_review
from bonus_core.engine.allocation import round_pool
from bonus_core.engine.evaluator import EvalError
from bonus_core.engine.issues import EngineError

_BLANK_POLICY = re.compile(r'空|缺失|查不到|无记录|\bblank\b|\bmissing\b', re.I)
_BLANK_SCOPE_GUIDANCE = (
    '其他字段有值的行也可能缺少某项指标。核对原文是否将该指标空单元格视同查不到；'
    '须排除时在 filter 写 {"原始列名":{"not":[""]}} 并引用处理依据，数值 0 仍按其原文口径处理。'
)
_SETTLEMENT_QUESTION_GUIDANCE = (
    '本段复核已确认资金的人员分配。材料已明确的待分配去向及本期未触发的条件按原文处理。'
    '若本期个人金额仍因材料缺项或冲突无法确定，在 unclear 说明影响，并附'
    '{"settlement_question":{"text":"请财务确认的具体问题","options":[],"sources":[{"doc":"材料名","quote":"逐字原文"}]}}。'
    '可用原文给出备选口径，需补清单时直接说明所需字段。程序逐字验证引用并等待财务答复。'
)


def groups(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _report_sum_observations(totals):
    """Compare only amounts computed from the reviewer's own report, without assuming the items are additive."""
    if len(totals) < 2:
        return []
    observations = []
    for name, target in totals.items():
        others = {label: amount for label, amount in totals.items() if label != name}
        subtotal = sum(others.values(), Decimal(0))
        observations.append({'target': name, 'other_items': list(others),
                             'raw_equal': target == subtotal,
                             'rounded_sum_equal': round_pool(target) == round_pool(subtotal),
                             'displayed_sum_equal': round_pool(target) == sum(map(round_pool, others.values()), Decimal(0))})
    return observations


def formula_evidence(material):
    """Formula review receives actual engine attachments in its task input."""
    start, marker, body = material.rpartition('\n## 数据概况（程序扫描；非生产公式或答案）\n')
    if marker:
        try:
            profile, end = json.JSONDecoder().raw_decode(body)
        except ValueError:
            return material
        if isinstance(profile, dict) and 'relations' in profile:
            profile.pop('relations')
            return start + marker + _json(profile) + body[end:]
    return material


def source_tier_series(tables):
    """Read selectable rate series from physical tables without referring to production rules."""
    from bonus_agent.coefficient import interval

    series = {}
    for table in tables:
        rows = table.get('rows')
        if not rows or min(map(len, rows)) < 2:
            continue
        for vertical, grid in ((True, list(zip(*rows))), (False, rows)):
            labels = grid[0][1:]
            try:
                if any(len(cell) != 1 for cell in labels):
                    continue
                ranges = [interval(cell[0]) for cell in labels]
            except ToolError:
                continue
            ordered = sorted(range(len(ranges)), key=lambda i: (ranges[i]['lower'] is not None,
                                                               ranges[i]['lower']))
            for col, values in enumerate(grid[1:], 2):
                if len(values) != len(labels) + 1 or any(len(cell) != 1 for cell in values[1:]):
                    continue
                values_header = ' '.join(values[0])
                percent_header = re.search(r'[（(]\s*%\s*[）)]', values_header)
                rates = [number(cell[0] + ('%' if percent_header and '%' not in cell[0] else ''))
                         for cell in values[1:]]
                if any(rate is None for rate in rates):
                    continue
                identity = f"{table['file']}::P{table['page']}.T{table['table']}." + ('C' if vertical else 'R') + str(col)
                series[identity] = {
                    'doc': table['file'], 'page': table['page'], 'table': table['table'], 'caption': table['caption'],
                    'heading': values_header,
                    'bands': [{**ranges[i], 'rate': str(rates[i]), 'rate_literal': values[i + 1][0],
                               **{k: str(ranges[i][k]) if ranges[i][k] is not None else None
                                  for k in ('lower', 'upper')},
                               'range_cell': [i + 2, 1] if vertical else [1, i + 2],
                               'rate_cell': [i + 2, col] if vertical else [col, i + 2]} for i in ordered],
                }
    return series


def compile_source_tier(spec, series):
    """Compile only the band set the reviewer selected; numeric values come from source cells."""
    from bonus_agent.coefficient import resolve

    if set(spec) - {'source_series', 'mode', 'purpose', 'below_first', 'applies_when'}:
        raise ValueError('使用 source_series 时不得另填档位或数值')
    source = series.get(spec['source_series']) if isinstance(spec['source_series'], str) else None
    if source is None:
        raise ValueError('source_series 须逐字选择来源表列或行 ID')
    mode, purpose = spec.get('mode'), spec.get('purpose')
    if (mode, purpose) not in (('excess', 'amount'), ('whole', 'amount'), ('whole', 'coefficient')):
        raise ValueError('mode 和 purpose 须根据原文选择 excess/amount、whole/amount 或 whole/coefficient')
    bands = copy.deepcopy(source['bands'])
    for band in bands:
        for end in ('lower', 'upper'):
            if band[end] is not None:
                band[end] = Decimal(band[end])
    if (bands[0]['lower'] is not None or bands[-1]['upper'] is not None
            or any(a['upper'] is None or a['upper'] != b['lower'] for a, b in zip(bands, bands[1:]))):
        raise ValueError('来源系列存在未覆盖或重叠区间，请按原文明确档位边界')
    if mode == 'whole':
        for band in bands:
            band['value_text'] = band['rate_literal']
        try:
            resolve(bands)
        except ToolError as exc:
            raise ValueError(str(exc)) from None
    lower = [Decimal(0) if band['lower'] is None else band['lower'] for band in bands]
    if any(a >= b for a, b in zip(lower, lower[1:])):
        raise ValueError('来源阶梯的0起点无法表达其非正下界，请明确原始档位与适用范围')
    below = spec.get('below_first', 'first' if purpose == 'coefficient' else None)
    if below not in ('zero', 'first'):
        raise ValueError('金额阶梯须依据原文填写 below_first=zero 或 first')
    if purpose == 'coefficient' and below != 'first':
        raise ValueError('无下界的系数档覆盖低于0的值，须使用 below_first=first')
    return {
        'kind': 'tier', 'purpose': purpose, 'mode': mode, 'below_first': below,
        'steps': [[str(point), band['rate']] for point, band in zip(lower, bands)],
        'step_inclusive': [True, *(band['left'] is not False for band in bands[1:])],
        'sources': [{'doc': source['doc'], 'page': source['page'], 'quote': band[field]}
                    for band in bands for field in ('text', 'rate_literal')],
        **({'applies_when': spec['applies_when']} if 'applies_when' in spec else {}),
    }


def period_counts(values, period):
    counts = dict.fromkeys(('prior_period_rows', 'current_period_rows', 'later_period_rows',
                            'overlap_rows', 'unparsed_rows'), 0)
    for value in values:
        span = period.span_of(value)
        if span is None:
            key = 'unparsed_rows'
        elif span[1] < period.start:
            key = 'prior_period_rows'
        elif span[0] > period.end:
            key = 'later_period_rows'
        elif period.start <= span[0] <= span[1] <= period.end:
            key = 'current_period_rows'
        else:
            key = 'overlap_rows'
        counts[key] += 1
    return counts


def source_catalog(ws, evidence=None):
    """Expose only verified original excerpts and sheet headers, never ledger explanations or calculated rows."""
    corpus = ws._corpus()
    visible = norm(evidence if evidence is not None else reread.material(ws))
    sources, pages = {}, {}

    def add(doc, text, *, physical_header=False):
        quote = text.strip()
        if not 3 <= len(quote) <= MAX_QUOTE or len(sources.get(doc, {})) >= 48:
            return
        literal = norm(quote)
        if ((literal not in visible and (not physical_header or norm(doc) not in visible))
                or literal not in corpus.get(doc, '') or check_quote(doc, quote, corpus)):
            return
        ref = {'doc': doc, 'quote': quote}
        if doc not in pages:
            pages[doc] = ws._page_texts(doc)
        located = ws._quote_pages(quote, pages[doc])
        if len(located) == 1:
            ref['page'] = located[0]
        sources.setdefault(doc, {})['src_' + fingerprint(ref)[:16]] = ref

    for question in ws.questions:
        if question.answer not in (None, '') and not getattr(question, 'withdrawn', ''):
            add(CHAT_SOURCE, str(question.answer))
    for line in ws.said.splitlines():
        add(CHAT_SOURCE, line)
    for doc in sorted(ws.inv.docs, key=lambda d: d.name):
        for line in ws._lines(doc.name):
            if len(sources.get(doc.name, {})) >= 48:
                break
            if not line.strip().startswith('#'):
                fragments = [line] if len(line) <= MAX_QUOTE else re.split(r'(?<=[。！？；;])', line)
                for fragment in fragments:
                    add(doc.name, fragment)
    for sheet in sorted(ws.inv.sheets, key=lambda s: (s.file, s.sheet)):
        # Synthetic or combined headers which don't occur in the workbook itself are excluded by add().
        add(sheet.file, ' | '.join(str(h) for h in sheet.headers[:8]), physical_header=True)
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


def population_source_options(ws, requirements, allocation):
    """Expose field provenance, leaving eligibility and source selection to the reviewer."""
    keys = {item['level']: item['key'] for item in allocation}
    options = {}
    for label, requirement in requirements.items():
        key = keys.get(label)
        if not key and label in ws.datasets:
            key = ws.datasets[label].key
        if requirement['kind'] == 'rows':
            names = [requirement['dataset']]
        else:
            names = [name for name, dataset in sorted(ws.datasets.items()) if key in dataset.fields]
        options[label] = []
        for name in names:
            mapping, dataset = ws.mapping.get(name), ws.datasets.get(name)
            if mapping is None or dataset is None:
                continue
            key_columns = (mapping.combine or {}).get(key)
            if key_columns is None and key in mapping.columns:
                key_columns = [mapping.columns[key]]
            options[label].append({
                'dataset': name, 'file': mapping.file, 'sheet': mapping.sheet,
                'key': key, 'key_columns': key_columns or [],
                'period_fields': {field: mapping.columns[field] for field, spec in dataset.fields.items()
                                  if (spec.type == 'date' or field == dataset.period) and field in mapping.columns},
            })
    return options


def population_source_hints(ws, options, catalog):
    """Rank relevant literal excerpts for navigation. IDs confer no range approval."""
    eligibility_words = ('筛选', '排除', '剔除', '纳入', '不参与', '不计入', '只取')
    period_words = ('范围', '期间', '当期', '当月', '月份', '本年', '累计')

    def terms(values):
        return {word for value in values if isinstance(value, str)
                for word in (scope_norm(value), scope_norm(re.split(r'[（(]', value, maxsplit=1)[0]))
                if len(word) >= 2}

    result = {}
    for label, datasets in options.items():
        identity_terms = terms([label.removeprefix('rows:'), *(col for ds in datasets for col in ds['key_columns'])])
        field_terms = terms([col for ds in datasets for col in ws.mapping[ds['dataset']].columns.values()])
        identity, eligibility = [], []
        for ident, ref in catalog.items():
            quote = scope_norm(ref['quote'])
            key_hits = sum(term in quote for term in identity_terms)
            data_hits = sum(term in quote for term in field_terms)
            if key_hits:
                identity.append((-key_hits, -data_hits, ident))
            selects_rows = any(word in quote for word in eligibility_words)
            if (selects_rows or any(word in quote for word in period_words)) and (key_hits or data_hits):
                eligibility.append((not selects_rows, -(key_hits + data_hits), ident))
        result[label] = {'identity': [row[-1] for row in sorted(identity)[:5]],
                         'eligibility': [row[-1] for row in sorted(eligibility)[:7]]}
    return result


def population_catalog(ws, options, catalog):
    """Keep policy quotations and physical table headers in their distinct places."""
    policies = {}
    headers = {}
    for ident, ref in catalog.items():
        locations = header_locations(ws, ref['doc'], ref['quote'])
        if not locations:
            policies[ident] = ref
        for location in locations:
            headers.setdefault(location, {})[ident] = ref
    for datasets in options.values():
        for dataset in datasets:
            location = (Path(dataset['file'] or '').name, dataset['sheet'])
            refs = headers.get(location)
            if refs:
                dataset['header_sources'] = refs
    return policies


def sparse_source_rows(ws, requirements):
    """Show physical row locations and blanks without applying production eligibility."""
    sheets = {(sheet.file, sheet.sheet): sheet for sheet in ws.inv.sheets}
    samples = {}
    for label, requirement in requirements.items():
        mapping = ws.mapping.get(requirement.get('dataset'))
        sheet = sheets.get((mapping.file, mapping.sheet)) if mapping else None
        if not sheet or len(sheet.headers) < 3 or sheet.segments or sheet.remaps or sheet.unsure:
            continue
        rows = []
        for line, values in sheet.rows:
            populated = [(index, value) for index, value in enumerate(values)
                         if value is not None and str(value).strip()]
            if len(populated) != 1:
                continue
            index, value = populated[0]
            if index >= len(sheet.headers) or not sheet.headers[index] or len(str(value)) > 120:
                continue
            rows.append({'file': sheet.file, 'sheet': sheet.sheet, 'row': line,
                         'populated': {sheet.headers[index]: str(value)},
                         'other_columns_empty': len(sheet.headers) - 1})
            if len(rows) == 6:
                break
        if rows:
            samples[label] = rows
    return samples


class PopulationDifference(Exception):
    def __init__(self, report, selection):
        self.report = report
        self.selection = selection


class PoolDifference(Exception):
    def __init__(self, report, declaration):
        self.report = report
        self.declaration = declaration


class InvalidPopulations(ValueError):
    def __init__(self, accepted, errors):
        self.accepted, self.errors = accepted, errors
        hints = {
            'missing_scope_source': '补充范围规则原文，优先用本段 source_catalog 的 ID',
            'invalid_scope_source': 'sources 每项只写 source_id 或 doc/quote/page',
            'unknown_scope_source_document': '资料名称按来源目录原样填写',
            'scope_quote_not_found': '按 source_index 替换该引文，引用本段目录中的原文 ID 或逐字复制',
            'scope_quote_wrong_page': '用目录中的页码或原文所在页',
            'scope_header_other_table': '用所声明来源表的字段，并引用支持取数范围的业务原文',
            'scope_rule_needed': '引用确立对象、筛选和期间的规则句或已确认答复',
            'invalid_source_catalog_id': '选择本段目录中存在的完整 ID，勿附改写的引文',
            'unknown_source_dataset': 'dataset 按本段实际的标准数据集名填写',
            'period_field': '仅修复本份范围声明。period 填本数据集的标准字段或对应原表中的唯一原始列名；依据不需按日期过滤时填 null',
            'filter_shape': 'filter 须为对象；排除空格用 {"原始列名":{"not":[""]}}。'
                            'not 内填字符串数组，空格用空字符串表示',
        }
        guidance = [hints[code] for code in sorted({e['code'] for e in errors} & hints.keys())]
        suffix = '。' + '。'.join(guidance) if guidance else ''
        super().__init__('范围声明未通过合同校验：' + _json(errors) + suffix)


class InvalidReviewShape(ValueError):
    """The response could not be read as the requested JSON object."""


def _prior_helper_amendments(row):
    """Retain partial edits that reached whole-draft evaluation in this exact independent review."""
    changes = {}
    for attempt in row.get('formula_failures', ()):
        declaration = attempt.get('declaration') or {}
        if (attempt.get('local_error') and not declaration.get('unclear')
                and isinstance(declaration.get('helpers'), dict)):
            changes.update(declaration['helpers'])
    return changes


def _part_protocol_failure(row):
    if row.get('state') != 'invalid':
        return False
    section = row.get('section')
    failures = row.get('formula_failures') or []
    if failures and failures[-1].get('binding') and failures[-1]['diagnostic'] == row.get('diagnostic'):
        return True
    scope_response = section in ('populations', 'population_clarification', 'pool_finance_recheck', 'pool_batch_effects')
    if any(e['diagnostic'] == row.get('diagnostic') and (scope_response or e['received_fields'] is None)
           for e in row.get('protocol_failures', [])):
        return True
    errors = row.get('contract_errors', [])
    if section == 'populations' and errors:
        return all(e['code'] == 'period_field' for e in errors)
    return (section == 'population_clarification' and bool(errors)
            and all(e['code'] == 'contract_shape' for e in errors))


class BasisReviewParts:
    def __init__(self, flow, name, rs, evidence, structure, signature, *, resume_signatures=()):
        self.ws, self.flow, self.name, self.rs = flow.ws, flow, name, rs
        self.evidence, self.structure, self.signature = evidence, structure, signature
        self.resume_signatures = resume_signatures
        self.records = flow.state.setdefault('basis_review_parts', {}).setdefault(name, {})
        self.base = structure['base']
        self.scope_questions = {}
        self.scope_confirmations = {}
        self.reused_settlement_scopes = []
        self.terminal_declaration = None

    @cached_property
    def _scope_env(self):
        return self.ws._independent_env(self.name, {}, basis=True)

    @staticmethod
    def visible_structure(flow, name, structure, evidence=None, *, workspace=None):
        ws = flow.ws if workspace is None else workspace
        base = {k: structure[k] for k in ('scheme', 'pool_scope', 'period', 'allocation', 'columns',
                                         'source_mapping', 'data_scope')}
        base['upstream_schemes'] = []
        for scheme, revision in sorted(flow.ws._deps(name)):
            if scheme in flow.ws.schemes:
                upstream = flow.ws._ruleset(scheme)
                base['upstream_schemes'].append({'scheme': scheme, 'revision': revision,
                    'pool_scope': upstream.pool.scope, 'levels': [{'level': lv.level, 'key': lv.key} for lv in upstream.allocation],
                    'approved': flow.current('approved:' + scheme)})
        requirements = structure['population_requirements']
        options = population_source_options(ws, requirements, base['allocation'])
        catalog = source_catalog(ws, evidence)
        policies = population_catalog(ws, options, catalog)
        return {'base': base, 'population_requirements': requirements,
                'dataset_names': sorted(base['data_scope']), 'source_catalog': catalog, 'policy_sources': policies,
                'population_source_options': options,
                'population_source_hints': population_source_hints(ws, options, policies),
                'sparse_source_rows': sparse_source_rows(ws, requirements),
                'reports': [(field, row[:2]) for field in ('details', 'totals') for row in structure[field]]}

    def _replay_part(self, sig, section, messages, effort):
        if sig in self.records:
            return
        packets = [messages]
        added = {'populations': _BLANK_SCOPE_GUIDANCE, 'pool': _SETTLEMENT_QUESTION_GUIDANCE}.get(section)
        if added and added in messages[0]['content']:
            leading = messages[0]['content'].replace(added, '', 1)
            packets.append([{**messages[0], 'content': leading}, messages[1]])
        attempts = self.flow.state['attempts']
        for review in self.resume_signatures:
            for packet in packets:
                prior_sig = fingerprint((review, section, packet, effort))
                prior = self.records.get(prior_sig, {})
                usable = prior.get('state') in ('valid', 'unresolved')
                prior_attempts = attempts.get(f'{self.name}:part:{prior_sig}', 0)
                invalid = prior.get('state') == 'invalid' and prior_attempts > 0
                saved_scope = (section in ('populations', 'population_clarification') and prior_attempts > 0
                               and prior.get('state') in ('invalid', 'partial')
                               and isinstance(prior.get('model_declaration'), dict))
                truncated = (prior.get('state') == 'error' and prior_attempts > 0
                             and _known_length(prior.get('request')))
                if usable or invalid or truncated or saved_scope:
                    self.records[sig] = {**copy.deepcopy(prior), 'replayed_from': prior_sig}
                    if invalid or truncated or saved_scope:
                        attempts[f'{self.name}:part:{sig}'] = prior_attempts
                    if saved_scope:
                        self.records[sig]['recheck_saved_declaration'] = True
                    elif invalid and prior.get('formula_failures'):
                        self.records[sig]['recheck_saved_declaration'] = 'formula'
                    return

    def _part(self, section, targets, instruction, contract, validate, *, excerpt=None, saved_only=False):
        self.ws._check_source_version()
        if excerpt == '' and section in ('pool_measure', 'pool_clarification', 'reports', 'report_clarification',
                                          'report_structure_repair', 'pool_evidence_confirmation', 'pool_applicability',
                                          'pool_finance_recheck', 'pool_batch_effects'):
            confirmed = [{'id': q.id, 'related': q.related, 'answer': q.answer}
                         for q in self.flow.questions_for(self.name) if q.answer not in (None, '') and not q.withdrawn]
            if confirmed:
                contract = {**contract, 'confirmed_answers': confirmed}
        sys = ('【独立总额分段复核】按原材料独立重建本段声明。只输出本段的 JSON 对象，其他段由程序处理。'
               '已列出的映射、范围、期间和分类是待核事实。核对当期对象、历史累计窗口、空值和真实分类。'
               'unknown、values_truncated 或 fields_complete=false 均不证明未展示的值不存在。'
               '业务口径无法从原文或财务答复确定时用 unclear 字符串数组具体指出问题；程序计算金额。' + instruction)
        evidence = (excerpt if excerpt is not None else
                    formula_evidence(self.evidence) if section in ('pool', 'reports') else self.evidence)
        prefix = evidence + '\n独立复核结构：' + _json(self.base)
        population_section = section in ('populations', 'population_clarification')
        if population_section:
            prefix += '\n允许的标准数据集名称：' + _json(self.structure['dataset_names'])
            prefix += '\n业务原文目录 source_catalog：' + _json(self.structure['policy_sources'])
        prefix += '\n本段输入：'
        messages = [{'role': 'system', 'content': sys}, {'role': 'user', 'content': prefix + _json(contract)}]
        checker = self.ws.llm.checker_llm
        effort = getattr(checker, 'basis_review_reasoning_effort', None)
        sig = fingerprint((self.signature, section, messages, effort))
        self._replay_part(sig, section, messages, effort)
        if saved_only and self.records.get(sig, {}).get('state') != 'valid':
            return None
        row = self.records.setdefault(sig, {'section': section, 'targets': targets})
        if section == 'pool':
            self._pool_record = row
        elif section == 'pool_clarification':
            self._pool_clarification_record = row
        if section == 'reports':
            self._report_record = row
        if row.get('state') in ('valid', 'unresolved'):
            if row['state'] == 'valid' and (population_section or section in ('pool', 'pool_clarification', 'pool_measure')
                                            or row.get('replayed_from')):
                try:
                    validate(row['value'])
                except ValueError as exc:
                    raise ToolError(f'已保存的独立声明无法验证：{exc}') from None
            return copy.deepcopy(row['value'])
        key = f'{self.name}:part:{sig}'
        attempts = self.flow.state['attempts']
        before = attempts.get(key, 0)
        if replay := row.pop('recheck_saved_declaration', False):
            declaration = (row['formula_failures'][-1]['declaration'] if replay == 'formula'
                           else row['model_declaration'])
            try:
                return self._accept_response(_json(declaration), row, section,
                                             targets, {}, targets, validate)
            except ToolError:
                if replay != 'formula' or not (row.get('formula_failures') or [{}])[-1].get('binding'):
                    raise
            finally:
                self._checkpoint_part(sig, row, before)
        accepted = row.get('accepted', {})
        pending = targets
        if accepted:
            self._validate_populations({'populations': accepted}, list(accepted))
            if self.population_report['status'] != 'PASS':
                raise PopulationDifference(self.population_report, accepted)
            pending = [label for label in targets if label not in accepted]
            messages[1]['content'] = prefix + _json({k: {n: values[n] for n in pending} for k, values in contract.items()})
        failure = (row.get('formula_failures') or [{}])[-1]
        binding = failure.get('binding')
        # Older checkpoints exhausted their attempts without the engine's actionable field diagnosis.
        new_binding_feedback = bool(binding and not row.get('binding_feedback_sent') and before == 2)
        if before >= 2 and not new_binding_feedback:
            message = f'独立复核分段 {section} {targets} 已达两次尝试，诊断：{row.get("diagnostic", "请求未完成")}'
            if _part_protocol_failure(row) and (section != 'populations' or row.get('contract_errors')):
                raise LLMError(message)
            raise ToolError(message)
        limits = {}
        request = row.get('request', {})
        previous_output = request.get('requested_output_tokens')
        if row.get('state') == 'error' and request.get('finish_reason') == 'length':
            if not _known_length(request) or type(previous_output) is not int or not 0 < previous_output < 65536:
                raise ToolError('分段输出曾截断，但缺少已确认的用量或可提高的输出上限')
            limits['max_tokens'] = min(previous_output * 2, 65536)
            if 'finish_reason=length' in getattr(self.ws, 'checker_down', ''):
                self.ws.checker_down = ''
        if row.get('diagnostic'):
            feedback = '上一份本段声明的程序诊断：' + row['diagnostic'] + '。请仅按原材料修复本段。'
            if row.get('formula_failures'):
                feedback += ' 上次提交的本段独立算式：' + _json(row['formula_failures'][-1]['declaration'])
                if section == 'pool_clarification':
                    feedback += ' 本次基于已提交的辅助公式局部修改继续，同名修改覆盖旧值：' + _json(_prior_helper_amendments(row))
            if binding:
                scope = self._formula_source_observations['engine_scope_observations'].get(binding['scope'], {})
                visible = scope.get('readable_tables', {})
                packet = {**binding, 'scope_objects': scope.get('objects'),
                          'linked_rows': {name: visible.get(name) for name in binding['datasets']}}
                feedback += (' 本次失败字段和所属原表：' + _json(packet)
                             + '。修正你自己的 helper，在相应明细表的行上下文读取字段。唯一值取 single_value(表达式, of: "表名")，'
                               '如原文要求累计才使用 sum；按已核对原文处理缺行。同时查看独立草稿内其他层级的同类字段读取。')
            if section == 'population_clarification':
                template = {'populations': {label: {'exclude_blank_columns': ['<观察到的原表空值列>'],
                                                     'sources': [{'source_id': '<支持排除的实际依据ID>'}]}
                                            for label in pending}, 'unclear': []}
                feedback += ('本次只填要排除空值的原始列名和所依据的原文。程序按原声明保留表、键和期间。'
                             '把占位符替换为真实列名和依据。依据不足时填 unclear。'
                             '严格输出以下 JSON 结构，各范围的值必须是对象。' + _json(template))
            messages.append({'role': 'user', 'content': feedback})
        error = None
        with reread.basis_part_request_policy(self.ws):
            model = reread.guard_request(self.ws, messages, checker_only=True, **limits)
            if limits and (cap := getattr(model, '_max_output_tokens', None)) and int(cap) <= previous_output:
                raise ToolError('复核服务的输出上限无法提高，保留截断记录')
            attempts[key] = before + 1
            sent = True
            try:
                raw = reread._ask(self.ws, messages, checker_only=True, **limits)
            except RunBudgetError as exc:
                call = getattr(model, 'last_call', {})
                if getattr(exc, 'request_not_sent', False) or (call.get('status') == 'budget_blocked' and
                        call.get('request_attempts') == [] and not call.get('budget_request_sequence') and
                        not call.get('response_received')):
                    attempts[key] = before
                    sent = False
                raise
            except LLMError as exc:
                row.update(state='error', checker_error=str(exc)[:300])
                error = exc
            finally:
                if sent:
                    if binding:
                        row['binding_feedback_sent'] = True
                    call = getattr(model, 'last_call', {})
                    row['request'] = {k: call[k] for k in ('model', 'status', 'finish_reason', 'response_received',
                                      'response_id', 'usage_reported', 'requested_output_tokens') if k in call}
        if error is not None:
            self._checkpoint_part(sig, row, attempts[key])
            if before == 0 and _known_length(row['request']):
                return self._part(section, targets, instruction, contract, validate, excerpt=excerpt)
            raise error
        try:
            return self._accept_response(raw, row, section, targets, accepted, pending, validate)
        except ToolError as exc:
            if not _part_protocol_failure(row):
                raise
            if before:
                if section == 'populations' and not row.get('contract_errors'):
                    raise
                raise LLMError(f'独立复核两次未返回可校验的声明。{exc}') from None
        finally:
            if row.get('state') in ('valid', 'unresolved', 'invalid', 'partial'):
                self._checkpoint_part(sig, row, attempts[key])
        return self._part(section, targets, instruction, contract, validate, excerpt=excerpt)

    def _checkpoint_part(self, signature, row, attempts):
        persist = getattr(self.ws, '_save_basis_part', None)
        if persist is not None:
            persist(self.ws.round, self.name, signature, row, attempts)

    @staticmethod
    def _accept_response(raw, row, section, targets, accepted, pending, validate):
        value = None
        formula_declaration = None
        try:
            try:
                value = parse_json(raw)
            except (LLMError, ValueError):
                raise InvalidReviewShape('未输出可解析的完整 JSON 对象') from None
            if not isinstance(value, dict):
                raise InvalidReviewShape('须输出 JSON 对象')
            if section == 'pool_batch_effects':
                row['model_declaration'] = copy.deepcopy(value)
            if section in ('pool', 'reports', 'pool_clarification', 'report_clarification'):
                formula_declaration = copy.deepcopy(value)
                row['formula_declaration'] = formula_declaration
            if 'unclear' in value:
                uncertainty = value['unclear']
                if uncertainty is None or uncertainty is False:
                    value['unclear'] = []
                elif isinstance(uncertainty, str):
                    value['unclear'] = [uncertainty.strip()] if uncertainty.strip() else []
                elif isinstance(uncertainty, list) and all(isinstance(q, str) for q in uncertainty):
                    value['unclear'] = [q.strip() for q in uncertainty if q.strip()]
                else:
                    # Preserve unexpected substantive uncertainty; never use a malformed flag as consent.
                    value['unclear'] = [_json(uncertainty)]
            if 'settlement_question' in value and not value.get('unclear'):
                raise InvalidReviewShape('settlement_question 必须与尚未解决的 unclear 一起提交')
            if value.get('unclear'):
                if section == 'pool_batch_effects':
                    validate(value)
                row.update(state='unresolved', value={'unclear': value['unclear']})
                if section == 'pool_batch_effects':
                    row['value']['blockers'] = copy.deepcopy(value['blockers'])
                if section == 'pool' and isinstance(value.get('settlement_question'), dict):
                    row['value']['settlement_question'] = copy.deepcopy(value['settlement_question'])
                if section in ('pool', 'reports', 'pool_clarification', 'report_clarification'):
                    row['independent_draft'] = copy.deepcopy(value)
                return copy.deepcopy(row['value'])
            scope_section = section in ('populations', 'population_clarification')
            if (scope_section and 'populations' not in value
                    and set(value) - {'unclear'} == set(pending) and all(isinstance(value[n], dict) for n in pending)):
                value = {'populations': {name: value[name] for name in pending}, 'unclear': []}
            if scope_section and isinstance(value.get('populations'), dict):
                supplied = value['populations']
                aliases = {name.removeprefix('rows:'): name for name in pending
                           if name.startswith('rows:') and name.removeprefix('rows:') not in targets}
                normalized = {aliases.get(name, name): selection for name, selection in supplied.items()}
                if len(normalized) == len(supplied):
                    value['populations'] = normalized
            if accepted:
                returned = value.get('populations')
                if not isinstance(returned, dict) or set(returned) - set(pending):
                    raise ValueError('补充声明只能包含本次剩余的范围名，禁止重写已保存的独立范围')
                value['populations'] = {**accepted, **returned}
            if scope_section and isinstance(value.get('populations'), dict):
                row['model_declaration'] = copy.deepcopy(value)
            validate(value)
        except PopulationDifference as exc:
            row.update(state='partial', accepted=copy.deepcopy(exc.selection))
            raise
        except InvalidPopulations as exc:
            row.update(state='invalid', accepted=copy.deepcopy(exc.accepted), contract_errors=exc.errors, diagnostic=str(exc))
            raise ToolError(f'独立复核分段 {section} {targets} 未通过：{exc}') from None
        except ValueError as exc:
            row.update(state='invalid', diagnostic=str(exc))
            if isinstance(exc, InvalidReviewShape):
                row.setdefault('protocol_failures', []).append({
                    'diagnostic': str(exc),
                    'received_fields': sorted(key[:80] for key in value)[:16] if isinstance(value, dict) else None,
                })
            if formula_declaration is not None:
                failure = {'declaration': formula_declaration, 'diagnostic': str(exc),
                           'response_id': row.get('request', {}).get('response_id')}
                if exc.__cause__ is not None:
                    # Only the local checkpoint gets evaluator details, which can include amounts.
                    failure['local_error'] = str(exc.__cause__)[:1200]
                    if binding := getattr(exc.__cause__, 'review_binding', None):
                        failure['binding'] = copy.deepcopy(binding)
                failures = row.setdefault('formula_failures', [])
                if not failures or failures[-1] != failure:
                    failures.append(failure)
            raise ToolError(f'独立复核分段 {section} {targets} 未通过：{exc}') from None
        row.update(state='valid', value=copy.deepcopy(value))
        row.pop('diagnostic', None)
        row.pop('accepted', None)
        row.pop('contract_errors', None)
        return value

    @staticmethod
    def _shape(value, fields):
        provided = set(value) - {'unclear'}
        required = set(fields)
        if provided != required:
            diagnostic = '只允许并须完整填写本段字段：' + '、'.join(fields)
            missing = sorted(required - provided)
            extra = sorted(key[:80] for key in provided - required)[:16]
            if missing:
                diagnostic += '。缺少字段 ' + _json(missing)
            if extra:
                diagnostic += '。多出字段 ' + _json(extra)
            raise InvalidReviewShape(diagnostic)
        for field in fields:
            if field == 'sources':
                if not isinstance(value[field], list):
                    raise InvalidReviewShape('sources 须为 JSON 数组')
            elif field != 'pool' and not isinstance(value[field], dict):
                raise InvalidReviewShape(f'{field} 须为 JSON 对象')

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
        _, root, levels, _ = self._scope_env
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

    def _shared_row_options(self, original, report, verified):
        """Find independent declarations with matching source, period and physical rows."""
        def origin(selection):
            mapping = self.ws.mapping[selection['dataset']]
            date = selection['period']
            return (mapping.file, mapping.sheet, mapping.header_rows, mapping.unpivot,
                    None if date is None else mapping.columns.get(date, date), selection['window'])

        choices = {}
        checked_peers = dict(verified)
        checked_peers.update({check['scope']: original[check['scope']] for check in report['checks']
                              if check['status'] == 'PASS' and check['scope'] in original})
        for issue in report['checks']:
            if issue['status'] == 'PASS' or issue.get('difference', {}).get('code') == 'scope_blank_value_policy':
                continue
            label = issue['scope']
            if not label.startswith('rows:') or issue['extra'] or not issue['missing']:
                continue
            current = original[label]
            signature = origin(current)
            peers = {}
            candidates = list(checked_peers.items())
            for row in self.records.values():
                if row.get('section') != 'populations' or row.get('state') != 'valid':
                    continue
                prior = row.get('value', {}).get('populations', {}).get(label)
                if prior and prior.get('dataset') == current['dataset']:
                    # Earlier independent selections are proposals. Their citations, physical rows
                    # and applicability must all pass again before they can affect this review.
                    candidates.append((f'{label} / 历次独立声明 {fingerprint(prior["filter"])[:12]}', prior))
            for peer_name, peer in candidates:
                filters = peer['filter']
                if (peer_name.startswith('rows:') and filters != current['filter'] and origin(peer) == signature
                        and all(filters.get(col) == value for col, value in current['filter'].items())):
                    peers.setdefault(fingerprint(filters), (peer_name, peer))
            for peer_name, peer in peers.values():
                candidate = {**current, 'filter': peer['filter'], 'sources': [*current['sources'], *peer['sources']]}
                _, root, levels, _ = self._scope_env
                check = check_populations(self.ws, self.rs, {'populations': {label: candidate}}, root, levels,
                                          scope_names=[label])
                if check['status'] == 'PASS':
                    choices.setdefault(label, {})[peer_name] = {
                        'peer_scope': peer_name, 'prior_declaration': current, 'peer_declaration': peer,
                        'witness_rows': issue['missing'], 'candidate_declaration': candidate}
        return choices

    def can_revisit_shared_rows(self, selection, report):
        verified = {}
        for labels in groups(list(self.structure['population_requirements']), 4):
            if any(label in selection for label in labels):
                break
            part = self._population_part(labels, saved_only=True)
            if not part or part.get('unclear') or self.population_report['status'] != 'PASS':
                break
            verified.update(part['populations'])
        return bool(self._shared_row_options(selection, report, verified))

    def _clarify_shared_rows(self, part, labels, verified):
        """Only the independent reviewer can select which eligible source policy applies."""
        original = part['populations']
        options = self._shared_row_options(original, self.population_report, verified)
        if not options:
            return part

        choices = {label: next(iter(peers.values())) if len(peers) == 1 else {'alternatives': list(peers.values())}
                   for label, peers in options.items()}
        template = {label: next(iter(peers)) if len(peers) == 1 else '<按原文选一个 peer_scope>'
                    for label, peers in options.items()}

        def validate(value):
            self._shape(value, ['use_peer'])
            selected = value['use_peer']
            if not isinstance(selected, dict) or selected.keys() != options.keys() or any(
                    not isinstance(peer, str) or peer not in options[label] for label, peer in selected.items()):
                raise ValueError('只允许确认列出的同源范围；原文口径不同或依据不足时填写 unclear')

        decision = self._part(
            'population_scope_choice', list(choices),
            '核对同一张原表、同一日期字段和期间的两个取数用途。peer_declaration 是你此前独立核对过的范围。'
            '历次独立声明只作为候选，须按当前材料和本批已确认口径重新判断；旧声明与当前规则冲突时保留分歧。'
            '核对原文确定对相同物理行的资格条件能否用于当前用途。查看 witness_rows 的来源行位置。'
            '从每个范围列出的 peer_scope 中选取原文支持的资格条件，填入 use_peer。即使当期物理行完全一样，'
            '仍须核对适用对象和排除条件。原文不能裁定时输出具体 unclear，保留差异。',
            {'choices': choices, 'output_template': {'use_peer': template, 'unclear': []}}, validate)
        if decision.get('unclear'):
            return part
        updated = {'populations': {**original, **{label: options[label][peer]['candidate_declaration']
                                                  for label, peer in decision['use_peer'].items()}}, 'unclear': []}
        self._validate_populations(updated, labels)
        return updated

    def _clarify_population_difference(self, part, labels):
        original = part['populations']
        blank_cells = blank_scope_witnesses(self.population_report)
        if not blank_cells:
            raise PopulationDifference(self.population_report, original)
        pending = list(blank_cells)
        policies = self.structure['policy_sources']
        source_candidates = {}
        for label, samples in blank_cells.items():
            fields = {scope_norm(column) for sample in samples for column in sample['empty_columns']}
            source_candidates[label] = {
                ident: ref for ident, ref in policies.items()
                if any(field and field in scope_norm(ref['quote']) for field in fields)
                and _BLANK_POLICY.search(ref['quote'])
            }
            source_candidates[label] = dict(list(source_candidates[label].items())[:8])

        def validate(value):
            self._shape(value, ['populations'])
            for label, amendment in value['populations'].items():
                if label not in blank_cells:
                    raise InvalidReviewShape(f'空值复核范围须为 {"、".join(blank_cells)}')
                if not isinstance(amendment, dict):
                    raise InvalidReviewShape(f'populations[{label!r}] 须为 JSON 对象')
                if set(amendment) == {'filter', 'sources'}:
                    value['populations'][label] = {**original[label], **amendment}
                    continue
                if 'exclude_blank_columns' not in amendment:
                    continue
                if set(amendment) != {'exclude_blank_columns', 'sources'}:
                    raise ValueError('空值修正只允许提交本段范围、空值列和原文依据')
                observed = {col for sample in blank_cells[label] for col in sample['empty_columns']}
                columns = amendment['exclude_blank_columns']
                if (not isinstance(columns, list) or not columns
                        or any(not isinstance(col, str) or col not in observed for col in columns)):
                    raise ValueError('exclude_blank_columns 须为证据所指原表列的非空数组')
                selection = self._exclude_blank_columns(original[label], columns)
                selection['sources'] = amendment['sources']
                value['populations'][label] = selection
            self._validate_populations(value, pending)
            for label, selection in value['populations'].items():
                before = original[label]
                if any(selection.get(k) != before.get(k) for k in ('dataset', 'key', 'period', 'window')):
                    raise ValueError('空值复核只允许修正对应列的筛选和依据，须保留来源、关联键和期间')
                columns = {col for sample in blank_cells[label] for col in sample['empty_columns']}
                old_filter, new_filter = before['filter'], selection['filter']
                for col in old_filter.keys() | new_filter.keys():
                    old, new = old_filter.get(col), new_filter.get(col)
                    if old != new and (col not in columns or not self._adds_blank_exclusion(old, new)):
                        raise ValueError('只允许在证据所指的列增加空值排除，保持其他取值和筛选')
                if old_filter != new_filter and not any(
                        _BLANK_POLICY.search(ref['quote'])
                        for ref in selection['sources']):
                    raise ValueError('新增空值排除须引用原文中的空值处理依据')

        response = self._part(
            'population_clarification', pending,
            '只复核所列范围，输出 {"populations":{"范围名":{"exclude_blank_columns":["原始列名"],'
            '"sources":[{"source_id":"本案原文ID"}]}},"unclear":[]}。'
            'prior_declaration 是你此前的独立声明。observed_empty_cells 是原工作簿中实际为空的单元格，'
            '请据原文确认这些行是否提供有效指标，并为结论引用确切的空值处理依据。'
            '原文要求把空单元格视为查不到时，列出 exclude_blank_columns。程序保留来源、键、期间和其他筛选。'
            '区别空单元格、数值 0 和业务对象是否存在。只处理列出的范围；依据不明时写 unclear。',
            {'prior_declaration': {k: original[k] for k in pending},
             'observed_empty_cells': blank_cells, 'source_candidates': source_candidates,
             'source_options': {k: self.structure['population_source_options'][k] for k in pending}},
            validate)
        if response.get('unclear'):
            differences = {row['scope']: row for row in self.population_report['checks'] if row['status'] == 'FAIL'}
            self.scope_questions = {
                label: samples for label, samples in blank_cells.items()
                if differences[label]['expected_count'] - differences[label]['actual_count'] == len(samples)
            }
            return response
        updated = {'populations': {**original, **response['populations']}, 'unclear': []}
        self._validate_populations(updated, labels)
        if self.population_report['status'] != 'PASS':
            raise PopulationDifference(self.population_report, updated['populations'])
        return updated

    @staticmethod
    def _exclude_blank_columns(selection, columns):
        candidate = copy.deepcopy(selection)
        for column in columns:
            old = candidate['filter'].get(column, {'not': []})
            if not isinstance(old, dict) or set(old) != {'not'}:
                raise ValueError('已有筛选不是排除条件，不能在该列追加空值排除')
            previous = [old['not']] if isinstance(old['not'], str) else old['not']
            candidate['filter'][column] = {'not': list(dict.fromkeys([*previous, '']))}
        return candidate

    def _apply_scope_choices(self, part, labels, choices):
        if not choices:
            return None
        witnesses = blank_scope_witnesses(self.population_report)
        if not witnesses or not witnesses.keys() <= choices.keys():
            return None
        candidate = copy.deepcopy(part)
        adopted = {}
        failures = {row['scope']: row for row in self.population_report['checks'] if row['status'] == 'FAIL'}
        for label, rows in witnesses.items():
            question, confirmed_rows = choices[label]
            failure = failures[label]
            if rows != confirmed_rows or failure['expected_count'] - failure['actual_count'] != len(rows):
                return None
            original = candidate['populations'][label]
            review = material_scope_review(self.ws, question)
            if review:
                selection = copy.deepcopy(review['declaration'])
                if any(selection.get(k) != original.get(k) for k in ('dataset', 'key', 'period', 'window')):
                    return None
            else:
                columns = sorted({column for row in rows for column in row['empty_columns']})
                try:
                    selection = self._exclude_blank_columns(original, columns)
                except ValueError:
                    return None
                selection['sources'].append({'doc': CHAT_SOURCE, 'quote': question.answer})
            candidate['populations'][label] = selection
            adopted[label] = {'question': question.id, 'answer': question.answer, 'by': question.answered_by,
                              'at': question.answered_at, 'cells': rows}
            if review:
                adopted[label]['source_review'] = review['decision_id']
        _, root, levels, _ = self._scope_env
        report = check_populations(self.ws, self.rs, candidate, root, levels, scope_names=labels)
        if report['status'] != 'PASS':
            return None
        self.population_report = report
        self.scope_confirmations.update(adopted)
        return candidate

    @staticmethod
    def _adds_blank_exclusion(old, new):
        def values(value):
            return {value} if isinstance(value, str) else set(value)

        if old is None or isinstance(old, dict):
            return (isinstance(new, dict) and set(new) == {'not'}
                    and values(new['not']) == (values(old['not']) if old else set()) | {''})
        return isinstance(new, (str, list)) and values(new) == values(old) - {''}

    @cached_property
    def _formula_source_observations(self):
        """Observe real attachments and source cells without sharing production formulas or amounts."""
        from bonus_agent.data_profile import _source_sheet
        from bonus_agent.source_cells import ppt_page_index, read_ppt_tables

        _, root, levels, evaluator = self._scope_env
        by_scope = {**levels, **{scope: list(nodes.values()) for scope, nodes in (root.index or {}).items()}}
        relationships = {}
        for scope, nodes in sorted(by_scope.items()):
            reached = Counter()
            for node in nodes:
                targets = {n.level for ref in evaluator._node_refs(node) for n in ref.chain()}
                reached.update((targets & by_scope.keys()) - {scope})
            visible = {}
            for dataset in self.rs.datasets:
                counts = [len(node.rows(dataset)) for node in nodes]
                if any(counts):
                    visible[dataset] = {'with_rows': sum(n > 0 for n in counts), 'max_rows_per_object': max(counts)}
            relationships[scope] = {'objects': len(nodes), 'readable_tables': visible,
                                    'reference_scope_coverage': dict(sorted(reached.items()))}

        file_roles = self.flow.state.get('plan', {}).get('files', {})
        known_files = {path.name for path in self.ws.inv.files}
        corpus = self.ws._corpus()
        excerpts = list(dict.fromkeys((c.file, c.quote) for c in self.ws.clauses
                                      if c.file in known_files and file_roles.get(c.file) not in ('ignored', 'reference')
                                      and not check_quote(c.file, c.quote, corpus)))
        pool_sources = [{'doc': s.doc, 'quote': s.quote} for s in self.rs.pool.sources
                        if file_roles.get(s.doc) not in ('ignored', 'reference') and not check_quote(s.doc, s.quote, corpus)]
        periods = {}
        empty_fields = {}
        period = self.ws.period
        for dataset, definition in self.rs.datasets.items():
            with_empty_policy = {field: spec.if_empty for field, spec in definition.fields.items()
                                 if spec.if_empty is not None}
            if with_empty_policy:
                attached = root.rows(dataset)
                empty_fields[dataset] = {
                    field: {'policy': policy, 'rows_seen_by_formula': len(attached),
                            'null': sum(row.values.get(field) in (None, '') for row in attached),
                            'numeric_zero': sum(isinstance(row.values.get(field), Decimal) and row.values[field] == 0
                                                for row in attached)}
                    for field, policy in with_empty_policy.items()}
            if not period or not definition.period or dataset not in self.ws.mapping:
                continue
            mapping = self.ws.mapping[dataset]
            column = mapping.columns.get(definition.period)
            try:
                sheet = _source_sheet(self.ws, mapping)
            except (KeyError, ValueError):
                continue
            if sheet.headers.count(column) != 1:
                continue
            index = sheet.headers.index(column)
            counts = period_counts((row[index] if index < len(row) else None for _, row in sheet.rows), period)
            mapped = period_counts((row.values.get(definition.period) for row in root.rows(dataset)), period)
            periods[dataset] = {'file': sheet.file, 'sheet': sheet.sheet, 'field': definition.period,
                                'source_column': column, 'mapped_window': definition.window,
                                'mapped_rows_by_period': mapped, **counts}
        ppt_sources = []
        remaining = 1500
        for path in self.ws.inv.files:
            if path.suffix.lower() != '.pptx' or file_roles.get(path.name) in ('ignored', 'reference'):
                continue
            for page in ppt_page_index(path):
                if not page['table_count']:
                    continue
                for table in read_ppt_tables(path, page['page']):
                    rows = [[cell['lines'] for cell in row['cells']] for row in table['rows']]
                    cost = sum(map(len, rows))
                    if cost <= remaining:
                        remaining -= cost
                        ppt_sources.append({'file': path.name, 'page': page['page'], 'table': table['table'],
                                            'caption': page['context'], 'rows': rows})
                    else:
                        ppt_sources.append({'file': path.name, 'page': page['page'], 'table': table['table'],
                                            'caption': page['context'], 'omitted_rows': len(rows)})
        return {'engine_scope_observations': relationships, 'source_period_coverage': periods,
                'ppt_parameter_tables': ppt_sources, 'pool_source_excerpts': pool_sources,
                'applied_field_empty_policy': empty_fields, 'verified_source_tier_series': source_tier_series(ppt_sources),
                'rule_source_excerpts': [{'doc': doc, 'quote': quote} for doc, quote in excerpts[:80]]}

    def _validate_formulas(self, value, pool=None, items=(), *, tier_usage=None, report_totals=None):
        from bonus_core.dsl.parser import repair_unclosed_parenthesis

        self._shape(value, ['details', 'totals'] if pool else ['pool', 'helpers', 'tables'])
        if pool is None:
            for name, table in value['tables'].items():
                if isinstance(table, dict) and 'source_series' in table:
                    try:
                        value['tables'][name] = compile_source_tier(
                            table, self._formula_source_observations['verified_source_tier_series'])
                    except ValueError as exc:
                        raise ValueError(f'档位表 {name}：{exc}') from None
                elif not isinstance(table, dict) or table.get('kind') not in ('tier', 'lookup'):
                    raise ValueError(f'档位表 {name} 须选 source_series 或使用原生 kind=tier/lookup 定义')
        if pool:
            for field in ('details', 'totals'):
                if set(value[field]) != {r[1][0] for r in items if r[0] == field}:
                    raise ValueError(f'{field} 须且仅须覆盖本段指定的报告名')
        expressions = ([(field, row[0], row[1], value[field][row[0]]) for field, row in items] if pool else
                       [('pool', 'pool', self.rs.pool.scope, value['pool'])])
        if any(not isinstance(expr, str) or not expr.strip() for _, _, _, expr in expressions):
            raise ValueError('每个独立算式必须为非空字符串')
        current = 'helpers/tables'
        amounts = {}
        repairs = []
        try:
            if pool is None and value['helpers']:
                from bonus_agent.semantics import ground_identity_literals
                from bonus_core.dsl.models import Variable

                variables = {name: Variable.model_validate(item) for name, item in value['helpers'].items()}
                for name, variable in variables.items():
                    if repaired := repair_unclosed_parenthesis(variable.expr):
                        repairs.append({'target': f'variables.{name}', 'submitted': variable.expr, 'applied': repaired})
                        value['helpers'][name] = {**value['helpers'][name], 'expr': repaired}
                        variables[name] = variable.model_copy(update={'expr': repaired})
                declared = self.rs.model_copy(update={'variables': variables})
                roles = self.flow.state.get('plan', {}).get('files', {})
                allowed = {path.name for path in self.ws.inv.files if roles.get(path.name) not in ('ignored', 'reference')}
                refs = {clause.id for clause in self.ws.clauses if clause.file in allowed}
                grounded = []
                for name in variables:
                    expr, evidence = ground_identity_literals(self.ws, declared, name, refs)
                    if evidence:
                        value['helpers'][name] = {**value['helpers'][name], 'expr': expr}
                        grounded.extend(evidence)
                if grounded:
                    self._pool_record['grounded_identity_literals'] = grounded
            for i, (field, label, level, expr) in enumerate(expressions):
                if repaired := repair_unclosed_parenthesis(expr):
                    repairs.append({'target': f'{field}.{label}', 'submitted': expr, 'applied': repaired})
                    if field == 'pool':
                        value['pool'] = repaired
                    else:
                        value[field][label] = repaired
                    expressions[i] = (field, label, level, repaired)
            _, root, levels, ev = self.ws._independent_env(self.name, pool or value, basis=True)
            for field, current, level, expr in expressions:
                nodes = [root] if level == 'global' else levels.get(level) or list((root.index or {}).get(level, {}).values())
                if not nodes:
                    raise ValueError('核算层级没有对象')
                amounts = self.ws._independent_eval(ev, expr, nodes, f'{field}/{current}')
                if field == 'totals' and report_totals is not None:
                    report_totals[current] = sum(amounts.values(), Decimal(0))
            self.ws._independent_holes(ev.rs, ev)
            if tier_usage is not None:
                for name in value['tables']:
                    stats = ev.tier_stats.get(name, {})
                    tier_usage[name] = {'calls': stats.get('n', 0), 'boundary_hits': stats.get('boundary_hits', {})}
        except (ToolError, EvalError, EngineError, ValueError) as exc:
            # Provider input may contain only source data and independent declarations. Engine exceptions
            # can contain evaluated amounts or internal node values and must stay on this side of the check.
            raise ValueError(f'独立算式 {current} 未通过 DSL 或求值校验。请按字段、层级、分档和语法检查') from exc
        if repairs:
            record = self._report_record if pool else self._pool_record
            record['syntax_repairs'] = repairs
        return amounts

    def _draft_cohorts(self, expressions):
        """Summarize actual text categories and attached dates without using production expressions."""
        from bonus_core.dsl.parser import Call, Cmp, Name, Str, names, walk

        _, root, levels, _ = self._scope_env
        scope = self.rs.pool.scope
        nodes = levels.get(scope) or list((root.index or {}).get(scope, {}).values())
        fields = set()
        dates = set()
        for ast in expressions:
            for part in walk(ast):
                if isinstance(part, Cmp):
                    for a, b in ((part.left, part.right), (part.right, part.left)):
                        if isinstance(a, Name) and isinstance(b, Str):
                            fields.add(a.id)
                if isinstance(part, Call) and part.fn in ('min', 'max', 'single_value'):
                    ds = part.kw().get('of')
                    if isinstance(ds, Str) and ds.value in self.rs.datasets:
                        dates.update((ds.value, field) for field in names(part)
                                     if self.rs.datasets[ds.value].fields.get(field) is not None
                                     and self.rs.datasets[ds.value].fields[field].type == 'date')
        keys = sorted(k for k in fields if any(isinstance(n.attrs.get(k), str) for n in nodes))[:4]
        if not keys or not dates:
            return {}
        groups = {}
        for node in nodes:
            key = tuple(str(node.attrs.get(field, '')) for field in keys)
            state = groups.setdefault(key, {'category': dict(zip(keys, key)), 'objects': 0, 'dates': {}})
            state['objects'] += 1
            for dataset, field in sorted(dates):
                items = [str(row.values[field]) for row in node.rows(dataset) if row.values.get(field)]
                if items:
                    stats = state['dates'].setdefault(f'{dataset}/{field}', {'earliest': min(items), 'latest': max(items), 'objects': 0})
                    stats['earliest'] = min(stats['earliest'], min(items))
                    stats['latest'] = max(stats['latest'], max(items))
                    stats['objects'] += 1
        return {'scope': scope, 'objects': len(nodes), 'groups': list(groups.values())[:24], 'omitted': max(0, len(groups)-24)}

    def _clarify_pool_draft(self, unresolved, validate, populations=None):
        draft = getattr(self, '_pool_record', {}).get('independent_draft', {})
        if (not isinstance(draft.get('pool'), str) or not isinstance(draft.get('helpers'), dict)
                or not isinstance(draft.get('tables'), dict) or not draft['tables']):
            return unresolved
        from bonus_core.dsl.parser import ExprError, names, parse
        from bonus_core.dsl.validator import validate_basis

        observed = self._formula_source_observations
        registry = observed['verified_source_tier_series']
        try:
            tables = {name: compile_source_tier(table, registry) for name, table in draft['tables'].items()
                      if isinstance(table, dict) and 'source_series' in table}
            if len(tables) != len(draft['tables']):
                return unresolved
            rs = self.ws._independent_declarations(self.rs, {**draft, 'tables': tables})
        except (ToolError, ValueError):
            return unresolved
        checked = rs.model_copy(update={'pool': rs.pool.model_copy(update={'expr': draft['pool']}),
                                        'checks': [], 'report': None})
        errors = {}
        expressions = []
        for name, variable in rs.variables.items():
            try:
                expressions.append(parse(variable.expr))
            except ExprError as exc:
                errors[('syntax', f'variables.{name}')] = str(exc)
        for issue in validate_basis(checked):
            if issue.severity == 'blocking':
                errors.setdefault((issue.code, issue.where), issue.message)
        if not errors:
            return self._confirm_pool_applicability(unresolved, draft, validate, populations)
        used = set().union(*(names(expr) for expr in expressions))
        _, root, levels, _ = self._scope_env
        scopes = {rs.pool.scope, *(variable.scope for variable in rs.variables.values())}
        direct_fields = {}
        observed_values = {}
        for scope in sorted(scopes - {'global'}):
            nodes = levels.get(scope) or list((root.index or {}).get(scope, {}).values())
            direct_fields[scope] = dict(Counter(field for node in nodes for field in node.attrs))
            used.update(direct_fields[scope])
            values = {}
            for field in direct_fields[scope]:
                counts = Counter(node.attrs[field] for node in nodes
                                 if isinstance(node.attrs.get(field), str) and node.attrs[field])
                if 0 < len(counts) <= 24:
                    values[field] = dict(sorted(counts.items()))
            observed_values[scope] = values
        field_contract = {name: {'link': dataset.link, 'key': dataset.key,
                                 'fields': {field: spec.type for field, spec in dataset.fields.items() if field in used}}
                          for name, dataset in self.rs.datasets.items() if used & dataset.fields.keys()}
        for name, schema in field_contract.items():
            mapping = self.ws.mapping.get(name)
            if mapping is not None:
                schema['source_columns'] = {field: column for field, column in mapping.columns.items()
                                            if field in schema['fields']}
        boundaries = {name: {k: table[k] for k in ('mode', 'purpose', 'steps', 'step_inclusive')}
                      for name, table in tables.items() if table['purpose'] == 'coefficient'}
        removable = {target.removeprefix('variables.') for code, target in errors
                     if code == 'name_clash' and target.startswith('variables.')}
        contract = {
            'prior_independent_declaration': draft,
            'source_verified_bands': boundaries,
            'engine_static_issues': [{'code': code, 'target': target, 'message': message}
                                     for (code, target), message in errors.items()],
            'field_contract': field_contract,
            'direct_fields_by_scope': direct_fields,
            'observed_direct_values_by_scope': observed_values,
            'removable_name_clashes': sorted(removable),
            'observed_categories_and_linked_dates': self._draft_cohorts(expressions),
            'mapped_date_coverage': observed['source_period_coverage'],
            'data_links': observed['engine_scope_observations'],
            'field_empty_policy': observed['applied_field_empty_policy'],
            'rule_source_excerpts': observed['rule_source_excerpts'],
            'dsl': self.flow._dsl(),
        }
        merged = {}

        def validate_amendment(value):
            self._shape(value, ['helpers'])
            helpers = dict(draft['helpers'])
            prior_changes = _prior_helper_amendments(getattr(self, '_pool_clarification_record', {}))
            for name, var in {**prior_changes, **value['helpers']}.items():
                original = draft['helpers'].get(name)
                if var is None and (name in removable or original is None and name in prior_changes):
                    helpers.pop(name, None)
                    continue
                if not isinstance(var, dict):
                    raise ValueError('辅助变量须为带层级和表达式的对象')
                allowed = scopes if original is None else {original.get('scope', 'global')}
                if var.get('scope') not in allowed:
                    raise ValueError('保留已有辅助变量层级；新增辅助变量须使用本段已核对的层级')
                helpers[name] = var
            revised = {**draft, 'tables': copy.deepcopy(draft['tables']),
                       'helpers': helpers, 'unclear': []}
            validate(revised)
            merged.update(revised)

        response = self._part('pool_clarification', ['helpers'],
            '本段为同一份独立声明的局部纠错。只输出 {"helpers":{"改动的变量":{"scope":"原层级","expr":"修复式"}},"unclear":[]}。'
            '保留已选的制度表、分档用途和奖金池定义。未修改的变量省略。'
            '仅 removable_name_clashes 中的重名变量可写 null 删除，须同时修正依赖它的表达式，明确聚合表和原始字段。'
            '依据 source_verified_bands 核对原表端点，step_inclusive=true 表示端点归本档，否则归前一档。'
            '根据实际分类与关联日期确认有疑义的旧项目等分支是否会命中。本期未命中的歧义留作复用前提，不阻断本期。'
            '把月度横表里的字段放到对应表的行上下文，例如 single_value(period_value(12个实际列), of: "原标准明细表")。'
            '参照已有 count(...) 的表名和字段归属，保留当前缺行逻辑。日期须为 date 类型，'
            '已采用 year_to_date 窗口的明细直接聚合该窗口，不重写固定年份。'
            'field_contract 的 source_columns 将标准字段映射到原始列名。direct_fields_by_scope 显示真实对象上存在的标准字段及覆盖数，'
            '原稿遗漏这些字段时仍可引用。业务名称与原列名可能不同，按已映射列、挂接覆盖和原文核查共同决定分类的字段。'
            'observed_direct_values_by_scope 是实际可读取的原值及对象数，含原稿尚未引用的分类字段。'
            '按原文字面匹配客户和区域，包括括号。金额先除以原汇率再乘调整后汇率，代数上等于乘两汇率之比。'
            '当原文给出明确算式及所需原列时，按原文使用。怀疑规则冲突须指出相互冲突的两处原文。'
            'unclear 只列经实际数据和原文核对后仍影响本批金额的事项。',
            contract, validate_amendment, excerpt='')
        if not response.get('unclear'):
            if not merged:
                validate_amendment(response)
            return merged
        saved = getattr(self, '_pool_clarification_record', {}).get('independent_draft', {})
        if not populations or not isinstance(saved.get('helpers'), dict) or not saved['helpers']:
            return response
        try:
            validate_amendment({'helpers': saved['helpers']})
        except ValueError:
            return response

        scope = self.rs.pool.scope
        nodes = levels.get(scope) or list((root.index or {}).get(scope, {}).values())
        alignments = {}
        for dataset, definition in self.rs.datasets.items():
            for field in observed_values.get(scope, {}).keys() & definition.fields.keys():
                comparisons = []
                for node in nodes:
                    own = node.attrs.get(field)
                    values = {row.values[field] for row in node.rows(dataset) if isinstance(row.values.get(field), str)}
                    if own not in (None, '') and values:
                        comparisons.append(values == {own})
                if comparisons:
                    alignments.setdefault(dataset, {})[field] = {'compared': len(comparisons), 'exact': sum(comparisons)}

        def validate_confirmation(value):
            if set(value) - {'unclear'} != {'confirmed'} or value['confirmed'] is not True:
                raise InvalidReviewShape('通过须填写 confirmed=true；仍有未明口径填写具体 unclear')

        confirmation = self._part(
            'pool_evidence_confirmation', ['pool_evidence'],
            '核对上一份独立修正的保留事项。independent_declaration 是同一份已通过语法和求值的独立草稿。'
            '参考表逐值匹配用 linked_field_exact_matches 核对，范围合格行和空值排除用 verified_populations 核对。'
            '按原文和原列核对单位与金额算式；原金额除以原汇率再乘调整汇率等价于按两汇率比值调基。'
            '只有确认所列事项均有可核实证据时才输出 {"confirmed":true,"unclear":[]}。'
            '仍有未明口径则指出具体原文或实际缺失字段与受影响分支，输出 unclear。保持独立声明不变。',
            {'independent_declaration': merged, 'prior_unclear': response['unclear'],
             'verified_populations': populations, 'linked_field_exact_matches': alignments,
             'field_contract': field_contract, 'observed_direct_values_by_scope': observed_values,
             'applied_field_empty_policy': observed['applied_field_empty_policy'],
             'rule_source_excerpts': observed['rule_source_excerpts']},
            validate_confirmation, excerpt='')
        return confirmation if confirmation.get('unclear') else merged

    def _resume_answered_pool(self, validate, populations):
        """Ask the independent reviewer to adjudicate its earlier executable draft against actual finance replies."""
        scopes = self.flow.state.get('applicability_questions', {})
        answers = {}
        for q in self.flow.questions_for(self.name):
            scope = scopes.get(q.id, {})
            if (q.answer and q.answered_by and not q.answered_by.startswith('材料《') and not q.withdrawn
                    and scope.get('scheme') == self.name
                    and scope.get('question') == self.flow._question_identity(q)
                    and scope.get('files') == self.ws._source_version and scope.get('period') == str(self.ws.period)):
                answers[q.id] = {'doc': f'本批财务答复 {q.id}', 'question': q.text, 'quote': q.answer}
        if not answers or not getattr(self.ws, '_review_archive_unchanged', False):
            return None
        model = getattr(self.ws.llm.checker_llm, 'model', None)
        seen = set()
        for row in reversed(list(self.records.values())):
            request = row.get('request', {})
            draft = row.get('independent_draft')
            if (row.get('section') != 'pool' or row.get('state') != 'unresolved' or row.get('replayed_from')
                    or not isinstance(draft, dict) or not draft.get('unclear') or not model
                    or request.get('model') != model or not request.get('response_received')
                    or not request.get('response_id') or request.get('finish_reason') != 'stop'
                    or not request.get('usage_reported')):
                continue
            identity = fingerprint(draft)
            if identity in seen:
                continue
            seen.add(identity)
            tables = draft.get('tables', {})
            if not isinstance(tables, dict) or any(not isinstance(t, dict) or t.get('source_series') not in
                    self._formula_source_observations['verified_source_tier_series'] for t in tables.values()):
                continue
            self._pool_record = {}
            try:
                self._validate_formulas({**copy.deepcopy(draft), 'unclear': []})
            except ValueError:
                continue
            # Selection is by provenance, recency and executability, before any comparison to production money.
            return self._confirm_pool_applicability(draft, draft, validate, populations, finance=answers)
        return None

    def _confirm_pool_applicability(self, unresolved, draft, validate, populations, *, finance=None):
        """An executable draft can still have real business reservations. Inspect each on the same inputs."""
        if not populations:
            return unresolved
        observed = self._formula_source_observations
        excerpts = observed['rule_source_excerpts'] + observed['pool_source_excerpts']
        sources = {f'S{i}': {'doc': doc, 'quote': quote} for i, (doc, quote) in enumerate(
            dict.fromkeys((s['doc'], s['quote']) for s in excerpts), 1)}
        if not sources:
            return unresolved
        candidate = {**copy.deepcopy(draft), 'unclear': []}
        usage = {}
        try:
            amounts = self._validate_formulas(candidate, tier_usage=usage)
        except ValueError:
            return unresolved
        for name, table in draft['tables'].items():
            series = observed['verified_source_tier_series'][table['source_series']]
            usage[name].update({k: series[k] for k in ('doc', 'page', 'table', 'caption', 'heading', 'bands')})
            usage[name]['mode'] = table.get('mode')
            usage[name]['purpose'] = table.get('purpose')
        issues = {str(i): issue for i, issue in enumerate(unresolved['unclear'], 1)}
        if finance:
            sources.update(finance)
            issues.update({ident: decision['question'] for ident, decision in finance.items()})
        signs = Counter('negative' if v < 0 else 'positive' if v > 0 else 'zero' for v in amounts.values())

        scopes = self.flow.state.get('applicability_questions', {})
        current = {q.text: q.id for q in self.ws.questions if not q.withdrawn and not q.answer
                   and scopes.get(q.id, {}).get('question') == self.flow._question_identity(q)
                   and scopes[q.id].get('scheme') == self.name and scopes[q.id].get('files') == self.ws._source_version
                   and scopes[q.id].get('period') == str(self.ws.period)}

        def check_effects(open_issues, prior_objections=()):
            if not finance or not usage or not all('boundary_hits' in issue or any(t in issue for t in usage)
                                                   for issue in open_issues):
                return None
            pending = {current.get(issue, f'open_{i}'): issue for i, issue in enumerate(open_issues, 1)}
            effects = {}
            for ident, issue in pending.items():
                named = [name for name in usage if re.search(r'(?<![A-Za-z0-9_])' + re.escape(name) + r'(?![A-Za-z0-9_])', issue)]
                if 'boundary_hits' in issue and not any(name + '.calls' in issue for name in named):
                    metric, tables = 'boundary_hits', list(usage)
                    triggered = {name: usage[name]['boundary_hits'] for name in tables if usage[name]['boundary_hits']}
                    reason = f'独立公式的 {len(tables)} 张阶梯及系数表，本批实际分界值命中次数合计为 0。'
                elif named and 'calls' in issue and 'boundary_hits' not in issue:
                    metric, tables = 'calls', named
                    triggered = {name: usage[name]['calls'] for name in tables if usage[name]['calls']}
                    reason = '本批独立计算实际调用次数：' + '、'.join(f'{name} 为 0' for name in tables) + '。'
                else:
                    return None
                if triggered:
                    for q in self.ws.questions:
                        if q.id == ident:
                            q.diagnosis['本批影响'] = '本批实际触发：' + _json(triggered)
                    return {'unclear': list(open_issues)}
                effects[ident] = {'metric': metric, 'tables': tables, 'reason': reason, 'observations': {
                    name: {'source_series': draft['tables'][name]['source_series'], 'doc': usage[name]['doc'],
                           'page': usage[name]['page'], 'calls': usage[name]['calls'], 'boundary_hits': usage[name]['boundary_hits']}
                    for name in tables}}

            def check(value):
                if value.get('unclear'):
                    blockers = value.get('blockers')
                    if not isinstance(blockers, list) or len(blockers) != len(value['unclear']):
                        raise InvalidReviewShape('每项 unclear 须附 blockers 的 subject、kind、evidence_id、quote 与 impact，指明本批真实冲突')
                    for blocker in blockers:
                        if not isinstance(blocker, dict) or not all(isinstance(blocker.get(k), str) and blocker[k].strip()
                                for k in ('subject', 'kind', 'evidence_id', 'quote', 'impact')):
                            raise InvalidReviewShape('请按合同提交具体疑义和可核对的原文或独立算式')
                        subject, kind, evidence = blocker['subject'], blocker['kind'], blocker['evidence_id']
                        if subject not in {*pending, *finance, 'new'} or kind not in ('active_condition', 'source_conflict', 'formula_mismatch', 'answer_incomplete'):
                            raise InvalidReviewShape('subject 使用 open_issues、finance_answers 的实际键或 new；kind 使用合同中的类别')
                        if subject in finance and (kind == 'active_condition' or evidence in finance):
                            raise InvalidReviewShape('财务已经裁定该项；本批使用所选依据符合答复。重新质疑须指出材料冲突、算式偏离或答复未涵盖的条件')
                        cite = sources.get(evidence, {}).get('quote') or draft['helpers'].get(evidence, {}).get('expr')
                        if evidence == 'independent_pool':
                            cite = draft['pool']
                        if evidence in usage:
                            cite = _json(usage[evidence])
                        if not cite or blocker['quote'] not in cite:
                            raise InvalidReviewShape('quote 须为 evidence_id 所指来源、表的实测记录或辅助公式中的原文')
                    return
                if (set(value) - {'unclear'} != {'decision', 'reason'} or value['decision'] != 'keep'
                        or not isinstance(value['reason'], str) or not value['reason'].strip()):
                    raise InvalidReviewShape('明确裁决本批全部事项，仅提交 decision=keep 与具体判断理由 reason，仍有影响时填写 unclear 和 blockers')

            outcome = self._part('pool_batch_effects', list(pending),
                'open_issues 的键是本次待核事项；finance_answers 的键是财务已裁定的原问题，原文里的事项序号仅供溯源。'
                '逐项检查实际触发条件，按本批财务的原话核对整份独立公式是否执行其决定。财务指定采用某来源即已确认本批依据；'
                '所选表被调用、与其他表数值相同或推测可能另有版本，都不能单独构成相反证据。不要要求财务重复证明已作出的选择。'
                'previous_objections 是待核意见，结合所引材料和已答复的原问题裁决。存在影响本批的问题时，提交 unclear 字符串数组，'
                '并为每项按相同顺序提交 blockers 数组。每项包含 subject（实际问题键或 new）、kind（active_condition、source_conflict、'
                'formula_mismatch 或 answer_incomplete）、evidence_id（来源 ID、实测表名、独立 helper 名或 independent_pool）、'
                'quote（其内可逐字核验的内容）、impact（命中条件、受影响项目或指标及仍缺什么）。重新质疑已答复事项须具体指出'
                '相反的制度原文、与答复不符的独立公式或答复未涵盖的业务条件。'
                'observed_effects 由引擎逐表保存，包含实际调用、分界值命中和来源位置。逐项判断观测是否覆盖所问事项。'
                '仅当全部观测支持本批不受影响，整份独立公式均执行本批财务决定时，输出 '
                '{"decision":"keep","reason":"说明证据如何覆盖本批疑义、独立公式怎样落实财务决定","unclear":[]}。'
                '这一决定覆盖本次请求的全部待核事项与财务答复，程序绑定原记录和来源。'
                '已有书面规定可作为来源，未来批次需重新检查。此判断不设立未来的端点、费率或负值默认规则。',
                {'open_issues': pending, 'observed_effects': effects, 'finance_answers': finance,
                 'previous_objections': list(prior_objections), 'source_excerpts': sources,
                 'tier_usage': usage, 'sign_counts': dict(signs), 'independent_pool': draft['pool'],
                 'independent_helpers': draft['helpers'], 'report_targets': self.structure['reports']}, check, excerpt='')
            if outcome.get('unclear'):
                return outcome
            validate(candidate)
            for effect in effects.values():
                effect['review_reason'] = outcome['reason']
            self.flow.record_inactive_applicability(self.name, pending, effects, fingerprint(candidate))
            return candidate

        if finance:
            prior = self.flow.state.get('references', {}).get(self.name, {})
            saved = (prior.get('spec') or {}).get('unclear')
            if prior.get('spec_state') == 'unresolved' and saved:
                previous_effects = any(r.get('section') == 'pool_batch_effects' and r.get('state') == 'unresolved'
                                       and r.get('value', {}).get('unclear') == saved for r in self.records.values())
                archived_effects = self.flow.state.get('batch_question_effects', {})
                candidate_digest = fingerprint(candidate)
                for row in reversed(list(self.records.values())):
                    outstanding = row.get('value', {}).get('unclear')
                    if (row.get('section') != 'pool_finance_recheck' or row.get('state') != 'unresolved'
                            or not outstanding or not set(outstanding) <= current.keys()):
                        continue
                    objections = saved if previous_effects else ()
                    if outstanding != saved and not previous_effects:
                        # A later report may be waiting after this exact pool has already passed.
                        # Rebuild the full observation packet. Only an exact independent request can replay.
                        if not all(archived_effects.get(current[text], {}).get('candidate_digest') == candidate_digest
                                   for text in outstanding):
                            continue
                        objections = next((r['value']['unclear'] for r in reversed(list(self.records.values()))
                                           if r.get('section') == 'pool_batch_effects' and r.get('state') == 'unresolved'
                                           and (r.get('value') or {}).get('unclear')), ())
                    if result := check_effects(outstanding, objections):
                        return result
                    break

        def validate_findings(value):
            if finance and value.get('decision') == 'rebuild':
                if (set(value) - {'unclear'} != {'decision', 'reason'} or not isinstance(value.get('reason'), str)
                        or not value['reason'].strip()):
                    raise InvalidReviewShape('重建须指出哪处独立表达式与本批材料冲突')
                return
            if finance and value.get('decision') != 'keep':
                raise InvalidReviewShape('审阅本批全部公式后使用 keep 或 rebuild')
            self._shape({k: v for k, v in value.items() if k != 'decision'} if finance else value, ['resolutions'])
            if value['resolutions'].keys() != issues.keys():
                raise InvalidReviewShape('请逐项按原疑义编号提交判断；仍有未解事项时用 unclear 列出')
            for ident, finding in value['resolutions'].items():
                ids = finding.get('source_ids') if isinstance(finding, dict) else None
                if (not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in sources for i in ids)
                        or finance and ident in finance and ident not in ids
                        or not isinstance(finding.get('reason'), str) or not finding['reason'].strip()):
                    raise InvalidReviewShape('每项判断须有已核对的 source_ids 和说明实际影响的 reason')

        renewal = ('之前的独立草稿属于同一批原材料和期间，本轮取数范围已逐项重新核验。独立检查全部表达式、'
                   '终点口径和本批财务答复；逐项核实暂扣、分配和符号。确认整份草稿仍适用时，在 resolutions '
                   '结果另加 "decision":"keep"；每个财务问题的 source_ids 须包含它自己的答复 ID。'
                   '逐项完成后按 {"decision":"keep","resolutions":{"每个事项ID":{"source_ids":["来源的实际ID"],"reason":"判断理由"}},"unclear":[]} 输出。'
                   '原文或答复需要更改算式时，只返回 {"decision":"rebuild","reason":"具体矛盾","unclear":[]}。'
                   if finance else '')
        answer = self._part('pool_finance_recheck' if finance else 'pool_applicability', list(issues),
            '仅核对本份独立算式列出的疑义是否影响本批。表的页码、标题、逐格档位来自实际制度表。'
            'usage.calls 是本批实际计算使用各表的次数，boundary_hits 只列恰好等于来源分档点的实际调用。'
            'excess 按各档内金额累进，连续档位在分界点的边际金额为零；条件系数则须核对原文包含关系。'
            'sign_counts 给出独立终点值符号的对象数。不同页的数值相同时请核实各自标题、列和注明的适用范围；'
            '怀疑错页须指出相互冲突的原文。对已触发的条件，请逐句核对本批公式、扣减和舍入的完整原文。'
            '不能据引文及实际取数排除的疑义留在 unclear，并说明实际受影响的条件和仍缺的证据。'
            '所有事项均解决才返回 {"resolutions":{"1":{"source_ids":["S1"],"reason":"原文及实际触发情况"}},"unclear":[]}。'
            '保留原独立表达式，程序仍将逐对象比对金额。' + renewal,
            {'issues': issues, 'source_excerpts': sources, 'tier_usage': usage, 'sign_counts': dict(signs),
             'independent_pool': draft['pool'], 'independent_helpers': draft['helpers'],
             **({'verified_populations': populations, 'report_targets': self.structure['reports']} if finance else {})},
            validate_findings, excerpt='')
        if answer.get('unclear'):
            return check_effects(answer['unclear']) or answer
        if finance and answer.get('decision') == 'rebuild':
            return None
        validate(candidate)
        return candidate

    def _report_literal_observations(self, pool):
        """Check the independent helpers' exact categorical spellings against current linked rows."""
        from bonus_core.dsl.parser import Cmp, Name, Str, parse, walk

        predicates = set()
        for helper in pool['helpers'].values():
            for part in walk(parse(helper['expr'])):
                if isinstance(part, Cmp):
                    for field, literal in ((part.left, part.right), (part.right, part.left)):
                        if isinstance(field, Name) and isinstance(literal, Str):
                            predicates.add((helper.get('scope', 'global'), field.id, literal.value))
        _, root, levels, _ = self._scope_env
        observations = []
        for scope, field, literal in sorted(predicates):
            nodes = [root] if scope == 'global' else levels.get(scope) or list((root.index or {}).get(scope, {}).values())
            direct = [n for n in nodes if isinstance(n.attrs.get(field), str)]
            if not direct:
                continue
            linked = {}
            for dataset, definition in self.rs.datasets.items():
                if field not in definition.fields:
                    continue
                paired = []
                for n in direct:
                    values = {r.values[field] for r in n.rows(dataset) if isinstance(r.values.get(field), str)}
                    if values:
                        paired.append(values == {n.attrs[field]})
                if paired:
                    linked[dataset] = {'objects_with_values': len(paired), 'exact_matches': sum(paired)}
            observations.append({'scope': scope, 'field': field, 'literal': literal, 'observed_objects': len(direct),
                                 'exact_literal_matches': sum(n.attrs[field] == literal for n in direct),
                                 'linked_field_agreement': linked})
        return observations

    def _clarify_report_draft(self, unresolved, pool, items):
        draft = getattr(self, '_report_record', {}).get('independent_draft', {})
        if not draft or not any(isinstance(draft.get(k), dict) and draft[k] for k in ('details', 'totals')):
            return unresolved
        report_totals = {}
        try:
            self._validate_formulas(draft, pool, items, report_totals=report_totals)
        except ValueError:
            observed = self._formula_source_observations
            return self._part('report_structure_repair', [row[0] for _, row in items],
                '逐项核对原稿的疑义与实际数据，重写本段独立报表。details、totals 必须分别使用 reports 指定的名称和层级。'
                '在各项声明层级写表达式，程序逐对象求值并汇总。引用已通过的独立辅助变量；如需奖金池表达式，'
                '使用 independent_pool.pool 中的算式，勿假设存在名为 pool 的变量。'
                'engine_row_coverage 按真实作用域给出挂接行数上界；literal_observations 是精确原值及关联字段逐对象核对。'
                '按来源确定业务适用性；确有未解口径时保留具体疑义与受影响的实际记录。'
                '输出 {"details":{"指定明细名":"表达式"},"totals":{"指定汇总名":"表达式"},"unclear":[]}，空组为 {}。',
                {'reports': items, 'prior_report': draft, 'independent_pool': pool,
                 'engine_row_coverage': observed['engine_scope_observations'],
                 'literal_observations': self._report_literal_observations(pool),
                 'source_excerpts': observed['rule_source_excerpts'], 'dsl': self.flow._dsl()},
                lambda value: self._validate_formulas(value, pool, items), excerpt='')
        observed = self._formula_source_observations
        levels = {row[1] for _field, row in items} | {self.rs.pool.scope}
        row_coverage = {level: stats for level, stats in observed['engine_scope_observations'].items()
                        if level in levels}
        from bonus_agent.facts import split_target, target_parts

        details = {row[0] for field, row in items if field == 'details'}
        document_labels = []
        for heading, targets in self.flow.state.get('plan', {}).get('output_targets', {}).items():
            title, _, layout = heading.partition('（')
            if not layout:
                title, _, layout = heading.partition('(')
            if title not in details or not any(word in layout for word in ('工作表', '模块', '列', '一行')):
                continue
            if any(owner == self.name and target_parts(path) == ['report', 'details', title]
                   for owner, path in map(split_target, targets)):
                document_labels.append(title)
        reply = self._part('report_clarification', [row[0] for _field, row in items],
            '请只复核 prior_report 的 unclear 是否在本期仍影响金额。只返回 {"unclear":[]} 或仍有影响的具体疑义。'
            '原草稿各索引与公式已执行校验，保留草稿，勿改写算式或重新审核已核对的上游规则。'
            '参考 source_excerpts 区分核算工作表名称和应展示的金额字段。'
            'output_document_labels 是计划的工作表或输出模块标题，同名明细索引用于指向模块要展示的金额明细。'
            '有明确原文指定该模块保留哪些逐对象金额时，按原文核对已声明的展示表达式，标题本身无需额外定义金额。'
            'engine_row_coverage 是已按本期映射处理后该层每个对象可读的真实行，with_rows 和 objects 一致时该表在本层没有缺行；'
            '若每个对象至多一行，可用单值读取。applied_field_empty_policy 中 skip 已排除空值行，空值行和数值零分开。'
             'independent_sum_checks 来自你自己的本批报表公式。每项比较 target 和列出的其他项之和，'
             '只有材料或你的疑义提出该加总关系时才采用。raw_equal 比较全精度，rounded_sum_equal 比较全精度相加后分别到分，'
             'displayed_sum_equal 比较各项分别到分后相加与目标到分。请据实际观测判断本期报表是否存在需要处理的差额。'
             '仅在业务要求更高精度或本期实际到分金额有差异时提出相应疑义；明确指出受影响的报表项及依据。'
            '请将下一期才会遇到的情形留作适用条件，本期 unresolved 只保留原文和实际数据仍无法消解的问题。'
            '没有实际影响时返回空数组，程序还会逐项比对金额。',
             {'prior_report': draft, 'engine_row_coverage': row_coverage, 'independent_sum_checks': _report_sum_observations(report_totals),
             'applied_field_empty_policy': observed['applied_field_empty_policy'],
             'source_excerpts': observed['rule_source_excerpts'], 'output_document_labels': document_labels},
            lambda value: self._shape(value, []), excerpt='')
        return reply if reply.get('unclear') else {**draft, 'unclear': []}

    def _population_part(self, labels, *, saved_only=False):
        required = self.structure['population_requirements']
        source_options = self.structure['population_source_options']
        source_hints = self.structure['population_source_hints']
        sparse_rows = self.structure.get('sparse_source_rows', {})
        template = {k: {'dataset': required[k].get('dataset', '<能据原文识别合格对象的标准数据集>'),
                        **({'key': '<该来源的标准对象标识>'} if required[k]['kind'] == 'objects' else {}),
                        'filter': '<原始列名构成的JSON筛选对象>', 'period': '<标准行日期字段或JSON null>',
                        'window': '<根据原文选择 period/year_to_date/to_date>',
                        'sources': [{'doc': '<材料名>', 'quote': '<支持该范围的逐字引文>'}]} for k in labels}
        return self._part('populations', labels,
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
            'sparse_source_rows 展示原表只有一个非空值的实际行。结合该值、空列和规则判断业务资格；'
            '这些行可能是占位，也可能是缺失数据的业务行，不能仅因稀疏就排除。'
            '占位或小计行是否参与须有独立材料依据；原列排除可写 {"列名":{"not":["占位标签"]}}。'
            f'{_BLANK_SCOPE_GUIDANCE}'
            'output_template 按本次请求逐个给出 JSON 键；所有尖括号占位符都要据原文换成正确类型的声明。'
            'sources 引用支持本次对象、资格或期间口径的原文。材料确有歧义、原表缺少关键字段，'
            '或无法用以上方式表示时，说明具体缺口并保留 unclear；示例条件不能替代本批依据。'
            'source_options 只核实标准表名、键和日期字段的位置。按原文另查纳入、排除和统计期间，'
            '不要只引用项目键定义就漏掉决定行资格的规则。必要时同时引用键定义和筛选或期间依据。'
            'source_hints 的 ID 只是便于查找的原文，须阅读相应原文和其他相关条款后独立决定。'
            'dataset 必须从允许的标准数据集名称中逐字选取，文件名和工作表名用于引用材料。'
            '每条 sources 可采用 {"source_id":"目录中的完整ID"} 或 {"doc":"材料名","quote":"原文"}。'
            '优先引用支持对象、期间或筛选的业务句。source_options 已给出该范围各候选数据集的原始列。'
            '需要附表头时，只能用最终选定 dataset 对应的 header_sources；其余候选和参考表的表头不可并列作为依据。'
            '程序逐字核验原文并核对表头所属工作表，实际范围仍按独立声明逐行读取。',
            {'population_requirements': {k: required[k] for k in labels}, 'output_template': template,
             'source_options': {k: source_options[k] for k in labels},
             'source_hints': {k: source_hints[k] for k in labels},
             'sparse_source_rows': {k: sparse_rows.get(k, []) for k in labels}},
            lambda value: self._validate_populations(value, labels), saved_only=saved_only)

    def _saved_settlement_populations(self, previous):
        if previous is None:
            return None
        verified = {}
        for labels in groups(list(self.structure['population_requirements']), 4):
            try:
                part = previous._population_part(labels, saved_only=True)
            except (ToolError, ValueError, PopulationDifference):
                return None
            if not part or part.get('unclear') or previous.population_report['status'] != 'PASS':
                return None
            verified.update(part['populations'])
        return verified or None

    def _review_populations(self, saved_populations, scope_choices):
        verified = {}
        for labels in groups(list(self.structure['population_requirements']), 4):
            part = saved_populations._population_part(labels, saved_only=True) if saved_populations else None
            if part is None:
                part = self._population_part(labels)
            else:
                self.population_report = saved_populations.population_report
            if not part.get('unclear') and self.population_report['status'] != 'PASS':
                part = self._clarify_shared_rows(part, labels, verified)
                if self.population_report['status'] != 'PASS':
                    part = (self._apply_scope_choices(part, labels, scope_choices)
                            or self._clarify_population_difference(part, labels))
            if part.get('unclear'):
                return part
            verified.update(part['populations'])
        return {'populations': verified}

    def generate(self, *, saved_populations=None, scope_choices=None, settlement_scope=None):
        reused = self._saved_settlement_populations(settlement_scope)
        selected = {'populations': reused} if reused else self._review_populations(saved_populations, scope_choices)
        if selected.get('unclear'):
            return selected
        # Descriptions are free-form production draft text and may include its formula or result.
        reports = self.structure['reports']
        source_context = dict(self._formula_source_observations)
        source_context['verified_source_tier_series'] = {
            key: {k: v for k, v in series.items() if k not in ('doc', 'page', 'table', 'bands')}
            for key, series in source_context['verified_source_tier_series'].items()
        }
        pool_amounts = {}

        def validate_pool(value):
            pool_amounts.update(self._validate_formulas(value))

        if reused:
            def validate_scope_and_pool(value):
                if value.get('keep_reviewed_population') is not True:
                    # No affirmative opinion on the new answer: run the standard scope review.
                    value.clear()
                    value['keep_reviewed_population'] = False
                    return
                formula = {key: item for key, item in value.items()
                           if key not in ('keep_reviewed_population', 'terminal_review')}
                validate_pool(formula)
                value.update(formula)

            pool = self._review_pool(reports, source_context, validate_scope_and_pool, reused=reused)
            if pool.pop('keep_reviewed_population', False) is True:
                self.reused_settlement_scopes = sorted(reused)
                self.terminal_declaration = pool.pop('terminal_review', None)
            elif pool.get('unclear'):
                return pool  # An unanswered business rule cannot become a money declaration.
            else:
                selected = self._review_populations(saved_populations, scope_choices)
                if selected.get('unclear'):
                    return selected
                reused = None
        focused_reports = False
        if not reused:
            pool = self._resume_answered_pool(validate_pool, selected['populations'])
            if pool is not None and pool.get('unclear'):
                return pool
            focused_reports = pool is not None
            if pool is None:
                pool = self._review_pool(reports, source_context, validate_pool)
        result = {'details': {}, 'totals': {}, **selected, 'unclear': []}
        if pool.get('unclear'):
            pool = self._clarify_pool_draft(pool, validate_pool, result['populations'])
            if pool.get('unclear'):
                return pool
            focused_reports = True
        result.update(pool)
        comparison = self.flow.compare_pool(self.flow.state['basis'][self.name], pool_amounts, self.rs.pool.scope)
        if comparison['issues']:
            pool = self._review_pool_measure(pool, validate_pool)
            if pool.get('unclear'):
                return pool
            result.update(pool)
            comparison = self.flow.compare_pool(self.flow.state['basis'][self.name], pool_amounts, self.rs.pool.scope)
        if comparison['issues']:
            raise PoolDifference(comparison, result)
        report_guidance = ('两组均须提供，空组填 {}。可引用本次独立复核上段已建立的变量和表，严禁改写其定义。')
        for items in groups(reports, 9 if focused_reports else 3):
            report_input = {'reports': items, 'independent_pool': pool, 'dsl': self.flow._dsl()}
            if focused_reports:
                report_input['rule_source_excerpts'] = source_context['rule_source_excerpts']
            part = self._part('reports', [(field, row[0]) for field, row in items],
                '输出 {"details":{"本段明细名":"独立算式"},"totals":{"本段汇总名":"独立算式"},"unclear":[]}。'
                + report_guidance + ('报告名及层级来自 reports；在该层级求值，由程序聚合各对象汇总，global 才需要汇总层公式。'
                                      '逐项核对规则的取整时点、暂扣去向及可分配金额。' if focused_reports else ''),
                report_input, lambda value: self._validate_formulas(value, pool, items),
                **({'excerpt': ''} if focused_reports else {}))
            if part.get('unclear'):
                part = self._clarify_report_draft(part, pool, items)
                if part.get('unclear'):
                    return part
            for field in ('details', 'totals'):
                result[field].update(part[field])
        return result

    def _review_pool_measure(self, pool, validate):
        """Review the meaning of the independent terminal expression once, without either amount."""
        from bonus_core.dsl.parser import names, parse

        helpers = pool['helpers']
        reached = set()
        waiting = list(names(parse(pool['pool'])) & helpers.keys())
        while waiting:
            name = waiting.pop()
            if name not in reached:
                reached.add(name)
                waiting.extend((names(parse(helpers[name]['expr'])) & helpers.keys()) - reached)
        unused = sorted(helpers.keys() - reached)
        if not unused:
            return pool
        observations = self._formula_source_observations
        excerpts = observations['rule_source_excerpts'] + observations['pool_source_excerpts']
        if not excerpts:
            return pool
        corpus = self.ws._corpus()
        roles = self.flow.state.get('plan', {}).get('files', {})

        def verify(value):
            self._shape(value, ['pool', 'sources'])
            if not isinstance(value['pool'], str) or not value['pool'].strip():
                raise ValueError('金额口径复核需给出独立终点表达式')
            sources = value['sources']
            if not sources:
                raise ValueError('金额口径复核须引用确定所求金额的原文')
            for ref in sources:
                if (not isinstance(ref, dict) or not isinstance(ref.get('doc'), str)
                        or not isinstance(ref.get('quote'), str) or roles.get(ref['doc']) in ('ignored', 'reference')
                        or check_quote(ref['doc'], ref['quote'], corpus)):
                    raise ValueError('金额口径复核的来源须为本批材料及其中可逐字核对的原文')
            candidate = {**pool, 'pool': value['pool']}
            validate(candidate)
            value['pool'] = candidate['pool']

        decision = self._part(
            'pool_measure', ['pool'],
            '核对你自己的独立算式究竟计算了哪一项金额。unused_helpers 是你已声明但终点表达式未用的辅助指标，'
            '有些可能仅供其他报表使用。按原文确定供下一步分配的资金处在哪个业务环节，'
            '逐项区分总额、扣减或暂缓及最后可用于分配的余额。材料定义不同则遵从本批口径。'
            '沿用已独立构建的辅助指标、表及原字段。原表达式符合原文就保留，需修正时只更换 pool。'
            '返回 {"pool":"独立表达式","sources":[{"doc":"材料名","quote":"确定金额口径的逐字原文"}],"unclear":[]}。'
            '若口径无法从原文确定，给出具体疑义。不得通过试金额选择表达式。',
            {'prior_pool': pool['pool'], 'scope': self.rs.pool.scope, 'independent_helpers': helpers,
             'unused_helpers': unused, 'independent_table_names': sorted(pool['tables']),
             'datasets': {n: {'key': d.key, 'link': d.link, 'fields': list(d.fields)} for n, d in self.rs.datasets.items()},
             'source_excerpts': excerpts, 'dsl': self.flow._dsl()}, verify, excerpt='')
        if decision.get('unclear'):
            return decision
        return {**pool, 'pool': decision['pool']}

    def _review_pool(self, reports, source_context, validate, *, reused=None):
        contract = {'reports': reports, 'dsl': self.flow._dsl(), **source_context}
        instruction = ''
        excerpt = None
        if reused:
            contract['previous_independent_populations'] = reused
            instruction = (
                '面对本轮财务答复，先独立确认 previous_independent_populations 中的对象范围、名单和行资格、'
                '关联及期间依据是否仍全部适用。旧声明已由程序从本期原表复验，业务适用性仍以本轮答复为准。'
                '有范围改变或无法确认沿用时只输出 {"keep_reviewed_population":false}，程序另行发起取数复核。'
                '全部仍适用时才在本段金额声明加 "keep_reviewed_population":true。金额规则始终按新答复重新判断。'
                '若范围仍适用、金额仍有材料疑义，输出 unclear 和对应业务问题。')
            if focused := reread.settlement_material(self.ws, self.name):
                excerpt, selected, pages = focused
                # The same rule quotes and table text already appear in the retained originals.
                contract.pop('rule_source_excerpts', None)
                contract.pop('pool_source_excerpts', None)
                contract['ppt_parameter_tables'] = [r for r in contract['ppt_parameter_tables'] if
                    r['file'] in selected and (r['file'] not in pages or r['page'] in pages[r['file']])]
                contract['verified_source_tier_series'] = {
                    key: series for key, series in contract['verified_source_tier_series'].items()
                    if any(key.startswith(f"{r['file']}::P{r['page']}.T{r['table']}.")
                           for r in contract['ppt_parameter_tables'])}
                full = self.ws._ruleset(self.name)
                report = full.report
                contract['terminal_targets'] = {
                    'details': {item.name: item.level for item in report.details},
                    'pending': {item.name: item.level for item in report.pending},
                    'people_level': full.allocation[-1].level,
                    'rounding_modes': [item.rounding for item in report.distributions],
                }
                compact = (
                    '复核已确认资金的人员分配。项目资金通过 scheme_paid(方案, "R0") 获取，不重算上游。'
                    '独立阅读原文、财务答复和标准字段。只输出一个 JSON 对象，字段为 pool、helpers、tables、'
                    'keep_reviewed_population、unclear 以及 terminal_review。'
                    'pool 返回 pool_scope 的逐对象金额。helpers 是 {变量: {"scope": 层级, "expr": 算式}}，'
                    'tables 不用时填 {}。原文未明确则用字符串数组 unclear，已明确请填 []。'
                    '如缺任何取数证据，或未展开文件和页可能改变结果，只输出 {"keep_reviewed_population":false}，不推定无关。'
                    '确认原取数声明仍适用才填 keep_reviewed_population:true。'
                    'terminal_review 是同一份独立计算的完整终检：'
                    '{"people":{"mode":"after_sum","key":"人员字段","expr":"未舍入金额"},'
                    '"details":{"报告名":"独立原值式"}, "pending":{"待分配名":"原值式"},'
                    '"items":[{"name":"计算项", "how":"取数与口径", "columns":["工作表/表头"],'
                    '"constants":[{"value":0.1,"unit":"%","meaning":"例"}]}]}。'
                    'mode 由原文决定；allocation 模式要提供 weights。每个已列出的报告和待分配均须覆盖。'
                    'details/pending 在声明层级求值，和 pool 共用独立 helpers/tables。'
                    'items 写实际参与的列、比例、期间和条件；例子不当作依据。'
                    '不得用生产公式或输出金额推断。保留角色原值，仅在原文规定时舍入。'
                    + instruction + _SETTLEMENT_QUESTION_GUIDANCE)
                return self._part('pool', ['pool'], compact, contract, validate, excerpt=excerpt)
        return self._part('pool', ['pool'],
            '输出 {"pool":"独立表达式","helpers":{"变量":{"scope":"层级或global","expr":"表达式"}},"tables":{},"unclear":[]}。'
            '独立制定奖金池和后续报告共用的变量、档位表。报告列表用于规划共用定义，此段不输出报告算式。'
            'pool 按 pool_scope 对每个对象返回一个金额；全体对象合计属于报告汇总。'
            '依据 pool_source_excerpts 和本批原文确认该金额在预扣和分配的哪个环节。'
            '材料要求引用上游已确认方案时，upstream_schemes 仅提供可引用的方案/修订、层级与标识；'
            '按原文决定是否使用 scheme_paid(方案名,修订名)。'
            'engine_scope_observations 来自引擎对当前数据的实际挂接。'
            '通过编号可引用的独立实体可在其 scope 里计算明细聚合，再在当前层直接引用该变量。'
            '跨表是否匹配、预算有记录的范围，均按实际挂接和材料口径判断。'
            'applied_field_empty_policy 显示读取后可求值行中的空值和数值零。'
            'policy=skip 的来源空值行已先剔除；count(of: "该表")==0 表示该对象没有可用行，'
            '保护 single_value 在空集上不求值。仍有数值0的行按其制度查档。'
            '用 source_period_coverage 核对历史文件是否已有本期物理行；按原文和期间字段避免历史表与本期明细重复计入。'
            'mapped_rows_by_period 表示当前作用域映射后可统计的行；它与原始物理行统计用于交叉核对。'
            '使用程序列出的实际分类字面值，包括括号等符号；关注源列货币，核对不同指标适用的区域归类。'
            '有缺失默认依据时先用 count(of: "表") == 0 判断确实缺行，再传给 tier_ratio 的 missing 参数。'
            'missing 在分母表达式前求值，分母为零时也会返回约定的默认系数；多条冲突记录仍需核对。'
            '用带物理坐标的 PPT 原表和 rule_source_excerpts 核对范围和包含端点。'
            '原始档位可选 verified_source_tier_series 的 ID。tables 的值示例'
            '{"source_series":"某文件::P1.T1.C2","mode":"excess","purpose":"amount","below_first":"first"}。'
            '程序从所选原行/列编译费率和端点。系数表用 mode=whole 和 purpose=coefficient；不同客户/区域系数各选自己的行。'
            '其余表用 {"kind":"tier","mode":"whole","purpose":"coefficient","steps":[[0,1]],'
            '"step_inclusive":[true],"below_first":"first"} 或 {"kind":"lookup","mapping":{"指标":1}}。'
            '阶梯或查找函数的表名用标识符，例如 tier(sales_tier, base) 和 tier_ratio(coef, amount, budget, 1)。'
            'unclear 须指出当前对象和原文仍缺失或冲突的业务证据；仅存在已定义的默认值情形时按原文写公式。'
            + (_SETTLEMENT_QUESTION_GUIDANCE if self.flow._settlement_parent(self.name) else '')
            + instruction,
            contract, validate, excerpt=excerpt)
