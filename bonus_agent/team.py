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
READS = {'read_document', 'read_sheet', 'read_parameter_table', 'read_workspace',
         'list_files', 'query_dataset', 'diagnose_datasets', 'inspect_sheet', 'view_image'}
EVIDENCE = {'record_facts', 'record_parameter_cells', 'record_clauses', 'cover_clauses',
            'bind_facts', 'validate_parameters', 'remove_facts', 'answer_question', 'ask_question', 'note'}
DATA = {'define_dataset', 'update_dataset', 'remove_dataset', 'set_rules', 'prepare_data'}
FORMULA = {'set_rules', 'compile_table', 'bind_facts', 'check_formula_task'}


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
        self.meter = Meter(self.state['meter'], ws.llm.usage, maximum, stop)
        self.primary = ws.llm
        ws.llm = BudgetModel(ws.llm, self.meter)
        self.serial = 0
        self.checkpoint = lambda: None

    def owners(self):
        from bonus_agent.construction import TaskBoard
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
                'answers':[(q.id,q.answer,q.withdrawn) for q in self.ws.questions],
                'facts':[f.model_dump(mode='json',exclude={'targets'}) for f in self.ws.facts],
                'clauses':[c.model_dump(mode='json',exclude={'where','checked'}) for c in self.ws.clauses]})
        return self.ws._flow().version()

    def tools(self, definitions, spec, owner):
        from bonus_agent.agent import READ_ONLY
        if spec:
            return [t for t in definitions if t['name'] in READ_ONLY | FORMULA | {'ask_question', 'note'}]
        allowed = {'rules': EVIDENCE, 'data': DATA | EVIDENCE,
                   'basis': {'calculate_basis', 'ask_question', 'note'},
                   'review': {'review_basis', 'ask_question', 'note'},
                   'allocation': {'trial_run', 'ask_question', 'note'}}.get(owner)
        if allowed is not None:
            return [t for t in definitions if t['name'] in READ_ONLY | allowed]
        return definitions

    def propose(self, system, task, definitions):
        from bonus_agent.agent import openai_tools
        from bonus_agent.context import task_card
        self.meter.check()
        card = json.loads(task_card(self.ws))
        owners = self.owners()
        stage, scheme = self.ws._flow().stage()
        program_tool = {4:'calculate_basis', 5:'review_basis', 7:'trial_run'}.get(stage)
        # Capture all immutable packets before worker threads start.
        packets = []
        for owner, spec in owners:
            version = self.version(spec)
            record = self.state['tasks'].setdefault(owner, {'attempts': {}, 'status': 'READY'})
            if record.get('version') != version:
                record.update(version=version, status='READY')
            if record['status'] == 'BLOCKED':
                if record.pop('diagnosis_pending', False) and getattr(self.primary, 'think_llm', None) is not None:
                    from bonus_agent.recovery import local_advice
                    record['diagnosis'] = local_advice(BudgetModel(self.primary.think_llm, self.meter),
                        record.get('problem', ''), [], self.ws.facts, self.ws.next_hint())
                raise LLMError('team.blocked：' + owner + '；' + record.get('problem', ''))
            history = self.state['histories'].setdefault(owner, [])
            # Completed action/result pairs only; no other agent's reasoning is transferred.
            while len(json.dumps(history, ensure_ascii=False)) > 16000 and len(history) > 2:
                del history[:2]
            packet = {'role': owner, 'owned_target': spec, 'state': copy.deepcopy(card),
                      'instruction': task, 'version': version}
            if spec:
                packet['state']['workflow']['construction']['active'] = spec
                packet['state'].pop('current_task_evidence', None)
                packet['state']['evidence'].pop('uncovered', None)
            messages = [{'role': 'system', 'content': system + '\n你是受限模块Agent。只处理owned_target；'
                         '依赖不满足就反馈，不猜测规则，不直接口算金额。一次只调用一个工具。'
                         '任务局部通过不等于整案正确。需要变更公共口径时ask_question，不修改其他目标。'},
                        {'role': 'user', 'content': json.dumps(packet, ensure_ascii=False)}] + copy.deepcopy(history)
            packets.append((owner, spec, version, messages, self.tools(definitions, spec, owner)))

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
                act = BudgetModel(model, self.meter).chat_json(messages, max_tokens=8192,
                    think=bool(spec and spec['target'] in ('/pool','/allocation','/payout','/offsets')))
                calls = [{'name': act['tool'], 'arguments': json.dumps(act.get('args', {}), ensure_ascii=False)}]
            else:
                model.think_llm = None
                reply = BudgetModel(model, self.meter).chat_tools(messages, openai_tools(tools), max_tokens=8192,
                    think=bool(spec and spec['target'] in ('/pool','/allocation','/payout','/offsets')))
                calls = reply['tool_calls']
            if len(calls) != 1 or calls[0]['name'] not in {t['name'] for t in tools}:
                raise LLMError('team.protocol：模块必须只提交一个允许的工具调用；' + owner)
            action = calls[0]
            record['status'] = 'VALIDATING'
            return {**action, '_owner': owner, '_spec': spec, '_version': version,
                    '_request':dict(getattr(model, 'last_call', {}))}

        # Preserve successful proposals if another worker fails. No workspace tools run in threads.
        results, errors = [], []
        self.checkpoint()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(call, p) for p in packets]
            for packet, future in zip(packets, futures):
                try:
                    results.append(future.result())
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
        if call['_version'] != self.version(spec):
            raise ToolError('team.stale：输入或依赖已变化，拒绝提交旧候选')
        if not spec:
            if call['_owner'] == 'data' and call['name'] == 'set_rules' and set(args) - {'scheme','allocation','patches','bindings'}:
                raise ToolError('team.permission：数据角色只能声明实体结构和数据修补，不修改金额公式')
            return
        name = call['name']
        parts = target_parts(spec['target'])
        if name == 'check_formula_task' and args.get('task') != spec['id']:
            raise ToolError('team.permission：只能检查自己的任务')
        if name in {'set_rules', 'compile_table'}:
            if args.get('scheme') != spec['scheme']:
                raise ToolError('team.permission：方案必须显式等于任务方案')
            if name == 'compile_table':
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
        self.authorize(call, args)
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
            return value
        self.authorize(call, args)
        # Helper model calls updated the live ledger while this draft was private.
        # Never replace their usage with the pre-call copy in the draft.
        clone.workflow['team'] = self.state
        self.ws.__dict__.update(clone.__dict__)
        self.state = self.ws.workflow['team']
        self.meter.state = self.state['meter']
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
        record.update(status='READY' if ok else 'PENDING', last_tool=call['name'], last_result=result[:1200])
        if ok and call['name'] == 'check_formula_task':
            record['status'] = 'PASSED'
        if not ok:
            key = fingerprint([call['_version'], call['name'], result[:1000]])
            record['attempts'][key] = record['attempts'].get(key, 0) + 1
            if record['attempts'][key] >= 2:
                record.update(status='BLOCKED', problem=result[:1200], diagnosis_pending=True)
        self.checkpoint()
