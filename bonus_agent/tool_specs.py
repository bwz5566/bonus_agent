"""发给模型的工具定义（JSON schema）。只有数据，没有逻辑；执行在 agent.Workspace 的同名方法里。

从 agent.py 拆出，agent 仍按原名导出（from bonus_agent.agent import TOOLS 照常可用）。
"""


# ---------------- 工具定义 ----------------
def _obj(props: dict, required: list[str] = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required)}


_S = {"type": "string"}
_I = {"type": "integer"}
FIELD_SCHEMA = _obj({"column": {**_S, "description": "Excel 表头原文"},
                     "type": {"type": "string", "enum": ["money", "number", "int", "ratio", "text", "enum", "date", "id"]},
                     "unit": {"type": "string", "enum": ["元", "万元"], "description": "money 字段必填"},
                     "if_empty": {"type": "string", "description": "可选：这一列的空单元格按这个值处理（如 0）；写 skip 表示空格子所在的行当作没有"
                                  "（材料说空着按“查不到 / 没有”处理时用，按对象取值时和查不到一样）。"
                                  "只在材料写明或财务答复过时填，说明里写依据；拿不准先 ask_question"},
                     "desc": _S}, ["column", "type"])

TOOLS: list[dict] = [
    {"name": "list_files", "description": "列出本批次所有文件；Excel 列出每个工作表及自动识别的表头。",
     "parameters": _obj({})},
    {"name": "read_document", "description": "读取 Word / PPT / 图片文件的文字和表格（图片显示为占位符，用 view_image 查看）。",
     "parameters": _obj({"file": _S, "offset": {**_I, "description": "从第几个字符开始，默认 0"},
                         "limit": {**_I, "description": "可选：最多读多少字，只看某一段时用（如 3000）"},
                         "folded": {"type": "boolean", "description": "可选：true 时把预筛折叠起来的页（背景介绍、封面等）也一起返回"}},
                        ["file"])},
    {"name": "view_image", "description": "查看文件中的第 index 张图片（从 1 开始，默认 1），返回图片内容的逐字转写。",
     "parameters": _obj({"file": _S, "index": _I, "focus": {**_S, "description": "可选：希望重点看清的内容"}},
                        ["file"])},
    {"name": "suggest_links",
     "description": "挂接建议（只读）：比较参考表（预算、名单、各类系数和标记表）和主数据表（每行一笔业务记录的那张表）"
                    "的编号类列（月份、客户、型号、区域），列出哪几列对得上、各能对上多少行、应挂在哪一层（项目 / 客户 / 区域），"
                    "以及同名列哪一列对得上、表是否跨期间。define_dataset 定义参考表之前先用它，按结果写 combine / link / key。",
     "parameters": _obj({"file": {**_S, "description": "参考表所在文件"}, "sheet": _S,
                         "base_file": {**_S, "description": "主数据表所在文件（如销售单）"}, "base_sheet": _S},
                        ["file", "sheet", "base_file", "base_sheet"])},
    {"name": "read_sheet", "description": "查看 Excel 工作表的原始内容（带 Excel 行号），用于判断表头位置、合计行、说明文字等。",
     "parameters": _obj({"file": _S, "sheet": _S, "start_row": _I, "rows": _I}, ["file", "sheet"])},
    {"name": "note",
     "description": "把读到的关键信息记在工作区（如某条规则、某个参数、文件用途、待核对事项）。笔记每轮都会附给你，"
                    "较早的对话会被裁剪，重要内容请及时记下。同一 topic 再次记录会覆盖，text 为空则删除。",
     "parameters": _obj({"topic": _S, "text": _S}, ["topic", "text"])},
    {"name": "update_dataset", "description": "局部修改已保存的数据定义，支持补字段和修正合成键。fields按字段合并属性（值写null删除字段），combine按目标列合并；其他字段、筛选和展开方式保留，不重抄成功映射。",
      "parameters": _obj({"name":_S,"changes":{"type":"object","properties":{"key":{"type":["string","null"]},"link":{"type":["string","null"]},"period":_S,"window":_S,"filter":{"type":"object"},"population_from":_S,"unmatched":{"type":"string","enum":["block","ignore"],"description":"ignore 须同时给 unmatched_basis（写明对不上的记录不参与的原文）"},"unmatched_basis":_obj({"file":_S,"quote":_S},["file","quote"]),"unpivot":{"type":"object","description":"仅合并要修正的展开属性，如share_from；保留已保存的columns/value等"},"fields":{"type":"object","description":"只提供要添加、修改或删除的字段：值为属性对象表示添加/修改，值为null表示删除该字段；其他字段保留"},"combine":{"type":"object","description":"要更新的合成键及组成原列；字段column须指向该合成列"}}}},["name","changes"])},
    {"name":"diagnose_datasets","description":"不建树、不算金额，检查数据表身份、主外键及期间覆盖，返回缺失编号、日期列候选和可执行局部修复建议。建树失败时先用本工具，不要反复evaluate。",
     "parameters":_obj({})},
    {"name":"plan_calculation","description":"先盘点全部材料，声明核算目标、需要的输出和全部方案。files为文件名到rule/data/rule_and_data/reference/ignore的映射，不能漏文件。",
     "parameters":_obj({'summary':_S,'outputs':{'type':'array','items':_S},'files':{'type':'object','additionalProperties':_S},'schemes':{'type':'array','items':_S},'ignored':{'type':'array','items':_obj({'ref':_S,'reason':_S},['ref','reason'])}},['summary','outputs','files','schemes'])},
    {"name":"read_workspace","description":"分页检索facts/clauses/rules/variables/tables/mapping/basis/checks/tool_results/construction。rules默认给组件索引，按next_call读取节点和已有绑定；name可为/variables/变量名/expr或方案::/tables/表名等精确指针。construction按name=任务id读取计划和局部检查；checks读整体编译问题；tool_results按工具名读取其最近一次完整长结果。",
     "parameters":_obj({'section':_S,'scheme':_S,'name':_S,'query':{**_S,'description':'全文片段检索，可按参数名称片段查已有项'},'file':_S,'page':_I,'offset':{**_I,'minimum':0},'limit':{**_I,'minimum':1,'maximum':50,'description':'最多50条；同时按结果长度缩页，必须使用返回的next_call，不自行猜offset'},'detail_offset':{**_I,'minimum':0,'description':'单条过大时按next_call分片续读'},'detail':{'type':'boolean','description':'facts/clauses列表默认只给名称、取值、含义；true返回完整原文与坐标。rules的true读取完整定义，默认优先组件索引和定向节点。'}},['section'])},
    {"name":"query_dataset","description":"独立统计一张已定义原表，使用DSL聚合表达式，如sum(amount, of: \"明细表\")。无需allocation，不受其他表的建树错误影响；不用于证明正式金额正确。",
     "parameters":_obj({'dataset':_S,'expr':_S},['dataset','expr'])},
    {"name":"prepare_data","description":"检查全部已声明数据的转换、有效范围、身份、关联和期间，程序签发当前数据版本的通过回执。失败必须修正，不能直接试算。","parameters":_obj({})},
    {"name":"compile_formulas","description":"编译当前阶段公式并签发版本回执：总额确认前检查基础金额依赖，确认后检查完整分配/发放/报表。均检查循环、字段、作用域及参数/条款绑定，基础通过不能代替正式分配检查。","parameters":_obj({})},
    {"name":"plan_formula_tasks","description":"先规划构建小任务：每项一个变量、一张参数表或pool/allocation/report，声明依赖、条款ID、参数名。按id分批合并；程序检查完成后才推进，不能自行标完成。",
     "parameters":_obj({'tasks':{'type':'array','items':_obj({'id':_S,'scheme':_S,'target':{**_S,'description':'如/variables/base、/tables/rate、/pool、/allocation、/report'},'title':_S,
         'depends_on':{'type':'array','items':_S},'clauses':{'type':'array','items':_S},'facts':{'type':'array','items':_S}},['id','scheme','target'])},'replace':{'type':'boolean','description':'默认按id合并；true明确替换计划，受影响的检查失效'}},['tasks'])},
    {"name":"check_formula_task","description":"程序检查小任务的依赖、公式、参数绑定和明确算法语义。成功才推进下一项；局部通过不是金额验收，全部任务通过后仍须整体编译及独立核算。",
     "parameters":_obj({'task':_S},['task'])},
    {"name":"submit_formula_task","description":"优先用于当前构建任务：一次提交完整value、来源参数facts（已有则省略）和bindings。程序在隔离工作区登记、绑定并检查任务；全部通过才一起保存并推进，失败保留原版本。value只写目标本身，如变量写{scope,expr,sources}，不再包variables。仍须独立金额核算。",
     "parameters":_obj({'task':_S,'value':{'description':'任务目标的完整对象；allocation/payout/checks等目标传完整列表','anyOf':[{'type':'object'},{'type':'array','items':{'type':'object'}}]},
         'facts':{'type':'array','items':{'type':'object'},'description':'同record_facts；原文位置、取值和引文须有效'},
         'bindings':{'type':'array','items':{'type':'object'},'description':'同bind_facts；仅给本目标绑定，程序自动保留该参数在其他目标的已有绑定'}},['task','value'])},
    {"name":"calculate_basis","description":"仅计算当前方案基础金额、逐对象底稿及各类总额，不计算分配权重、人员分配或实发。须先通过prepare_data和compile_formulas。",
     "parameters":_obj({'scheme':_S})},
    {"name":"review_basis","description":"独立模型按原文重建当前方案的总额、逐对象及汇总公式，程序全量比较。通过后本轮停止，等待财务在页面确认总额；模型不能自行确认。",
     "parameters":_obj({'scheme':_S})},
    {"name":"read_parameter_table", "description":"省略page时列出PPT每页真实文字、标题候选、表格数、已登记参数；指定page则返回同页文字、原表与坐标。标题即使在表格下方也属于本页。结合page_context及行列标题核对适用条件后，优先record_parameter_cells按坐标登记，不转抄。",
     "parameters":_obj({"file":_S,"page":_I},["file"])},
    {"name":"record_parameter_cells","description":"按PPT已核对的页/表/单元格登记，value、quote、参数名和来源由程序生成；不传自拟的数值或原文。applies_to写这些单元格适用的区分范围（区域、事业部、产品线或页标题），程序核对它在本页原文里，防止选错相似的页。已有同坐标且验证有效的参数直接复用。多行单元格必须指定line，unit仅在原文明确时提供。",
     "parameters":_obj({'file':_S,'page':_I,'table':_I,'applies_to':_S,'cells':{'type':'array','items':_obj({'row':_I,'column':_I,'line':_I,'unit':_S},['row','column'])}},['file','page','table','applies_to','cells'])},
    {"name": "record_facts",
     "description": "登记从材料里读到的参数和口径（比例、金额、阶梯、系数、门槛、日期、取数口径等），程序据此比对冲突。"
                    "每个文件里出现的都要登记，同一参数在几个文件（或一个文件的几处）出现就登记几次，用同一个 name；"
                    "取值不一致时程序会自动向财务提问，你不要自行取舍。value 写原文的写法（如 5%、30万元、1.2），"
                    "quote 逐字复制包含该取值的原文（不超过 200 字），程序会核对 quote 在文件里、value 在 quote 里。",
     "parameters": _obj({"facts": {"type": "array", "items": _obj({
         "name": {**_S, "description": "参数名，写清适用范围，如 季度奖-部门净利润提成比例"},
         "value": {**_S, "description": "原文写法，一条只写一个值（阶梯、系数表每一档分开登记）"},
         "file": {**_S, "description": "出处文件名；财务在对话里发的内容写「财务对话」"}, "quote": _S,
         "page": {**_I, "description": "多页文档的原文页码；参数表尚未创建时也可先登记页码"},
         "context": {**_S, "description": "原文适用标题，须出现在引用所在页；用于区分重复参数表"},
          "source_cell": {"type": "object", "description": "PPT多值表行必须精确定位，坐标从1开始；多行单元格指定line。value须对应选中行，不能整行挑数。",
                          "properties": {"table": _I, "row": _I, "column": _I, "line": _I}, "required": ["table","row","column"]},
         "meaning": {**_S, "description": "可选：文字口径的规范说法，如 含税"},
         "component": {"type": "string", "enum": ["literal", "lower", "upper"], "description": "区间值选择下界或上界，保留原文如1000-3000万"},
         "unit": {**_S, "description": "裸数的明确单位，如%，quote须同时包含单位表头；不得凭参数名猜测"},
         "override": {"type": "boolean", "description": "原文明确写了修改旧规定（修改为 / 以本通知为准）时为 true，"
                                                         "quote 要包含这句话"}}, ["name", "value", "file", "quote"])}},
         ["facts"])},
    {"name": "bind_facts", "description": "把已登记的参数绑定到精确规则或数据筛选位置；多页文档须给page和原文适用标题context。expr字符串本身可绑定其中明确使用的数值，无需虚构threshold/default_value子节点。筛选参数先read_workspace(section='mapping',name=数据表名)核对实际列和条件。不能以另一处相同数字代替。",
     "parameters": _obj({"bindings": {"type": "array", "items": _obj({"name": _S,
          "targets": {"type": "array", "items": _S, "description": "如 tables.eu.steps.0.1、方案::/variables/bonus/expr；真实筛选值用 方案::/mapping/数据表/filter/销售金额/not/0（排除列表首项），包含列表为/filter/列名/0，单值为/filter/列名。路径中~和/分别转义为~0和~1。仅支持真实执行的filter，不支持任意映射备注。"},
         "page": _I, "context": _S, "component": {"type": "string", "enum": ["literal", "lower", "upper"]},
          "unit": _S, "unit_basis": _obj({'file': _S, 'quote': {**_S, 'description': '原文或财务确认，必须明确提及此参数名/适用标题和单位；不能借用其他数字的百分号'}}, ['file', 'quote'])}, ["name", "targets"])}}, ["bindings"])},
    {"name": "validate_parameters", "description": "局部参数预检：自动修正已声明阶梯下界绑定的共享单位区间投影，返回剩余缺口与具体动作；不重跑金额、不改规则数值。",
     "parameters": _obj({"repair": {"type": "boolean"}})},
    {"name": "validation_report", "description": "查看可复核的验证状态、覆盖缺口和失败反例；不调用模型、不更改规则。", "parameters": _obj({})},
    {"name": "review_calculation", "description": "关键复核：独立模型只看材料和数据结构重新解释规则并全量复算，返回差异；不修改规则。首次金额试算后尽早执行，不必等证据全部齐全。",
     "parameters": _obj({})},
    {"name":"compile_coefficient_table","description":"原文是指标区间→系数时使用。每档range/value直接引用PPT区间格/系数格，程序一次登记全部端点与取值、核对边界并编译，失败整批不保存。用tier_rate(表,指标)取系数；不用于金额超额累进。不写-1等人造下界。",
     "parameters":_obj({'name':_S,'scheme':_S,'applies_to':_S,
         'cells_from':_obj({'file':_S,'page':_I,'table':_I},['file','page','table']),
         'rows':{'type':'array','items':_obj({'range':_S,'value':_S},['range','value'])}},['name','rows','cells_from','applies_to'])},
    {"name": "compile_table", "description": "从登记参数直接编译金额阶梯/类别查找表。指标区间取系数优先compile_coefficient_table，不能当金额累进；不把普通页标题填为algorithm_basis。value、lower引用参数名；PPT表格单元格可写cells_from={file,page,table}后用R行C列引用（多行单元格加L行号，如R3C2L2）；首档lower可省略表示0。程序换算单位并绑定来源，不要重新抄比例或编造负下界。",
     "parameters": _obj({"name": _S, "scheme": _S, "kind": {"type": "string", "enum": ["tier", "lookup"]},
        "cells_from": _obj({"file": _S, "page": _I, "table": _I}, ["file", "page", "table"]),
        "mode": {"type": "string", "enum": ["whole", "excess"]}, "applies_when": _S,
         "algorithm_basis": _obj({'file':_S,'quote':{**_S,'description':'逐字写出超额累进或整额分档依据；与mode冲突拒绝保存，省略mode则按明确原文确定'},'page':_I},['file','quote']),
        "lower_inclusive": {"type": "boolean"}, "below_first": {"type": "string", "enum": ["zero", "first"]},
        "rows": {"type": "array", "items": _obj({"value": _S, "lower": _S, "key": _S}, ["value"])}}, ["name", "rows"])},
    {"name": "preview_repair", "description": "隔离试算最小规则修复，返回反例和金额影响。原工作区不变；不允许改答案、输入、校验器或既有对账。每版本最多6次。",
     "parameters": _obj({"scheme": _S, "parts": {"type": "object", "additionalProperties": True}, "reason": _S}, ["parts", "reason"])},
    {"name": "verify_repair", "description": "在隔离修复候选上按原文验算缺失样本，原工作区不变。",
     "parameters": _obj({"candidate": _S, "node": _S, "expr": _S, "basis": _S, "scheme": _S, "at": _S,
         "amount": _S, "helpers": {"type": "object", "additionalProperties": True}}, ["candidate", "node", "expr", "basis"])},
    {"name": "apply_repair", "description": "重新核验候选的证据、独立复算、样本、金额闭合及程序检查，全通过且原版本未变才应用。不能自行豁免。",
     "parameters": _obj({"candidate": _S}, ["candidate"])},
    {"name": "record_clauses",
     "description": "读完一份规则文档后，把其中与计算有关的条款逐条摘进条款清单（取数口径、筛选和排除、公式、阶梯、系数、舍入、"
                    "分配方法、发放和暂缓、抵扣、校验和报错要求、版本和调整要求等），一条一个要求，不要合并。"
                    "清单每一步都附给你，不会被裁剪；写完规则后用 cover_clauses 标注每条落在哪里，finish 前程序会检查。",
     "parameters": _obj({"clauses": {"type": "array", "items": _obj({
         "point": {**_S, "description": "要点：用自己的话写这条要求什么（不超过 100 字）"},
         "file": {**_S, "description": "出处文件名；财务在对话里发的内容写「财务对话」"},
         "quote": {**_S, "description": "逐字复制原文关键句（不超过 200 字）"}}, ["point", "file", "quote"])}},
         ["clauses"])},
    {"name": "cover_clauses",
     "description": "标注条款清单里每条落在规则的哪里：{\"C1\": \"variables.net_receipt\", \"C2\": \"checks.grade_known、pool\"}。"
                    "可写 tables.名字 / variables.名字 / pool / allocation.层级 / payout.编号 / offsets.编号 / checks.编号 / "
                    "revisions.R1 / datasets.表名（单位换算、筛选等在数据表定义里完成的）；"
                    "引擎已自动保证的写「引擎自带：说明」（如 主键重复报错、最大余数法、各级合计等于上级）；"
                    "本批不涉及的写「不适用：原因」；等财务答复的写「已提问 q1」。",
     "parameters": _obj({"items": {"type": "object", "additionalProperties": _S}}, ["items"])},
    {"name": "remove_facts",
     "description": "删掉台账里某个参数名的全部登记（登记错了时用，如把阶梯几档登记成了同一个名字）；依据它的、还没答复的冲突问题会被撤回。",
     "parameters": _obj({"name": _S}, ["name"])},
    {"name": "define_dataset",
     "description": "定义（或重新定义）一张数据表。实体表写 key（每行一个对象，如人员、团队，以及被别的表按编号引用的合同、订单）；"
                    "明细表写 link（每行挂到某层级对象或某个实体上，如收款记录 link=合同编号字段）；公司级表 key、link 都不写。"
                    "同名字段表示关联。一张工作表混放多类数据时（某一列区分这行属于哪类对象），"
                    "用 filter 只取其中一类，并把通用的编号列映射成这类对象自己的编号字段名。返回校验结果和换算后的样例行。",
     "parameters": _obj({"name": {**_S, "description": "中文表名，如 人员、合同明细"}, "file": _S, "sheet": _S,
                         "header_rows": {"type": "array", "items": _I, "description": "可选：表头所在的 Excel 行号，多行表头写多个"},
                         "key": _S, "link": _S, "desc": _S,
                         "unmatched": {"type": "string", "enum": ["block", "ignore"], "description":
                                       "可选：明细表的 link 编号在上级里找不到时怎么办。默认 block（试算阻断，要问财务）；"
                                       "材料原文写明、或财务答复过“找不到的不参与 / 按默认值”后才可写 ignore，这些行不参与计算。"
                                       "实体表作下一层分配对象（分配层级用 parent_key 挂到上一层）时也可以写"},
                         "unmatched_basis": {**_obj({"file": _S, "quote": _S}, ["file", "quote"]), "description":
                                             "写 unmatched: ignore 的依据在材料原文里时填：file 材料文件名，quote 逐字复制写明"
                                             "“对不上的不参与”的那句原文；依据是财务答复时不用填"},
                          "population_from": {**_S, "description": "实体范围来自有效明细时，写按本实体key关联的明细表名，共用过滤后的有效对象集合"},
                          "distinct": {"type": "boolean", "description": "可选：按 key 的不同取值各建一个对象（同一编号的多行合成一个，"
                                      "其余字段在同一编号下要一样）。没有单独的主表、又要按这个编号汇总时用，如从明细里得到每个客户一个“客户”"},
                         "filter": {"type": "object", "description": '可选：只取这些行，{"列名": "取值"} 或 {"列名": ["取值1", "取值2"]}；'
                                    '去掉某些行写 {"列名": {"not": ["取值"]}}（如材料说“剔除的不计算”“金额为 0 的不计算”：'
                                    '{"状态": {"not": ["剔除"]}}、{"销售金额": {"not": ["0"]}}，数字按数值比）。'
                                    '不要用它筛月份或日期，按期间取数用 period'},
                         "period": {**_S, "description": "可选：记业务日期或月份的字段名。写了之后计算时只取落在本次核算期间里的行，"
                                    "同一版规则换任何月份、季度都能算。业务明细（销售、回款、出货等）有日期或月份列时都要写；"
                                    "名单、预算这类跨期的表不写，要取当期的值时在表达式里用 period_start() / period_end()"},
                         "window": {"type": "string", "enum": ["period", "year_to_date", "to_date"], "description":
                                    "可选，写了 period 才有用：取哪段时间的行。period（默认）只取本期；year_to_date 取本期末所在年份年初到本期末；"
                                    "to_date 取本期末及以前全部行。规则要用累计或历史数据（如按年内累计算月均）时用；同一份明细既要当期又要累计，"
                                    "就定义成两张表"},
                         "combine": {"type": "object", "description":
                                     '可选：几列合起来才能确定一个对象时（如 月份+客户编码+型号 才是一个项目），合成一个编号列：'
                                     '{"新列名": ["列1", "列2", ...]}，各段用 | 连接；fields 里用新列名（类型 id），可作 key 或 link。'
                                     '两张表要按同一组列关联时，两边都按同样的列顺序合成。不要只拿其中一列（如只按型号）去关联'},
                         "unpivot": {"type": "object", "description":
                                     '可选：几列放的是同一类值（如几个角色列各放一个人名）时展开成每个值一行：'
                                     '{"columns": ["列1", "列2"], "value": "展开后的列名", "label": "记原列名的列名，如 角色"}。'
                                     '空格子不展开；fields 和 filter 用展开后的列名。从这几列取全部对象时配合 key + distinct。'
                                     '格子里写成“名字+百分比”（如 张三50%）、财务答复了怎么拆之后：加 "share": "记比例的列名"，名字和比例就拆开'
                                     '（比例是小数，没写百分比的是 1，字段类型用 ratio，权重里乘上它）；财务说余下的部分记为“未分配”时再加 '
                                     '"remainder": "{label}未分配"（{label} 换成原列名；余下的部分另记一行，比例是 1 − 写明的比例）。'
                                     '空格子默认不展开；材料或财务说空着的那一份也要单列（如某个角色没填人，记为“××未分配”）时加 '
                                     '"blank": "{label}未分配"，空格子也记一行。'
                                     '几个人的比例集中写在表里另一列的文字里（如“甲60%，乙40%”）、人名格子里只有名字时：加 "share_from": "那一列的列名"，'
                                     '按名字到那一列取比例（该列为空表示只有一个人、比例 1；格子里自己写了百分比的用格子里的）；要同时写 share。'
                                     '这一列的比例只管其中几列（如两个技术支持列）时，用 "share_from_columns": ["列名"] 指明，其余列只认格子里自己写的比例。'
                                     '写了 blank 时，某几列空着就是没有这个人（如第二个人那列）、不该记未分配的，把它们列进 "blank_except": ["列名"]。'
                                     '展开后没有每行唯一的编号、又要当最末层时，加字段 {"column": "（行号）", "type": "id"} 作 key'},
                         "fields": {"type": "object", "additionalProperties": FIELD_SCHEMA,
                                    "description": "字段名（英文小写加下划线） -> 定义。重新定义同一张表、字段不变时可以不写（沿用上一次的）"}},
                        ["name", "file", "sheet"])},
    {"name": "remove_dataset", "description": "删除一张已定义的数据表。", "parameters": _obj({"name": _S}, ["name"])},
    {"name": "set_rules",
     "description": "写入或修改计算规则。只给出要改的部分：tables / variables 按名字合并（值为 null 表示删除），"
                    "pool、allocation、payout、offsets、checks、revisions 整体替换。返回静态校验结果。"
                    "一批文件里有多个方案（如季度奖、过程奖、年度奖）时，每个方案用 scheme 分别写 pool 等；"
                    "各方案相同的部分（分配层级与权重、数据校验、共用的参数表和变量）写到 scheme=\"共用\"，会合并进每个方案。"
                    "只有一个方案时不写 scheme。bindings可同时提交参数绑定，绑定不合法则规则和绑定都不保存；工具保存草稿不等于公式通过。",
     "parameters": _obj({"scheme": {**_S, "description": "方案名（按材料起，如 月度、年度）；共用部分写 共用"},
                         "tables": {"type": "object"}, "variables": {"type": "object"}, "pool": {"type": "object"},
                          "bindings": {'type': 'array', 'items': _obj({'name': _S, 'targets': {'type': 'array', 'items': _S},
                              'page': _I, 'context': _S, 'unit': _S, 'component': {'type': 'string', 'enum': ['literal', 'lower', 'upper']},
                              'unit_basis': _obj({'file': _S, 'quote': _S}, ['file', 'quote'])}, ['name', 'targets'])},
                         "allocation": {"type": "array", "items": {"type": "object"}},
                         "payout": {"type": "array", "items": {"type": "object"},
                                    "description": '发放条件：[{"id": "cash", "scope": "个人", "pay_ratio": "可发比例表达式"}] '
                                                   '或 hold_ratio（挂起比例）'},
                         "offsets": {"type": "array", "items": {"type": "object"},
                                     "description": '已发抵扣：[{"id": "prior", "scope": "个人", "expr": "已发金额表达式"}]，'
                                                    '可用 scheme_paid("方案名", "修订名") 引用另一个方案某修订的本次实发'},
                         "checks": {"type": "array", "items": {"type": "object"}},
                         "report": {"type": "object", "description":
                                    '结果文件的固定输出（汇总核算 + 分配人员金额），按材料或场景的输出要求写：'
                                    '{"totals": [{"name": "核算类型名称", "level": "层级名或 global", "expr": "在该层每个对象上算、再合计的表达式"}], '
                                    '"people": "最末层上表示人的字段或变量"}。totals 按要求的顺序列出各类金额（如各类暂扣、正常发放、合计）；'
                                    'people 写了之后每人参与的各条合计成一行（未分配的行按它们的名称各成一行）。'
                                    '各层的计算过程会自动写成「核算底稿」：每个对象一行，原始字段在前，能翻译的变量写成 Excel 公式'},
                         "patches": {"type": "array", "items": {"type": "object"},
                                     "description": '数据修订（只影响本方案）：并入补充表 {"kind": "append", "source": "补充表", "into": "原表", '
                                                    '"unique": "单号字段", "values": {"flag_field": {"补充表写法": "原表写法"}}}；'
                                                    '更正 {"kind": "correct", "source": "更正表", "target": "原表", "key": "编号字段", '
                                                    '"field": "field_name", "value": "new_value", "labels": {"更正表里的字段写法": "原表字段名"}} 或 '
                                                    '{"kind": "correct", "source": "修订表", "target": "原表", "key": "编号字段", '
                                                    '"set": {"原表字段": "new_field"}, "expect": {"原表字段": "old_field"}}'},
                         "revisions": {"type": "array", "items": {"type": "object"},
                                       "description": '人工调整的修订版本，后一个包含前面的调整：[{"name": "R1", "adjust": '
                                                      '[{"node": "团队:T01", "amount": 900000, "reason": "..."}]}]；R0 为不调整的基准'}})},
    {"name": "trial_run", "description": "用当前数据表和规则试算，返回奖金池、各层级金额分布、各修订的变化和提示。不写 scheme 时试算全部方案。",
     "parameters": _obj({"scheme": _S})},
    {"name": "explain", "description": "查看计算链：节点 id（如 人员:S01、团队:T01、全部）或变量追踪 id 的公式、代入数值后的算式和结果。",
     "parameters": _obj({"id": _S, "scheme": _S, "revision": {**_S, "description": "默认 R0"}}, ["id"])},
    {"name": "evaluate", "description": "在某个节点上用引擎计算任意表达式（如 sum(amount)），用于核对数据和规则。"
                                        "节点可以是层级对象或关联实体（如 合同:C01）。需要先在 set_rules 里设定 allocation。",
     "parameters": _obj({"expr": _S, "node": {**_S, "description": "节点 id，默认 全部"}, "scheme": _S}, ["expr"])},
    {"name": "ask_question",
     "description": "向财务提问：材料之间有冲突、缺少必要的信息（参数、口径、数据）、或能有两种理解时使用。"
                    "不要自行假设或补出算法；提问后继续做不受影响的部分，问完后调用 finish 结束本轮，"
                    "财务答复后下一轮接着做。options 列出可选的理解，suggested 是你的建议（只供财务参考，不会被采用）。"
                    "related 写受影响的方案或规则（如 方案名.offsets）；回应程序检查或复读的某一条时写它给的编号。blocking=false 只用于不影响任何金额的提示。",
     "parameters": _obj({"text": _S, "options": {"type": "array", "items": _S}, "suggested": _I, "related": _S,
                         "blocking": {"type": "boolean"},
                         "evidence": {"type": "array", "description": "问题和建议的依据，显示在答复页上给财务对照：每条 file 写材料文件名"
                                      "（财务在对话里说的写「财务对话」），quote 逐字复制原文或数据行里的取值（一句、一行）。"
                                       "写了 suggested 时所有引用必须核对通过。Excel可改传sheet、row（原Excel行号）、columns（精确列名），程序生成真实引用；禁止省略号拼接。",
                                       "items": _obj({"file": _S, "quote": _S, "sheet": _S, "row": _I,
                                                      "columns": {"type": "array", "items": _S}}, ["file"])}}, ["text", "options"])},
    {"name": "reply",
     "description": "对话时用：回复财务这条消息并结束本次对话。改过规则、数据表或台账时，程序会先自动试算全部方案并做完成前检查，"
                    "不通过会把原因返回给你修正。回复里的金额要来自 evaluate / explain / 试算结果，不要自己算。",
     "parameters": _obj({"text": _S, "ignored": {"type": "array", "items": _obj({"ref": _S, "reason": _S})}}, ["text"])},
    {"name": "answer_question",
     "description": "记录待确认问题的答复，两种情况：① 对话时，财务在消息里明确答复了，把他的原话记为答复（没说清楚的不要替他选，用 reply 反问）；"
                    "② 材料里（如后来补充的答复文件、确认单）已经明确答复了这个问题，file 写那个文件、quote 逐字复制答复原文，"
                    "程序核对原文后关闭问题，口径单上会请财务确认。材料没有明确答复的，不能自己关闭。",
     "parameters": _obj({"id": _S, "answer": {**_S, "description": "财务的原话、他选的选项，或材料答复的要点"},
                         "file": {**_S, "description": "不在对话时必填：答复所在的材料文件"},
                         "quote": {**_S, "description": "不在对话时必填：逐字复制的答复原文"}}, ["id", "answer"])},
    {"name": "verify",
     "description": "独立验算一个对象的金额：不看规则，按材料原文从原始字段另写一遍算式，由引擎在节点 at 上算出，和试算结果对到分。"
                    "要验算哪些对象由程序指定（见工作区状态的“待独立验算”），全部通过才能 finish。算式里不能引用规则变量（只能用字段、"
                    "参数表和函数）；at 默认是该对象自己（对自己的明细求和），按份额分配的对象在 全部 上用 where 按编号筛选计算。"
                    "对不上时先想清楚是规则错了还是验算式写错了，改对的那一边；拿不准就 ask_question。",
     "parameters": _obj({"node": {**_S, "description": "要验算的对象，如 人员:S01"}, "expr": _S,
                         "basis": {**_S, "description": "依据的材料原文（条款号或原句）"},
                         "at": {**_S, "description": "在哪个节点上计算，默认是 node 自己。一般不要写：上层的数用 scope 为上层的辅助变量在 node 上直接引用"}, "scheme": _S,
                         "amount": {**_S, "enum": ["应得", "实发"], "description": "验算应得（默认）还是本次实发（扣暂缓、已发之后）"},
                         "helpers": {"type": "object", "description": "可选：验算自己定义的辅助变量 {名字: {scope, expr}}，"
                                     "写法同规则的 variables，不能和规则变量同名、不能引用规则变量。跨表的中间数先在对应层算出来，"
                                     "如 {\"v_obj_amt\": {\"scope\": \"关联实体表名\", \"expr\": \"sum(amount, of: \\\"明细表名\\\")\"}}，"
                                     "再在参与记录上引用 v_obj_amt * ratio_field。定义过的辅助变量会留着（见工作区状态），"
                                     "后面的 verify 直接引用，只写新的或要改的；scope 写层级名、关联实体表名或 global。"
                                     "验算里对本层或上层的实体表求和（如在最末层 sum(v_up_w, of: \"上层实体表\", where: 上上层编号字段 == \"某编号\")）"
                                     "按上一层范围取同级再用 where 筛选；share(上层变量) 在下层求值时取所属上层对象在同级里的份额"}},
                        ["node", "expr", "basis"])},
    {"name": "finish", "description": "完成。要求每个方案在最近一次修改之后都已成功试算，程序指定的对象都已独立验算通过。",
     "parameters": _obj({"summary": {**_S, "description": "两三句话说明：什么奖金、奖金池怎么来、怎么分、发放条件"},
                         "ignored": {"type": "array", "items": _obj({"ref": _S, "reason": _S})}}, ["summary"])},
]
