"""A typed, data-driven calculation program for fixed financial workflows.

Only lifecycle and operator semantics are fixed. Dataset names, grouping keys,
metrics, dependencies, rates, funding destinations and roles are supplied by
the current material-backed contract. Expressions are data (no eval, imported
code, or arbitrary Python). The entire project DAG is evaluated twice, with
Decimal arithmetic and exact rational arithmetic, before it can be confirmed.
"""
from __future__ import annotations

from collections import defaultdict
from calendar import monthrange
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal as D, ROUND_HALF_UP, localcontext
from fractions import Fraction as F
import json
import hashlib
from typing import Any

from bonus_core.data.table import to_date
from bonus_core.data.period import Period
from bonus_core.dsl.models import to_decimal
from bonus_core.engine.commission import tier_amount, tier_check, bracket


class ProgramError(ValueError):
    """A diagnostic that can be routed to the precise contract owner."""
    def __init__(self, message, *, owner='project_policy', kind='contract', dataset=None,
                 metric=None, key=None, location=None, rules=(), details=None):
        super().__init__(message)
        self.owner=owner;self.kind=kind;self.dataset=dataset;self.metric=metric
        self.key=key;self.location=location;self.rules=list(rules);self.details=details or {}

    def diagnostic(self):
        return {'identified':str(self),'owner':self.owner,'kind':self.kind,'dataset':self.dataset,
                'metric':self.metric,'key':self.key,'location':self.location,'rules':self.rules,
                'details':self.details}


def contract(value, *, required=(), optional=(), label='合同', owner='project_policy'):
    if not isinstance(value,dict):raise ProgramError(f'{label} 必须是对象',owner=owner)
    missing=set(required)-set(value);extra=set(value)-set(required)-set(optional)
    if missing or extra:
        raise ProgramError(f'{label} 缺字段 {sorted(missing)}；未知字段 {sorted(extra)}',owner=owner)
    return value


def names(value,label,owner='project_policy',*,nonempty=True):
    if not isinstance(value,list) or (nonempty and not value) or any(not isinstance(x,str) or not x.strip() for x in value) or len(set(value))!=len(value):
        raise ProgramError(f'{label} 必须是无重复的字符串列表',owner=owner)
    return value


@dataclass(frozen=True)
class Record:
    values: dict
    source: str
    cells: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Dataset:
    fields: dict
    rows: tuple[Record,...]
    evidence: tuple[str,...] = ()


@dataclass(frozen=True)
class Schedule:
    steps: tuple
    method: str = 'whole'
    negative: str = 'error'
    below: str = 'first'
    evidence: tuple = ()


@dataclass
class Inputs:
    datasets: dict[str,Dataset]
    parameters: dict[str,Any] = field(default_factory=dict)
    schedules: dict[str,Schedule] = field(default_factory=dict)
    parameter_evidence: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)


def _scalar(x):
    return x is None or isinstance(x,(str,int,float,bool,D,F,date))


def _number(x):
    return isinstance(x,(D,F,int,float)) and not isinstance(x,bool)


def _decimal(x):
    if isinstance(x,F):return D(x.numerator)/D(x.denominator)
    if isinstance(x,D):return x
    return to_decimal(x)


def _native(x,exact=False):
    if _number(x):
        return (x if isinstance(x,F) else F(_decimal(x))) if exact else _decimal(x)
    return x


def _round(x,digits,exact):
    if not _number(digits) or int(digits)!=digits or not 0<=int(digits)<=8:
        raise ProgramError(f'舍入位数无效：{digits!r}')
    digits=int(digits)
    if exact:
        sign=-1 if x<0 else 1
        q=abs(F(x))*10**digits
        n,r=divmod(q.numerator,q.denominator)
        return F(sign*(n+(2*r>=q.denominator)),10**digits)
    return _decimal(x).quantize(D(1).scaleb(-digits),rounding=ROUND_HALF_UP)


def _same(a,b):
    if _number(a) and _number(b):
        x,y=_decimal(a),_decimal(b)
        return abs(x-y)<=D('1e-24')*max(D(1),abs(x),abs(y))
    return type(a)==type(b) and a==b


def _field_refs(expr):
    if isinstance(expr,dict):
        if set(expr)=={'field'}:return {expr['field']}
        return set().union(*(_field_refs(v) for v in expr.values()))
    if isinstance(expr,list):return set().union(*(_field_refs(x) for x in expr))
    return set()


def _metric_confidence(metric,traces,dependencies):
    """Evidence-grade, not a model probability or a correctness guarantee."""
    score=100;reasons=[]
    if not metric.get('rules'):score-=10;reasons.append('辅助指标没有单独制度条款；按依赖和实际数据核对')
    for t in traces:
        if t['kind']!='query':continue
        if t['selected']==0 and t['aggregate'] not in ('exists','count'):
            score=min(score,75);reasons.append(f'{t["dataset"]} 无匹配，采用明确空集规则 {t["query"]["empty"]}')
        if t['selected']>t['numeric_or_values'] and t['aggregate'] not in ('exists','count'):
            score=min(score,70);reasons.append(f'{t["dataset"]} 存在已授权忽略的空值')
        if t['aggregate']=='unique' and t['selected']>1:
            score=min(score,80);reasons.append(f'{t["dataset"]} 唯一属性来自多条经核验同值记录')
    if dependencies:
        low=min(dependencies.values())
        if low<score:score=low;reasons.append('依赖指标含需优先复核的证据状态')
    return {'score':max(0,score),'reasons':list(dict.fromkeys(reasons)),'arithmetic':'Decimal与有理数一致','meaning':'证据风险优先级，不是正确概率'}


# Arity, including optional arguments, is checked before any data calculation.
# Extend here and the evaluator together, not by injecting code or a prompt.
OPERATORS={
    'add':(2,None),'sub':(2,2),'mul':(2,None),'div':(2,2),'neg':(1,1),
    'abs':(1,1),'min':(1,None),'max':(1,None),'round':(2,2),
    'eq':(2,2),'ne':(2,2),'lt':(2,2),'le':(2,2),'gt':(2,2),'ge':(2,2),
    'and':(2,None),'or':(2,None),'not':(1,1),'if':(3,3),'coalesce':(2,None),
    'is_missing':(1,1),'in':(2,None),'months_between':(2,2),'days_between':(2,2),
    'month':(1,1),'year':(1,1),'quarter':(1,1),'concat':(1,None),'casefold':(1,1),'in_period':(1,1),
}
AGGREGATES={'sum','avg','count','count_distinct','unique','min','max','exists'}
PERIOD_VALUES={'month','start','end','year_start','month_number','year'}
QUERY_KEYS={'dataset','aggregate','match','where','value','empty','nulls','duplicates'}


