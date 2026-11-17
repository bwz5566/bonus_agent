"""Experimental bounded agents. Models propose; a single writer commits tool actions."""
from __future__ import annotations

import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from bonus_agent.llm import LLMError, Usage, parse_json
from bonus_agent.recovery import fingerprint

_CALLS = threading.BoundedSemaphore(2)
MAX_ACTIONS = 4
READS = {'read_document', 'read_sheet', 'read_parameter_table', 'read_workspace',
         'list_files', 'query_dataset', 'diagnose_datasets', 'inspect_sheet', 'view_image'}
EVIDENCE = {'record_facts', 'record_parameter_cells', 'record_clauses', 'cover_clauses',
            'bind_facts', 'validate_parameters', 'remove_facts', 'answer_question', 'ask_question', 'note'}
DATA = {'define_dataset', 'update_dataset', 'remove_dataset', 'set_rules', 'prepare_data'}
FORMULA = {'set_rules', 'compile_table', 'compile_coefficient_table', 'bind_facts', 'check_formula_task','patch_rule_component'}


class Meter:
    """Reservations cover concurrent calls; usage survives same-case resumes."""
    def __init__(self, state, usage, maximum, stop=None):
        self.state, self.usage, self.maximum = state, usage, maximum
        self.stop = stop or (lambda: False)
        self.lock = threading.RLock()
        self.reserved = 0
        state.setdefault('tokens', 0)
        state.setdefault('calls', 0)
        state.setdefault('active_seconds', 0.0)
        self.last_tick = time.monotonic()

    def check(self):
        with self.lock:
            now = time.monotonic()
            self.state['active_seconds'] += now - self.last_tick
            self.last_tick = now
            if self.state['active_seconds'] >= 1800:
                raise LLMError('team.budget：累计运行时间已达1800秒，保留任务现场')
        if self.stop():
            raise LLMError('team.stopped：已停止；未启动新调用')
        if self.maximum and self.state['tokens'] >= self.maximum:
            raise LLMError('team.budget：累计Token预算耗尽，保留任务现场')

    def invoke(self, model, method, messages, **kwargs):
        from bonus_agent.context import guard_messages
        # Per-request model state is private, including reasoning and usage counters.
        chosen = model.think_llm if kwargs.get('think') and getattr(model, 'think_llm', None) else model
        local = copy.copy(chosen)
        local.think_llm = None
        local.usage = Usage()
        output = kwargs.get('max_tokens', 8192)
        if kwargs.get('think') and getattr(local,'think_body',None):
            output=max(output,local.think_max_tokens)
        if getattr(local,'_max_output_tokens',None):output=min(output,int(local._max_output_tokens))
        kwargs['max_tokens']=output
        tools = kwargs.get('tools')
        estimate = guard_messages(messages, local, output_tokens=output, tools=tools)
        reserve = estimate + output
        while not _CALLS.acquire(timeout=0.1):
            self.check()
        try:
            with self.lock:
                self.check()
                if self.maximum and self.state['tokens'] + self.reserved + reserve > self.maximum:
                    raise LLMError('team.budget：剩余额度不足以预留本次输入与输出，未调用模型')
                self.reserved += reserve
            try:
                return getattr(local, method)(messages, **kwargs)
            finally:
                with self.lock:
                    self.reserved -= reserve
                    self.usage += local.usage
                    self.state['tokens'] += local.usage.prompt_tokens + local.usage.completion_tokens
                    self.state['calls'] += local.usage.calls
                    model.last_call = dict(getattr(local, 'last_call', {}))
                    model.last_reasoning = getattr(local, 'last_reasoning', '')
        finally:
            _CALLS.release()


class BudgetModel:
    def __init__(self, model, meter):
        self.model, self.meter = model, meter

    def __copy__(self):
        return BudgetModel(copy.copy(self.model), self.meter)

    def __setattr__(self, key, value):
        if key in ('model', 'meter'):
            object.__setattr__(self, key, value)
        else:
            setattr(object.__getattribute__(self, 'model'), key, value)

    def __getattr__(self, key):
        model = object.__getattribute__(self, 'model')
        if key in ('checker_llm', 'think_llm', 'vision_llm'):
            child = getattr(model, key, None)
            return BudgetModel(child, self.meter) if child is not None else None
        if key == 'usage':
            return self.meter.usage
        return getattr(model, key)

    def chat(self, messages, **kwargs):
        return self.meter.invoke(self.model, 'chat', messages, **kwargs)

    def chat_json(self, messages, **kwargs):
        return parse_json(self.chat(messages, json_out=True, **kwargs))

    def chat_tools(self, messages, tools, **kwargs):
        return self.meter.invoke(self.model, 'chat_tools', messages, tools=tools, **kwargs)


