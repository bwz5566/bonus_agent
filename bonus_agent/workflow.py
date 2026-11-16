"""Program-owned receipts for staged calculation. No model can approve a total."""
from __future__ import annotations

import copy
import json
import hashlib
from pathlib import Path
from datetime import datetime
from decimal import Decimal
from graphlib import TopologicalSorter

from bonus_agent.recovery import fingerprint
from bonus_core.engine.allocation import round_pool

STAGES = (
    ('盘点材料', 'plan_calculation'), ('整理规则', 'record_facts / record_clauses'),
    ('准备数据', 'prepare_data'), ('编译公式', 'compile_formulas'),
    ('基础金额', 'calculate_basis'), ('独立复核总额', 'review_basis'),
    ('等待总额确认', '由财务在总额确认页操作'), ('人员分配', 'trial_run'),
    ('全量终检', 'review_calculation / verify / finish'),
)

_CORE=Path(__file__).resolve().parent.parent/'bonus_core'
_EXECUTION_PROFILE=hashlib.sha256(b''.join(p.read_bytes() for p in sorted(_CORE.rglob('*.py')))+Path(__file__).read_bytes()).hexdigest()


def fail(message):
    from bonus_agent.agent import ToolError
    raise ToolError('workflow.gate：' + message)


class Workflow:
    def __init__(self, ws):
        self.ws = ws
        self.state = ws.workflow
        self.state.setdefault('receipts', {})
        self.state.setdefault('basis', {})
        self.state.setdefault('attempts', {})

    def version(self, kind='formula'):
        ws = self.ws
        body = {'execution_profile':_EXECUTION_PROFILE,'files': ws._source_version, 'period': str(ws.period),
                'datasets': {n:d.model_dump(mode='json') for n,d in ws.datasets.items()},
                'mapping': {n:m.model_dump(mode='json') for n,m in ws.mapping.items()}}
        if kind == 'data':
            body['structure'] = {s:{'patches':ws._merged(s).get('patches'),
                'allocation':[{k:lv.get(k) for k in ('level','key','parent_key')} for lv in ws._merged(s).get('allocation') or []]} for s in ws.schemes}
        else:
            body.update(rules=ws._rules_print(), plan=self.state.get('plan'),
                        facts=[f.model_dump(mode='json') for f in ws.facts],
                        clauses=[c.model_dump(mode='json') for c in ws.clauses],
                        answers=[(q.id,q.answer,q.withdrawn) for q in ws.questions], said=ws.said,transcripts=ws.transcripts)
        return fingerprint(body)

    def receipt(self, name, payload=None, *, kind='formula'):
        rec = {'version':self.version(kind), 'kind':kind, 'at':datetime.now().isoformat(timespec='seconds'),
               'payload':copy.deepcopy(payload or {})}
        self.state['receipts'][name] = rec
        return rec

    def current(self, name):
        rec = self.state['receipts'].get(name)
        return bool(rec and rec['version'] == self.version(rec.get('kind','formula')))

    def require(self, name):
        self.ws._check_source_version()
        if not self.current(name):
            fail(f'{name} 尚未通过或版本已变化；下一步：{self.hint()}')
        if name.startswith('basis:'):
            scheme=name.split(':',1)[1]
            if self.state['receipts'][name]['payload'].get('digest')!=fingerprint(self.state['basis'].get(scheme)):
                fail('预核算工件与通过回执不一致，请重新核算')

    def has_plan(self):
        return self.state.get('plan',{}).get('files_version')==fingerprint(self.ws._source_version)

    def skipped(self):
        return {x['ref'] for x in self.state.get('plan',{}).get('ignored',[])} if self.has_plan() else set()

    def plan(self, summary, outputs, files, schemes, ignored=None):
        known = {p.name for p in self.ws.inv.files}
        if not summary.strip() or not outputs or not schemes or len(set(schemes)) != len(schemes):
            fail('计划须包含任务说明、非空输出清单和不重复的方案名')
        if any(not isinstance(x,str) or not x.strip() for x in [*outputs,*schemes]):
            fail('输出和方案名须为非空文字')
        if set(files) != known:
            fail(f'材料分类须覆盖全部文件；缺少 {sorted(known-set(files))}，未知 {sorted(set(files)-known)}')
        if any(v not in ('rule','data','rule_and_data','reference','ignore') for v in files.values()):
            fail('材料用途须为rule/data/rule_and_data/reference/ignore；忽略依据另登记到条款/说明')
        ignored=ignored or []
        if any(not isinstance(x,dict) or x.get('ref') not in known or not str(x.get('reason','')).strip() for x in ignored):
            fail('忽略项须给真实文件名ref和非空reason')
        if {x['ref'] for x in ignored}!={n for n,r in files.items() if r=='ignore'}:
            extra={x['ref']:files[x['ref']] for x in ignored if files[x['ref']]!='ignore'}
            missing=sorted(n for n,r in files.items() if r=='ignore' and n not in {x['ref'] for x in ignored})
            fail(f'plan.ignore_role：ignored中用途不为ignore的文件及实际用途={extra}；缺少忽略依据={missing}。'
                 'reference保留作参考时从ignored删除；真正不参与时才分类为ignore并写依据。两处必须一致')
        self.state['plan'] = {'summary':summary,'outputs':outputs,'files':files,'schemes':schemes,
                              'ignored':ignored,'files_version':fingerprint(self.ws._source_version)}
        return {'status':'PASS','plan':self.state['plan'],'next':self.hint()}

    def rule_requirements(self):
        ws = self.ws
        plan = self.state.get('plan') or {}
        relevant = {n for n,r in plan.get('files',{}).items() if r in ('rule','rule_and_data')}
        from bonus_agent.agent import RULE_SUFFIXES
        relevant |= {p.name for p in ws.inv.files if p.suffix.lower() in RULE_SUFFIXES and p.name not in self.skipped()}
        result=[]
        for n in sorted(relevant):
            facts=sum(f.file==n for f in ws.facts);clauses=sum(c.file==n for c in ws.clauses)
            result.append({'file':n,'facts':facts,'clauses':clauses,
                           'missing':(['facts'] if not facts else [])+(['clauses'] if not clauses else [])})
        return result

    def rules_ready(self):
        missing=[item['file'] for item in self.rule_requirements() if item['missing']]
        # Automatic document coverage remains mandatory; model classification cannot exempt policy documents.
        missing += self.ws._page_gaps(self.skipped())
        return missing

    def prepare_data(self):
        from bonus_core.data.population import prepare, gaps
        from bonus_core.engine.tree import build_tree
        if not self.has_plan():fail('材料清单或版本已变化，先完成plan_calculation')
        if not self.ws.datasets:fail('尚未定义数据表')
        issues=[]; counts={}
        names=self.ws.schemes or [None]
        for name in names:
            rs=self.ws._ruleset(name) if name else self._data_rules()
            keys={d.key for d in rs.datasets.values() if d.key}
            level_keys={lv.key for lv in rs.allocation}
            for n,d in rs.datasets.items():
                if d.link and d.link not in keys|level_keys:issues.append(f'{n} 关联字段 {d.link} 没有父实体')
                if d.key in level_keys and d.link:issues.append(f'{n} 是分配实体，不能同时声明link')
            data,_=self.ws._data(rs)
            data,patched=prepare(rs,data)
            issues += [str(i) for i in patched if i.severity=='blocking']
            issues += [message for _,message in gaps(rs,data,self.ws.mapping)]
            if rs.allocation:
                issues += [str(i) for i in build_tree(rs,data)[2] if i.severity=='blocking']
            else:
                for n,d in rs.datasets.items():
                    if d.key:
                        keys=[r.values.get(d.key) for r in data[n].rows]
                        if None in keys or len(set(keys))!=len(keys):issues.append(f'{n} 主键为空或重复')
            counts.update({n:len(t.rows) for n,t in data.items()})
        diagnostic=self.ws.diagnose_datasets()
        issues += [json.dumps(i,ensure_ascii=False) for i in diagnostic['issues']
                   if i.get('code') not in ('dataset.reference','dataset.identity') or
                   self.ws.datasets[i['dataset']].unmatched!='ignore']
        report={'status':'FAIL' if issues else 'PASS','counts':counts,'issues':issues,'diagnostic':diagnostic}
        self.state['data_report']={'version':self.version('data'),'report':copy.deepcopy(report)}
        if not issues:self.receipt('data',report,kind='data')
        return report

    def _data_rules(self):
        from bonus_core.dsl.models import RuleSet, Pool
        return RuleSet(name='数据准备',version='workflow',datasets=self.ws.datasets,pool=Pool(expr='0'),allocation=[])

    def query_dataset(self, dataset, expr):
        """Read one mapped source without requiring any unrelated relationship or allocation."""
        from bonus_core.data.mapping import apply_mapping
        from bonus_core.dsl.models import RuleSet, Pool
        from bonus_core.engine.tree import Node, ROOT_LEVEL
        from bonus_core.engine.evaluator import Evaluator
        self.ws._check_source_version()
        if dataset not in self.ws.datasets:fail('请先定义要统计的数据表')
        definition=self.ws.datasets[dataset]
        if definition.population_from:
            fail('该实体范围来自有效明细；请查询population_from明细表，不能把未裁定的原表统计当成有效对象统计')
        ds=definition.model_copy(update={'key':None,'link':None})
        rs=RuleSet(name='原表统计',version='workflow',datasets={dataset:ds},pool=Pool(expr=expr),allocation=[])
        data,issues=apply_mapping(rs,self.ws.data_files,{dataset:self.ws.mapping[dataset]},self.ws.period)
        blocking=[str(i) for i in issues if i.severity=='blocking']
        if blocking:fail('原表统计存在数据问题：'+str(blocking[:8]))
        root=Node(ROOT_LEVEL,ROOT_LEVEL,None);root.own_rows[dataset]=data[dataset].rows
        ev=Evaluator(rs,root,{},period=self.ws.period)
        value=ev.evaluate(expr,root,'source_query','source_query')
        return {'dataset':dataset,'rows':len(data[dataset].rows),'value':str(value),
                'rendered':ev.traces['source_query'].rendered,
                'scope':'仅按该表映射、筛选与期间统计，不证明其他关联或金额通过验收'}

    def compile(self):
        from bonus_core.dsl.parser import names, parse
        self.require('data')
        missing=self.rules_ready()
        if missing:fail('规则证据未齐：'+str(missing[:8]))
        if set(self.ws.schemes)!=set(self.state['plan']['schemes']):
            fail('已声明方案与计划不一致；须落实全部方案，调整计划须依据原文')
        issues=self.ws._binding_gaps()+self.ws._clause_gaps(self.skipped())
        graph={}
        for s in self.ws.schemes:
            rs=self.ws._ruleset(s)
            issues += [str(i) for i in self.ws._validate(s,rs) if i.severity=='blocking']
            deps={n:names(parse(v.expr)) & set(rs.variables) for n,v in rs.variables.items()}
            try:
                order=list(TopologicalSorter(deps).static_order())
            except ValueError as exc:
                issues.append(str(exc));order=[]
            graph[s]={'order':order,'dependencies':{n:sorted(v) for n,v in deps.items()}}
        self.ws._order(self.ws.schemes)  # Inter-scheme cycles are also invalid.
        report={'status':'FAIL' if issues else 'PASS','issues':issues,'graph':graph}
        if not issues:self.receipt('formulas',report)
        return report

    def references(self, name):
        refs={}
        for scheme,revision in self.ws._deps(name):
            self.require('approved:'+scheme)
            if scheme not in self.ws.results or scheme in self.ws.untested:self.ws._trial(scheme)
            if revision not in self.ws.results[scheme]:fail(f'依赖修订 {scheme}/{revision} 不存在')
            refs[f'{scheme}/{revision}']=self.ws.results[scheme][revision]
        return refs

    def calculate_basis(self, scheme=None):
        from bonus_core.engine.run import Engine
        self.require('data');self.require('formulas')
        name=self.ws._scheme(scheme)
        rs=self.ws._ruleset(name);data,_=self.ws._data(rs)
        basis=Engine.prepare(rs,data,self.references(name),self.ws.period).calculate_basis()
        self.state['basis'][name]=basis
        self.receipt('basis:'+name,{'digest':fingerprint(basis)})
        return {'status':'PASS','scheme':name,'digest':fingerprint(basis),
                'pool':basis['pool'],'totals':basis['totals'],'objects':len(basis['objects']),
                'next':'review_basis；此处没有计算人员分配或实发金额'}

    def review_basis(self, scheme=None):
        from bonus_agent import reread as rr
        name=self.ws._scheme(scheme)
        self.require('basis:'+name)
        version=self.version();basis=self.state['basis'][name];digest=fingerprint(basis)
        if self.current('review:'+name):return {'status':'PASS','scheme':name,'next':'等待财务确认总额'}
        rs=self.ws._ruleset(name)
        from bonus_agent.extract_rules import data_dictionary
        structure={'scheme':name,'pool_scope':rs.pool.scope,
                   'period':str(self.ws.period),
                   'data_scope':{n:{'period':d.period,'window':d.window,'population_from':d.population_from,
                                    'filter':self.ws.mapping[n].filter} for n,d in self.ws.datasets.items()},
                   'data_checks':self.state['receipts']['data']['payload'],
                   'allocation':[{'level':lv.level,'key':lv.key,'parent_key':lv.parent_key} for lv in rs.allocation],
                   'columns':{n:m.columns for n,m in self.ws.mapping.items()},
                   'details':[(d.name,d.level,d.desc) for d in (rs.report.details if rs.report else [])],
                   'totals':[(t.name,t.level,t.desc) for t in (rs.report.totals if rs.report else [])]}
        from bonus_agent.context import guard_messages
        msgs=[{'role':'system','content':
            '【独立总额复核】只按原文和数据字典重建公式，不看生产公式或答案。只输出JSON对象：'
            '{"pool":"独立奖金池表达式","helpers":{"变量":{"scope":"层级或global","expr":"表达式"}},'
            '"tables":{},"details":{"报告名":"独立表达式"},"totals":{"汇总名":"独立表达式"},"unclear":[]}。'
            '每个报告项都须给独立公式；材料不清楚写unclear，不猜测。金额由程序计算。'},
            {'role':'user','content':rr.material(self.ws)+'\n'+data_dictionary(self.ws.datasets)+
             '\n结构（不含生产公式）：'+json.dumps(structure,ensure_ascii=False)+
             '\n表达式语法：'+self._dsl()}]
        signature=fingerprint({'messages':msgs,'execution_profile':_EXECUTION_PROFILE})
        prior=self.state.get('references',{}).get(name)
        cached=bool(prior and prior.get('input_signature')==signature)
        if cached:
            spec=copy.deepcopy(prior['spec'])
        else:
            key=f'{name}:{signature}'
            attempts=self.state['attempts'].get(key,0)
            if attempts>=2:fail('同份独立复核输入已达预算，须补充证据或修复程序；修改主公式不能重置顾问预算')
            guard_messages(msgs,getattr(self.ws.llm,'checker_llm',None) or self.ws.llm,output_tokens=8192)
            self.state['attempts'][key]=attempts+1
            from bonus_agent.llm import parse_json
            spec=parse_json(rr._ask(self.ws,msgs))
        if not isinstance(spec,dict) or not isinstance(spec.get('pool'),str) or spec.get('unclear'):
            fail('独立复核没有完整公式或存在未明确口径，保持阻断：'+str(spec.get('unclear') if isinstance(spec,dict) else type(spec).__name__))
        _,root,by_level,ev=self.ws._independent_env(name,spec)
        targets=[root] if rs.pool.scope=='global' else by_level[rs.pool.scope]
        pools=self.ws._independent_eval(ev,spec['pool'],targets,'预核算奖金池')
        actual={k:Decimal(v['amount']) for k,v in basis['objects'].items()}
        issues=[]
        reference_pools={k:round_pool(v) for k,v in pools.items()}
        examples=[]
        def differences(label,actual,reference):
            for key in sorted(set(actual)|set(reference)):
                if actual.get(key)!=reference.get(key):
                    examples.append({'item':label,'object':key,'actual':str(actual.get(key)),
                                     'independent':str(reference.get(key))})
        if reference_pools!=actual:
            issues.append('逐对象奖金池或对象集合不一致');differences('奖金池',actual,reference_pools)
        expected_total=round_pool(sum((v if rs.pool.scope=='global' else round_pool(v) for v in pools.values()),Decimal(0)))
        if expected_total!=Decimal(basis['pool']):issues.append('奖金池汇总不一致')
        details=self.ws._independent_details(name,spec)
        wanted={json.dumps([report,nid],ensure_ascii=False):Decimal(v['amount'])
                for report,rows in basis['details'].items() for nid,v in rows.items()}
        if details!=wanted:
            issues.append('逐对象明细金额或覆盖集合不一致');differences('对象明细',wanted,details)
        totals={}
        for item in (rs.report.totals if rs.report else []):
            expr=(spec.get('totals') or {}).get(item.name)
            if not isinstance(expr,str) or not expr:fail(f'缺少汇总项 {item.name} 的独立公式')
            nodes=[root] if item.level=='global' else by_level.get(item.level) or list((root.index or {}).get(item.level,{}).values())
            if not nodes:fail('独立汇总没有核算对象')
            totals[item.name]=round_pool(sum(self.ws._independent_eval(ev,expr,nodes,'预核算汇总'+item.name).values(),Decimal(0)))
        actual_totals={k:Decimal(v) for k,v in basis['totals'].items()}
        if totals!=actual_totals:
            issues.append('各类汇总金额不一致');differences('汇总',actual_totals,totals)
        self.ws._independent_holes(ev.rs,ev)
        report={'status':'FAIL' if issues else 'PASS','issues':issues,'examples':examples[:8],'basis_digest':digest,'cached':cached,
                'checked_objects':len(actual)+len(wanted),'by':self.ws.checker_down or
                getattr(getattr(self.ws.llm,'checker_llm',None),'name',self.ws.llm.name)}
        self.state.setdefault('references',{})[name]={'version':version,'basis_digest':digest,'input_signature':signature,
                                                    'spec':spec,'report':copy.deepcopy(report)}
        if not issues:
            self.receipt('review:'+name,report)
            self.state['waiting']=name
            self.ws.done=True
        else:
            self.state['last_review']=report
        return report

    @staticmethod
    def _dsl():
        from bonus_agent.auto import prompt
        from bonus_core.dsl.functions import describe
        text=prompt('extract_rules')
        return text[text.index('## 表达式语法'):text.index('## 发放与抵扣')].replace('{functions}',describe())

    def approve(self, name, by, digest):
        if not by.strip():fail('确认人不能为空')
        self.require('review:'+name)
        if self.ws.pending:fail('存在待答复的业务问题，不能确认')
        prior=copy.deepcopy(self.state['basis'][name])
        if fingerprint(prior)!=digest:fail('确认的总额版本已变化，请刷新核对')
        self.calculate_basis(name)  # Recompute from immutable source inputs, not a browser payload.
        if self.state['basis'][name]!=prior:fail('重新计算的金额或对象范围与待确认版本不一致')
        self.receipt('approved:'+name,{'by':by,'basis_digest':digest})
        self.state.pop('waiting',None)
        return {'status':'PASS','scheme':name,'by':by,'digest':digest}

    def guard_allocation(self, name):
        self.require('data');self.require('formulas');self.require('basis:'+name)
        self.require('review:'+name);self.require('approved:'+name)
        digest=fingerprint(self.state['basis'][name])
        if any(self.state['receipts'][k+name]['payload'].get('basis_digest')!=digest for k in ('review:','approved:')):
            fail('金额基线与复核或确认回执不一致')

    def stage(self):
        if not self.has_plan():return 0,None
        if self.rules_ready():return 1,None
        if not self.current('data'):return 2,None
        if not self.current('formulas'):return 3,None
        for s in self.ws._order(self.ws.schemes):
            if not self.current('basis:'+s):return 4,s
            if not self.current('review:'+s):return 5,s
            if not self.current('approved:'+s):return 6,s
            if s not in self.ws.results or s in self.ws.untested:return 7,s
        return 8,None

    def hint(self):
        stage,scheme=self.stage()
        if stage==1:
            missing=[item for item in self.rule_requirements() if item['missing']]
            if missing:
                return '只补缺失项，不重读已登记的整份参数表：'+ '；'.join(
                    f"《{item['file']}》已有{item['facts']}个参数、{item['clauses']}条条款，须用"
                    +'/'.join('record_facts' if part=='facts' else 'record_clauses' for part in item['missing'])
                    +'补齐原文登记' for item in missing[:3])
            gaps=self.ws._page_gaps(self.skipped())
            return '只核对并登记尚未覆盖的原文页：'+'；'.join(gaps[:3])
        last=self.state.get('data_report',{})
        if stage==2 and last.get('version')==self.version('data') and last.get('report',{}).get('status')=='FAIL':
            return '先按数据反例修正字段、期间、完整编号或有效对象范围，再prepare_data；不要原样重试：'+str(last['report']['issues'][:2])[:650]
        return STAGES[stage][1]+(f'（方案：{scheme}）' if scheme else '')

    def summary(self):
        index,scheme=self.stage()
        return {'stage':index,'name':STAGES[index][0],'scheme':scheme,'next':self.hint(),
                'rule_requirements':self.rule_requirements() if index==1 else [],
                'plan':self.state.get('plan'),
                'receipts':{n:{'current':self.current(n),'at':r['at']} for n,r in self.state['receipts'].items()}}

    def allowed(self, tool, stage=None):
        if stage is None:stage,_=self.stage()
        minima={'prepare_data':1,'compile_formulas':3,'calculate_basis':4,'review_basis':5,
                'trial_run':7,'evaluate':4,'explain':7,'review_calculation':8,'verify':8,
                'preview_repair':8,'verify_repair':8,'apply_repair':8}
        if tool=='finish':return stage==8 or bool(self.ws.pending)
        return stage>=minima.get(tool,0)