def canonical_expression(expr,period,*,depth=0):
    """Normalize only exact, semantics-preserving model protocol aliases.

    A literal wrapper is not a new operator. A single-month run's explicit
    current-month argument to in_period is redundant. Longer or ambiguous
    periods, unknown operators and nonliteral wrappers stay unchanged and are
    rejected by the normal typed validator; no business rules are guessed.
    """
    if depth>60 or not isinstance(expr,dict):return expr
    norm=lambda value:canonical_expression(value,period,depth=depth+1)
    if set(expr) in ({'value'},{'const'}) and _scalar(next(iter(expr.values()))):return next(iter(expr.values()))
    if set(expr)=={'op','args'} and isinstance(expr['args'],list):
        op=expr['op'];args=[norm(x) for x in expr['args']]
        if op=='in_period' and len(args)==2 and args[1]=={'period':'month'}:
            selected=period if isinstance(period,Period) else Period.parse(period) if period is not None else None
            if (selected is not None and selected.months==1 and selected.start.day==1
                    and selected.end.day==monthrange(selected.end.year,selected.end.month)[1]):args=args[:1]
        return {'op':op,'args':args}
    if set(expr)=={'query'} and isinstance(expr['query'],dict):
        q=dict(expr['query'])
        if q.get('aggregate') in ('count','exists'):
            # These operators do not inspect a value field; null handling is
            # immaterial. A reserved population dataset already defines its
            # scope, and the empty count/exists semantics are intrinsic.
            q.setdefault('nulls','error');q.setdefault('empty','zero')
            if q.get('dataset') in ('$project','$population'):q.setdefault('match',{})
        if isinstance(q.get('match'),dict):q['match']={k:norm(v) for k,v in q['match'].items()}
        for k in ('where','value'):
            if k in q:q[k]=norm(q[k])
        return {'query':q}
    if set(expr)=={'tier'} and isinstance(expr['tier'],dict) and 'value' in expr['tier']:
        return {'tier':{**expr['tier'],'value':norm(expr['tier']['value'])}}
    return expr


def canonical_program(plan,period):
    """Canonicalize known executable fields, never arbitrary source metadata."""
    if not isinstance(plan,dict):return plan
    population=plan.get('population')
    if isinstance(population,dict) and 'where' in population:
        population['where']=True if population['where']=={} else canonical_expression(population['where'],period)
    metrics=plan.get('metrics')
    if isinstance(metrics,dict):
        for m in metrics.values():
            if isinstance(m,dict) and 'expr' in m:m['expr']=canonical_expression(m['expr'],period)
    checks=plan.get('checks')
    if isinstance(checks,list):
        for check in checks:
            if isinstance(check,dict):
                if 'message' not in check and isinstance(check.get('label'),str):check['message']=check.pop('label')
                if 'expr' in check:check['expr']=canonical_expression(check['expr'],period)
    return plan


def canonical_roster(plan,period):
    for role in plan.get('roles',{}).values() if isinstance(plan,dict) and isinstance(plan.get('roles'),dict) else ():
        if not isinstance(role,dict):continue
        if 'rate' in role:role['rate']=canonical_expression(role['rate'],period)
        spec=role.get('recipients')
        if isinstance(spec,dict):
            if isinstance(spec.get('match'),dict):spec['match']={k:canonical_expression(v,period) for k,v in spec['match'].items()}
            for k in ('where','default_share'):
                if k in spec:spec[k]=canonical_expression(spec[k],period)
    return plan


@dataclass
class Context:
    keys: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    project_rows: tuple = ()
    row: Record | None = None
    trace: list | None = None
    source: str | None = None