class TeamSession:
    def __init__(self, ws, maximum, stop=None):
        self.ws = ws
        self.state = ws.workflow.setdefault('team', {'tasks': {}, 'histories': {}, 'meter': {}})
        for key in ('tasks','histories','meter'):self.state.setdefault(key,{})
        self.meter = Meter(self.state['meter'], ws.llm.usage, maximum, stop)
        self.primary = ws.llm
        ws.llm = BudgetModel(ws.llm, self.meter)
        self.serial = 0
        self.batch_versions = {}
        self.checkpoint = lambda: None

    def owners(self):
        from bonus_agent.construction import TaskBoard
        pending=next((r for r in reversed(list(self.state.get('repairs',{}).values())) if r['status']=='PENDING'),None)
        if pending:return [('repair:'+pending['id'],None)]
        stage = self.ws._flow().stage()[0]
        board = TaskBoard(self.ws)
        if stage == 3:
            board.refresh() if hasattr(board, 'refresh') else None
            pending = [(n, board.state['tasks'][n]) for n, s in board.statuses().items()
                       if s['status'] == 'PENDING' and not s.get('cycle')]
            if pending:
                return [('formula:' + n, spec) for n, spec in pending[:2]]
        return [({0: 'coordinator', 1: 'rules', 2: 'data', 3: 'coordinator', 4: 'basis',
                  5: 'review', 6: 'confirmation', 7: 'allocation', 8: 'output'}.get(stage, 'coordinator'), None)]

    def version(self, spec=None):
        from bonus_agent.construction import TaskBoard
        if spec:
            return fingerprint({'component':TaskBoard(self.ws).statuses()[spec['id']]['version'],
                'financial_context':self.ws.said,
                'answers':[(q.id,q.answer,q.withdrawn) for q in self.ws.questions]})
        return self.ws._flow().version()

    def tools(self, definitions, spec, owner):
        from bonus_agent.agent import READ_ONLY
        common={'request_module_repair'}
        if owner.startswith('repair:'):
            owner=self.state['repairs'][owner.split(':',1)[1]]['kind']
            common.add('finish_module_repair')
        if spec:
            return [t for t in definitions if t['name'] in READ_ONLY | FORMULA | common | {'ask_question', 'note'}]
        allowed = {'rules': EVIDENCE, 'data': DATA | EVIDENCE,
                   'basis': {'calculate_basis', 'ask_question', 'note'},
                   'review': {'review_basis', 'ask_question', 'note'},
                   'allocation': {'trial_run', 'ask_question', 'note'}}.get(owner)
        if allowed is not None:
            return [t for t in definitions if t['name'] in READ_ONLY | allowed | common |
                    ({'patch_rule_component'} if owner=='data' else set())]
        return definitions

    def propose(self, system, task, definitions):
        from bonus_agent.agent import openai_tools
        from bonus_agent.context import task_card
        self.meter.check()
        card = json.loads(task_card(self.ws))
        owners = self.owners()
        stage, scheme = self.ws._flow().stage()
        program_tool = {4:'calculate_basis', 5:'review_basis', 7:'trial_run'}.get(stage)
        if owners[0][0].startswith('repair:'):program_tool=None
        if owners[0][0].startswith('repair:'):program_tool=None
        # Capture all immutable packets before worker threads start.
        packets = []
        for owner, spec in owners:
            version = self.version(spec)
            record = self.state['tasks'].setdefault(owner, {'attempts': {}, 'status': 'READY'})
            if record.get('version') != version:
                if record.get('problem'):
                    record['previous_problem']={'version':record.get('version'),'problem':record.pop('problem')}
                record.pop('diagnosis_pending',None)
                record.update(version=version, status='READY')
            if record['status'] == 'BLOCKED':
                if record.pop('diagnosis_pending', False) and getattr(self.primary, 'think_llm', None) is not None:
                    from bonus_agent.recovery import local_advice
                    try:
                        record['diagnosis'] = local_advice(BudgetModel(self.primary.think_llm, self.meter),
                            record.get('problem', ''), [], self.ws.facts, self.ws.next_hint())
                    except (LLMError,ValueError) as exc:
                        record['diagnosis_error']=str(exc)
                    self.checkpoint()
                raise LLMError('team.blocked：' + owner + '；' + record.get('problem', ''))
            history = self.state['histories'].setdefault(owner, [])
            # Completed action/result pairs only; no other agent's reasoning is transferred.
            while len(json.dumps(history, ensure_ascii=False)) > 16000 and len(history) > 2:
                del history[:2]
            packet = {'role': owner, 'owned_target': spec, 'state': copy.deepcopy(card),
                      'instruction': task, 'version': version}
            if owner.startswith('repair:'):
                packet['repair']=copy.deepcopy(self.state['repairs'][owner.split(':',1)[1]])
            if spec:
                packet['state']['workflow']['construction']['active'] = spec
                packet['state'].pop('current_task_evidence', None)
                packet['state']['evidence'].pop('uncovered', None)
            messages = [{'role': 'system', 'content': system + '\n你是受限模块Agent。只处理owned_target；'
                          '依赖不满足就反馈，不猜测规则，不直接口算金额。每次调用1至4个允许的工具；按依赖顺序提交。'
                          '任务局部通过不等于整案正确。证据、映射或组件缺失用request_module_repair内部退回；'
                          '只有原文政策有歧义才ask_question。大分配、报表用patch_rule_component分块写入，不重抄整根。'},
                        {'role': 'user', 'content': json.dumps(packet, ensure_ascii=False)}] + copy.deepcopy(history)
            current_hint = (f'当前任务{spec["id"]}，只处理{spec["scheme"]}::{spec["target"]}，绑定证据后check_formula_task；不修改其他目标。'
                            if spec else self.ws._flow().hint())
            if owner.startswith('repair:'):
                rec=packet['repair']
                current_hint=f'内部修复{rec["id"]}：{rec["reason"]}；有实质修改后finish_module_repair(repair="{rec["id"]}")，返回原任务重新校验。'
            if owner.startswith('repair:'):
                request=packet['repair']
                current_hint=f'内部修复{request["id"]}：{request["reason"]}。检查原文并修复后finish_module_repair(repair="{request["id"]}")返回原任务；笔记不是修复完成证明。'
            if owner.startswith('repair:'):
                current_hint='修复转派：'+packet['repair']['reason']+'；完成实质修复后finish_module_repair(repair="'+packet['repair']['id']+'")返回原任务重新校验。'
            messages.append({'role':'user', 'content':'以当前程序状态为准继续，不要因上一工具成功就结束。当前下一步：'
                             + current_hint + '\n优先补程序列出的缺失材料；已有登记先查索引，不从头遍历所有已读材料。'
                             '\n只处理本角色允许范围；没有可执行动作时用note或ask_question说明具体阻塞，不以纯文本代替工具。'})
            packets.append((owner, spec, version, messages, self.tools(definitions, spec, owner)))

        if stage==2 and not self.ws.datasets and owners[0][0]=='data':
            owner,spec,version,_,_=packets[0]
            if self.state.get('data_manifest_version')!=version:
                self.serial+=1
                self.state['data_manifest_version']=version
                return {'content':'','tool_calls':[{'id':f'team_inputs_{self.serial}','name':'list_files','arguments':'{}',
                    '_owner':owner,'_spec':None,'_version':version}]}

        if program_tool:
            owner, spec, version, _, _ = packets[0]
            self.serial += 1
            return {'content':'', 'tool_calls':[{'id':f'team_program_{self.serial}', 'name':program_tool,
                    'arguments':json.dumps({'scheme':scheme}), '_owner':owner, '_spec':None, '_version':version}]}

        def call(packet):
            owner, spec, version, messages, tools = packet
            model = copy.copy(self.primary)
            model.last_call, model.last_reasoning = {}, ''
            record = self.state['tasks'][owner]
            record['status'] = 'RUNNING'
            if model.tool_mode == 'json':
                messages[0]['content'] += '\n只输出JSON对象：{"tool":"工具名","args":{}}。工具定义：' + json.dumps(tools, ensure_ascii=False)
            model.think_llm = None
            attempts = record.setdefault('protocol_attempts', {})
            allowed = {t['name'] for t in tools}
            while attempts.get(version, 0) < 2:
                reply = {}
                problem = ''
                try:
                    if model.tool_mode == 'json':
                        raw = BudgetModel(model, self.meter).chat(messages, json_out=True, max_tokens=8192,
                            think=bool(spec and spec['target'] in ('/pool','/allocation','/payout','/offsets')))
                        reply = {'content':raw}
                        try:
                            act = parse_json(raw)
                        except LLMError as exc:
                            raise ValueError(str(exc)) from exc
                        calls = [{'name': act['tool'], 'arguments': json.dumps(act.get('args', {}), ensure_ascii=False)}]
                        reply['tool_calls'] = calls
                    else:
                        reply = BudgetModel(model, self.meter).chat_tools(messages, openai_tools(tools), max_tokens=8192,
                            think=bool(spec and spec['target'] in ('/pool','/allocation','/payout','/offsets')))
                        calls = reply.get('tool_calls', [])
                    if not isinstance(calls, list) or not calls:
                        problem = 'no_calls：未返回工具调用'
                    elif len(calls) > MAX_ACTIONS:
                        problem = f'batch_too_large：返回{len(calls)}个调用，上限{MAX_ACTIONS}，整批未执行'
                    elif any(not isinstance(c,dict) or c.get('name') not in allowed for c in calls):
                        problem = 'disallowed_tools：包含当前角色或阶段不允许的工具，整批未执行'
                    else:
                        for c in calls:
                            args = json.loads(c.get('arguments') or '{}')
                            if not isinstance(args,dict):
                                problem = 'arguments：工具参数必须是JSON对象，整批未执行'
                except (ValueError, KeyError, TypeError) as exc:
                    problem = f'malformed_response：{type(exc).__name__}，整批未执行'
                if not problem:
                    record['status'] = 'VALIDATING'
                    return [{**c, '_owner':owner, '_spec':spec, '_version':version,
                             '_request':dict(getattr(model,'last_call',{}))} for c in calls]
                attempts[version] = attempts.get(version,0) + 1
                reports = record.setdefault('protocol_reports', [])
                reports.append({'version':version,'attempt':attempts[version], 'problem':problem,
                    'response_excerpt':json.dumps(reply,ensure_ascii=False,default=str)[:12000],
                    'excerpt_only':True, 'request':dict(getattr(model,'last_call',{}))})
                del reports[:-4]
                messages.append({'role':'user','content':'程序拒绝上一响应：'+problem+
                    '。尚未执行其中任何动作。按当前任务继续，提交1至4个允许的工具调用。当前下一步：'+
                    (f'{spec["id"]} {spec["target"]}' if spec else self.ws._flow().hint())})
            raise LLMError('team.protocol：同版本协议纠正已达2次，保存响应诊断并停止；'+owner+'；'+record['protocol_reports'][-1]['problem'])

        # Preserve successful proposals if another worker fails. No workspace tools run in threads.
        results, errors = [], []
        self.checkpoint()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(call, p) for p in packets]
            for packet, future in zip(packets, futures):
                try:
                    batch = future.result()
                    batch_id = f'{self.serial}:{packet[0]}'
                    self.batch_versions[batch_id] = packet[2]
                    results.extend({**c,'_batch':batch_id} for c in batch)
                except Exception as exc:
                    owner = packet[0]
                    self.state['tasks'][owner].update(status='BLOCKED', problem=str(exc))
                    errors.append(str(exc))
        self.checkpoint()
        if not results:
            raise LLMError('; '.join(errors))
        self.serial += 1
        for i, action in enumerate(results):
            action['id'] = f'team_{self.serial}_{i}'
        return {'content': '', 'tool_calls': results}

    def authorize(self, call, args):
        from bonus_agent.agent import ToolError
        from bonus_agent.facts import target_parts, split_target
        spec = call.get('_spec')
        from bonus_agent.tool_specs import TOOLS
        permitted = self.tools([t for t in TOOLS if self.ws._flow().allowed(t['name'])], spec, call['_owner'])
        if call['name'] not in {t['name'] for t in permitted}:
            raise ToolError('team.permission：当前角色或阶段不允许此工具')
        expected = self.batch_versions.get(call.get('_batch'), call['_version'])
        if expected != self.version(spec):
            raise ToolError('team.stale：输入或依赖已变化，拒绝提交旧候选')
        owner=call['_owner']
        if owner.startswith('repair:'):
            repair_id=owner.split(':',1)[1]
            if call['name']=='finish_module_repair' and args.get('repair')!=repair_id:
                raise ToolError('team.permission：只能返回自己的转派')
            if call['name']=='request_module_repair':
                ancestor=self.state['repairs'][repair_id]
                visited=set()
                while ancestor:
                    if ancestor['kind']==args.get('kind'):
                        raise ToolError('repair.cycle：同一修复链不能返回已访问角色；保留当前问题继续定位')
                    visited.add(ancestor['id'])
                    parent=ancestor.get('parent')
                    ancestor=self.state['repairs'].get(parent) if parent and parent not in visited else None
            owner=self.state['repairs'][repair_id]['kind']
            if call['name']=='request_module_repair':
                ancestor=repair_id
                while ancestor:
                    rec=self.state['repairs'][ancestor]
                    if rec['kind']==args.get('kind'):raise ToolError('repair.cycle：转派链不可回到已在等待的角色')
                    ancestor=rec.get('parent')
        if call['name']=='request_module_repair' and spec and args.get('task') not in (None,spec['id']):
            raise ToolError('team.permission：只能为当前任务请求修复')
        if not spec:
            if owner == 'data' and call['name'] == 'set_rules' and set(args) - {'scheme','allocation','patches','bindings'}:
                raise ToolError('team.permission：数据角色只能声明实体结构和数据修补，不修改金额公式')
            if owner=='data' and call['name']=='patch_rule_component' and target_parts(args.get('path',''))[:1]!=['allocation']:
                raise ToolError('team.permission：数据角色只能分块声明allocation实体结构')
            return
        name = call['name']
        parts = target_parts(spec['target'])
        if name=='patch_rule_component' and (args.get('scheme')!=spec['scheme'] or target_parts(args.get('path',''))[:len(parts)]!=parts):
            raise ToolError('team.permission：分块路径超出任务目标')
        if name == 'check_formula_task' and args.get('task') != spec['id']:
            raise ToolError('team.permission：只能检查自己的任务')
        if name in {'set_rules', 'compile_table', 'compile_coefficient_table'}:
            if args.get('scheme') != spec['scheme']:
                raise ToolError('team.permission：方案必须显式等于任务方案')
            if name in {'compile_table','compile_coefficient_table'}:
                if parts[0] != 'tables' or args.get('name') != parts[1]:
                    raise ToolError('team.permission：只能修改自己的参数表')
            else:
                rules = {k:v for k,v in args.items() if k not in ('scheme', 'bindings')}
                if set(rules) != {parts[0]} or (len(parts) == 2 and set(rules[parts[0]]) != {parts[1]}):
                    raise ToolError('team.permission：set_rules只能修改任务拥有的目标')
        bindings = args.get('bindings', [])
        if name == 'bind_facts':
            bindings = args.get('bindings', [])
        for binding in bindings:
            prior = next((f.targets for f in self.ws.facts if f.name == binding.get('name')), [])
            changed = set(prior) ^ set(binding.get('targets', []))
            for target in changed:
                scheme, path = split_target(target)
                if scheme != spec['scheme'] or target_parts(path)[:len(parts)] != parts:
                    raise ToolError('team.permission：参数绑定越过任务目标')

    def execute(self, call, args):
        """Validate on a private workspace; exceptions never publish partial changes."""
        self.meter.check()
        from bonus_agent.agent import ToolError
        if call['name']=='finish' and any(r.get('deferred') for r in self.state.get('requirements',{}).values()):
            if self.state.get('final_coverage_version')!=self.ws.validation_fingerprint():
                raise ToolError('coverage.final：人员、实发或抵扣条款须先完成当前版本review_calculation全量复核，再finish')
        try:
            self.authorize(call, args)
        except ToolError as exc:
            if not str(exc).startswith('team.stale'):raise
            rec=self.state['tasks'].setdefault(call['_owner'],{'attempts':{}})
            rec['reschedules']=rec.get('reschedules',0)+1
            if rec['reschedules']>3:raise ToolError('team.contention：连续候选过期，暂停检查依赖；未计业务修复次数')
            call['_deferred']=True
            return {'status':'STALE','next':'已丢弃候选；按当前输入和依赖重新调度，不计算金额、不累计业务纠错失败'}
        if call['name']=='request_module_repair' and call.get('_spec'):
            args={**args,'task':call['_spec']['id']}
        if call['name']=='request_module_repair' and call['_owner'].startswith('repair:'):
            args={**args,'task':self.state['repairs'][call['_owner'].split(':',1)[1]]['task']}
        if call['name']=='request_module_repair' and call['_owner'].startswith('repair:'):
            args={**args,'task':self.state['repairs'][call['_owner'].split(':',1)[1]]['task']}
        # Inventory and caches must be private as well; LLM budget remains shared.
        clone = copy.deepcopy(self.ws, {id(self.ws.llm): self.ws.llm})
        from bonus_agent.agent import ToolError
        try:
            value = getattr(clone, call['name'])(**args)
        except LLMError as exc:
            raise ToolError(str(exc)) from exc
        if (isinstance(value, dict) and value.get('status') == 'PARTIAL') or clone._fact_rejections:
            # Diagnostics are still returned, but the partial batch is not committed.
            self.ws._fact_rejections = copy.deepcopy(clone._fact_rejections)
            return {'status':'FAIL', 'transaction':'ROLLED_BACK',
                    'issues':copy.deepcopy(clone._fact_rejections), 'candidate_result':value,
                    'notice':'本批未提交任何修改；candidate_result仅为隔离草稿反馈，其登记数量不是实际已保存数量。修正拒绝项后重新提交整批。'}
        self.authorize(call, args)
        if call['name']=='request_module_repair' and call['_owner'].startswith('repair:'):
            value['repair']['parent']=call['_owner'].split(':',1)[1]
        # Helper model calls updated the live ledger while this draft was private.
        # Never replace their usage with the pre-call copy in the draft.
        live_meter=self.state['meter']
        if call['name']=='review_calculation' and isinstance(value,dict) and value.get('status')=='PASS':
            checks=value.get('validation',{}).get('checks',[])
            if any(x.get('id')=='independent.full' and x.get('status')=='PASS' for x in checks):
                clone.workflow['team']['final_coverage_version']=clone.validation_fingerprint()
        # Program-owned repair queue mutations belong to the candidate; model-call usage belongs to live state.
        clone.workflow['team']['meter'] = live_meter
        self.ws.__dict__.update(clone.__dict__)
        self.state = self.ws.workflow['team']
        self.meter.state = self.state['meter']
        self.state['tasks'].setdefault(call['_owner'],{'attempts':{}})['reschedules']=0
        if call.get('_batch') in self.batch_versions:
            self.batch_versions[call['_batch']] = self.version(spec=call.get('_spec'))
        return value

    def observe(self, call, result, ok):
        owner = call['_owner']
        record = self.state['tasks'][owner]
        history = self.state['histories'][owner]
        # These are durable program records, not reconstructed API assistant turns.
        # Each request starts a fresh native-tool conversation, so thinking/tool protocols
        # cannot inherit missing reasoning_content or dangling tool_call_ids.
        history.extend([{'role': 'user', 'content': '程序记录的上一候选动作：' + json.dumps({'tool':call['name'], 'args':call['arguments']}, ensure_ascii=False)},
                        {'role': 'user', 'content': result}])
        if call.get('_deferred'):
            record['status']='STALE'
            self.checkpoint()
            return
        record.update(status='READY' if ok else 'PENDING', last_tool=call['name'], last_result=result[:1200])
        if ok and call['name']=='request_module_repair':record['status']='WAITING'
        if ok and call['name'] == 'check_formula_task':
            record['status'] = 'PASSED'
        if not ok:
            route=('rules' if any(s in result for s in ('construction.constant','source_hash','原文版本变化','需要重新登记','bind_facts 需要已登记')) else
                   'data' if any(s in result for s in ('parent_unknown','link_unmatched','关联字段没有父实体','coverage.grain')) else
                   'coordinator' if any(s in result for s in ('逐对象奖金池或对象集合不一致','各类汇总金额不一致','逐对象明细金额或覆盖集合不一致')) else None)
            if route and not owner.startswith('repair:'):
                from bonus_agent.team_support import request_repair
                try:
                    request_repair(self.ws,route,result[:2000],call['_spec']['id'] if call.get('_spec') else None)
                    call['_routed']=True
                    record.update(status='WAITING',problem='已自动退回'+route+'角色修复')
                    self.checkpoint()
                    return
                except Exception as exc:
                    record['transfer_error']=str(exc)
            key = fingerprint([call['_version'], call['name'], result[:1000]])
            record['attempts'][key] = record['attempts'].get(key, 0) + 1
            if record['attempts'][key] >= 2:
                record.update(status='BLOCKED', problem=result[:1200], diagnosis_pending=True)
        self.checkpoint()