class CalculationProgram:
    def __init__(self,inputs: Inputs,plan: dict,period):
        self.inputs=inputs
        self.period=Period.parse(period)
        if self.period is None:raise ProgramError('必须明确核算期间',owner='scope_mapping')
        plan=canonical_program(deepcopy(plan),self.period);self.plan=plan
        self.period_label=self.period.end.strftime('%Y%m')
        contract(plan,required=('population','metrics','funding'),optional=('checks',),label='项目计算合同')
        self.population=contract(plan['population'],required=('dataset','keys','where','rules'),label='项目集合',owner='scope_mapping')
        if self.population['dataset'] not in inputs.datasets:raise ProgramError('项目来源数据集不存在',owner='scope_mapping')
        self.sales=inputs.datasets[self.population['dataset']]
        self.keys=names(self.population['keys'],'项目唯一键','scope_mapping')
        if not set(self.keys)<=set(self.sales.fields):raise ProgramError('项目键必须来自主数据集的真实字段',owner='scope_mapping')
        self.metrics=plan['metrics']
        if not isinstance(self.metrics,dict) or not self.metrics:raise ProgramError('至少需要一个可计算指标')
        if any(not isinstance(k,str) or not k.strip() for k in self.metrics):raise ProgramError('指标ID必须为非空字符串')
        self._validate_expr(self.population['where'],set(),set(),self.sales.fields,owner='scope_mapping')
        self.deps={}
        for name,metric in self.metrics.items():
            contract(metric,required=('label','expr','rules'),optional=('type','owner'),label=f'指标 {name}')
            owner=metric.get('owner','project_policy')
            if owner not in ('scope_mapping','reference_mapping','project_policy','coefficient_policy','hold_policy','roster'):
                raise ProgramError(f'指标 {name} 无效责任节点 {owner}')
            self.deps[name]=self._validate_expr(metric['expr'],set(self.metrics),set(self.keys),None,owner=owner)
        from graphlib import TopologicalSorter, CycleError
        try:self.order=list(TopologicalSorter(self.deps).static_order())
        except CycleError as exc:raise ProgramError(f'指标存在循环依赖：{exc.args[-1]}') from exc
        self.funding=contract(plan['funding'],required=('total','payable','alloc','destinations','labels','summary_digits','rules'),label='资金去向',owner='hold_policy')
        destinations=self.funding['destinations']
        if not isinstance(destinations,dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in destinations.items()):raise ProgramError('暂扣去向应是指标ID到报表名称的映射',owner='hold_policy')
        funds=[self.funding[x] for x in ('total','payable','alloc')]+list(destinations)
        if any(x not in self.metrics for x in funds):raise ProgramError(f'资金去向引用了未编制指标：{funds}',owner='hold_policy')
        # Alloc/payable may refer to the same metric when the policy requires no
        # per-project rounding. Withheld categories cannot alias funding fields.
        if len(set(destinations)&(set(funds[:3])|{'total','payable','alloc','round_delta'})):raise ProgramError('暂扣去向不能重复计算总额/应发金额或使用内部资金字段名',owner='hold_policy')
        labels=self.funding['labels'];contract(labels,required=('total','alloc'),label='汇总名称',owner='hold_policy')
        if len(set([labels['total'],labels['alloc'],*destinations.values()]))!=len(destinations)+2:raise ProgramError('资金汇总名称重复',owner='hold_policy')
        digits=self.funding['summary_digits']
        if type(digits) is not int or not 0<=digits<=8:raise ProgramError('汇总舍入位数无效',owner='hold_policy')
        self.checks=plan.get('checks',[])
        if not isinstance(self.checks,list):raise ProgramError('检查项需要列表')
        for check in self.checks:
            contract(check,required=('id','expr','message','rules'),optional=('owner',),label='核验公式')
            self._validate_expr(check['expr'],set(self.metrics),set(self.keys),None,owner=check.get('owner','project_policy'))
        self._population_rows=()
        self._index={}
        self._query_cache={}

    def _validate_expr(self,expr,metrics,keys,row_fields,*,owner,depth=0):
        if depth>60:raise ProgramError('表达式嵌套过深，拆分为具名指标',owner=owner)
        if _scalar(expr):
            if _number(expr):_decimal(expr)
            return set()
        if not isinstance(expr,dict):raise ProgramError(f'无效表达式 {expr!r}',owner=owner)
        def recurse(x,fields=row_fields):return self._validate_expr(x,metrics,keys,fields,owner=owner,depth=depth+1)
        if 'op' in expr:
            contract(expr,required=('op','args'),label='运算表达式',owner=owner)
            op,args=expr['op'],expr['args']
            if op not in OPERATORS or not isinstance(args,list):raise ProgramError(f'未支持的运算 {op!r}；需要扩展算子或澄清规则',owner=owner,kind='unsupported')
            low,high=OPERATORS[op]
            if len(args)<low or high is not None and len(args)>high:raise ProgramError(f'运算 {op} 参数数量不符',owner=owner)
            # A typed YYYYMM field compared with a full-month-aligned
            # period date is a month-grain comparison, not an arbitrary
            # text/date comparison. Normalize the actual static anchor.
            if op in ('eq','ne','lt','le','gt','ge') and len(args)==2:
                def month_operand(x):
                    if not isinstance(x,dict):return False
                    if set(x)=={'field'} and isinstance(row_fields,dict):return row_fields.get(x['field'],{}).get('kind')=='month'
                    if set(x)=={'key'} and x['key'] in keys:return self.sales.fields.get(x['key'],{}).get('kind')=='month'
                    return False
                def aligned_period(x):
                    if not isinstance(x,dict) or set(x)!={'period'}:return None
                    k=x['period']
                    if k=='year_start':return date(self.period.end.year,1,1)
                    if k=='start' and self.period.start.day==1:return self.period.start
                    if k=='end' and self.period.end.day==monthrange(self.period.end.year,self.period.end.month)[1]:return self.period.end
                    return None
                for i in (0,1):
                    if month_operand(args[i]) and (anchor:=aligned_period(args[1-i])) is not None:args[1-i]=anchor.strftime('%Y%m')
            return set().union(*(recurse(x) for x in args))
        if len(expr)!=1:raise ProgramError(f'表达式必须是单一形式：{expr!r}',owner=owner)
        kind,value=next(iter(expr.items()))
        allowed={'ref':metrics,'key':keys,'field':row_fields or set(),'param':set(self.inputs.parameters),'period':PERIOD_VALUES}
        if kind in allowed:
            if not isinstance(value,str) or value not in allowed[kind]:raise ProgramError(f'{kind} 引用不存在或不属于当前作用域：{value!r}',owner=owner)
            return {value} if kind=='ref' else set()
        if kind=='num':
            _decimal(value);return set()
        if kind=='tier':
            contract(value,required=('schedule','value','output'),label='阶梯表达式',owner=owner)
            if value['schedule'] not in self.inputs.schedules or value['output'] not in ('amount','rate'):
                raise ProgramError(f'阶梯表或输出类型无效：{value!r}',owner=owner)
            return recurse(value['value'])
        if kind=='query':
            contract(value,required=('dataset','aggregate','match','empty','nulls'),optional=('where','value','duplicates'),label='聚合表达式',owner=owner)
            ds=value['dataset'];agg=value['aggregate']
            if ds in ('$project','$population'):fields=self.sales.fields
            elif ds in self.inputs.datasets:fields=self.inputs.datasets[ds].fields
            else:raise ProgramError(f'聚合引用未绑定的数据集 {ds!r}',owner=owner,kind='binding',dataset=ds,details={'available_datasets':sorted(self.inputs.datasets)})
            if agg not in AGGREGATES or value['empty'] not in ('error','null','zero') or value['nulls'] not in ('error','ignore'):
                raise ProgramError(f'{ds} 聚合、空集或空值处理未明确',owner=owner)
            if agg=='unique' and value.get('duplicates') not in ('error','identical'):
                raise ProgramError(f'{ds} 的唯一值查找必须明确重复行处理',owner=owner)
            if agg not in ('count','exists') and 'value' not in value:raise ProgramError(f'{ds}/{agg} 缺少求值表达式',owner=owner)
            match=value['match']
            if not isinstance(match,dict) or not set(match)<=set(fields):raise ProgramError(f'{ds} 关联字段不在源表：{match}',owner=owner)
            deps=set().union(*(recurse(v) for v in match.values()))
            try:
                if 'where' in value:deps|=recurse(value['where'],fields)
                if 'value' in value:deps|=recurse(value['value'],fields)
            except ProgramError as exc:
                if 'field 引用不存在' in str(exc):
                    exc.kind='binding';exc.dataset=ds;exc.details={'actual_fields':sorted(fields),'query':value}
                raise
            return deps
        raise ProgramError(f'未知表达式类型 {kind!r}',owner=owner,kind='unsupported')

    def _period(self,name):
        return {'month':self.period_label,'start':self.period.start,'end':self.period.end,
                'year_start':date(self.period.end.year,1,1),'month_number':self.period.end.month,
                'year':self.period.end.year}[name]

    def _bool(self,v):
        if not isinstance(v,bool):raise ProgramError(f'条件必须返回布尔值，实际 {v!r}')
        return v

    def _num(self,v):
        if not _number(v):raise ProgramError(f'需要数字，实际 {v!r}；空值必须有明确规则')
        return v

    def eval(self,expr,ctx: Context,*,exact=False):
        val=lambda e:self.eval(e,ctx,exact=exact)
        if _scalar(expr):return _native(expr,exact)
        if 'op' in expr:
            op,args=expr['op'],expr['args']
            # Lazy branches are mandatory: a zero denominator or unmatched
            # reference in an inapplicable branch must not execute.
            if op=='if':return val(args[1]) if self._bool(val(args[0])) else val(args[2])
            if op=='and':return all(self._bool(val(x)) for x in args)
            if op=='or':return any(self._bool(val(x)) for x in args)
            if op=='coalesce':
                for x in args:
                    a=val(x)
                    if a is not None:return a
                return None
            a=[val(x) for x in args]
            if op=='is_missing':return a[0] is None
            if op=='not':return not self._bool(a[0])
            if op in ('eq','ne','in'):
                same=lambda x,y:(type(x)==type(y) or _number(x) and _number(y)) and x==y
                if op=='in':return any(same(a[0],x) for x in a[1:])
                return same(a[0],a[1]) if op=='eq' else not same(a[0],a[1])
            if op in ('lt','le','gt','ge'):
                if (not _number(a[0]) or not _number(a[1])) and type(a[0])!=type(a[1]) or None in a:
                    raise ProgramError(f'比较两侧类型/空值不一致：{a!r}')
                if op=='lt':return a[0]<a[1]
                if op=='le':return a[0]<=a[1]
                if op=='gt':return a[0]>a[1]
                return a[0]>=a[1]
            if op=='concat':return ''.join(str(x) for x in a)
            if op=='casefold':
                if not isinstance(a[0],str):raise ProgramError('casefold只用于文本')
                return a[0].casefold()
            if op=='in_period':
                found=self.period.contains(a[0])
                if found is None:raise ProgramError(f'实际日期/月 {a[0]!r} 不能唯一判断是否属于 {self.period.label}',kind='data')
                return found
            if op in ('months_between','days_between','month','year','quarter'):
                dates=[to_date(x) for x in a]
                if op=='months_between':n=(dates[1].year-dates[0].year)*12+dates[1].month-dates[0].month
                elif op=='days_between':n=(dates[1]-dates[0]).days
                elif op=='month':n=dates[0].month
                elif op=='year':n=dates[0].year
                else:n=(dates[0].month-1)//3+1
                return _native(n,exact)
            a=[self._num(x) for x in a]
            if op=='add':return sum(a,_native(0,exact))
            if op=='sub':return a[0]-a[1]
            if op=='mul':
                from functools import reduce
                from operator import mul
                return reduce(mul,a)
            if op=='div':
                if a[1]==0:raise ProgramError('分母为零，必须根据制度选择缺值/零值处理',kind='data')
                return a[0]/a[1]
            if op=='neg':return -a[0]
            if op=='abs':return abs(a[0])
            if op=='min':return min(a)
            if op=='max':return max(a)
            if op=='round':return _round(a[0],a[1],exact)
            raise ProgramError(f'无法执行运算 {op}',kind='unsupported')
        kind,x=next(iter(expr.items()))
        if kind=='num':return _native(_decimal(x),exact)
        if kind=='ref':return ctx.metrics[x]
        if kind=='key':return _native(ctx.keys[x],exact)
        if kind=='field':
            if ctx.row is None:raise ProgramError(f'字段 {x} 不在行级作用域')
            if ctx.trace is not None:ctx.trace.append({'kind':'cell','source':ctx.row.cells.get(x,ctx.row.source),'field':x,'value':ctx.row.values.get(x)})
            return _native(ctx.row.values[x],exact)
        if kind=='param':
            if ctx.trace is not None:ctx.trace.append({'kind':'parameter','id':x,'source':self.inputs.parameter_evidence.get(x),'value':self.inputs.parameters[x]})
            return _native(self.inputs.parameters[x],exact)
        if kind=='period':return _native(self._period(x),exact)
        if kind=='query':return self.query(x,ctx,exact=exact)
        if kind=='tier':
            schedule=self.inputs.schedules[x['schedule']];v=self._num(val(x['value']))
            steps=schedule.steps
            if ctx.trace is not None:ctx.trace.append({'kind':'schedule','id':x['schedule'],'value':v,'evidence':schedule.evidence})
            if x['output']=='rate':
                if v<steps[0][0] and schedule.below=='error':raise ProgramError(f'{x["schedule"]} 观测值 {v} 低于第一档',kind='data')
                if v<steps[0][0] and schedule.below=='zero':return _native(0,exact)
                if exact:
                    selected=steps[0][1]
                    for low,rate,inc in steps:
                        if v>F(low) or inc and v==F(low):selected=rate
                    return F(selected)
                return bracket(v,steps)[1]
            if v<0 and schedule.negative=='error':raise ProgramError(f'{x["schedule"]} 未授权负基数',kind='data')
            if 0<=v<steps[0][0]:
                if schedule.below=='error':raise ProgramError(f'{x["schedule"]} 观测值 {v} 低于第一档',kind='data')
                if schedule.below=='zero':return _native(0,exact)
            return tier_check(v,steps,schedule.method,schedule.negative) if exact else tier_amount(v,steps,schedule.method,schedule.negative)
        raise ProgramError(f'未知表达式 {expr!r}')

    def selected_rows(self,q,ctx,*,exact=False):
        name=q['dataset']
        if name=='$project':rows=ctx.project_rows
        elif name=='$population':rows=self._population_rows
        else:rows=self.inputs.datasets[name].rows
        fields=tuple(q['match'])
        values=tuple(self.eval(q['match'][f],ctx,exact=exact) for f in fields)
        if any(x is None for x in values):raise ProgramError(f'{name} 关联键含空值 {values}',dataset=name,kind='data')
        if fields and name=='$project':
            rows=[r for r in rows if tuple(r.values[f] for f in fields)==values]
        elif fields:
            cachekey=(name,fields)
            if cachekey not in self._index:
                ix=defaultdict(list)
                for r in rows:ix[tuple(r.values[f] for f in fields)].append(r)
                self._index[cachekey]=ix
            rows=self._index[cachekey].get(values,[])
        for row in rows:
            rctx=Context(ctx.keys,ctx.metrics,ctx.project_rows,row,None,ctx.source)
            if 'where' in q and not self._bool(self.eval(q['where'],rctx,exact=exact)):continue
            rctx.trace=ctx.trace
            yield row,rctx

    def _query_cache_key(self,q,ctx,exact):
        """Cache only immutable non-project queries with exact context deps."""
        if q['dataset']=='$project' or ctx.row is not None:return None
        refs={'key':set(),'ref':set()}
        def visit(x):
            if isinstance(x,dict):
                if 'query' in x:return False  # nested query may depend on project rows
                for kind in refs:
                    if set(x)=={kind}:refs[kind].add(x[kind]);return True
                return all(visit(v) for v in x.values())
            if isinstance(x,list):return all(visit(v) for v in x)
            return True
        if not visit(q):return None
        try:
            deps=tuple((kind,k,type((ctx.keys if kind=='key' else ctx.metrics)[k]).__name__,(ctx.keys if kind=='key' else ctx.metrics)[k])
                       for kind,ids in refs.items() for k in sorted(ids))
            cachekey=(json.dumps(q,sort_keys=True,ensure_ascii=False,default=str,separators=(',',':')),deps,exact,ctx.trace is not None)
            hash(cachekey);return cachekey
        except (KeyError,TypeError):return None

    def query(self,q,ctx,*,exact=False):
        cachekey=self._query_cache_key(q,ctx,exact)
        if cachekey is not None and cachekey in self._query_cache:
            value,events=self._query_cache[cachekey]
            if ctx.trace is not None:ctx.trace.extend(events)
            return value
        trace_start=len(ctx.trace) if ctx.trace is not None else 0
        name=q['dataset'];agg=q['aggregate'];rows=[];values=[];items=[]
        used_fields=_field_refs(q.get('value'))|_field_refs(q.get('where'))|set(q['match'])
        # Keep precise locations of selected rows separate from rows inspected
        # for a predicate; they support count/duplicate/missing diagnostics.
        for row,rctx in self.selected_rows(q,ctx,exact=exact):
            rows.append(row.source)
            if agg not in ('exists','count'):
                value=self.eval(q['value'],rctx,exact=exact)
                if value is None:
                    if q['nulls']=='ignore':continue
                    raise ProgramError(f'{name}/{agg} 匹配到空值：{row.source}',dataset=name,location=row.source,kind='data')
                values.append(value)
                if ctx.trace is not None:items.append({'source':row.source,'value':value,'fields':{k:v for k,v in row.values.items() if k in used_fields},'cells':{k:v for k,v in row.cells.items() if k in used_fields}})
            elif ctx.trace is not None:items.append({'source':row.source,'value':True,'fields':{},'cells':{}})
        def finish(value):
            if ctx.trace is not None:ctx.trace.append({'kind':'query','dataset':name,'aggregate':agg,'query':q,'rows':rows,'items':items,
                'selected':len(rows),'numeric_or_values':len(values),'result':value})
            if cachekey is not None:self._query_cache[cachekey]=(value,tuple(ctx.trace[trace_start:]) if ctx.trace is not None else ())
            return value
        if agg=='exists':return finish(bool(rows))
        if agg=='count':return finish(_native(len(rows),exact))
        if not values:
            if q['empty']=='null':return finish(None)
            if q['empty']=='zero':return finish(_native(0,exact))
            raise ProgramError(f'{name}/{agg} 没有可用匹配行',dataset=name,kind='data',details={'match':q.get('match'),'rows':rows})
        if agg=='unique':
            if len(values)>1 and q['duplicates']=='error':
                raise ProgramError(f'{name} 匹配到重复记录，未授权合并：{rows[:5]}',dataset=name,kind='duplicate',details={'rows':rows[:20],'values':list(map(str,values[:20]))})
            if any(v!=values[0] or not (_number(v) and _number(values[0]) or type(v)==type(values[0])) for v in values[1:]):
                raise ProgramError(f'{name} 预期唯一值却存在冲突：{list(zip(rows,values))[:5]}',dataset=name,kind='duplicate',details={'rows':rows[:20],'values':list(map(str,values[:20]))})
            return finish(values[0])
        if agg=='count_distinct':return finish(_native(len(set(values)),exact))
        if agg in ('sum','avg'):
            total=sum((self._num(x) for x in values),_native(0,exact))
            return finish(total if agg=='sum' else total/_native(len(values),exact))
        if agg=='min':return finish(min(values))
        if agg=='max':return finish(max(values))
        raise ProgramError(f'未支持的聚合 {agg}',kind='unsupported')

    def _context_error(self,exc,name,key,metric):
        if not isinstance(exc,ProgramError):exc=ProgramError(str(exc),kind='arithmetic')
        exc.metric=exc.metric or name;exc.key=exc.key or key
        exc.owner=metric.get('owner','project_policy');exc.rules=metric.get('rules',[])
        return exc

    def prepare_population(self):
        self._index.clear()
        self._query_cache.clear()
        groups=defaultdict(list);included=[];excluded=[]
        for row in self.sales.rows:
            try:keep=self._bool(self.eval(self.population['where'],Context(row=row)))
            except Exception as exc:raise ProgramError(f'项目范围判断失败：{row.source}：{exc}',owner='scope_mapping',location=row.source,rules=self.population['rules']) from exc
            if not keep:excluded.append(row.source);continue
            key=tuple(row.values[f] for f in self.keys)
            if any(x is None or x=='' for x in key):raise ProgramError(f'项目唯一键不完整：{key} ({row.source})',owner='scope_mapping',location=row.source,kind='data')
            groups[key].append(row);included.append(row)
        if not groups:raise ProgramError('项目范围筛选后没有有效对象',owner='scope_mapping',kind='data')
        self._population_rows=tuple(included)
        return groups,excluded

    def basis(self):
        """Return frozen project amounts, exact checks and evidence."""
        with localcontext() as arithmetic:
            arithmetic.prec=40
            groups,excluded=self.prepare_population();projects={};cross_checks=0
            self._contexts={}
            trace_pool={};trace_identity={};risk_index=defaultdict(list)
            funding=self.funding;destinations=funding['destinations'];digits=funding['summary_digits']
            raw_sums=defaultdict(lambda:D(0));exact_sums=defaultdict(lambda:F(0))
            for key,raw in sorted(groups.items(),key=lambda kv:tuple(map(str,kv[0]))):
                identity=json.dumps(list(key),ensure_ascii=False,default=str,separators=(',',':'))
                attrs=dict(zip(self.keys,key));rows=tuple(raw)
                dctx=Context(attrs,{},rows);fctx=Context(attrs,{},rows)
                traces={};confidence={}
                for name in self.order:
                    metric=self.metrics[name];expr=metric['expr'];dctx.trace=[]
                    try:
                        d=self.eval(expr,dctx);f=self.eval(expr,fctx,exact=True)
                        if not _same(d,f):raise ProgramError(f'Decimal与有理数独立运算不一致：{d!r} / {f!r}',kind='arithmetic')
                        dtype=metric.get('type')
                        if dtype and d is not None:
                            ok={'number':_number(d),'text':isinstance(d,str),'bool':isinstance(d,bool),'date':isinstance(d,date)}.get(dtype,False)
                            if not ok:raise ProgramError(f'指标 {name} 类型应为 {dtype}，实际 {d!r}')
                    except Exception as exc:raise self._context_error(exc,name,identity,metric) from exc
                    dctx.metrics[name]=d;fctx.metrics[name]=f;cross_checks+=1
                    confidence[name]=_metric_confidence(metric,dctx.trace,{dep:confidence[dep]['score'] for dep in self.deps[name]})
                    if confidence[name]['score']<100:risk_index[name].append({'project':identity,**confidence[name]})
                    compact=[]
                    for event in dctx.trace:
                        if event['kind']=='query':
                            old=trace_identity.get(id(event))
                            if old is not None and old[0] is event:sig=old[1]
                            else:
                                sig=hashlib.sha256(json.dumps(event,ensure_ascii=False,sort_keys=True,default=str,separators=(',',':')).encode('utf8')).hexdigest()
                                trace_identity[id(event)]=(event,sig)
                            trace_pool.setdefault(sig,event);compact.append({'kind':'query_ref','id':sig})
                        elif event['kind']!='cell':compact.append(event) # physical query cells retained in shared query evidence
                    traces[name]=compact
                dctx.trace=None
                for check in self.checks:
                    try:
                        if not self._bool(self.eval(check['expr'],dctx)) or not self._bool(self.eval(check['expr'],fctx,exact=True)):
                            raise ProgramError(check['message'],kind='check')
                    except Exception as exc:raise self._context_error(exc,check['id'],identity,check) from exc
                try:
                    ids=[funding[k] for k in ('total','payable','alloc')]
                    tot,net,alloc=(self._num(dctx.metrics[x]) for x in ids)
                    etot,enet,ealloc=(F(self._num(fctx.metrics[x])) for x in ids)
                    amounts={k:self._num(dctx.metrics[k]) for k in destinations}
                    exact_amounts={k:F(self._num(fctx.metrics[k])) for k in destinations}
                    if etot!=enet+sum(exact_amounts.values(),F(0)):
                        raise ProgramError(f'完整资金去向不闭合：总额 {tot}，应发 {net}，暂扣 {amounts}',owner='hold_policy',kind='balance',key=identity,rules=funding['rules'])
                    if not _same(tot,net+sum(amounts.values(),D(0))):raise ProgramError('十进制资金去向不闭合',owner='hold_policy',kind='arithmetic',key=identity)
                except ProgramError:raise
                for label,d,f in [('total',tot,etot),('payable',net,enet),('alloc',alloc,ealloc),('round_delta',alloc-net,ealloc-enet)]+[(k,amounts[k],exact_amounts[k]) for k in amounts]:
                    raw_sums[label]+=d;exact_sums[label]+=f
                projects[identity]={'key':identity,'key_values':attrs,'metrics':dctx.metrics,'total':tot,'payable':net,'alloc':alloc,
                    'round_delta':alloc-net,'destinations':amounts,'sources':[r.source for r in rows],'traces':traces,'metric_confidence':confidence}
                self._contexts[identity]=(dctx,fctx)
            for name in raw_sums:
                if not _same(raw_sums[name],exact_sums[name]):raise ProgramError(f'汇总 {name} 独立复算不同',owner='hold_policy',kind='arithmetic')
            labels=funding['labels'];totals={labels['alloc']:_decimal(_round(exact_sums['alloc'],digits,True))}
            totals.update({label:_decimal(_round(exact_sums[k],digits,True)) for k,label in destinations.items()})
            totals[labels['total']]=_decimal(_round(exact_sums['total'],digits,True))
            self._basis={'projects':projects,'key_fields':self.keys,'metric_fields':{k:v['label'] for k,v in self.metrics.items()},'trace_pool':trace_pool,
                    'totals':totals,'pool':raw_sums['alloc'],'sums':dict(raw_sums),'funding':funding,
                    'objects':{k:{'raw':v['payable'],'amount':v['alloc']} for k,v in projects.items()},
                    'checks':{'projects':len(projects),'population_rows':len(self._population_rows),'excluded':excluded,'arithmetic_checks':cross_checks,
                              'funding_checks':len(projects)},'parameter_evidence':self.inputs.parameter_evidence,
                    'indicator_confidence':{name:{'min_score':min((v['score'] for v in risk_index[name]),default=100),
                        'risk_projects':len(risk_index[name]),'examples':sorted(risk_index[name],key=lambda v:v['score'])[:3]} for name in self.metrics}}
            return self._basis

    def validate_roster(self,plan):
        contract(plan,required=('roles','rounding','rules'),label='人员分配合同',owner='roster')
        canonical_roster(plan,self.period)
        roles=plan['roles']
        if not isinstance(roles,dict) or not roles:raise ProgramError('未定义任何分配角色',owner='roster')
        labels=[]
        for key,role in roles.items():
            contract(role,required=('label','rate','recipients','rules'),label=f'角色 {key}',owner='roster')
            if not isinstance(key,str) or not key.strip() or not isinstance(role['label'],str) or not role['label'].strip():raise ProgramError('角色ID/名称为空',owner='roster')
            labels.append(role['label'])
            self._validate_expr(role['rate'],set(self.metrics),set(self.keys),None,owner='roster')
            spec=role['recipients'];kind=spec.get('kind') if isinstance(spec,dict) else None
            if kind=='pending':
                contract(spec,required=('kind','label','rules'),label=f'{key} 待分配去向',owner='roster');continue
            contract(spec,required=('kind','dataset','match','pairs','missing_share','missing','duplicates','rules'),
                     optional=('where','text_share','default_share','pending_label'),label=f'{key} 名单绑定',owner='roster')
            if kind not in ('rows','columns'):raise ProgramError(f'{key} 未支持的名单布局 {kind!r}',owner='roster',kind='unsupported')
            ds=spec['dataset']
            if ds not in self.inputs.datasets:raise ProgramError(f'{key} 名单数据集 {ds!r} 未绑定',owner='roster')
            fields=self.inputs.datasets[ds].fields
            match=spec['match']
            if not isinstance(match,dict) or not set(match)<=set(fields):raise ProgramError(f'{key} 名单关联字段不正确',owner='roster')
            for expr in match.values():self._validate_expr(expr,set(self.metrics),set(self.keys),None,owner='roster')
            if 'where' in spec:self._validate_expr(spec['where'],set(self.metrics),set(self.keys),fields,owner='roster')
            if spec['missing_share'] not in ('single','equal','remaining_equal','error') or spec['missing'] not in ('pending','error') or spec['duplicates'] not in ('error','identical','sum'):
                raise ProgramError(f'{key} 缺比例、缺名单或重复处理规则未明确',owner='roster')
            if spec['missing']=='pending' and (not isinstance(spec.get('pending_label'),str) or not spec['pending_label'].strip()):
                raise ProgramError(f'{key} 允许待分配时必须声明真实待分配去向名称',owner='roster')
            if spec['missing_share']=='single':
                if 'default_share' not in spec:raise ProgramError(f'{key} 单人缺比例需要明确默认份额',owner='roster')
                self._validate_expr(spec['default_share'],set(self.metrics),set(self.keys),None,owner='roster')
            pairs=spec['pairs']
            if not isinstance(pairs,list) or not pairs or kind=='rows' and len(pairs)!=1:raise ProgramError(f'{key} 姓名/份额列未绑定',owner='roster')
            for pair in pairs:
                contract(pair,required=('name',),optional=('id','share'),label=f'{key} 名单列',owner='roster')
                if any(x not in fields for x in pair.values() if x is not None):raise ProgramError(f'{key} 名单列引用不在真实数据集：{pair}',owner='roster')
            if spec.get('text_share') is not None and spec['text_share'] not in fields:raise ProgramError(f'{key} 文字比例列不存在',owner='roster')
        if len(set(labels))!=len(labels):raise ProgramError('不同角色名称重复',owner='roster')
        rounding=plan['rounding']
        if isinstance(rounding,dict) and rounding.get('when')=='none' and 'digits' not in rounding:
            # No rounding is executed in this stage. Preserve the confirmed
            # basis display precision as technical metadata only.
            rounding['digits']=self.funding['summary_digits']
        rounding=contract(rounding,required=('when','digits','rules'),label='人员舍入',owner='roster')
        if rounding['when'] not in ('after_sum','per_record','none') or type(rounding['digits']) is not int or not 0<=rounding['digits']<=8:
            raise ProgramError('人员舍入规则无效',owner='roster')
        return plan

    def _recipients(self,spec,ctx,*,exact=False):
        """Resolve a long or wide roster by its declared grain and priorities."""
        if spec['kind']=='pending':return [(spec['label'],spec['label'],_native(1,exact),'pending','制度定义待分配')]
        import re
        entries=[]
        for row,_ in self.selected_rows(spec,ctx,exact=exact):
            raw=row.values
            text=raw.get(spec.get('text_share')) if spec.get('text_share') else None
            if text not in (None,''):
                # Explicit name+percentage text is not mixed with fallback
                # name columns. A malformed text value is a specific data error.
                for part in re.split(r'[,，、;；\n]+',str(text)):
                    m=re.fullmatch(r'\s*(.+?)\s*(\d+(?:\.\d+)?)\s*[%％]\s*',part)
                    if not m or not m[1].strip():raise ProgramError(f'{row.source} 无法解析实际人员比例 {part!r}',owner='roster',dataset=spec['dataset'],location=row.source,kind='data')
                    name=m[1].strip();entries.append((name,name,_native(D(m[2])/100,exact),row.source))
                continue
            for pair in spec['pairs']:
                name=raw.get(pair['name']);idval=raw.get(pair.get('id')) if pair.get('id') else name
                share=raw.get(pair.get('share')) if pair.get('share') else None
                if name in (None,''):
                    if idval not in (None,'') or share not in (None,'',0):raise ProgramError(f'{row.source} 有人员编号/份额但缺姓名',owner='roster',location=row.source,kind='data')
                    continue
                if idval in (None,''):raise ProgramError(f'{row.source} 姓名 {name!r} 缺人员唯一编号',owner='roster',location=row.source,kind='data')
                value=None if share in (None,'') else _native(self._num(share),exact)
                entries.append((str(idval),str(name),value,row.source))
        # Identity and duplicate policy are explicit. Never normalize a role's
        # arbitrary weights to 100% or double-pay a retransmitted row.
        by_id={}
        for pid,name,share,loc in entries:
            if pid not in by_id:by_id[pid]=[name,share,[loc]];continue
            old=by_id[pid]
            if old[0]!=name:raise ProgramError(f'同一人员编号 {pid} 对应不同姓名：{old[0]} / {name}',owner='roster',kind='duplicate')
            if spec['duplicates']=='error' or spec['duplicates']=='identical' and old[1]!=share:
                raise ProgramError(f'人员 {pid} 重复份额未获授权：{old[2]+[loc]}',owner='roster',kind='duplicate')
            if spec['duplicates']=='sum':
                if old[1] is None or share is None:raise ProgramError(f'人员 {pid} 重复记录缺比例，不能求和',owner='roster',kind='data')
                old[1]+=share
            old[2].append(loc)
        missing=[v for v in by_id.values() if v[1] is None]
        known=sum((v[1] for v in by_id.values() if v[1] is not None),_native(0,exact))
        if missing:
            mode=spec['missing_share']
            if mode=='single' and len(by_id)==1:
                default=self.eval(spec['default_share'],ctx,exact=exact)
            elif mode=='equal' and len(missing)==len(by_id):default=_native(1,exact)/_native(len(missing),exact)
            elif mode=='remaining_equal':default=(_native(1,exact)-known)/_native(len(missing),exact)
            else:raise ProgramError(f'有 {len(by_id)} 名人员但 {len(missing)} 人缺对应比例；不能猜测',owner='roster',kind='data',details={'names':[v[0] for v in missing]})
            for v in missing:v[1]=default
        zero,one=_native(0,exact),_native(1,exact)
        if any(not _number(v[1]) or not zero<=v[1]<=one for v in by_id.values()):raise ProgramError('名单个人份额必须在0至1之间',owner='roster')
        used=sum((v[1] for v in by_id.values()),zero)
        # Decimal division of an authorised equal split can differ by an
        # infinitesimal value; the rational execution is the exact oracle.
        if used>one and (exact or not _same(used,one)):raise ProgramError(f'名单份额合计超过100%：{used}',owner='roster',kind='balance')
        result=[(pid,v[0],v[1],'person','；'.join(v[2])) for pid,v in sorted(by_id.items())]
        if used<one and (exact or not _same(used,one)):
            if spec['missing']=='error':raise ProgramError(f'名单缺少 {one-used} 的角色份额',owner='roster',kind='data')
            result.append((spec['pending_label'],spec['pending_label'],one-used,'pending','明确的角色剩余/缺名单政策'))
        return result

    def settlement(self,plan,basis=None,*,overrides=()):
        self.validate_roster(plan)
        if not hasattr(self,'_basis') or basis is not None and basis!=self._basis:
            current=self.basis()
            if basis is not None and current!=basis:raise ProgramError('人员分配所用项目金额不等于当前原件复算',owner='roster')
        basis=self._basis;roles=plan['roles'];rounding=plan['rounding'];digits=rounding['digits'];per=rounding['when']=='per_record'
        override_map={}
        for item in overrides:
            key,role=item.get('project'),item.get('role')
            if role not in roles:
                role=next((k for k,v in roles.items() if v['label']==role),None)
            if key not in basis['projects'] or role not in roles or (key,role) in override_map:
                raise ProgramError('人员修订不匹配本版项目/角色或存在重叠',owner='roster')
            rows=[];seen=set();total=D(0)
            for r in item.get('shares',[]):
                name=str(r.get('name','')).strip();share=_decimal(r.get('share'));pid=str(r.get('id') or name)
                if not name or pid in seen or not D(0)<=share<=D(1):raise ProgramError('人员修订含无效身份或比例',owner='roster')
                seen.add(pid);total+=share;rows.append((pid,name,share,'person',f'财务修订/{item.get("id","")}'))
            if not rows or total>D(1):raise ProgramError('人员修订没有合法份额或超过100%',owner='roster')
            if total<D(1):rows.append((role+'未分配',role+'未分配',D(1)-total,'pending','财务修订剩余份额'))
            override_map[(key,role)]=rows
        with localcontext() as arithmetic:
            arithmetic.prec=40
            all_rows=[];people=defaultdict(lambda:F(0));identity_names={};raw_people=defaultdict(lambda:F(0))
            rates_proof=[];count=0
            for key,project in basis['projects'].items():
                dc,fc=self._contexts[key];rs={};ers={};role_rows=[];exact_sum=F(0)
                exact_alloc=F(fc.metrics[self.funding['alloc']])
                for rid,role in roles.items():
                    try:
                        dc.trace=[];rate=self._num(self.eval(role['rate'],dc));erate=F(self._num(self.eval(role['rate'],fc,exact=True)))
                        if not _same(rate,erate) or not F(0)<=erate<=F(1):raise ProgramError('角色比例不在0～100%或独立复算不同',owner='roster')
                        rates_proof.append({'project':key,'role':rid,'rate':rate,'sources':dc.trace,'rules':role['rules']});dc.trace=None
                        rs[rid]=rate;ers[rid]=erate
                        if (key,rid) in override_map:
                            drows=override_map[(key,rid)];frows=[(p,n,F(sh),dst,src) for p,n,sh,dst,src in drows]
                        else:
                            drows=self._recipients(role['recipients'],dc);frows=self._recipients(role['recipients'],fc,exact=True)
                        if len(drows)!=len(frows):raise ProgramError('人员份额精确复算与十进制记录数不同',owner='roster')
                        if sum((r[2] for r in frows),F(0))!=F(1):raise ProgramError('角色人员与待分配份额未完整覆盖100%',owner='roster')
                        for d,f in zip(drows,frows):
                            pid,name,share,dest,src=d
                            if (pid,name,dest,src)!=(f[0],f[1],f[3],f[4]) or not _same(share,f[2]):raise ProgramError('人员份额独立复算不同',owner='roster')
                            raw=project['alloc']*rate*share;eraw=exact_alloc*erate*f[2]
                            if not _same(raw,eraw):raise ProgramError('角色金额独立复算不同',owner='roster')
                            ident=(dest,pid)
                            if ident in identity_names and identity_names[ident]!=name:raise ProgramError(f'人员身份 {pid} 对应不同姓名',owner='roster',kind='data')
                            identity_names[ident]=name;raw_people[ident]+=eraw;people[ident]+=_round(eraw,digits,True) if per else eraw
                            exact_sum+=eraw;count+=1
                            record={'project':key,'role':role['label'],'role_id':rid,'person_id':pid,'name':name,'role_rate':rate,
                                    'share':share,'raw':raw,'destination':dest,'source':src}
                            rec=role['recipients'];pairs=rec.get('pairs',[])
                            if dest=='person' and (key,rid) not in override_map and pid==name and pairs and not rec.get('text_share') and all(x.get('name')==x.get('id') for x in pairs):
                                # This is an actual identifier used as an
                                # attribution display placeholder, not a claim
                                # about the employee's real master name.
                                record['name_is_identity']=True
                            role_rows.append(record)
                    except Exception as exc:
                        raise self._context_error(exc,rid,key,{'owner':'roster','rules':role['rules']}) from exc
                if sum(ers.values(),F(0))!=F(1):raise ProgramError(f'项目 {key} 制度角色比例合计 {sum(ers.values(),F(0))} 不为100%',owner='roster',key=key,kind='balance')
                if exact_sum!=exact_alloc:raise ProgramError(f'项目 {key} 人员及待分配未覆盖可分配金额',owner='roster',key=key,kind='balance')
                all_rows.extend(role_rows)
            items=[{'id':pid,'name':identity_names[(dest,pid)],'raw':_decimal(raw_people[(dest,pid)]),'amount':_decimal(value if rounding['when']=='none' else _round(value,digits,True)),'destination':dest}
                   for (dest,pid),value in sorted(people.items())]
            named=sum((r['amount'] for r in items if r['destination']=='person'),D(0));pending=sum((r['amount'] for r in items if r['destination']=='pending'),D(0))
            return {'rows':all_rows,'people':items,'allocated':named,'pending':pending,'residual':basis['sums']['alloc']-named-pending,
                    'checks':{'projects':len(basis['projects']),'role_rows':count,'people':sum(r['destination']=='person' for r in items),
                              'pending_categories':sum(r['destination']=='pending' for r in items)},'parameter_evidence':rates_proof,'rounding':rounding}


CONTRIBUTION_DATASET='approved_contributions'
CONTRIBUTION_FIELDS={'project':'id','role_id':'id','person_id':'id','name':'text','name_is_identity':'bool','raw':'money','rounded_record':'money','destination':'text'}


def personal_program_inputs(inputs,settlement):
    """An immutable standard data relation derived from confirmed project money.

    It is not a new model estimate. A later employee-level plan can query
    these actual attributed records, a separate employee master and offsets.
    """
    if CONTRIBUTION_DATASET in inputs.datasets:raise ProgramError(f'{CONTRIBUTION_DATASET} 为实际核算生成的保留关系',owner='roster')
    digits=settlement['rounding']['digits'];rows=[]
    for i,r in enumerate(settlement['rows'],1):
        if r['destination']!='person':continue
        raw=r['raw'];values={'project':r['project'],'role_id':r['role_id'],'person_id':r['person_id'],'name':r.get('original_identity_display',r['name']),
            'name_is_identity':bool(r.get('name_is_identity')),
            'raw':raw,'rounded_record':_round(raw,digits,False) if settlement['rounding']['when']=='per_record' else raw,'destination':'person'}
        where=f'已确认项目归属/{r["project"]}/{r["role_id"]}/{i}'
        rows.append(Record(values,where,{k:where+'/'+k for k in values}))
    return Inputs({**inputs.datasets,CONTRIBUTION_DATASET:Dataset({k:{'kind':v,'nullable':False} for k,v in CONTRIBUTION_FIELDS.items()},tuple(rows))},
                  inputs.parameters,inputs.schedules,inputs.parameter_evidence,inputs.diagnostics)


def apply_person_program(inputs,basis,settlement,spec,period):
    """Calculate a distinct employee-level program after attribution.

    This supports employee master populations including zero-order people,
    personal assessment factors, offsets, caps/floors and arbitrary additional
    metrics. The bridge separates rule-driven adjustments from actual rounding
    residuals so the first confirmed amount is never misrepresented as pay.
    """
    contract(spec,required=('program','identity_key','name_metric','attributed_metric','contribution_amount','rules'),label='人员层核算合同',owner='roster')
    if spec['contribution_amount'] not in ('raw','rounded_record'):raise ProgramError('人员层必须明确引用归属原值或逐记录舍入值',owner='roster')
    p=CalculationProgram(personal_program_inputs(inputs,settlement),spec['program'],period)
    if p.keys!=[spec['identity_key']] or spec['name_metric'] not in p.metrics or spec['attributed_metric'] not in p.metrics:
        raise ProgramError('人员层主集合须明确唯一身份字段、姓名及上游归属金额指标',owner='roster')
    # All named contributions must be represented by exactly one identity in
    # the person master. A zero-transaction employee is preserved as an object
    # rather than disappearing with the group-by on the contribution table.
    try:result=p.basis()
    except ProgramError as exc:exc.owner='roster';raise
    people=[];attrib_by_id=defaultdict(lambda:D(0));names_by_id={}
    with localcontext() as ctx:
        ctx.prec=40
        for row in p.inputs.datasets[CONTRIBUTION_DATASET].rows:
            pid=row.values['person_id'];name=row.values['name']
            if row.values['name_is_identity']:
                if name!=pid:raise ProgramError(f'人员身份 {pid} 的编号占位记录不一致',owner='roster',kind='data')
            else:
                if pid in names_by_id and names_by_id[pid]!=name:raise ProgramError(f'人员身份 {pid} 姓名冲突',owner='roster')
                names_by_id[pid]=name
            attrib_by_id[pid]+=row.values[spec['contribution_amount']]
        seen=set()
        for project,person in result['projects'].items():
            pid=str(person['key_values'][spec['identity_key']]);metrics=person['metrics'];name=metrics[spec['name_metric']]
            if not isinstance(name,str) or not name or pid in seen:raise ProgramError(f'人员层身份或姓名无效：{pid}/{name}',owner='roster')
            seen.add(pid)
            if pid in names_by_id and names_by_id[pid]!=name:raise ProgramError(f'人员层主数据 {pid}/{name} 与项目归属姓名 {names_by_id[pid]} 不一致',owner='roster',kind='data')
            actual=p._num(metrics[spec['attributed_metric']]);expected=attrib_by_id[pid]
            if not _same(actual,expected):raise ProgramError(f'人员 {pid} 已确认项目归属金额 {expected} 与人员公式取数 {actual} 不一致',owner='roster',key=pid,metric=spec['attributed_metric'],kind='balance')
            people.append({'id':pid,'name':name,'raw':person['total'],'amount':person['alloc'],'destination':'person','attributed':actual,
                           'entitled':person['total'],'payable':person['payable'],'destinations':person['destinations'],'metrics':metrics,'person_project':project})
        absent=set(attrib_by_id)-seen
        if absent:raise ProgramError(f'已确认项目归属 {sorted(absent)} 未进入人员主集合',owner='roster',kind='data')
        named_raw=sum((r['raw'] for r in settlement['rows'] if r['destination']=='person'),D(0))
        pending_raw=sum((r['raw'] for r in settlement['rows'] if r['destination']=='pending'),D(0))
        attrib=sum((r['attributed'] for r in people),D(0));entitled=result['sums']['total'];paid=result['sums']['alloc']
        pending_items=[r for r in settlement['people'] if r['destination']=='pending'];pending=sum((r['amount'] for r in pending_items),D(0))
        destinations={k:result['sums'].get(k,D(0)) for k in p.funding['destinations']}
        adjustment=attrib-entitled
        attribution_delta=basis['sums']['alloc']-attrib-pending
        personal_round=result['sums']['round_delta'];residual=attribution_delta-personal_round
        if not _same(basis['sums']['alloc'],paid+pending+adjustment+sum(destinations.values(),D(0))+residual):
            raise ProgramError('项目归属→人员规则→发放全过程不闭合',owner='roster',kind='balance')
        if not _same(named_raw+pending_raw,basis['sums']['alloc']):raise ProgramError('人员层输入项目归属原值不闭合',owner='roster',kind='balance')
        bridge={'approved_basis':basis['sums']['alloc'],'named_attributed':attrib,'pending':pending,'entitled':entitled,
                'rule_adjustment':adjustment,'deductions':destinations,'paid':paid,'attribution_residual':attribution_delta,
                'personal_round_delta':personal_round,'residual':residual,'destination_labels':p.funding['destinations']}
        master_names={r['id']:r for r in people};resolved_rows=[]
        for r in settlement['rows']:
            if r.get('name_is_identity') and r['person_id'] in master_names:
                resolved=master_names[r['person_id']];source=result['projects'][resolved['person_project']]['sources']
                r={**r,'original_identity_display':r.get('original_identity_display',r['name']),'name':resolved['name'],'name_source':source}
            resolved_rows.append(r)
        amended={**settlement,'rows':resolved_rows,'attribution_people':settlement['people'],'people':people+pending_items,'allocated':paid,'pending':pending,'residual':residual,
                 'person_basis':result,'funding_bridge':bridge,'person_plan':spec,
                 'checks':{**settlement['checks'],'people':len(people),'person_arithmetic_checks':result['checks']['arithmetic_checks'],'person_master_coverage':len(seen)}}
        return amended
