"""大模型主导的奖金计算智能体：不写死流程，模型通过工具自己决定看什么、怎么建模、怎么核对。

模型能做的事都封装成工具（看文件、看表、看图、定义数据表、写规则、试算、查算式、算表达式、提问、完成），
程序只负责：执行工具、校验输入、保存工作区状态。金额始终由计算引擎按模型写的规则算出。

长任务的上下文控制：工作区状态（笔记、已定义的数据表、规则、问题、试算结论）每轮重新附在请求末尾，
所以模型不会因为对话变长而“忘记”已经做过的事。对话历史超过上限时才整理一次：较早的工具结果只留摘要，
仍然太长就丢弃最早的几轮。两次整理之间历史只追加、不改动，请求前缀保持不变，便于前缀缓存命中
（私有化部署的 vLLM 自动缓存；百炼部分模型可在配置里开启显式缓存）。
"""
from __future__ import annotations

import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml
from pydantic import ValidationError

from bonus_agent.auto import (DATA_SUFFIXES, MAX_FILES, Inventory, PlanDataset, TooManyFiles, build_inventory,
                              check_dataset, collect_files, filter_sheet)
from bonus_agent import case as cs, clauses as cl, failure, fixes, lint
from bonus_agent import facts as fx
from bonus_agent.extract_rules import Extraction, Question, check_quotes, prompt
from bonus_agent.llm import LLM, LLMError, image_part, parse_json
from bonus_core.data.mapping import DatasetMapping, apply_mapping
from bonus_core.data.patches import apply_patches
from bonus_core.data.table_detect import table_at
from bonus_core.dsl.functions import describe as describe_functions
from bonus_core.dsl.models import DatasetDef, RuleSet, Variable
from bonus_core.dsl.parser import SELF_COMPARE_HINT, Call, ExprError, Num, parse, self_compares, walk
from bonus_core.dsl.parser import names as parse_names
from bonus_core.dsl.scope import Layout
from bonus_core.dsl.validator import validate
from bonus_core.engine.allocation import AdjustConflict
from bonus_core.engine.evaluator import EvalError, Evaluator, fmt
from bonus_core.engine.issues import EngineError, Issue
from bonus_core.engine.run import Adjustment, Engine, Result, compute
from bonus_core.engine.tree import ROOT_LEVEL, build_tree
from bonus_core.report.excel import write_excel

MAX_RESULT_CHARS = 6000
DEFAULT_SCHEME = "默认"        # 只有一个方案时的方案名
SCHEME_REF = re.compile(r"""scheme_paid\(\s*["']([^"']+)["']\s*(?:,\s*["']([^"']+)["']\s*)?\)""")   # 规则里引用别的方案
SHARED = "共用"                # 多个方案共用的部分（权重、校验、共用变量），合并进每个方案
AGENT_TEMPERATURE = 0          # 工作循环用 0：同样的材料和状态尽量走同样的路，过程稳定、可复现
MAX_STUCK = 3                  # 同一调用原样重复、或同一工具连续失败达到这个次数就算卡住
MIN_CONTEXT = 65536            # 模型服务上下文长度的下限：智能体每步输入约 4～5 万 tokens（材料包、工作区状态、对话历史），再加输出
PHASES = (     # 工作分阶段：每个阶段的产出都存在工作区里（结构化），进入下一阶段时清空对话历史，只带工作区和材料包往下走
    ("读懂材料", "逐份读规则文档：record_facts 登记参数，record_clauses 摘出计算条款（这两样是后面写规则和验算的依据）",
     "每份规则文档都登记了参数、摘出了条款"),
    ("写规则并试算", "define_dataset 定义数据表，set_rules 写规则，cover_clauses 标注每条条款落在哪，trial_run 试算并抽查",
     "每个方案都已试算，条款全部落实"),
    ("独立验算", "对“待独立验算”里的每个对象用 verify 按材料原文另写算式，和试算结果对到分；对不上就用 explain 找出哪一步不同，改错的那一边",
     "待独立验算全部通过"),
    ("核对并结束", "调用 finish；被拦下的逐条处理：规则错了就改，需要财务定的用 ask_question，数字对不上的不能自己放过",
     "finish 成功"),
)
SOURCE_CHECKS = 5               # 每轮提问前查原文最多几次（每次一次交叉验证模型调用）
IDLE_WARN = 6                  # 连续这么多次只查询（evaluate / explain）、没有任何改动：提醒根据已查到的做决定；两倍就算卡住
QUERY_TOOLS = {"evaluate", "explain"}
DEEP_LIMIT = 24                # 每一轮最多这么多步用深度思考（出错、卡住的那几步），防止一直思考把时间和 tokens 用完
MAX_REFLECT = 2                # 卡住时先让智能体停下来反思、换做法接着做，最多这么多次；再卡住才停下，把原因报给财务
KEEP_FULL_RESULTS = 6          # 整理历史时，最近几条工具结果保留全文
BRIEF_CHARS = 240              # 较早的工具结果保留的开头字数
READ_ONLY = {"list_files", "read_document", "read_sheet", "view_image", "explain", "evaluate"}
NO_IGNORE = ("查找表 ", "抵扣 ")    # 程序检查里不能写进 ignored 放过的项
MUST_FIX = ("抵扣 ",)              # 连提问也不能放过、只能改规则的项
VERIFY_TOL = Decimal("0.005")   # 按份额分配时，独立算式和引擎未舍入的份额之差不超过半分，才算尾差
WRITE_TOOLS = {"define_dataset", "remove_dataset", "set_rules", "record_facts", "remove_facts", "ask_question",
               "record_clauses", "cover_clauses",
               "answer_question"}
RULE_SUFFIXES = {".docx", ".pptx", ".pdf", ".png", ".jpg", ".jpeg", ".txt", ".md"}   # 规则文档：要登记参数
CHAT_SOURCE = "财务对话"       # 财务在对话里贴的规则原文、口径说明：和材料一样可以引用核对
ANSWER_NOTE = "回答财务"       # 财务的说明里带了提问：智能体把回答记在这条笔记里，网页放在本轮回复的开头（没算完也显示）
TRIAGE_MIN_PAGES = 4            # 至少这么多页的文档才做预筛（短文档整份都有用的多，不值得多调一次模型）
TRIAGE_CHUNK_CHARS = 24000      # 预筛一次送给模型的字数上限，超过就分几次
TRIAGE_PAGE_CHARS = 900         # 每页送多少字给模型判断
FOLD_LABELS = ("背景", "装饰", "空白")    # 预筛判为这几类、又没有表格和带单位数字的页，折叠成一行，不进上下文
PIN_DOC_CHARS = 12000          # 不超过这么长的文档全文附在任务里（材料包），不必用工具读
PIN_TOTAL_CHARS = 40000        # 材料包里文档全文的总长上限
PIN_SHEET_ROWS = 30            # 不超过这么多行的工作表整表附上，更长的只附表头和前几行


class ToolError(Exception):
    """工具参数或执行出错，错误信息原样返回给模型。"""


# ---------------- 工具定义 ----------------
def _obj(props: dict, required: list[str] = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required)}


_S = {"type": "string"}
_I = {"type": "integer"}
FIELD_SCHEMA = _obj({"column": {**_S, "description": "Excel 表头原文"},
                     "type": {"type": "string", "enum": ["money", "number", "int", "ratio", "text", "enum", "date", "id"]},
                     "unit": {"type": "string", "enum": ["元", "万元"], "description": "money 字段必填"},
                     "if_empty": {"type": "string", "description": "可选：这一列的空单元格按这个值处理（如 0）。"
                                  "只在材料写明或财务答复过时填，说明里写依据；拿不准先 ask_question"},
                     "desc": _S}, ["column", "type"])

TOOLS: list[dict] = [
    {"name": "list_files", "description": "列出本批次所有文件；Excel 列出每个工作表及自动识别的表头。",
     "parameters": _obj({})},
    {"name": "read_document", "description": "读取 Word / PPT / 图片文件的文字和表格（图片显示为占位符，用 view_image 查看）。",
     "parameters": _obj({"file": _S, "offset": {**_I, "description": "从第几个字符开始，默认 0"},
                         "folded": {"type": "boolean", "description": "可选：true 时把预筛折叠起来的页（背景介绍、封面等）也一起返回"}},
                        ["file"])},
    {"name": "view_image", "description": "查看文件中的第 index 张图片（从 1 开始，默认 1），返回图片内容的逐字转写。",
     "parameters": _obj({"file": _S, "index": _I, "focus": {**_S, "description": "可选：希望重点看清的内容"}},
                        ["file"])},
    {"name": "read_sheet", "description": "查看 Excel 工作表的原始内容（带 Excel 行号），用于判断表头位置、合计行、说明文字等。",
     "parameters": _obj({"file": _S, "sheet": _S, "start_row": _I, "rows": _I}, ["file", "sheet"])},
    {"name": "note",
     "description": "把读到的关键信息记在工作区（如某条规则、某个参数、文件用途、待核对事项）。笔记每轮都会附给你，"
                    "较早的对话会被裁剪，重要内容请及时记下。同一 topic 再次记录会覆盖，text 为空则删除。",
     "parameters": _obj({"topic": _S, "text": _S}, ["topic", "text"])},
    {"name": "record_facts",
     "description": "登记从材料里读到的参数和口径（比例、金额、阶梯、系数、门槛、日期、取数口径等），程序据此比对冲突。"
                    "每个文件里出现的都要登记，同一参数在几个文件（或一个文件的几处）出现就登记几次，用同一个 name；"
                    "取值不一致时程序会自动向财务提问，你不要自行取舍。value 写原文的写法（如 5%、30万元、1.2），"
                    "quote 逐字复制包含该取值的原文（不超过 200 字），程序会核对 quote 在文件里、value 在 quote 里。",
     "parameters": _obj({"facts": {"type": "array", "items": _obj({
         "name": {**_S, "description": "参数名，写清适用范围，如 季度奖-部门净利润提成比例"},
         "value": {**_S, "description": "原文写法，一条只写一个值（阶梯、系数表每一档分开登记）"},
         "file": {**_S, "description": "出处文件名；财务在对话里发的内容写「财务对话」"}, "quote": _S,
         "meaning": {**_S, "description": "可选：文字口径的规范说法，如 含税"},
         "override": {"type": "boolean", "description": "原文明确写了修改旧规定（修改为 / 以本通知为准）时为 true，"
                                                         "quote 要包含这句话"}}, ["name", "value", "file", "quote"])}},
         ["facts"])},
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
                         "distinct": {"type": "boolean", "description": "可选：按 key 的不同取值各建一个对象（同一编号的多行合成一个，"
                                      "其余字段在同一编号下要一样）。没有单独的主表、又要按这个编号汇总时用，如从明细里得到每个客户一个“客户”"},
                         "filter": {"type": "object", "description": '可选：只取这些行，{"列名": "取值"} 或 {"列名": ["取值1", "取值2"]}'},
                         "fields": {"type": "object", "additionalProperties": FIELD_SCHEMA,
                                    "description": "字段名（英文小写加下划线） -> 定义。重新定义同一张表、字段不变时可以不写（沿用上一次的）"}},
                        ["name", "file", "sheet"])},
    {"name": "remove_dataset", "description": "删除一张已定义的数据表。", "parameters": _obj({"name": _S}, ["name"])},
    {"name": "set_rules",
     "description": "写入或修改计算规则。只给出要改的部分：tables / variables 按名字合并（值为 null 表示删除），"
                    "pool、allocation、payout、offsets、checks、revisions 整体替换。返回静态校验结果。"
                    "一批文件里有多个方案（如季度奖、过程奖、年度奖）时，每个方案用 scheme 分别写 pool 等；"
                    "各方案相同的部分（分配层级与权重、数据校验、共用的参数表和变量）写到 scheme=\"共用\"，会合并进每个方案。"
                    "只有一个方案时不写 scheme。",
     "parameters": _obj({"scheme": {**_S, "description": "方案名（按材料起，如 月度、年度）；共用部分写 共用"},
                         "tables": {"type": "object"}, "variables": {"type": "object"}, "pool": {"type": "object"},
                         "allocation": {"type": "array", "items": {"type": "object"}},
                         "payout": {"type": "array", "items": {"type": "object"},
                                    "description": '发放条件：[{"id": "cash", "scope": "个人", "pay_ratio": "可发比例表达式"}] '
                                                   '或 hold_ratio（挂起比例）'},
                         "offsets": {"type": "array", "items": {"type": "object"},
                                     "description": '已发抵扣：[{"id": "prior", "scope": "个人", "expr": "已发金额表达式"}]，'
                                                    '可用 scheme_paid("方案名", "修订名") 引用另一个方案某修订的本次实发'},
                         "checks": {"type": "array", "items": {"type": "object"}},
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
                         "blocking": {"type": "boolean"}}, ["text", "options"])},
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


def _in_text(quote: str, text: str, least: int = 8) -> bool:
    """quote 能在 text 里逐字找到（忽略空白、全半角和首尾标点），且至少 least 个字。"""
    norm = lambda x: re.sub(r"\s+", "", unicodedata.normalize("NFKC", x or ""))
    q = norm(quote).strip("。，,.；;：:「」“”\"'")
    return len(q) >= least and q in norm(text)


def _used_helpers(expr: str, pool: dict) -> dict:
    """验算式（经辅助变量传递）用到的辅助变量。写错的式子原样带上，交给后面的检查报错。"""
    out, todo = {}, [expr]
    while todo:
        try:
            got = set(parse_names(parse(todo.pop())))
        except ExprError:
            continue
        for n in sorted(got & set(pool) - set(out)):
            out[n] = pool[n]
            todo.append(str(pool[n].get("expr", "")))
    return out


def _helper_scope(rs: RuleSet, name: str, scope: str) -> str:
    """辅助变量的 scope：层级名、关联实体表名或 global。写成层级的实体表名（如「个人」层的「员工」表）时换成层级名，两者是同一个对象。"""
    levels = {lv.level for lv in rs.allocation}
    lay = Layout(rs)
    if scope in levels or scope in lay.aux or scope in ("global", ROOT_LEVEL):
        return "global" if scope == ROOT_LEVEL else scope
    d = rs.datasets.get(scope)
    if d is not None and d.key in lay.key_level:
        return rs.allocation[lay.key_level[d.key]].level
    raise ToolError(f"辅助变量 {name} 的 scope「{scope}」不对：只能写层级（{'、'.join(levels)}）、"
                    f"关联实体表（{'、'.join(lay.aux) or '无'}）或 global")


def _verify_key(scheme: str, node: str, amount: str) -> str:
    return f"{scheme}|{node}" + ("" if amount == "应得" else f"|{amount}")


def _split_thought(arguments: str) -> tuple[str, str]:
    """(思考, 去掉思考后的参数)：重复调用按去掉思考后的参数判断。"""
    try:
        a = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return "", arguments
    if not isinstance(a, dict):
        return "", arguments
    t = a.pop("thought", "")
    return (t.strip() if isinstance(t, str) else ""), json.dumps(a, ensure_ascii=False, sort_keys=True)


def _fix_arguments(text: str) -> str:
    """模型输出的参数不是合法 JSON 时尽量修复；修不好就换成说明错误的合法 JSON。
    历史里的参数必须是合法 JSON，否则服务端会拒绝之后的所有请求。修复过的参数带 _note，执行时告诉模型改了什么。"""
    try:
        json.loads(text or "{}")
        return text or "{}"
    except ValueError:
        pass
    raw = (text or "").strip()
    err, pos = "参数不是 JSON 对象", None
    try:
        json.loads(raw)
    except json.JSONDecodeError as e:
        err, pos = e.msg, e.pos
    fixed = _repair_json(raw, close=False)
    if fixed is not None and isinstance(fixed[0], dict):
        fixed[0]["_note"] = f"（参数格式有误，程序已自动修正：{fixed[1]}。下次按正确格式写。）"
        return json.dumps(fixed[0], ensure_ascii=False)
    for cand, note in ((raw, "后面多出的内容已忽略"), (re.sub(r",\s*([}\]])", r"\1", raw), "多余的逗号已去掉"),
                       *_split_objects(raw)):
        try:
            obj, end = json.JSONDecoder().raw_decode(cand)
        except json.JSONDecodeError:
            continue
        if '"' in cand[end:]:          # 后面还有内容（多半是被截断或拆开的参数），不能悄悄丢掉
            continue
        if isinstance(obj, dict):
            obj["_note"] = f"（参数格式有误，程序已自动修正：{note}。下次按正确格式写。）"
            return json.dumps(obj, ensure_ascii=False)
    near, hint = "", ""
    if pos is not None:
        near = f"，出错位置（▲处）：…{raw[max(0, pos - 80):pos]}▲{raw[pos:pos + 40]}…"
        if raw[pos:pos + 1] == "{" and re.search(r"}\s*,\s*$", raw[:pos]):
            hint = ("这里在一个对象里写了 }, { 把各项分成了几个对象。几项要写在同一个对象里："
                    "{\"a\": {...}, \"b\": {...}}，不要写成 {\"a\": {...}}, {\"b\": {...}}。")
    return json.dumps({"_invalid_json": f"参数不是合法 JSON（{err}{near}），未执行。{hint}请重新调用：字符串用双引号，"
                                        "表达式里的双引号要转义成 \\\"，不要有注释和多余的逗号；参数很长时分几次调用"
                                        "（如 set_rules 先写 variables，再写 allocation、pool），每次少写一些"}, ensure_ascii=False)


def _split_objects(raw: str):
    """常见写法错误：一个对象里的几项被 }, { 分成了几个对象（{"a": {...}}, {"b": {...}}）。
    在解析出错的位置去掉多出的 } 和 {，合成一个对象；最多修 50 处，修完能解析才算数。"""
    text, fixed = raw, 0
    for _ in range(50):
        try:
            json.loads(text)
            break
        except json.JSONDecodeError as e:
            m = re.search(r"}\s*,\s*$", text[:e.pos])
            if not (e.msg.startswith("Expecting property name") and text[e.pos:e.pos + 1] == "{" and m):
                return
            text = text[:m.start()] + ", " + text[e.pos + 1:]
            fixed += 1
    if fixed:
        yield text, f"把被 }}, {{ 分开的 {fixed + 1} 段合成了一个对象"


def _coerce_args(tool: str, args: dict) -> dict:
    """常见的参数写法偏差改回来：数组、对象被写成 JSON 字符串（思考模式下常见）；tables / variables 只写了一项、
    把名字放进了 name 字段（{"name": "x", "expr": ...} → {"x": {"expr": ...}}）。"""
    props = next((t["parameters"]["properties"] for t in TOOLS if t["name"] == tool), {})
    out = dict(args)
    for k, v in args.items():
        if isinstance(v, str) and props.get(k, {}).get("type") in ("array", "object") and v.strip()[:1] in "[{":
            try:
                out[k] = json.loads(v)
                continue
            except ValueError as e:
                err = e
            got = _repair_json(v)
            extra = _sibling_args(v, props, args) if got is None else None
            if extra is not None:
                out.update(extra)
                out["_fixed"] = (out.get("_fixed", "") + f"（参数 {k} 的字符串后面接着写了 {'、'.join(x for x in extra if x != k)}，"
                                 "程序已拆成单独的参数。下次每个参数分开写，直接写对象、不要套引号）")
                continue
            if got is not None:
                out[k] = got[0]
                out["_fixed"] = (out.get("_fixed", "") + f"（参数 {k} 写成了字符串，JSON 也有错，程序已修正：{got[1]}；"
                                 "请核对内容是否完整。下次直接写对象，不要套引号）")
            else:
                raise ToolError(f"参数 {k} 写成了 JSON 字符串，而且这段 JSON 本身有错（{err}）。"
                                f"{k} 直接写成{'对象 {...}' if props[k]['type'] == 'object' else '列表 [...]'}，不要外面套引号；"
                                "内容很长时分几次提交")
    for k, v in list(out.items()):         # 对象写成了列表：[{"x": {...}}, ...] 或 [{"name"/"id": "x", ...}, ...]
        if props.get(k, {}).get("type") == "object" and isinstance(v, list) and v and all(isinstance(i, dict) for i in v):
            out[k] = _list_to_obj(v)
    if tool == "set_rules":
        for k in ("tables", "variables"):
            v = out.get(k)
            if isinstance(v, dict) and isinstance(v.get("name"), str) and ("expr" in v or "kind" in v):
                item = {a: b for a, b in v.items() if a != "name"}
                out[k] = {v["name"]: item}
            v = out.get(k)
            # 别的参数（checks、payout…）写进了 variables / tables 里面：不像一个变量或表（没有 expr / kind），就挪出来
            moved = [a for a, b in (v.items() if isinstance(v, dict) else [])
                     if a in props and a not in ("thought", "scheme", k) and a not in args
                     and not (isinstance(b, dict) and ("expr" in b or "kind" in b))]
            for a in moved:
                out[a] = v.pop(a)
            if moved:
                out["_fixed"] = (out.get("_fixed", "") + f"（{'、'.join(moved)} 是 set_rules 的参数，写进了 {k} 里面，"
                                 f"程序已挪到外面。下次和 {k} 并列写）")
    return out


def _sibling_args(text: str, props: dict, args: dict) -> dict | None:
    """一个参数的 JSON 字符串后面接着写了别的参数：'{"x": {...}}, "checks": [...]'。
    拆成 {参数: 前一段, checks: [...]}；后面的键都是这个工具的其他参数、而且调用里没写过时才拆。"""
    key = next((k for k, v in args.items() if v is text), None)
    try:
        first, end = json.JSONDecoder().raw_decode(text.strip())
    except ValueError:
        return None
    rest = text.strip()[end:].strip()
    if not rest.startswith(","):
        return None
    body = "{" + rest[1:].strip() + "}"
    try:
        more = json.loads(body)
    except ValueError:
        more = (_repair_json(body) or [None])[0]
    if not isinstance(more, dict) or not more or not all(m in props and m not in args and m != key for m in more):
        return None
    if props.get(key, {}).get("type") == "object" and not isinstance(first, dict):
        return None
    return {key: first, **more}


def _missing_args(tool: str, e: TypeError) -> str:
    """Python 的 “missing 1 required positional argument: 'sheet'” 换成能照着改的话：缺哪个参数、它是什么。"""
    props = next((t["parameters"]["properties"] for t in TOOLS if t["name"] == tool), {})
    bad = re.findall(r"unexpected keyword argument '(\w+)'", str(e))
    if bad:       # 参数写成了别的工具的（如 record_facts 写了 note 的 topic / text）
        own = [k for k in props if k != "thought"]
        other = [t["name"] for t in TOOLS if t["name"] != tool and set(bad) <= set(t["parameters"]["properties"])]
        return (f"{tool} 没有参数 {'、'.join(bad)}，它的参数是 {'、'.join(own)}"
                + (f"；{'、'.join(bad)} 是 {'、'.join(other[:3])} 的参数，是不是想调用那个工具" if other else ""))
    names = re.findall(r"'(\w+)'", str(e)) if "required" in str(e) and "missing" in str(e) else []
    if not names:
        return str(e)
    return "缺少必填参数 " + "；".join(f"{n}（{props.get(n, {}).get('description', '')}）".replace("（）", "") for n in names)


_BARE_KEY = re.compile(r"([{,]\s*)(-?[A-Za-z0-9_.\u4e00-\u9fff]+)(\s*:)")


def _quote_keys(text: str) -> str:
    """对象的键没加引号（{1: 1.0}、{a: 1}，模型常见写法）：字符串外面的才补双引号。"""
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i] == '"':                       # 跳过整个字符串
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        m = _BARE_KEY.match(text, i) if text[i] in "{," else None
        if m and m.group(2) not in ("true", "false", "null"):
            out.append(f'{m.group(1)}"{m.group(2)}"{m.group(3)}')
            i = m.end()
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _escape_quotes(text: str) -> str:
    """字符串里的英文双引号没转义（原文写的是 "已确认"，抄进 quote 时没加 \\）：字符串在这个引号处提前结束，
    后面紧跟的不是 , } ] 而是文字，JSON 报 “Expecting ',' delimiter”。把这个引号转义，接着找下一处，最多 100 处。
    只修“引号后面紧跟文字”的位置；漏写逗号（"a" "b"）等其他错误不动。"""
    t = text
    for _ in range(100):
        try:
            json.loads(t)
            break
        except json.JSONDecodeError as e:
            if not e.msg.startswith("Expecting ',' delimiter") or e.pos >= len(t) or t[e.pos] in "\"{}[],:":
                break
            j = e.pos - 1
            while j >= 0 and t[j] in " \t\r\n":
                j -= 1
            if j < 0 or t[j] != '"' or (j > 0 and t[j - 1] == "\\"):
                break
            t = t[:j] + '\\"' + t[j + 1:]
    return t


def _repair_json(text: str, close: bool = True):
    """依次尝试几种常见修法，返回 (能解析的对象, 改了什么) 或 None。close=False 时不补结尾括号
    （整段参数被截断时不能当成写完了）。"""
    got = _repair_steps(text, close)
    if got is None and (esc := _escape_quotes(text)) != text:
        note = "字符串里没转义的双引号已转义"
        try:
            return json.loads(esc), note
        except ValueError:
            got = _repair_steps(esc, close)
            return (got[0], f"{note}、{got[1]}") if got else None
    return got


def _repair_steps(text: str, close: bool):
    cands, t = [], text
    fixes = [(_quote_keys, "键补了双引号"), (lambda x: re.sub(r",\s*([}\]])", r"\1", x), "去掉了多余的逗号")]
    if close:
        fixes.append((lambda x: _close_json(x) or x, "补上了结尾的括号"))
    for fix, note in fixes:
        u = fix(t)
        if u != t:
            cands.append(note)
            t = u
            try:
                return json.loads(t), "、".join(cands)
            except ValueError:
                continue
    return None


HISTORY_THOUGHT_CHARS = 300     # 历史里保留的思考长度：之后每一步都要重发，太长的只留开头


def _not_found_hint(id: str, known: list[str]) -> str:
    """explain 找不到节点时，列出名字里带同样片段（如员工编号）的已有节点，而不是只给一个示例。"""
    parts = [p for p in re.split(r"[:：/\-\s]+", id) if p]
    hits = {p: [k for k in known if p in k] for p in parts}
    near = []
    for p in sorted((p for p in parts if hits[p]), key=lambda p: len(hits[p])):   # 最少见的片段（如员工编号）优先
        near += [k for k in hits[p] if k not in near]
        if len(near) >= 8:
            break
    msg = f"没有找到「{id}」的计算链（这个对象不在这次试算的节点里，可能不属于这个上级或这个方案）"
    if near:
        return msg + "。名字相近的节点：" + "、".join(near[:8])
    return msg + "。节点 id 形如 " + "、".join(known[:5])


def _short_thought(text: str) -> str:
    """回复（JSON）里的 thought 太长时，记进历史的只留开头；执行和运行步骤里用的仍是原文。"""
    try:
        obj = json.loads(text)
    except ValueError:
        return text
    t = obj.get("thought") if isinstance(obj, dict) else None
    if not isinstance(t, str) or len(t) <= HISTORY_THOUGHT_CHARS:
        return text
    return json.dumps({**obj, "thought": t[:HISTORY_THOUGHT_CHARS] + "…（思考较长，已省略）"}, ensure_ascii=False)


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "…"


def _close_json(text: str) -> str | None:
    """JSON 只是结尾少了 } 或 ]（模型常见的漏写）：补上。不在字符串中间、括号配得上时才补。"""
    stack, in_str, esc = [], False, False
    for ch in text:
        if in_str:
            esc, in_str = (False, True) if esc else (ch == "\\", ch != '"')
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack.pop() != ch:
                return None
    if in_str or not stack:
        return None
    return text.rstrip() + "".join(reversed(stack))


def _list_to_obj(items: list[dict]):
    if all(len(i) == 1 for i in items):
        return {k: v for i in items for k, v in i.items()}
    for key in ("name", "id"):
        if all(isinstance(i.get(key), str) for i in items):
            out = {}
            for i in items:
                rest = {k: v for k, v in i.items() if k != key}
                out[i[key]] = next(iter(rest.values())) if len(rest) == 1 else rest
            return out
    return items


THOUGHT = {"type": "string", "description": "思考（必填，1～3 句、不超过 150 字）：上一步观察到了什么、为什么做这一步、预期看到什么；上一步出错时先说明原因。不要复述结果、原文和规则"}
for _t in TOOLS:          # 思考-行动-观察：每次行动都带上思考，记进运行步骤
    _p = _t["parameters"]
    _p["properties"] = {"thought": THOUGHT, **_p.get("properties", {})}
    _p["required"] = ["thought", *_p.get("required", [])]


def openai_tools() -> list[dict]:
    return [{"type": "function", "function": t} for t in TOOLS]


# ---------------- 工作区 ----------------
@dataclass
class Step:
    tool: str
    args: dict
    result: str
    ok: bool
    thought: str = ""
    deep: bool = False            # 这一步是在深度思考下做的（出错、卡住后）


@dataclass
class Workspace:
    llm: LLM
    inv: Inventory
    datasets: dict[str, DatasetDef] = field(default_factory=dict)
    mapping: dict[str, DatasetMapping] = field(default_factory=dict)
    drafts: dict[str, dict] = field(default_factory=dict)  # 方案 -> 规则草稿（tables / variables / pool / allocation ...）
    questions: list[Question] = field(default_factory=list)
    transcripts: dict[str, str] = field(default_factory=dict)
    triage: dict[str, dict] = field(default_factory=dict)   # 材料预筛：文档 -> {"hash": 内容指纹, "pages": {页码: {label, reason, topic, fold}}}
    results: dict[str, dict[str, Result]] = field(default_factory=dict)   # 方案 -> 修订（R0 为基准）-> 结果
    untested: set[str] = field(default_factory=set)        # 最近修改后还没试算成功的方案
    summary: str = ""
    ignored: list[dict] = field(default_factory=list)
    notes: dict[str, str] = field(default_factory=dict)
    done: bool = False
    chat_by: str = ""                                       # 对话模式：发消息的财务；空表示普通的一轮处理
    changed: bool = False                                   # 对话里改过规则、数据表、台账或问题
    reply_text: str = ""
    budget: str = ""
    _verify_misses: dict = field(default_factory=dict)       # 每个对象（金额）验算对不上的次数，提示先核对验算式自己
    checker_down: str = ""                                  # 交叉验证模型调用失败、改用主模型时的说明
    _source_calls: int = 0                                  # 本轮提问前查原文的次数
    _source_skip: bool = False                              # 刚查到原文退回过：下一次提问直接记录
    round: int = 1                                          # 第几轮（同一案卷和财务来回的次数）
    facts: list[fx.Fact] = field(default_factory=list)      # 参数台账：从材料里登记的参数和口径
    clauses: list[cl.Clause] = field(default_factory=list)  # 条款清单：每份规则文档的计算条款，及落在规则的哪里
    conflicts: dict[str, fx.Conflict] = field(default_factory=dict)   # 已提问的参数冲突
    _sheet_corpus: dict[str, str] = field(default_factory=dict)
    progress: tuple[int, int] = (0, 0)                      # 已用步数、上限
    pinned: set[str] = field(default_factory=set)           # 全文已附在材料包里的文件 / 工作表
    _verify_note: str = ""
    _probe_cache: dict = field(default_factory=dict)
    _prev_fields: dict = field(default_factory=dict)        # 每张表上一次定义成功的 fields：只改 key / link / filter 重新定义时沿用
    _gap_cache: tuple | None = None
    reread: dict = field(default_factory=dict)              # 独立复读的结果（见 reread.py），材料没变时复用
    check_vars: dict = field(default_factory=dict)          # 独立验算定义过的辅助变量 {名字: {scope, expr}}，后面的验算直接引用，不必重写
    verified: dict = field(default_factory=dict)            # "方案|对象" -> 独立验算记录 {expr, at, basis}，结束前按最新规则重新比对
    read_nums: dict = field(default_factory=dict)           # 方案 -> 规则读取的数据字段里出现的数（直接读表的参数算用上了）
    lint: dict = field(default_factory=dict)                # 方案 -> 程序检查结果（lint.Report），finish 前要处理其中的 gates
    said: str = ""                                          # 财务在对话里发过的文字：和材料一样可以引用（file 写「财务对话」）

    @property
    def data_files(self) -> list[Path]:
        return [f for f in self.inv.files if f.suffix.lower() in DATA_SUFFIXES]

    # ---- 文件 ----
    def _file(self, name: str) -> Path:
        hit = [f for f in self.inv.files if f.name == name or str(f) == name]
        if not hit:
            raise ToolError(f"没有文件「{name}」，可用文件：{[f.name for f in self.inv.files]}")
        return hit[0]

    def list_files(self) -> str:
        lines = []
        for f in self.inv.files:
            if f.suffix.lower() in DATA_SUFFIXES:
                lines.append(f"- {f.name}（Excel）")
                for ref, body in self.inv.sheet_text.items():
                    if ref.startswith(f.name + "/"):
                        st = self.inv.sheet(ref)
                        if st is None:
                            lines.append(f"  · 工作表「{ref.split('/', 1)[1]}」：未识别出规整表格（{body.count(chr(10)) + 1} 行），可用 read_sheet 查看")
                        else:
                            lines.append(f"  · 工作表「{st.sheet}」：表头在第 {st.header_row} 行，{len(st.rows)} 行数据"
                                         + (f"，{len(st.totals)} 行合计" if st.totals else "")
                                         + f"；表头 {[h for h in st.headers if h]}")
            else:
                d = next(d for d in self.inv.docs if d.name == f.name)
                imgs = sum(1 for b in d.blocks if b.kind == "image")
                lines.append(f"- {f.name}（{f.suffix[1:]}，约 {len(d.full_text())} 字" + (f"，{imgs} 张图片" if imgs else "") + "）")
        if self._chat_text():
            lines.append(f"- {CHAT_SOURCE}（财务在对话里发的文字和问题答复，约 {len(self._chat_text())} 字；里面的规则和参数与材料同样有效，"
                         f"record_facts / record_clauses 引用时 file 写「{CHAT_SOURCE}」）")
        return "\n".join(lines)

    def read_document(self, file: str, offset: int = 0, limit: int = MAX_RESULT_CHARS - 200, folded: bool = False) -> str:
        f = self._file(file)
        d = next((d for d in self.inv.docs if d.name == f.name), None)
        if d is None:
            raise ToolError(f"{file} 是 Excel，请用 read_sheet")
        if f.name in self.pinned and limit < 10 ** 9:
            return f"{f.name} 的全文已在任务开头的【材料包】里（每轮都在），直接看那里，不必再读。"
        summary = _json_data_summary(f)
        if summary and limit < 10 ** 9:
            return (summary + "\n这是结构化数据文件，不能逐页读、也不能直接定义成数据表。"
                    "同样的数据在 Excel 里有时用 Excel 的工作表定义数据表；它是唯一来源时请用 ask_question 请财务提供 Excel。")
        text = self._render_doc(d, folded)
        chunk = text[offset:offset + limit]
        more = len(text) - offset - len(chunk)
        return chunk + (f"\n\n……还有 {more} 字，用 offset={offset + len(chunk)} 继续读取" if more > 0 else "")

    def _render_doc(self, d, folded: bool = False) -> str:
        """文档的文字：按页分段，页首标一次页码；预筛判为背景、装饰、空白的页折叠成一行（folded=True 时照常展开）；
        每页都重复的页眉只留第一次；和前面相同的图片、转写不出内容的图片只留一句。"""
        pages = (self.triage.get(d.name) or {}).get("pages", {})
        header = _repeated_lines(d) if len({b.page for b in d.blocks}) >= TRIAGE_MIN_PAGES else set()
        same = self._same_images(d)
        parts, k, page, run, shown = [], 0, object(), [], set()

        def flush():
            if run:
                a, z = run[0][0], run[-1][0]
                span = f"第{a}页" if a == z else f"第{a}–{z}页"
                why = "；".join(dict.fromkeys(f"{r[1]}：{r[2]}" for r in run))[:300]
                parts.append(f"[{span} 已折叠（预筛判为不参与计算）：{why}。需要时 read_document 带 folded=true 查看]")
                run.clear()
        for b in d.blocks:
            if b.page != page:
                page = b.page
                info = pages.get(str(page)) or pages.get(page) or {}
                if info.get("fold") and not folded:
                    run.append((page, info.get("label", ""), info.get("reason", "")))
                else:
                    flush()
                    parts.append(f"—— {d.name}" + (f" 第{page}页" if page else "") + " ——")
            if b.kind == "image":
                k += 1
            info = pages.get(str(b.page)) or pages.get(b.page) or {}
            if info.get("fold") and not folded:
                continue
            if b.kind == "image":
                if k in same:       # 和前面某张一模一样（多为每页都有的模板图、logo）：不再重复转写内容
                    parts.append(f"[图片 #{k}：和图片 #{same[k]} 完全相同]")
                    continue
                t = self.transcripts.get(f"{d.name}#{k}")
                if t is not None and _empty_transcript(t):
                    parts.append(f"[图片 #{k}：没有可用的文字内容]")
                    continue
                parts.append(f"[图片 #{k}]" + (f"（已转写）\n{t}" if t else "（用 view_image 查看）"
                                               if self.llm.image_llm else "（当前模型不能看图，未转写）"))
            elif b.text:
                text = b.text
                if header:
                    keep = [ln for ln in text.split("\n") if ln.strip() not in header or ln.strip() not in shown]
                    shown |= {ln.strip() for ln in text.split("\n") if ln.strip() in header}
                    text = "\n".join(keep).strip()
                    if not text:
                        continue
                parts.append(("[表格]\n" if b.kind == "table" else "") + text)
        flush()
        return "\n\n".join(parts)

    def triage_all(self) -> None:
        """材料预筛：多页的规则文档先让模型按页分类（规则 / 参数 / 数据说明 / 背景 / 装饰 / 空白），
        背景、装饰、空白而且没有表格和带单位数字的页折叠起来，不进上下文。模型出错或输出不对就不折叠（宁可多留）。
        独立复读、常数出处核对仍看全文。内容没变的文档不重复预筛。"""
        import hashlib
        for d in self.inv.docs:
            pages = _doc_pages(d, self.transcripts)
            if len(pages) < TRIAGE_MIN_PAGES:
                continue
            h = hashlib.sha1("\n".join(f"{p}:{t}" for p, t in pages.items()).encode()).hexdigest()[:16]
            if (self.triage.get(d.name) or {}).get("hash") == h:
                continue
            labels: dict[str, dict] = {}
            chunk, size = [], 0
            batches = []
            for p, t in pages.items():
                piece = f"### 第{p}页\n{t[:TRIAGE_PAGE_CHARS]}"
                if chunk and size + len(piece) > TRIAGE_CHUNK_CHARS:
                    batches.append(chunk)
                    chunk, size = [], 0
                chunk.append(piece)
                size += len(piece)
            if chunk:
                batches.append(chunk)
            try:
                for part in batches:
                    out = self.llm.chat_json([{"role": "system", "content": prompt("triage")},
                                              {"role": "user", "content": f"【材料预筛】文件 {d.name}\n\n" + "\n\n".join(part)}],
                                             temperature=0)
                    for it in (out.get("pages") or []) if isinstance(out, dict) else []:
                        if isinstance(it, dict) and str(it.get("page", "")).strip():
                            labels[str(it["page"]).strip()] = {"label": str(it.get("label", "")).strip(),
                                                               "reason": str(it.get("reason", "")).strip()[:120],
                                                               "topic": str(it.get("topic", "") or "").strip()[:60]}
            except (LLMError, ValueError, TypeError):
                continue        # 预筛失败不影响开工：整份照常给
            tables = {str(b.page) for b in d.blocks if b.kind == "table"}
            for p, info in labels.items():
                text = pages.get(p if p in pages else int(p) if p.isdigit() else p, "")
                # 兜底：有表格、有带单位的数字（%、万、元、系数等）的页不折叠，哪怕模型判成了背景
                info["fold"] = info["label"] in FOLD_LABELS and p not in tables and not _has_figures(text)
            self.triage[d.name] = {"hash": h, "pages": labels}

    def triage_report(self) -> list[str]:
        """预筛折叠了哪些页（给财务和模型看）：文件 -> 页码和理由。"""
        out = []
        for name, t in self.triage.items():
            folded = [(p, i) for p, i in (t.get("pages") or {}).items() if i.get("fold")]
            if folded:
                pages = "、".join(f"第{p}页" for p, _ in sorted(folded, key=lambda x: int(x[0]) if x[0].isdigit() else 0))
                out.append(f"{name}：折叠了 {pages}（{'、'.join(dict.fromkeys(i['label'] for _, i in folded))}）")
        return out

    def _same_images(self, d) -> dict[int, int]:
        """文档里和前面某张字节完全相同的图片：序号 -> 第一次出现的序号（PPT 每页的 logo、模板底图）。"""
        import hashlib
        first, out = {}, {}
        for k, b in enumerate([b for b in d.blocks if b.kind == "image"], start=1):
            h = hashlib.sha1(b.image or b"").hexdigest() if b.image else None
            if h is None:
                continue
            if h in first:
                out[k] = first[h]
            else:
                first[h] = k
        return out

    def view_image(self, file: str, index: int = 1, focus: str = "") -> str:
        f = self._file(file)
        d = next((d for d in self.inv.docs if d.name == f.name), None)
        imgs = [b for b in d.blocks if b.kind == "image"] if d else []
        if not 1 <= index <= len(imgs):
            raise ToolError(f"{file} 中有 {len(imgs)} 张图片，index 应在 1~{len(imgs)}")
        orig = self._same_images(d).get(index)
        if orig and not focus and f"{d.name}#{orig}" in self.transcripts:     # 一模一样的图只转写一次
            text = self.transcripts[f"{d.name}#{orig}"]
            self.transcripts[f"{d.name}#{index}"] = text
            return f"（和图片 #{orig} 完全相同）\n{text}"
        b = imgs[index - 1]
        seer = self.llm.image_llm
        if seer is None:
            raise ToolError(f"当前模型 {self.llm.name} 不能看图，也没有配置看图的模型（models.yaml 里的 vision）。"
                            "图片里的规则如果影响计算，用 ask_question 请财务提供文字版，不要猜")
        ask = "请转写这张图片。" + (f"重点看清：{focus}" if focus else "")
        text = seer.chat([{"role": "system", "content": prompt("transcribe")},
                              {"role": "user", "content": [image_part(b.image, b.mime), {"type": "text", "text": ask}]}],
                             temperature=0).strip()
        self.transcripts[f"{d.name}#{index}"] = text
        return text

    def read_sheet(self, file: str, sheet: str, start_row: int = 1, rows: int = 40) -> str:
        f = self._file(file)
        if f"{f.name}/{sheet}" in self.pinned:
            return f"{f.name}/{sheet} 整表已在任务开头的【材料包】里（每轮都在），直接看那里，不必再读。"
        if f"{f.name}/{sheet}" not in self.inv.sheet_text:
            sheets = [r.split("/", 1)[1] for r in self.inv.sheet_text if r.startswith(f.name + "/")]
            raise ToolError(f"{f.name} 中没有工作表「{sheet}」，可选：{sheets}")
        import openpyxl
        ws = openpyxl.load_workbook(f, data_only=True, read_only=True)[sheet]
        start_row, rows = max(1, start_row), max(1, min(rows, 80))
        out, total = [], 0
        for i, r in enumerate(ws.iter_rows(values_only=True), start=1):
            total = i
            if start_row <= i < start_row + rows:
                cells = ["" if v is None else str(v).replace("\n", " ")[:30] for v in r]
                while cells and cells[-1] == "":
                    cells.pop()
                out.append(f"第{i}行 | " + " | ".join(cells))
        st = self.inv.sheet(f"{f.name}/{sheet}")
        head = (f"工作表共 {total} 行" + (f"，自动识别表头在第 {st.header_row} 行、{len(st.rows)} 行数据" if st else "，未自动识别出表格")
                + f"；以下为第 {start_row}~{min(start_row + rows - 1, total)} 行")
        if st and st.segments:
            head += "\n" + _segments_text(st)
        end = start_row + rows - 1
        tail = (f"\n（还有第 {end + 1}~{total} 行，用 start_row={end + 1} 继续；数据表不必读完，define_dataset 会读取全部行）"
                if end < total else "\n（已到末尾）")
        return head + "\n" + "\n".join(out) + tail

    def transcribe_all(self):
        """开工前把所有图片转写好，材料包里直接给文字，模型不必逐张查看。"""
        if self.llm.image_llm is None:
            return
        for d in self.inv.docs:
            same = self._same_images(d)
            for k, _ in enumerate([b for b in d.blocks if b.kind == "image"], start=1):
                if k in same and f"{d.name}#{same[k]}" in self.transcripts:
                    self.transcripts[f"{d.name}#{k}"] = self.transcripts[f"{d.name}#{same[k]}"]
                elif f"{d.name}#{k}" not in self.transcripts:
                    try:
                        self.view_image(d.name, k)
                    except (LLMError, ToolError):
                        pass       # 转写失败的图片留给模型自己用 view_image 看

    def briefing(self) -> str:
        """材料包：短文档全文（含图片转写）、小工作表整表、大工作表的表头和前几行。附在任务里，每轮都在，
        较早的对话被裁剪也不会丢，模型不用反复读。"""
        self.pinned = set()
        parts, used = [], 0
        if self.triage_report():
            parts.append("材料预筛（程序先让模型逐页分类）把介绍、目录、致谢这类不参与计算的页折叠了，文中标了“已折叠”：\n"
                         + "\n".join(f"- {r}" for r in self.triage_report())
                         + "\n觉得折叠的页里可能有规则时，用 read_document 带 folded=true 查看。")
        for d in self.inv.docs:
            text = self.read_document(d.name, 0, limit=10 ** 9)
            summary = _json_data_summary(self._file(d.name)) if len(text) > PIN_DOC_CHARS else ""
            if summary:
                parts.append(f"## 文件 {d.name}（结构化数据，共 {len(text)} 字，不要逐页读）\n{summary}")
                continue
            if len(text) <= PIN_DOC_CHARS and used + len(text) <= PIN_TOTAL_CHARS:
                used += len(text)
                self.pinned.add(d.name)
                parts.append(f"## 文件 {d.name}（全文）\n{_strip_tags(text, d.name)}")
            else:
                parts.append(f"## 文件 {d.name}（共 {len(text)} 字，太长未全附，以下为开头；需要时用 read_document 按 offset 读）\n"
                             + text[:1500])
        for f in self.data_files:
            for ref in [r for r in self.inv.sheet_text if r.startswith(f.name + "/")]:
                sheet = ref.split("/", 1)[1]
                full = self.read_sheet(f.name, sheet, 1, PIN_SHEET_ROWS + 1)
                if "（已到末尾）" in full:
                    self.pinned.add(ref)
                    parts.append(f"## 工作表 {ref}（整表）\n{full.split(chr(10), 1)[1].removesuffix('（已到末尾）').rstrip()}")
                else:
                    parts.append(f"## 工作表 {ref}（数据表，只附前 5 行；define_dataset 会读取全部行）\n"
                                 + self.read_sheet(f.name, sheet, 1, 6))
        if self._chat_text():
            parts.append(f"## {CHAT_SOURCE}（财务在对话里发的文字和问题答复，全文；引用时 file 写「{CHAT_SOURCE}」）\n{self._chat_text()}")
        return "\n\n".join(parts)

    def note(self, topic: str, text: str) -> str:
        if text.strip():
            self.notes[topic] = text.strip()
        else:
            self.notes.pop(topic, None)
        return f"已记录「{topic}」（共 {len(self.notes)} 条笔记，每轮都会附在工作区状态里）"

    # ---- 参数台账 ----
    def _chat_text(self) -> str:
        """来源「财务对话」的全文：财务在对话里发的文字，加上问题的答复（问题表里答的和对话里答的都算），
        这样按答复登记参数时也能引用原话核对。"""
        answered = [f"{q.id} {q.text} → {q.answer}" for q in self.questions     # 和任务里列答复的写法一样，照抄就能核对
                    if q.answer not in (None, "") and not getattr(q, "withdrawn", "")]
        return "\n\n".join([t for t in [self.said] if t] + answered)

    def _lines(self, name: str) -> list[str]:
        """文件的原文，按行：文档文字、图片转写、Excel 每行单元格。"""
        if name == CHAT_SOURCE:
            return self._chat_text().splitlines()
        f = self._file(name)
        if f.suffix.lower() in DATA_SUFFIXES:
            if f.name not in self._sheet_corpus:
                import openpyxl
                wb = openpyxl.load_workbook(f, data_only=True, read_only=True)
                self._sheet_corpus[f.name] = "\n".join(
                    " | ".join("" if v is None else str(v) for v in r) for w in wb.worksheets
                    for r in w.iter_rows(values_only=True))
            return self._sheet_corpus[f.name].splitlines()
        d = next(d for d in self.inv.docs if d.name == f.name)
        return (d.full_text() + "\n" + "\n".join(t for k, t in self.transcripts.items()
                                                   if k.rsplit("#", 1)[0] == f.name)).splitlines()

    def _corpus(self) -> dict[str, str]:
        """每个文件规范化后的全文，用来核对引用。"""
        out = {f.name: fx.norm("".join(self._lines(f.name))) for f in self.inv.files}
        if self._chat_text():
            out[CHAT_SOURCE] = fx.norm("".join(self._lines(CHAT_SOURCE)))
        return out

    def record_facts(self, facts: list[dict]) -> str:
        corpus, lines, added, dup = self._corpus(), [], 0, 0
        for i, a in enumerate(facts, start=1):
            try:
                f = fx.Fact.model_validate({**a, "round": self.round}) if isinstance(a, dict) else None
            except ValidationError as e:
                lines.append(f"第 {i} 条格式不对：{str(e)[:200]}")
                continue
            if f is None:
                lines.append(f"第 {i} 条应为对象")
                continue
            bad = fx.check_fact(f, corpus)
            if bad:
                near = fx.nearest(f, self._lines(f.file)) if f.file in corpus and "找不到" in bad else ""
                lines.append(f"第 {i} 条「{f.name}」未登记：{bad}" + (f"。原文里最接近的一句：{near}（照这句改 quote，不必重读文件）"
                                                                      if near else ""))
                continue
            if any((g.name, g.file, fx.norm(g.quote)) == (f.name, f.file, fx.norm(f.quote)) for g in self.facts):
                dup += 1
                continue
            self.facts.append(f)
            added += 1
        if not added:       # 一条也没登记上：作为失败返回，避免模型以为成功、反复提交同样的内容
            raise ToolError("没有登记任何新参数。" + (f"{dup} 条之前已登记过，不要重复登记。" if dup else "") + "\n".join(lines)
                            + f"\n下一步（程序判断）：{self.next_hint()}")
        new = fx.detect(self.facts, self.conflicts, self.questions, self.round)
        out = [f"已登记 {added} 条（台账共 {len(self.facts)} 条）。" + (f"另有 {dup} 条之前已登记过，已跳过。" if dup else "")] + lines
        for n in fx.multi_valued(self.facts):
            out.append(f"注意：「{n}」登记了几个不同的取值。如果是阶梯的几档、几个区域的系数这类一组参数，请用 remove_facts 删掉它，"
                       "每档（每项）用不同的名字重新登记（如 …-第1档）；如果同一参数在材料里前后写法矛盾，用 ask_question 问财务")
        for n in fx.mixed_names(self.facts):
            out.append(f"注意：「{n}」下既有数值又有文字说明，像是两个不同的参数（如权重 10% 和归一化公式），"
                       "请换成两个名字分别登记（如 …-权重、…-公式）；同一参数的不同写法才用同一个名字")
        if new:
            out.append("程序发现取值冲突，已自动向财务提问（阻断，不要自行取舍，相关规则先按待定处理）：")
            out += [f"- {q.id} {q.text}" for q in new]
        return "\n".join(out)

    # ---- 条款清单 ----
    def record_clauses(self, clauses: list[dict]) -> str:
        corpus, lines, added = self._corpus(), [], []
        for i, a in enumerate(clauses or [], start=1):
            if not isinstance(a, dict) or not str(a.get("point", "")).strip():
                lines.append(f"第 {i} 条要写 point（要点）、file、quote")
                continue
            file, quote, point = str(a.get("file", "")), str(a.get("quote", "")), str(a["point"]).strip()
            bad = cl.check_quote(file, quote, corpus)
            if bad:
                lines.append(f"第 {i} 条未登记：{bad}")
                continue
            if any(c.file == file and fx.norm(c.quote) == fx.norm(quote) and c.point == point for c in self.clauses):
                lines.append(f"第 {i} 条之前已登记过")
                continue
            c = cl.Clause(id=cl.next_id(self.clauses), point=point[:cl.MAX_POINT], file=file, quote=quote, round=self.round)
            self.clauses.append(c)
            added.append(c.id)
        if not added:
            raise ToolError("没有登记任何条款。\n" + "\n".join(lines))
        return (f"已登记 {len(added)} 条：{added[0]}～{added[-1]}（清单共 {len(self.clauses)} 条，每一步都附在工作区状态里）。"
                + ("\n" + "\n".join(lines) if lines else "") + "\n写好规则后用 cover_clauses 标注每条落在哪里")

    def cover_clauses(self, items: dict) -> str:
        if not isinstance(items, dict) or not items:
            raise ToolError('items 写成 {"C1": "variables.net_receipt", ...}')
        byid = {c.id: c for c in self.clauses}
        known, qids = cl.known_refs(self.drafts, self.datasets), {q.id for q in self.questions}
        ok, bad = [], []
        for cid, where in items.items():
            c = byid.get(str(cid).strip())
            if c is None:
                bad.append(f"{cid}：清单里没有这条")
                continue
            err = cl.check_where(str(where), known, qids)
            if err:
                bad.append(f"{cid}：{err}")
                continue
            c.where = str(where).strip()
            ok.append(c.id)
        left = [c.id for c in self.clauses if not c.where]
        if not ok:
            raise ToolError("没有标注任何条款：\n" + "\n".join(bad))
        return (f"已标注 {len(ok)} 条。" + ("\n未标注成功：\n" + "\n".join(bad) if bad else "")
                + (f"\n还没落实的条款：{'、'.join(left[:30])}" if left else "\n条款清单已全部落实")
                + "\n\n" + self._side_by_side([byid[i] for i in ok]))

    def _element_texts(self) -> dict[str, list[str]]:
        """规则元素位置 -> 各方案里的内容（各方案相同的只列一次）。"""
        out: dict[str, list[str]] = {}
        many = len(self.drafts) > 1
        for sch, d in self.drafts.items():
            for ref, line in _rules_lines(d):
                text = (f"「{sch}」" if many else "") + line.strip().split("  ", 1)[-1]
                if text not in out.setdefault(ref, []):
                    out[ref].append(text)
        for n, d in self.datasets.items():
            units = {f: fd.unit for f, fd in d.fields.items() if fd.unit}
            m = self.mapping.get(n)
            out[f"datasets.{n}"] = [f"数据表 {n} ← {m.file}/{m.sheet}" + (f" 筛选 {m.filter}" if m and m.filter else "")
                                    + (f"，金额单位 {units}（程序已换算成元）" if units else "")] if m else [f"数据表 {n}"]
        return out

    def _clause_sig(self, c, texts: dict[str, list[str]]) -> str:
        return "\n".join(t for r in cl.refs(c.where) for t in texts.get(r, []))

    def _side_by_side(self, clauses: list, limit: int = 6000) -> str:
        """条款原文和它落到的规则并排列出，让模型逐条核对；列出过的记下当时的规则内容。"""
        texts, lines = self._element_texts(), []
        for c in clauses:
            sig = self._clause_sig(c, texts)
            body = "\n".join(f"      {t[:200]}" for t in sig.splitlines()) or f"      {c.where}"
            lines.append(f"- {c.id} 原文（{c.file}）：{c.quote[:160]}\n    要点：{c.point}\n    规则：\n{body}")
            c.checked = sig
        text = "\n".join(lines)
        if len(text) > limit:
            text = text[:limit] + "……"
        return ("逐条核对：规则有没有完整体现原文？条件、范围（筛选、期间、截止日）、边界（含不含）、单位、取整、"
                "作用的层级是否一致？发现出入就改规则，再重新标注。\n" + text)

    def _clause_gaps(self, skipped: set[str]) -> list[str]:
        """结束前检查：每份规则文档都有条款；每条条款都落到了现有的规则元素上（规则改过后会重新核对）。"""
        docs = [f.name for f in self.inv.files if f.suffix.lower() in RULE_SUFFIXES]
        out = [f"规则文档「{d}」还没有摘出条款（用 record_clauses；确实与计算无关的写进 ignored，ref 写文件名）"
               for d in docs if d not in skipped and not any(c.file == d for c in self.clauses)]
        known, qids = cl.known_refs(self.drafts, self.datasets), {q.id for q in self.questions}
        for c in self.clauses:
            if c.file in skipped or c.id in skipped:
                continue
            err = cl.check_where(c.where, known, qids)
            if err:
                out.append(f"{c.id}「{c.point}」：{err}")
        return out

    # ---- 数据表 ----
    def define_dataset(self, name: str, file: str, sheet: str, fields: dict | None = None, key: str | None = None,
                       link: str | None = None, desc: str = "", header_rows: list[int] | None = None,
                       filter: dict | None = None, distinct: bool = False) -> str:
        reuse = ""
        if not fields:      # 重新定义时只改 key / link / filter：沿用上一次的字段（模型常只写要改的部分，反复因缺 fields 失败）
            fields = self._prev_fields.get(name)
            if not fields:
                raise ToolError(f"缺少 fields（字段名 -> {{column, type}}）。「{name}」之前没有定义成功过，要写全字段")
            reuse = f"没写 fields，沿用上一次定义「{name}」时的 {len(fields)} 个字段"
        out = self._define_dataset(name, file, sheet, fields, key, link, desc, header_rows, filter, distinct)
        self._prev_fields[name] = fields
        return out + (f"\n{reuse}" if reuse else "")

    def _define_dataset(self, name: str, file: str, sheet: str, fields: dict, key: str | None = None,
                        link: str | None = None, desc: str = "", header_rows: list[int] | None = None,
                        filter: dict | None = None, distinct: bool = False) -> str:
        f = self._file(file)
        try:
            st = table_at(f, sheet, header_rows) if header_rows else self.inv.sheet(f"{f.name}/{sheet}")
        except (KeyError, ValueError) as e:
            raise ToolError(str(e)) from None
        if st is None:
            raise ToolError(f"{f.name}/{sheet} 没有自动识别出表格，请先 read_sheet 查看，再用 header_rows 指定表头行")
        fixed = []
        if isinstance(fields, dict):     # key / link 写成了 Excel 列名（如 entity_id），而字段已改名（dept_id ← entity_id）
            for which, val in (("key", key), ("link", link)):
                hit = [f for f, d in fields.items() if isinstance(d, dict) and d.get("column") == val] if val and val not in fields else []
                if len(hit) == 1:
                    fixed.append(f"{which} 写的是列名 {val}，已改成对应的字段名 {hit[0]}（key / link 要写字段名）")
                    key, link = (hit[0], link) if which == "key" else (key, hit[0])
        try:
            pd = PlanDataset(sheet=sheet, key=key or None, link=link or None, desc=desc, fields=fields,
                             distinct=bool(distinct))
        except ValidationError as e:
            raise ToolError(f"参数格式不对：{e}") from None
        types = {fn: (fd.type, n) for n, d in self.datasets.items() if n != name for fn, fd in d.fields.items()}
        if filter is not None and not (isinstance(filter, dict) and all(isinstance(v, (str, int, float, list))
                                                                         for v in filter.values())):
            raise ToolError('filter 的格式是 {"列名": "取值"} 或 {"列名": ["取值1", "取值2"]}')
        flt = {str(k): ([str(x) for x in v] if isinstance(v, list) else str(v)) for k, v in (filter or {}).items()}
        d, m, warnings, errors = check_dataset(name, pd, st, types, header_rows, flt)
        if errors:
            owner = next((n for n, o in self.datasets.items() if n != name and key and o.key == key), None)
            if owner and any("重复值" in e for e in errors):
                errors.append(f"{key} 是「{owner}」的主键，这张表应写 link={key}（不写 key），每行挂到对应的{owner}上")
            raise ToolError("未保存，请修正：\n" + "\n".join(f"- {e}" for e in fixed + errors))
        warnings = fixed + warnings
        prev = (self.datasets.get(name), self.mapping.get(name))
        self.datasets[name], self.mapping[name] = d, m
        bad, loose = self._link_mismatch(name)
        if bad:          # 关联字段的取值一个都对不上：多半映射错了列（如编号列没有表头，拿别的列顶替），保存了也全挂不上
            if prev[0] is None:
                del self.datasets[name], self.mapping[name]
            else:
                self.datasets[name], self.mapping[name] = prev
            raise ToolError("未保存，请修正：\n" + "\n".join(f"- {e}" for e in bad))
        warnings += loose
        self.untested |= set(self.schemes)
        kind = f"实体表，主键 {d.key}" if d.key else f"明细表，关联 {d.link}" if d.link else "公司级表"
        sample = []
        from bonus_core.data.table import convert
        st, _ = filter_sheet(st, flt)
        for _, vals in st.rows[:3]:
            sample.append({fn: fmt(convert(vals[st.headers.index(c)], d.fields[fn])) for fn, c in m.columns.items()})
        lines = [f"已保存「{name}」（{kind}），{len(st.rows)} 行" + (f"，另有 {len(st.totals)} 行合计（不计入）" if st.totals else "")
                 + (f"，另有 {len(st.repeats)} 行是重复的表头（第 {'、'.join(map(str, st.repeats[:5]))} 行，不计入）"
                    if getattr(st, "repeats", None) else "")]
        if getattr(st, "segments", None):
            used = {st.headers.index(c) for c in m.columns.values() if c in st.headers}
            hit = [(j, {i: c for i, c in ch.items() if i in used}) for j, ch in st.segments]
            if any(ch for _, ch in hit):
                warnings.append(_segments_text(st, used) + "。数据按列的位置读（同一列在各段都归到同一个字段）："
                                "用到的这些列在两段里含义不一样时（如一段是金额、一段是比例文字），用 ask_question 问财务，不要猜")
            else:
                lines.append(_segments_text(st))
        lines += [f"注意：{w}" for w in warnings]
        lines += [f"关联提示：{h}" for h in self._relation_hints(name)]
        lines.append("前 3 行（金额已换算为元）：" + json.dumps(sample, ensure_ascii=False))
        return "\n".join(lines)

    def _values(self, name: str, fld: str) -> list[str]:
        """数据表某个字段的全部取值（文字），用来判断表之间的关联。"""
        from bonus_core.data.mapping import cell_text
        m = self.mapping[name]
        st = table_at(Path(m.file) if Path(m.file).exists() else self._file(Path(m.file).name), m.sheet, m.header_rows) \
            if m.header_rows else self.inv.sheet(f"{Path(m.file).name}/{m.sheet}")
        if st is None or fld not in m.columns:
            return []
        st, _ = filter_sheet(st, m.filter or None)
        i = st.headers.index(m.columns[fld])
        return [cell_text(v[i]) for _, v in st.rows if v[i] not in (None, "")]

    def _link_mismatch(self, name: str) -> tuple[list[str], list[str]]:
        """按 link 关联的表和被关联的实体表，取值要对得上：一个都对不上就不保存（返回错误），
        对上不到一半给提醒。两张表谁先定义都查（后定义实体表时，查已经按它关联的表）。"""
        d = self.datasets[name]
        pairs = []
        if d.link:
            owner = next((n for n, o in self.datasets.items() if n != name and o.key == d.link), None)
            if owner:
                pairs.append((name, owner))
        if d.key:
            pairs += [(n, name) for n, o in self.datasets.items() if n != name and o.link == d.key]
        bad, loose = [], []
        try:
            for child, owner in pairs:
                fld = self.datasets[child].link
                vals, keys = set(self._values(child, fld)), set(self._values(owner, fld))
                if not vals or not keys:
                    continue
                hit = len(vals & keys)
                col = self.mapping[child].columns.get(fld, fld)
                miss = sorted(vals - keys)[:3]
                if hit == 0:
                    msg = (f"「{child}」的 {fld}（列「{col}」）取值和「{owner}」的主键 {fld} 一个都对不上："
                           f"本表如 {sorted(vals)[:3]}，「{owner}」如 {sorted(keys)[:3]}。多半是映射错了列，"
                           f"或编号要由几列拼成；先 read_sheet 看清是哪一列（没有表头的列也能选，名字见表头列表），不要拿别的列顶替")
                    (bad if child == name else loose).append(msg)
                elif hit < len(vals) / 2:
                    loose.append(f"「{child}」的 {fld} 只有 {hit}/{len(vals)} 个取值能在「{owner}」里找到（对不上的如 {miss}），"
                                 f"对不上的行挂不到任何{owner}上；确认列和编号写法（空格、大小写、前导 0）")
        except Exception:      # 取值失败不影响保存
            pass
        return bad, loose

    def _relation_hints(self, name: str) -> list[str]:
        """程序按数据判断的关联问题：编号列取值是别的表的主键却没同名、link 找不到被关联的表、该写 key 的没写。"""
        d, out = self.datasets[name], []
        keys = {o.key: n for n, o in self.datasets.items() if o.key and n != name}
        try:
            for f, fd in d.fields.items():
                if fd.type not in ("id", "text") or f == d.key:
                    continue
                vals = set(self._values(name, f))
                if not vals:
                    continue
                for k, n in keys.items():
                    if k != f and k not in d.fields and vals <= set(self._values(n, k)):
                        out.append(f"字段 {f} 的取值都是「{n}」的主键 {k}（如 {sorted(vals)[0]}）。要和「{n}」关联，"
                                   f"请把这个字段命名为 {k}（column 不变）" + ("，并写 link" if not d.link and not d.key else ""))
            if d.link and d.link not in keys and not any(d.link == o.link for n, o in self.datasets.items() if n != name):
                own = [n for n, o in self.datasets.items() if n != name and d.link in o.fields]
                out.append(f"link={d.link} 目前没有以 {d.link} 为主键的表" + (
                    f"；「{own[0]}」有 {d.link} 列，如果它每行一个 {d.link}，把它重新定义为 key={d.link} 的实体表"
                    if own else "；它应该是某个层级（如部门、小组、个人）或关联实体的主键"))
            if not d.key and not d.link:
                ids = [f for f in d.fields if f in keys]
                if ids:
                    out.append(f"这张表有 {ids[0]} 字段却没写 link，会被当作公司级表；按 {ids[0]} 挂到「{keys[ids[0]]}」上请写 link={ids[0]}")
            for n, o in self.datasets.items():
                if n != name and o.link and o.link in d.fields and d.key != o.link and o.link not in keys:
                    vals = self._values(name, o.link)
                    if vals and len(set(vals)) == len(vals):
                        out.append(f"「{n}」按 {o.link} 关联，而本表每行一个不同的 {o.link}：本表应写 key={o.link}（成为关联实体）")
        except Exception:      # 提示只是辅助，取值失败不影响保存
            pass
        return out

    def remove_dataset(self, name: str) -> str:
        if name not in self.datasets:
            raise ToolError(f"没有数据表「{name}」")
        del self.datasets[name], self.mapping[name]
        self.untested |= set(self.schemes)
        return f"已删除「{name}」"

    # ---- 规则（可以有多个方案，"共用" 方案合并进每个方案） ----
    def _scheme(self, scheme: str | None, *, for_write: bool = False) -> str:
        if scheme:
            if not for_write and scheme not in self.schemes:
                raise ToolError(f"没有方案「{scheme}」，已有方案：{self.schemes or '无'}")
            return scheme
        if for_write or not self.schemes:
            return self.schemes[0] if len(self.schemes) == 1 else DEFAULT_SCHEME
        if len(self.schemes) == 1:
            return self.schemes[0]
        raise ToolError(f"有多个方案 {self.schemes}，请用 scheme 指定")

    @property
    def schemes(self) -> list[str]:
        """要计算的方案（不含共用部分）。"""
        return [s for s in self.drafts if s != SHARED]

    def set_rules(self, scheme: str | None = None, **parts) -> str:
        name = self._scheme(scheme, for_write=True)
        if name == SHARED and parts.get("pool") is not None:
            raise ToolError("pool 是每个方案自己的，不能写在共用部分")
        draft = json.loads(json.dumps(self.drafts.get(name, {})))
        for k in ("tables", "variables"):
            for n, v in (parts.get(k) or {}).items():
                if v is None:
                    draft.setdefault(k, {}).pop(n, None)
                else:
                    draft.setdefault(k, {})[n] = v
        for k in ("pool", "allocation", "payout", "offsets", "checks", "patches", "revisions"):
            if parts.get(k) is not None:
                draft[k] = parts[k]
        for lv in draft.get("allocation") or []:       # 最上层不需要权重，模型常写成 null
            if isinstance(lv, dict) and lv.get("weight") is None:
                lv["weight"] = ""
        try:
            _revisions(draft.get("revisions"), strict=True)
            targets = (self.schemes or [SHARED]) if name == SHARED else [name]   # 还没有方案时也先校验共用部分的格式
            rss = {t: self._ruleset(t, {**self.drafts, name: draft}) for t in targets}
        except (ValidationError, ValueError) as e:
            hint = ""
            for k in ("variables", "tables"):
                v = draft.get(k)
                if isinstance(v, dict) and ("expr" in v or "kind" in v):
                    hint += f"\n{k} 要写成 {{名字: {{...}}}}，每一项以名字为键，如 {{\"emp_sales\": {{\"expr\": \"...\", \"scope\": \"个人\"}}}}"
            raise ToolError(f"规则格式不对，未保存：{str(e)[:2500]}{hint}") from None
        self.drafts[name] = draft
        self.untested |= set(targets) - {SHARED}
        out = [f"已保存{'共用部分' if name == SHARED else f'方案「{name}」'}。"]
        self.last_blocking = set()
        for t, rs in rss.items():
            full = self._merged(t)
            issues = self._validate(t, rs)
            if "pool" not in full:
                issues = [i for i in issues if i.where != "pool"]
            later = ""
            if not full.get("allocation"):     # 还没写分配层级：层级名的 scope 先不算错，写好 allocation 后再校验
                wait = sorted({m.group(1) for i in issues if i.code == "scope_bad"
                               for m in [re.search(r"scope「([^」]*)」", i.message)] if m})
                issues = [i for i in issues if i.code not in ("no_levels", "scope_bad")]
                later = ("还没有分配层级（allocation）" + (f"，用到的层级 {'、'.join(wait)} 先记下" if wait else "")
                         + "；写好 allocation 后再完整校验。")
            if t == SHARED:     # 只有共用部分时，引用的变量可能由各方案自己定义（如共用的分配层级用各方案的权重变量）：先记下，合并后再校验
                names_later = sorted({m.group(1) for i in issues if i.code == "name_unknown"
                                      for m in [re.search(r"无法识别的名字 (\S+)", i.message)] if m})
                issues = [i for i in issues if i.code != "name_unknown"]
                if names_later:
                    later += (f"共用部分引用的 {'、'.join(names_later)} 还没有定义：如果由各方案自己定义，写入各方案后会合并校验；"
                              "否则在共用部分的 variables 里补上。")
            blocking = [str(i) for i in issues if i.severity == "blocking"]
            warns = [str(i) for i in issues if i.severity == "warning"]
            head = f"「{t}」" + ("（尚未设定 pool）" if "pool" not in full else "")
            self.last_blocking |= {(t, i.code, i.where) for i in issues if i.severity == "blocking"}
            if blocking:
                out.append(head + "静态校验发现问题，需要修正：\n" + "\n".join(f"- {e}" for e in blocking)
                           + fixes.hints([i for i in issues if i.severity == "blocking"]))
            else:
                out.append(head + "静态校验通过。" + ("\n提示：\n" + "\n".join(f"- {w}" for w in warns) if warns else ""))
            if later:
                out.append(later)
        tiers = [_tier_text(n, t) for n, t in (parts.get("tables") or {}).items() if isinstance(t, dict) and t.get("kind") == "tier"]
        if tiers:
            out.append("阶梯表按下面的意思计算，请和材料原文逐档核对：\n" + "\n".join(f"- {t}" for t in tiers if t))
        if not self.schemes:
            out.append("还没有具体方案；用 set_rules(scheme=方案名, pool=...) 写入各方案后再完整校验。")
        return "\n".join(out)

    def _merged(self, scheme: str, drafts: dict | None = None) -> dict:
        drafts = self.drafts if drafts is None else drafts
        base, own = drafts.get(SHARED, {}), drafts.get(scheme, {})
        out = {k: v for k, v in base.items() if k not in ("tables", "variables")}
        for k in ("tables", "variables"):
            if base.get(k) or own.get(k):
                out[k] = {**base.get(k, {}), **own.get(k, {})}
        out.update({k: v for k, v in own.items() if k not in ("tables", "variables")})
        return out

    def _ruleset(self, scheme: str | None = None, drafts: dict | None = None) -> RuleSet:
        scheme = scheme or self._scheme(None)
        draft = {k: v for k, v in self._merged(scheme, drafts).items() if k != "revisions"}
        ext = Extraction.model_validate({"pool": {"expr": "0"}, "allocation": [], **draft})
        return RuleSet(name=scheme if scheme != DEFAULT_SCHEME else (self.summary[:30] or "智能体草稿"),
                       version="agent-draft", datasets=self.datasets, **ext.model_dump(exclude={"questions"}))

    def _data(self, rs: RuleSet):
        data, issues = apply_mapping(rs, self.data_files, self.mapping)
        blocking = [i for i in issues if i.severity == "blocking"]
        if blocking:
            raise ToolError("数据问题：\n" + "\n".join(f"- {i}" for i in blocking[:8]))
        return data, issues

    def trial_run(self, scheme: str | None = None) -> str:
        if not self.datasets:
            raise ToolError("还没有定义数据表")
        names = [self._scheme(scheme)] if scheme or len(self.schemes) <= 1 else self._order(self.schemes)
        out = []
        for name in names:
            if "pool" not in self._merged(name) or not self._merged(name).get("allocation"):
                raise ToolError(f"方案「{name}」的规则还缺 pool 或 allocation")
            out.append(self._trial(name))
        return "\n\n".join(out)

    def _deps(self, name: str) -> set[tuple[str, str]]:
        """方案引用的其他方案（修订）：规则里 scheme_paid("方案", "修订") 的参数。"""
        out: set[tuple[str, str]] = set()

        def walk(v):
            if isinstance(v, str):
                out.update((m[0], m[1] or "R0") for m in SCHEME_REF.findall(v))
            elif isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
        walk(self._merged(name))
        return out

    def _order(self, names: list[str]) -> list[str]:
        """被引用的方案排在前面。"""
        out: list[str] = []

        def visit(n, path):
            if n in out:
                return
            if n in path:
                raise ToolError(f"方案之间循环引用：{' → '.join(path + [n])}")
            for d, _ in sorted(self._deps(n)):
                if d in self.schemes:
                    visit(d, path + [n])
            out.append(n)
        for n in names:
            visit(n, [])
        return [n for n in out if n in names]

    def _refs(self, name: str, _path: tuple = ()) -> dict[str, Result]:
        """本方案引用的其他方案结果；还没试算或改过的，先试算。"""
        refs = {}
        for s, rev in sorted(self._deps(name)):
            if s == name or s in _path:
                raise ToolError(f"方案「{name}」不能引用自己或形成循环引用（{s}）")
            if s not in self.schemes:
                raise ToolError(f"方案「{name}」引用了不存在的方案「{s}」，现有方案：{self.schemes}")
            if s in self.untested or s not in self.results:
                self._trial(s, _path + (name,))
            if rev not in self.results[s]:
                raise ToolError(f"方案「{name}」引用了「{s}」的修订 {rev}，但该方案只有 {list(self.results[s])}")
            refs[f"{s}/{rev}"] = self.results[s][rev]
        return refs

    def _validate(self, scheme: str, rs: RuleSet) -> list[Issue]:
        """静态校验；全局变量在某层按对象引用、又没明确写 scope 的，必须先处理（写明 scope），不能只当提示。"""
        variables = self._merged(scheme).get("variables") or {}
        out = []
        for i in validate(rs):
            m = re.match(r"变量 (\S+) 没写 scope", i.message) if i.code == "global_in_level" else None
            if m and "scope" not in (variables.get(m.group(1)) or {}):
                i = Issue.block(i.code, i.message + "。必须明确写 scope（要全公司合计就写 scope: global）", i.where)
            out.append(i)
        return out

    def _trial(self, name: str, _path: tuple = ()) -> str:
        rs = self._ruleset(name)
        guard = [str(i) for i in self._validate(name, rs) if i.code == "global_in_level" and i.severity == "blocking"]
        if guard:
            raise ToolError(f"方案「{name}」试算前需要修正：\n" + "\n".join(f"- {g}" for g in guard))
        data, issues = self._data(rs)
        refs = self._refs(name, _path)
        try:
            eng = Engine(rs, data, refs)
            res = {"R0": eng.compute()}
            done: list[Adjustment] = []
            for rev in _revisions(self._merged(name).get("revisions")):
                done += [Adjustment(a["node"], Decimal(str(a["amount"])), reason=a.get("reason", ""), id=a.get("id", ""),
                                    created_at=f"{len(done) + i:06d}") for i, a in enumerate(rev["adjust"])]
                res[rev["name"]] = eng.compute(list(done))
        except EngineError as e:
            raise ToolError(f"方案「{name}」试算失败：\n" + "\n".join(f"- {i}" for i in e.issues[:8]) + fixes.hints(e.issues)) from None
        except (EvalError, AdjustConflict, ValueError) as e:
            raise ToolError(f"方案「{name}」试算失败：{e}") from None
        self.results[name] = res
        self.read_nums[name] = fx.data_numbers(rs, data)
        self.untested.discard(name)
        self.untested |= {s for s in self.schemes if s != name and name in {d for d, _ in self._deps(s)}}  # 引用本方案的要重算
        base = res["R0"]
        lines = [f"方案「{name}」试算成功。奖金池 {fmt(Decimal(base.pool))} 元（精确值 {base.pool_exact}）"]
        for lv in base.levels:
            ns = sorted((n for n in base.nodes.values() if n.level == lv), key=lambda n: -float(n.final))
            if not ns:
                lines.append(f"「{lv}」0 个：这一层没有任何对象，没有人能分到钱。核对这一层的数据表、映射和筛选条件")
                continue
            total = sum(float(n.final) for n in ns)
            zero = sum(1 for n in ns if float(n.final) == 0)
            top = "，".join(f"{n.id}（{n.label}）{fmt(Decimal(n.final))}" for n in ns[:8])
            lines.append(f"「{lv}」{len(ns)} 个，合计 {total:,.2f}，为 0 的 {zero} 个；最多的：{top}"
                         + ("……" if len(ns) > 8 else "") + f"；最少的：{ns[-1].id} {fmt(Decimal(ns[-1].final))}")
        for lv in base.levels:                          # 抽查：每层取一个中间对象，列出权重代入后的数值
            ns = sorted((n for n in base.nodes.values() if n.level == lv), key=lambda n: float(n.final))
            nid = ns[len(ns) // 2].id if ns else ""
            tr = base.traces.get(f"{nid}/权重")
            while tr is not None and tr.expr.isidentifier() and f"{nid}/{tr.expr}" in base.traces:   # 权重直接写成变量名时展开
                tr = base.traces[f"{nid}/{tr.expr}"]
            if tr is not None and tr.rendered != tr.expr:
                lines.append(f"抽查「{lv}」{tr.id}：{tr.rendered[:400]} = {tr.value}")
        for rev, r in list(res.items())[1:]:
            changed = [k for k, n in r.nodes.items() if n.final != base.nodes[k].final]
            lines.append(f"修订 {rev}：{len(changed)} 个对象金额变化，奖金池 {fmt(Decimal(r.pool))}；"
                         + "，".join(f"{k} {fmt(Decimal(base.nodes[k].final))}→{fmt(Decimal(r.nodes[k].final))}"
                                    for k in changed[:6]))
        held = sum(float(p.held) for p in base.payouts.values())
        if base.payouts:
            rows = base.payout_rows()
            paid = sum(Decimal(p.paid) for p in rows)
            off = sum(Decimal(p.offset or 0) for p in rows)
            ded = sum(Decimal(getattr(p, "deducted", 0) or 0) for p in rows)
            over = sum(Decimal(p.overpaid or 0) for p in rows)
            lines.append(f"发放：本次实发合计 {paid:,.2f}，暂缓合计 {held:,.2f}"
                         + (f"，已发（预发）合计 {off:,.2f}，其中实际抵扣 {ded:,.2f}、超发待核对 {over:,.2f}"
                            f"（勾稽：应得 = 本次实发 + 暂缓 + 实际抵扣）" if off or over else ""))
            if base.rollups:
                rs_ = sorted(base.rollups.values(), key=lambda r: r.key)
                lines.append(f"按「{rs_[0].entity}」汇总后抵扣：{len(rs_)} 个；"
                             + "，".join(f"{r.key}（{r.label}）可发上限 {fmt(Decimal(r.cap))} 抵扣 {fmt(Decimal(r.deducted))} "
                                        f"实发 {fmt(Decimal(r.paid))}" + (f" 超发 {fmt(Decimal(r.overpaid))}" if Decimal(r.overpaid) else "")
                                        for r in rs_[:8]) + ("……" if len(rs_) > 8 else ""))
            if held == 0 and any(p.get("pay_ratio") or p.get("hold_ratio") for p in self._merged(name).get("payout") or []):
                lines.append("请核对：写了按比例发放，但所有对象都全额可发、暂缓为 0。比例的分子分母是否写反"
                             "（如应为 回款 ÷ 销售），筛选条件是否对")
        if refs:
            lines.append("引用：" + "、".join(refs))
        mismatch = self._period_mismatch(name)
        if mismatch:
            lines.append("请核对：" + mismatch)
        warns = [str(i) for i in list(issues) + list(base.issues) if i.severity == "warning"]
        if warns:
            lines.append("数据提示：\n" + "\n".join(f"- {w}" for w in warns[:8]))
        heads = {f"{n}.{f}": f"{m.sheet or ''} {c}" for n, m in self.mapping.items() for f, c in m.columns.items()}
        others = {w for k, d in self.drafts.items() if k != name
                  for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", json.dumps(d, ensure_ascii=False))}
        rep = self.lint[name] = lint.check(rs, data, self._rule_text(), base.traces, sourced=self._sources_readable(),
                                           headers=heads, tiers=base.tier_stats, used_elsewhere=others,
                                           coverage=base.coverage)
        if rep.warns:
            lines.append("程序检查提示：\n" + "\n".join(f"- {w}" for w in rep.warns[:8]))
        gates = self._open_gates(rep)
        if gates:
            lines.append(f"程序检查发现 {len(gates)} 处要处理（finish 前必须改好、问财务，或写进 finish 的 ignored 说明原因）：\n"
                         + "\n".join(f"- {g}" for g in gates[:10]))
        return "\n".join(lines)

    def _rule_text(self) -> str:
        """规则材料的原文（规则文档、没定义成数据表的工作表、财务的话和答复），程序检查用来找常数和取值的出处。"""
        used = {(m.file, m.sheet) for m in self.mapping.values()}
        parts = [l for f in self.inv.files if f.suffix.lower() not in DATA_SUFFIXES for l in self._lines(f.name)]
        for ref, body in self.inv.sheet_text.items():
            file, sheet = ref.split("/", 1)
            if (file, sheet) not in used and not any(Path(str(a)).name == file and b == sheet for a, b in used):
                parts.append(body)
        parts.append(self.said)
        parts += [f"{q.text} {q.answer or ''}" for q in self.questions]
        return "\n".join(parts)

    def _sources_readable(self) -> str:
        """常数出处能不能核对：没有规则材料，或有图片还没转写时返回原因（常数只提示、不拦）。"""
        if not any(f.suffix.lower() not in DATA_SUFFIXES for f in self.inv.files) and not self.said:
            return "没有规则文档"
        n = sum(1 for d in self.inv.docs for k, b in enumerate([b for b in d.blocks if b.kind == "image"], start=1)
                if f"{d.name}#{k}" not in self.transcripts)
        return f"材料里有 {n} 张图片还没转写" if n else ""

    def _reread_gaps(self, skipped: set[str]) -> list[tuple[str, str]]:
        """独立复读和规则的出入里还没处理的：写进 ignored 或已有问题提到的算处理过。复读调用失败不拦，口径单上会标出。"""
        from bonus_agent import reread as rr
        res = rr.run(self)
        asked = " ".join(f"{q.text} {q.answer or ''}" for q in self.questions)
        # 独立算式在数据上算出的数和规则不一样（至少两次独立写得一致）：智能体不能自己判定谁对、写进 ignored 放过，
        # 只能改规则，或者把两种算法写给财务定（问题里写明方案名和“奖金池 / 可发比例”）
        # 提问时 related 写了这条的编号就算处理过（按编号认，不靠问题措辞）；措辞里写到也认，作为兜底
        refs = self._asked_refs()
        decided = [f"{q.text} {q.answer or ''}" for q in self.questions if not getattr(q, "withdrawn", "")]
        return [(k, t) for k, t in rr.compare(self, res) if k not in skipped and k not in refs and k.split(" ", 2)[2] not in asked] \
            + [(k, t) for k, t in rr.calc_gaps(self, res)
               if k not in refs and not any(k.split(" ")[1] in q and k.split(" ", 2)[2] in q for q in decided)]

    def _asked_refs(self) -> set[str]:
        """问题 related 里写到的检查项编号（可以用、；, 分隔写多个）。"""
        out = set()
        for q in self.questions:
            if not getattr(q, "withdrawn", ""):
                out |= {r.strip(" 「」[]【】") for r in re.split(r"[、;；,，\n]", q.related or "") if r.strip()}
        return out

    def _open_gates(self, rep) -> list[str]:
        """还没处理的程序检查项：已写进 ignored，或已有问题提到这个取值 / 常数的，算处理过。
        有两类不能由智能体自己放过：照抄数据表的查找表（下一期悄悄算错），只能改规则或问财务；
        已发抵扣写进奖金池（发放表应得、抵扣、超发三列错），写进 ignored 或提问都不算，只能改成 offsets。"""
        skipped = {str(i.get("ref", "")) for i in self.ignored if not str(i.get("ref", "")).startswith(NO_IGNORE)}
        asked = " ".join(f"{q.text} {q.answer or ''}" for q in self.questions)
        # 每条后面给出写进 ignored 用的 ref（模型自己拼的 ref 常对不上，同一条拦了又拦）
        refs = self._asked_refs()
        return [g + (f"〔提问时 related 写「{k}」〕" if k.startswith(NO_IGNORE)
                     else f"〔放过时 ignored 的 ref 写「{k}」，提问时 related 写同样内容〕")
                if not k.startswith(MUST_FIX) else g
                for g, k in zip(rep.gates, rep.keys)
                if k.startswith(MUST_FIX) or (k not in skipped and k not in refs and k.split(" ", 1)[1] not in asked)]

    def _period_mismatch(self, name: str) -> str:
        """奖金池和分配权重用的统计期间（日期、period == "Q3" 这类取值）对不上时给出提示，常见于把按季度算的权重放在共用部分、
        年度方案也跟着用了。只提示，不阻断。"""
        full = self._merged(name)
        variables = full.get("variables") or {}

        def periods(expr: str, depth: int = 0) -> set[str]:
            out = set(re.findall(r'"(\d{4}-\d{2}-\d{2})"', expr)) | set(re.findall(r'==\s*"([^"]{1,4})"', expr))
            if depth < 4:
                for n in set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr)) & set(variables):
                    out |= periods(str((variables[n] or {}).get("expr", "")), depth + 1)
            return out
        own = [full.get("pool") or {}] + list(full.get("payout") or []) + list(full.get("offsets") or [])
        pool = periods(" ".join(str(v) for p in own if isinstance(p, dict) for k, v in p.items()
                                if k in ("expr", "pay_ratio", "hold_ratio")))
        weight = periods(" ".join(str(lv.get("weight") or "") for lv in full.get("allocation") or []))
        if pool and weight and not pool & weight:
            src = "（allocation 来自共用部分）" if "allocation" not in self.drafts.get(name, {}) else ""
            return (f"方案「{name}」的奖金池和发放用的期间是 {sorted(pool)}，分配权重用的是 {sorted(weight)}{src}。"
                    "如果权重应按本方案的期间计算，把 allocation 写到这个方案里")
        return ""

    @property
    def result(self) -> Result | None:
        """第一个方案的基准结果（兼容只有一个方案的情况）。"""
        s = self.schemes[0] if self.schemes else None
        return self.results.get(s, {}).get("R0") if s else None

    @property
    def dirty(self) -> bool:
        return bool(self.untested) or not self.schemes or any(s not in self.results for s in self.schemes)

    def explain(self, id: str, scheme: str | None = None, revision: str = "R0") -> str:
        name = self._scheme(scheme)
        res = self.results.get(name, {}).get(revision)
        if res is None:
            raise ToolError(f"方案「{name}」{revision} 还没有试算结果，先 trial_run")
        tr = res.traces
        keys = [k for k in tr if k == id or k.startswith(id + "/")] or [k for k in tr if id in k]
        if not keys:
            raise ToolError(_not_found_hint(id, list(res.nodes) + [k for k in tr if k not in res.nodes]))
        out = []
        for k in keys[:12]:
            t = tr[k]
            out.append(f"{t.id}：{t.expr}\n  = {t.rendered}\n  = {t.value}" + (f"\n  明细行：{', '.join(t.rows[:5])}" if t.rows else ""))
        if id in res.nodes:
            n = res.nodes[id]
            out.append(f"{id} 最终金额 {fmt(Decimal(n.final))}（权重 {n.weight}，上级 {n.parent}）")
        return "\n".join(out)

    def evaluate(self, expr: str, node: str = ROOT_LEVEL, scheme: str | None = None) -> str:
        name = self._scheme(scheme) if self.schemes else self._scheme(None, for_write=True)
        if not self._merged(name).get("allocation"):
            raise ToolError("需要先用 set_rules 设定 allocation（pool 可以先写 0）")
        # 验算时定义过的辅助变量在 evaluate 里也能用（规则里同名的变量优先），按正式规则的语义计算
        rule_vars = set((self._merged(name).get("variables") or {}))
        extra = {k: v for k, v in _used_helpers(expr, self.check_vars).items() if k not in rule_vars}
        try:
            same_field = self_compares(parse(expr))
        except ExprError:
            same_field = []
        if same_field:
            raise ToolError(f"{'、'.join(dict.fromkeys(same_field))} {SELF_COMPARE_HINT}")
        v, rendered = self._probe(name, expr, node, extra=extra or None)
        return f"{expr} = {rendered} = {fmt(v)}"

    def _probe(self, name: str, expr: str, node: str, helpers: dict | None = None, extra: dict | None = None):
        """在方案的数据上（已应用数据修订、带上引用的其他方案结果）对某个节点计算表达式，和正式计算用同样的数据。
        helpers：独立验算自己定义的辅助变量 {名字: {scope, expr}}，给出时只认这些变量，不带规则里的变量。
        extra：evaluate 用到的已保存辅助变量，加在规则变量旁边，语义和正式计算一样。"""
        key = id(self.results.get(name))           # 试算结果没变时规则和数据也没变，不必重读文件
        hit = self._probe_cache.get(name)
        if hit and hit[0] == key and name in self.results and name not in self.untested:
            rs, data, patched = hit[1:]
        else:
            rs = self._ruleset(name)
            data, _ = self._data(rs)
            data, patched = apply_patches(rs, data)
            self._probe_cache[name] = (key, rs, data, patched)
        if helpers is not None:
            try:
                hv = {k: Variable.model_validate(v) for k, v in helpers.items()}
            except ValueError as e:
                raise ToolError(f"辅助变量写法不对：{e}") from None
            hv = {k: v.model_copy(update={"scope": _helper_scope(rs, k, v.scope)}) for k, v in hv.items()}
            rs = rs.model_copy(update={"variables": hv})
        elif extra:
            try:
                ev_ = {k: Variable.model_validate(v) for k, v in extra.items()}
                ev_ = {k: v.model_copy(update={"scope": _helper_scope(rs, k, v.scope)}) for k, v in ev_.items()}
            except (ValueError, ToolError):
                ev_ = {}
            rs = rs.model_copy(update={"variables": {**rs.variables, **ev_}})
        root, by_level, issues = build_tree(rs, data)
        blocking = [i for i in patched + issues if i.severity == "blocking"]
        if blocking:
            raise ToolError("建树失败：" + "；".join(str(i) for i in blocking[:5]))
        nodes = {n.id: n for n in root.walk()}
        nodes.update({n.id: n for tbl in (root.index or {}).values() for n in tbl.values()})
        if node not in nodes:
            raise ToolError(f"没有节点「{node}」，例：{list(nodes)[:5]}")
        ev = Evaluator(rs, root, by_level, self._refs(name))
        ev.peer_scope = helpers is not None      # 独立验算：对本层或上层实体表求和按上一层范围取同级（见 Evaluator.peer_scope）
        try:
            v = ev._eval_traced(expr, nodes[node], "probe", "probe").v     # evaluate 也能看文字取值（如编号、状态）
        except (EvalError, ValueError) as e:
            raise ToolError(f"计算失败：{e}") from None
        return v, ev.traces["probe"].rendered

    def _independent_env(self, name: str, spec: dict):
        """独立复读写的算式的计算环境：只用它自己的辅助变量和参数表（不带规则的变量和参数表）。写法不对抛 ToolError。"""
        rs = self._ruleset(name)
        data, _ = self._data(rs)
        data, patched = apply_patches(rs, data)
        try:
            tables = Extraction.model_validate({"pool": {"expr": "0"}, "allocation": [],
                                                "tables": spec.get("tables") or {}}).tables
            hv = {k: Variable.model_validate(v) for k, v in (spec.get("helpers") or {}).items()}
            for e in [spec.get("pool"), spec.get("pay_ratio")] + [v.expr for v in hv.values()]:
                if e not in (None, ""):
                    parse(str(e))
        except (ValueError, ExprError) as e:
            raise ToolError(f"写法不对：{str(e)[:400]}") from None
        hv = {k: v.model_copy(update={"scope": _helper_scope(rs, k, v.scope)}) for k, v in hv.items()}
        rs = rs.model_copy(update={"variables": hv, "tables": tables})
        root, by_level, issues = build_tree(rs, data)
        blocking = [i for i in patched + issues if i.severity == "blocking"]
        if blocking:
            raise ToolError("建树失败：" + "；".join(str(i) for i in blocking[:3]))
        ev = Evaluator(rs, root, by_level, self._refs(name))
        ev.peer_scope = True
        return rs, root, by_level, ev

    @staticmethod
    def _independent_eval(ev, expr: str, nodes, label: str) -> dict[str, Decimal]:
        out = {}
        for n in nodes:
            try:
                v = ev.evaluate(expr, n, f"{n.id}/复读{label}", "复读")
            except (EvalError, ValueError) as e:
                raise ToolError(f"{label}在 {n.id} 上算不出来：{str(e)[:300]}") from None
            if isinstance(v, bool) or not isinstance(v, Decimal):
                raise ToolError(f"{label}在 {n.id} 上算出的不是数：{v}")
            out[n.id] = v
        return out

    def _independent_pool(self, name: str, spec: dict, strict: bool = False) -> dict[str, Decimal]:
        """独立复读按材料写的奖金池算式，在奖金池所在层的每个对象上计算。返回 {对象: 金额}；写法不对、算不出来抛 ToolError。
        strict：复读写算式时用，分档表有传入值不在任何一档里（比例悄悄成了 0）也算写法不对，交还复读改。"""
        rs, root, by_level, ev = self._independent_env(name, spec)
        targets = [root] if rs.pool.scope == "global" else by_level.get(rs.pool.scope, [])
        out = self._independent_eval(ev, str(spec["pool"]), targets, "奖金池")
        if strict:
            self._independent_holes(rs, ev)
        return out

    @staticmethod
    def _independent_holes(rs, ev) -> None:
        # 和规则的程序检查同一条：复读的分档表漏了最低一段（如下限 0 又不含下限，传入 0 就不在任何一档），
        # 不交还复读改的话，复读自己算错的数会被当成“规则可能错了”拿去问财务
        holes = lint.tier_holes(rs, ev.tier_stats)
        if holes:
            raise ToolError("；".join(
                f"分档表 {n} 有 {k} 个传入值不在任何一档里（第一档从 {lint._fmt(lo)} 起"
                f"{'、不含下限' if not rs.tables[n].lower_inclusive else ''}，最小传入值 {lint._fmt(low)}），比例按 0 算了。"
                f"按材料补上这一段：材料规定这些值按第一档算，就把第一档的下限写得更小（如低于可能的最小值）；规定不发就加一档比例 0"
                for n, lo, k, low in holes))

    def _independent_pay(self, name: str, spec: dict, strict: bool = False) -> dict[str, Decimal]:
        """独立复读按材料写的可发比例（本次可发上限 ÷ 应得），在发放层（最末层）每个对象上计算；没写返回 {}。"""
        if spec.get("pay_ratio") in (None, ""):
            return {}
        rs, root, by_level, ev = self._independent_env(name, spec)
        out = self._independent_eval(ev, str(spec["pay_ratio"]), by_level.get(rs.leaf.level, []), "可发比例")
        if strict:
            self._independent_holes(rs, ev)
        bad = [k for k, v in out.items() if v < 0 or v > 1]
        if bad:
            raise ToolError(f"可发比例要在 0~1 之间，{bad[0]} 上算出 {out[bad[0]]}")
        return out

    def _caps(self, name: str) -> dict[str, tuple[Decimal, Decimal, int]]:
        """规则算出的发放层每个对象：(应得, 可发上限, 发放条件个数)，未修订的 R0。"""
        res = self.results[name]["R0"]
        n = max(1, len(self._ruleset(name).payout))
        return {k: (Decimal(p.final), Decimal(p.cap or p.final), n) for k, p in res.payouts.items()}

    def _var_values(self, name: str, nids: list[str], spec: dict | None = None) -> dict[str, dict[str, Decimal]]:
        """在几个对象上算出每个变量的值：{对象: {变量: 值}}。spec 给出时算独立复读的辅助变量，否则算规则变量。
        算不出来的（如下层变量在上层对象上要先聚合、文字或是否值）跳过。只建一次树，所有变量共用一个计算器。"""
        if spec is not None and spec.get("_verify"):      # 独立验算的辅助变量：和 verify 一样用规则的参数表
            rs = self._ruleset(name)
            data, _ = self._data(rs)
            data, _ = apply_patches(rs, data)
            hv = {k: Variable.model_validate(v) for k, v in (spec.get("helpers") or {}).items()}
            hv = {k: v.model_copy(update={"scope": _helper_scope(rs, k, v.scope)}) for k, v in hv.items()}
            rs = rs.model_copy(update={"variables": hv})
            root, by_level, _ = build_tree(rs, data)
            ev = Evaluator(rs, root, by_level, self._refs(name))
            ev.peer_scope = True
            names = list(hv)
        elif spec is not None:
            rs, root, by_level, ev = self._independent_env(name, spec)
            names = list(spec.get("helpers") or {})
        else:
            rs = self._ruleset(name)
            data, _ = self._data(rs)
            data, _ = apply_patches(rs, data)
            root, by_level, _ = build_tree(rs, data)
            ev = Evaluator(rs, root, by_level, self._refs(name))
            names = list(rs.variables)
        nodes = {n.id: n for n in root.walk()}
        nodes.update({n.id: n for tbl in (root.index or {}).values() for n in tbl.values()})
        out: dict[str, dict[str, Decimal]] = {}
        for nid in nids:
            if nid not in nodes:
                continue
            got = out.setdefault(nid, {})
            for v in names:
                try:
                    x = ev.variable(v, nodes[nid]).v
                except Exception:
                    continue
                if isinstance(x, Decimal) and not isinstance(x, bool):
                    got[v] = x
        return out

    def _pool_values(self, name: str) -> dict[str, Decimal]:
        """规则算出的奖金池（未修订的 R0），按奖金池所在层的对象。"""
        res = self.results[name]["R0"]
        return {k.removesuffix("/奖金池"): Decimal(str(t.value)) for k, t in res.traces.items()
                if k.endswith("/奖金池") and k.count("/") == 1}

    # ---- 独立验算 ----
    def verify(self, node: str, expr: str, basis: str, at: str | None = None, scheme: str | None = None,
               amount: str = "应得", helpers: dict | None = None) -> str:
        name = self._scheme(scheme)
        if name in self.untested or name not in self.results:
            raise ToolError(f"方案「{name}」最近修改后还没有试算，先 trial_run")
        if amount not in ("应得", "实发"):
            raise ToolError("amount 写 应得 或 实发")
        if node not in self._amounts(name, amount):
            raise ToolError(f"方案「{name}」没有对象「{node}」的{amount}金额，要验算的对象见工作区状态的“待独立验算”")
        if not basis.strip():
            raise ToolError("basis 写依据的材料原文（条款号或原句）")
        if helpers is not None and not (isinstance(helpers, dict) and all(isinstance(v, dict) for v in helpers.values())):
            raise ToolError('helpers 写成 {"名字": {"scope": "项目", "expr": "..."}}')
        used = _used_helpers(expr, {**self.check_vars, **(helpers or {})})     # 之前定义过的辅助变量直接可用；记录里存全，单独可复算
        rec = {"expr": expr, "at": at or node, "basis": basis.strip(), "amount": amount,
               **({"helpers": used} if used else {})}
        bad = self._verify_mismatch(name, node, rec, explain=True)
        if helpers and not bad.startswith(("验算式写错了", "辅助变量", "验算式算不出来")):
            self.check_vars.update(helpers)      # 写法没问题的辅助变量留着复用（金额对不上时也留着，改哪个就重写哪个）
        if bad:
            raise ToolError(bad)
        self.verified[_verify_key(name, node, amount)] = rec
        note = self._verify_note          # 下面重新比对别的对象会覆盖它，先取出来
        left = self._verify_gaps()
        return (f"验算通过：{node} 的{amount} {note}。"
                + (f"还要验算：{'；'.join(left[:6])}" if left else "程序指定的对象都已验算通过"))

    def _amounts(self, name: str, kind: str) -> dict[str, tuple[Decimal, bool]]:
        """对象 -> (引擎金额, 是否带按份额分配的舍入尾差)。应得取各层对象金额；实发取发放口径（有按人汇总的抵扣时取汇总行）。"""
        res = self.results[name]["R0"]

        def tail(nid):
            n = res.nodes.get(nid)
            return n is not None and Decimal(n.exact).quantize(Decimal("0.01"), ROUND_HALF_UP) != Decimal(n.final)
        if kind == "应得":
            return {k: (Decimal(n.final), tail(k)) for k, n in res.nodes.items()}
        return {r.node_id: (Decimal(r.paid), any(tail(m) for m in getattr(r, "members", [r.node_id])))
                for r in res.payout_rows()}

    def _verify_mismatch(self, name: str, node: str, rec: dict, explain: bool = False) -> str:
        """按当前规则和数据重新比对一条验算记录；一致返回空串，并把比对说明放在 _verify_note。
        explain：这次是智能体调用 verify（不是事后重新比对），对不上时再逐步找分岔处、记对不上的次数。"""
        helpers = rec.get("helpers") or {}
        srcs = [rec["expr"]] + [str(h.get("expr", "")) for h in helpers.values()]
        try:
            asts = [parse(x) for x in srcs]
        except ExprError as e:
            return f"验算式写错了：{e}"
        same_field = [c for t in asts for c in self_compares(t)]
        if same_field:     # 1103 回归 Q2：where: project_id == project_id 把所有项目都加了进来，同一个验算对不上重试 8 次
            return f"验算式里的 {'、'.join(dict.fromkeys(same_field))} {SELF_COMPARE_HINT}"
        rs = self._ruleset(name)
        same = sorted(set(helpers) & set(rs.variables))
        if same:
            return f"辅助变量 {'、'.join(same)} 和规则变量同名，换个名字（如加 v_ 前缀），独立验算不能和规则混用"
        names = set().union(*(set(parse_names(t)) for t in asts))
        if not (names & set(rs.field_owner())) and not any(isinstance(c, Call) and c.fn == "count" for t in asts for c in walk(t)):
            # 1103 回归 Q7：模型把上一层的数（班组奖金池、本组权重合计）直接写成数字，被拦下后不知道怎么改，原样重复了 400 多次
            return ("验算式没有用到任何数据字段。独立验算要从原始数据算起（字段、参数表），不能直接写结果数字。"
                    "要用到别的层的数（如上一层的奖金池、本组的合计）时，用 helpers 在那一层另写辅助变量再引用："
                    "helpers 写 {\"v_上层数\": {\"scope\": \"那一层的层级名\", \"expr\": \"按材料从字段算起的式子\"}}，"
                    "expr 里引用 v_上层数；同一层的合计用 sum(本层辅助变量, of: \"本层的表名\")")
        digits = {id(c.args[1]) for t in asts for c in walk(t) if isinstance(c, Call) and c.fn == "round" and len(c.args) > 1}
        loose = [k for k in (lint._key(abs(n.value)) for t in asts for n in walk(t) if isinstance(n, Num) and id(n) not in digits)
                 if Decimal(k) not in lint.FREE and k not in lint.material_numbers(self._rule_text())]
        if loose:
            return (f"验算式里的数 {'、'.join(dict.fromkeys(loose))} 在材料里找不到。独立验算只能用材料写明的数"
                    "（比例、系数、阈值）和数据字段，不能把结果或中间结果直接写成数字")
        own = sorted((names - set(helpers)) & set(rs.variables))
        if own:
            return (f"验算式引用了规则变量 {'、'.join(own)}。独立验算不能借用规则的中间结果，按材料原文直接从字段、参数表重新写；"
                    "需要先在某一层算出中间数（如每个项目的有效销售额）时，用 helpers 自己定义辅助变量")
        kind = rec.get("amount", "应得")
        got = self._amounts(name, kind).get(node)
        if got is None:
            return f"{node} 现在没有{kind}金额（对象变了），按新的结果重新验算"
        try:
            v, rendered = self._probe(name, rec["expr"], rec["at"], helpers)
        except ToolError as e:
            hint = ""
            if rec["at"] != node:
                # 1103 回归 Q1：验算小组却在部门上算（at 写部门），部门上取不到这个小组自己的权重、字段，换了十几种写法都报错
                hint = (f"。你是在「{rec['at']}」上算「{node}」的：在别的对象上取不到「{node}」自己的字段和辅助变量。"
                        f"去掉 at，直接在「{node}」上算：上层的数（如上层的奖金池）写成 scope 为上层层级的辅助变量，在下层直接引用；"
                        f"同一个上层对象下各同级的合计写 sum(本层辅助变量, of: \"本层的表名\")，本对象的份额也可以写 share(本层辅助变量)")
            return f"验算式算不出来：{e}{hint}"
        if isinstance(v, bool) or not isinstance(v, Decimal):
            return f"验算式算出的不是金额：{fmt(v)}"
        final, has_tail = got
        mine = v.quantize(Decimal("0.01"), ROUND_HALF_UP)
        if mine == final:
            self._verify_note = f"引擎 {fmt(final)} 元，独立验算 {fmt(mine)} 元，一致"
            return ""
        if has_tail and abs(mine - final) <= Decimal("0.01"):     # 按份额分配，舍入后的尾差分到了别人身上
            exact = self.results[name]["R0"].nodes[node].exact if node in self.results[name]["R0"].nodes else ""
            if kind == "实发" or abs(v - Decimal(exact)) <= VERIFY_TOL:
                self._verify_note = f"引擎 {fmt(final)} 元，独立验算 {fmt(v)} 元；差 {fmt(final - mine)} 元是按份额分配的舍入尾差"
                return ""
        cents = abs(mine - final)
        # 只差一两分：多半是舍入的位置不同（如一个先把应得舍到分再乘比例，一个乘完一次舍入），不是口径错
        near = (f"只差 {fmt(cents)} 元，多半是舍入的位置不同：对照材料哪一步要先舍到分"
                "（如先把应得舍到分、再乘比例后再舍，还是一次算完再舍），把验算式里的 round 放到同样的位置。") \
            if Decimal(0) < cents <= Decimal("0.02") else ""
        key = _verify_key(name, node, kind)
        if explain:
            self._verify_misses[key] = self._verify_misses.get(key, 0) + 1
        return (f"{node} 的{kind}对不上：引擎算出 {fmt(final)} 元，独立验算 {rec['expr']} = {rendered} = {fmt(v)}。{near}"
                f"{self._engine_side(name, node, kind)}{self._verify_split(name, node, rec) if explain else ''}"
                f"先想清楚哪一边不符合材料（{rec['basis'][:100]}）：用 explain 查看 {node} 的计算链逐项对照，规则错了就改规则后重新试算，"
                "验算式错了就改验算式；拿不准就 ask_question"
                + (f"。{node} 已经对不上 {self._verify_misses[key]} 次：规则也可能是对的、错在验算式。"
                   f"先用 evaluate 在 {node}（下层的辅助变量在它下面的对象上）把验算式里每个辅助变量算出来，"
                   "和 explain 列出的规则中间数逐项对照，确认验算式本身没写错（筛选条件、汇总的表、取整），再决定改哪边"
                   if explain and self._verify_misses.get(key, 0) >= 2 else ""))

    def _verify_split(self, name: str, node: str, rec: dict) -> str:
        """验算对不上时，在这个对象上逐个算验算式的辅助变量和规则的变量，指出从哪一步开始不一样（同复读对不上）。"""
        from bonus_agent import reread as rr
        helpers = rec.get("helpers") or {}
        if not helpers:
            return ""
        try:
            mine = self._var_values(name, [node], {"helpers": helpers, "_verify": True})
            theirs = self._var_values(name, [node])
        except Exception:
            return ""
        found = rr.split_steps(helpers, mine, theirs, [node])
        if found:
            return f"程序在 {node} 上逐个算了验算式的辅助变量和规则的变量：{found}。"
        if mine.get(node) and theirs.get(node) and all(
                any(rr._same_num(x, y) for y in theirs[node].values()) for x in mine[node].values()):
            return "验算式的辅助变量在规则里都有同样的数，不一样的是最后把它们组合起来的这一步（验算式本身）。"
        return ""

    def _lineage(self, name: str, node: str) -> str:
        """对象的归属链（如 个人:E015 → 小组:G04（2部1组）→ 部门:D02（行业业务部）），验算时按它取上级的池和权重合计。"""
        nodes = self.results[name]["R0"].nodes
        chain, cur = [], nodes.get(node)
        while cur is not None and cur.parent and cur.parent in nodes and len(chain) < 10:
            cur = nodes[cur.parent]
            if cur.level == ROOT_LEVEL:
                break
            chain.append(cur.id + (f"（{cur.label}）" if cur.label and cur.label != cur.key else ""))
        return f"{node} 属于 " + " → ".join(chain) if chain else ""

    def _belongs(self, name: str, node: str) -> str:
        line = self._lineage(name, node)
        return "，" + line.removeprefix(f"{node} ") if line else ""

    def _engine_side(self, name: str, node: str, kind: str) -> str:
        """对不上时把引擎这一侧的关键数摆出来，便于逐项对照（归属、应得、暂缓、抵扣）。"""
        res = self.results[name]["R0"]
        parts = [self._lineage(name, node)]
        if node in res.nodes and kind == "应得":
            parts.append(f"引擎权重 {res.nodes[node].weight}")
        if kind == "实发":
            r = next((r for r in res.payout_rows() if r.node_id == node), None)
            if r is not None:
                parts.append(f"引擎：应得 {fmt(Decimal(r.final))}，暂缓 {fmt(Decimal(r.held))}，可发上限 {fmt(Decimal(r.cap or r.final))}，"
                             f"已发抵扣 {fmt(Decimal(r.offset))}（实际抵扣 {fmt(Decimal(r.deducted))}，超发待核对 {fmt(Decimal(r.overpaid))}），"
                             f"实发 {fmt(Decimal(r.paid))}"
                             "（实发对不上时先单独核对应得，再核对比例、暂缓和抵扣）")
        parts = [p for p in parts if p]
        return ("；".join(parts) + "。") if parts else ""

    def _verify_targets(self, name: str) -> list[tuple[str, str, str]]:
        """程序指定要独立验算的 (对象, 应得 / 实发, 原因)，不由模型挑：每层应得最高的；最末层再加最低的非零对象和一个为 0 的对象。
        有暂缓或已发抵扣时，再验算实发：实发最高的、有超发登记的、有暂缓的。"""
        base = self.results[name]["R0"]
        out = []
        for i, lv in enumerate(base.levels):
            ns = sorted((n for n in base.nodes.values() if n.level == lv), key=lambda n: (Decimal(n.final), n.id))
            if not ns:
                continue
            picks = [(ns[-1], "金额最高")]
            if i == len(base.levels) - 1:
                nz = [n for n in ns if Decimal(n.final) != 0]
                if nz and nz[0] is not ns[-1]:
                    picks.append((nz[0], "金额最低的非零"))
                zero = [n for n in ns if Decimal(n.final) == 0]
                if zero and zero[0] is not ns[-1]:
                    picks.append((zero[0], "金额为 0"))
            out += [(n.id, "应得", f"应得{why}，引擎算出 {fmt(Decimal(n.final))} 元" + self._belongs(name, n.id)) for n, why in picks]
        rs = self._ruleset(name)
        if rs.offsets or rs.payout:
            rows = sorted(base.payout_rows(), key=lambda r: (Decimal(r.paid), r.node_id))
            picks = [(rows[-1], "实发最高")] if rows else []
            picks += [(r, "有超发登记") for r in rows if Decimal(r.overpaid) > 0][:1]
            picks += [(r, "有暂缓") for r in rows if Decimal(r.held) > 0][:1]
            seen = set()
            for r, why in picks:
                if r.node_id not in seen:
                    seen.add(r.node_id)
                    out.append((r.node_id, "实发", f"{why}，引擎算出实发 {fmt(Decimal(r.paid))} 元" + self._belongs(name, r.node_id)))
        return out

    def _verify_gaps(self) -> list[str]:
        """还没通过独立验算的对象（含规则或数据改过之后重新比对不一致的）。"""
        key = (tuple((s, id(self.results.get(s)), s in self.untested) for s in self.schemes),
               json.dumps(self.verified, ensure_ascii=False, sort_keys=True))
        if self._gap_cache and self._gap_cache[0] == key:
            return list(self._gap_cache[1])
        out = self._verify_gaps_now()
        self._gap_cache = (key, out)
        return list(out)

    def _verify_gaps_now(self) -> list[str]:
        out = []
        for name in self.schemes:
            if name in self.untested or name not in self.results:
                continue
            for node, kind, why in self._verify_targets(name):
                rec = self.verified.get(_verify_key(name, node, kind))
                if rec is None:
                    out.append(f"方案「{name}」{node}（{why}" + ("，verify 写 amount=实发" if kind == "实发" else "") + "）")
                    continue
                bad = self._verify_mismatch(name, node, rec)
                if bad:
                    out.append(f"方案「{name}」{node} 之前验算通过，按现在的规则重新比对不一致：{bad[:300]}")
        return out

    # ---- 问题与完成 ----
    def ask_question(self, text: str, options: list[str] | None = None, suggested: int | None = None, related: str = "",
                     blocking: bool = True) -> str:
        same = next((q for q in self.questions if q.text.strip() == text.strip()), None)
        if same is not None:
            if same.answer:
                return f"这个问题（{same.id}）财务已答复：{same.answer}。请按答复处理"
            return f"这个问题已经问过（{same.id}），还在等财务答复"
        if blocking:
            found = self._answer_in_material(text, options or [])
            if found:
                return found
        q = Question(id=f"q{len(self.questions) + 1}", text=text, options=options or [], suggested=suggested,
                     related=related, blocking=blocking, round=self.round)
        self.questions.append(q)
        if blocking:
            return (f"已记录问题 {q.id}，等财务答复。不要自行选择口径或补出算法；"
                    "继续做不受影响的部分，全部问完后调用 finish 结束本轮")
        return f"已记录提示 {q.id}（不影响金额，不必等答复）"

    def _answer_in_material(self, text: str, options: list[str]) -> str:
        """提问前先查原文：让交叉验证模型只看材料（规则文档、财务答复、参数表，不含数据行）找能直接回答这个问题的原文。
        找到、而且这句原文逐字核对得上，就先不问财务，把原文退回给智能体按原文处理（1103 回归 Q1：财务答复里写明了回款口径，
        模型把两种理解拿去问财务）。智能体认为原文不够时再问一次，这次直接记录；每轮最多查 SOURCE_CHECKS 次，查不了就照常记录。"""
        from bonus_agent import reread as rr
        if self._source_skip or self._source_calls >= SOURCE_CHECKS:
            self._source_skip = False
            return ""
        self._source_calls += 1
        body = rr.material(self)
        msgs = [{"role": "system", "content": prompt("source_check")},
                {"role": "user", "content": body + "\n\n# 智能体准备问财务的问题\n" + text
                 + ("\n可选答案：" + "；".join(map(str, options)) if options else "")}]
        try:
            data = parse_json(rr._ask(self, msgs))
        except (LLMError, ValueError):
            return ""
        if not isinstance(data, dict) or data.get("answerable") is not True:
            return ""
        quote = str(data.get("quote") or "").strip()
        if not _in_text(quote, body):
            return ""          # 原文对不上（模型编的或改了字）：不算数，照常提问
        self._source_skip = True
        src = str(data.get("source") or "").strip()
        return (f"先别问财务：独立核对在材料里找到了能回答这个问题的原文{f'（{src}）' if src else ''}：「{quote}」。"
                f"按这句原文：{str(data.get('answer') or '').strip()}。请对照这句原文处理：规则或验算和原文不一致就改。"
                "如果你认为这句原文回答不了这个问题，再调用一次 ask_question，在问题里写明这句原文为什么不够（这次会直接记录）")

    @property
    def pending(self) -> list[Question]:
        """还在等财务答复的问题。"""
        return [q for q in self.questions if q.blocking and not q.answer and not q.withdrawn]

    def finish(self, summary: str, ignored: list[dict] | None = None, review: bool = True) -> str:
        # 上一轮登记过的忽略项继续有效，本轮同名的覆盖
        ignored = list({str(i.get("ref", "")): i for i in [*self.ignored, *(ignored or [])] if isinstance(i, dict)}.values())
        self.ignored = ignored                 # finish 被拦下时也保留，修正后再 finish 不必重写
        skipped = {str(i.get("ref", "")) for i in ignored}
        docs = [f.name for f in self.inv.files if f.suffix.lower() in RULE_SUFFIXES]
        bare = [d for d in docs if d not in skipped and not any(f.file == d for f in self.facts)]
        if bare:
            raise ToolError(f"这些规则文档还没有登记参数：{'、'.join(bare)}。请用 record_facts 登记其中的参数和口径"
                            "（程序靠台账比对冲突）；确实与计算无关的写进 finish 的 ignored（ref 写文件名，说明原因）")
        if self.pending:     # 有问题等答复：结束本轮，不出正式结果
            self.summary, self.ignored, self.done = summary, ignored, True
            return f"本轮结束，等待财务答复 {len(self.pending)} 个问题：{'、'.join(q.id for q in self.pending)}"
        if not self.schemes:
            raise ToolError("还没有写入规则")
        pending = [s for s in self.schemes if s in self.untested or s not in self.results]
        if pending:
            raise ToolError(f"方案 {pending} 最近一次修改之后还没有成功试算，请先 trial_run")
        gaps = self._clause_gaps(skipped)
        if gaps:
            raise ToolError("条款清单还有没落实的（逐条处理后再 finish；用 cover_clauses 标注）：\n"
                            + "\n".join(f"- {g}" for g in gaps[:15]) + (f"\n……共 {len(gaps)} 条" if len(gaps) > 15 else ""))
        miss = [n for n in fx.unused(self.facts, self.drafts, self.conflicts, self.questions,
                                set().union(*(self.read_nums.get(s, set()) for s in self.schemes))) if n.split("（")[0] not in skipped]
        if miss:
            raise ToolError(f"台账里这些参数的数值在规则里没有出现：{'、'.join(miss)}。请核对规则是否漏了或写错；"
                            "确实不参与计算的写进 ignored（ref 写参数名，说明原因）")
        gates = [f"方案「{s}」：{g}" for s in self.schemes if s in self.lint for g in self._open_gates(self.lint[s])]
        if gates:
            raise ToolError("程序检查还有没处理的（逐条改规则；需要财务确认的用 ask_question；确实无关的写进 ignored，"
                            "ref 照抄每条末尾〔〕里给出的写法，reason 说明原因）：\n" + "\n".join(f"- {g}" for g in gates[:12])
                            + (f"\n……共 {len(gates)} 处" if len(gates) > 12 else ""))
        diffs = self._reread_gaps(skipped)
        if diffs:
            other = getattr(self.llm, "checker_llm", None)
            who = (f"另一个模型 {other.name} 另起一次" if other is not None and not self.checker_down else "同一模型另起一次")
            raise ToolError(f"独立复读（{who}，只看材料和表头，没看你的规则）和规则有这些出入。逐条对照材料原文核对："
                            "规则错了就改规则；规则是对的（复读理解错了）就写进 finish 的 ignored，ref 写行首方括号里的编号，"
                            "reason 引用材料原文说明依据；材料没写清、需要财务定的用 ask_question（related 写同一个编号）。"
                            "其中「复读 奖金池 / 复读 可发比例」是在数据上算出来的数对不上，不能写进 ignored："
                            "找到两边从哪一步开始不一样，规则错了就改；你认为规则对，就用 ask_question 把两种算法和差异写给财务定"
                            "（related 写行首方括号里的编号）：\n"
                            + "\n".join(f"- [{k}] {t}" for k, t in diffs[:15])
                            + (f"\n……共 {len(diffs)} 处" if len(diffs) > 15 else ""))
        left = self._verify_gaps()
        if left:
            raise ToolError("独立验算还没全部通过（金额错一分都不能出结果）。对下面每个对象用 verify：按材料原文从原始字段另写算式，"
                            "不引用规则变量，引擎结果要和它对到分：\n" + "\n".join(f"- {g}" for g in left[:12])
                            + (f"\n……共 {len(left)} 个" if len(left) > 12 else ""))
        if review:                           # 规则在核对之后又改过的条款，结束前再并排核对一次
            texts = self._element_texts()
            stale = [c for c in self.clauses if cl.refs(c.where) and not c.where.startswith((cl.BUILTIN, cl.NOT_APPLY, cl.ASKED))
                     and self._clause_sig(c, texts) != c.checked]
            if stale:
                raise ToolError(f"结束前自查：这 {len(stale)} 条条款对应的规则在核对之后改过（或还没核对过），请逐条对照后再 finish"
                                "（没有问题就直接再调用 finish）：\n" + self._side_by_side(stale))
        self.summary, self.ignored, self.done = summary, ignored, True
        return "已完成"

    def reply(self, text: str, ignored: list[dict] | None = None) -> str:
        if not self.chat_by:
            raise ToolError("现在不是对话，请用 finish 结束")
        if self.changed and self.schemes:
            if any(s in self.untested or s not in self.results for s in self.schemes):
                out = self.trial_run()
                if any(s in self.untested or s not in self.results for s in self.schemes):
                    raise ToolError("修改后试算没有通过，请修正后再回复：\n" + out[:2000])
            self.finish(self.summary or "对话中修改", ignored, review=False)     # 财务直接要求的修改，不再整体自查
        self.reply_text, self.done = text.strip(), True
        return "已回复"

    def remove_facts(self, name: str) -> str:
        """删掉某个参数名下的全部登记（登记错了时用）；依据它的、还没答复的冲突问题由程序撤回。"""
        before = len(self.facts)
        self.facts = [f for f in self.facts if f.name.strip() != name.strip()]
        if len(self.facts) == before:
            raise ToolError(f"台账里没有「{name}」，已有：{'、'.join(dict.fromkeys(f.name for f in self.facts))[:800]}")
        gone = fx.withdraw_stale(self.facts, self.conflicts, self.questions)
        return (f"已删除「{name}」的 {before - len(self.facts)} 条登记" +
                (f"，撤回问题 {'、'.join(q.id for q in gone)}" if gone else "") + "。请按正确的名字重新登记")

    def answer_question(self, id: str, answer: str, file: str = "", quote: str = "") -> str:
        """对话时记录财务的原话；不在对话时，只能引用材料原文（如后来补充的答复文件）关闭问题，程序核对原文确实在材料里。"""
        done = next((q for q in self.questions if q.id == id and q.answer and not q.withdrawn), None)
        if done is not None and not self.chat_by:
            # 财务已在网页“问题”里答复过：不用再记，以前这里会报“不能替财务回答”，模型以为答复没生效
            return (f"{id} 已有财务答复：{done.answer}（{done.answered_by or '财务'}），不用再记录。"
                    "直接按这个答复修改规则（set_rules），再试算")
        if not self.chat_by and (not file or not quote.strip()):
            raise ToolError("现在不是和财务对话，你不能替财务回答。材料里（如后来补充的答复文件）已经有这个问题的答复时，"
                            "file 写那个文件、quote 逐字复制答复原文，程序核对后关闭问题；材料里没有就继续等财务答复。"
                            "如果是程序因为你登记错了而误报的冲突（如把阶梯几档登记成同一个名字），用 remove_facts 删掉那个名字的登记、"
                            "按正确的名字重新登记，程序会自动撤回问题")
        q = next((q for q in self.questions if q.id == id), None)
        if q is None or q.withdrawn:
            raise ToolError(f"没有待答复的问题 {id}")
        from datetime import datetime
        if self.chat_by:
            q.answer, q.answered_by = answer.strip(), f"{self.chat_by}（对话）"
        else:
            bad = fx.check_fact(fx.Fact(name=f"问题 {id} 的答复", value=quote, file=file, quote=quote),
                                self._corpus())
            if bad:
                raise ToolError(f"引用核对没通过：{bad}")
            q.answer = f"{answer.strip()}（依据《{file}》原文：{quote.strip()}）"
            q.answered_by = f"材料《{file}》"
        q.answered_at = datetime.now().isoformat(timespec="seconds")
        return f"已记录 {id} 的答复：{q.answer}。请按答复修改规则"

    def phase(self) -> int:
        """程序按工作区判断现在处在第几个阶段（0 起，见 PHASES）。规则改动后会退回前面的阶段。"""
        skipped = {str(i.get("ref", "")) for i in self.ignored}
        if self.schemes and not self.dirty and not self._clause_gaps(skipped):
            return 2 if self._verify_gaps() else 3
        docs = [f.name for f in self.inv.files if f.suffix.lower() in RULE_SUFFIXES and f.name not in skipped]
        read = all(any(f.file == d for f in self.facts) and any(c.file == d for c in self.clauses) for d in docs) \
            if docs else bool(self.clauses or self.datasets or self.drafts)
        return 1 if read else 0

    def next_hint(self) -> str:
        """程序根据工作区判断的下一步（模型原地打转或步数用得多时提示）。"""
        docs = [f.name for f in self.inv.files if f.suffix.lower() in RULE_SUFFIXES]
        bare = [d for d in docs if not any(f.file == d for f in self.facts)
                and d not in {str(i.get("ref", "")) for i in self.ignored}]
        if not self.datasets:
            step = f"用 define_dataset 定义数据表（数据文件：{'、'.join(f.name for f in self.data_files) or '无'}）"
        elif not self.schemes:
            step = ("共用部分已保存，接着用 set_rules(scheme=方案名) 写每个方案自己的 pool（和需要的 allocation、payout），"
                    "不要再重复提交共用部分" if SHARED in self.drafts else
                    "用 set_rules 写入规则（笔记和参数台账里已有的内容不必重读）")
        elif any(s in self.untested or s not in self.results for s in self.schemes):
            step = "用 trial_run 试算"
        elif bare:
            step = f"用 record_facts 登记这些文档的参数：{'、'.join(bare)}"
        elif self._clause_gaps({str(i.get("ref", "")) for i in self.ignored}):
            step = "用 record_clauses / cover_clauses 把条款清单逐条落实"
        elif self._verify_gaps():
            step = "用 verify 独立验算：" + "；".join(self._verify_gaps()[:3])
        else:
            step = "核对后调用 finish"
        return step + (f"；还没登记参数的规则文档：{'、'.join(bare)}" if bare and "record_facts" not in step else "")

    def state(self) -> str:
        """每轮附在请求末尾的工作区摘要。"""
        lines = ["【当前工作区状态】" + (f"（{self.budget}）" if self.budget else "")]
        k = self.phase()
        lines.append(f"- 当前阶段 {k + 1}/{len(PHASES)}：{PHASES[k][0]}。要做：{PHASES[k][1]}。完成标准：{PHASES[k][2]}")
        used, limit = self.progress
        if limit and used >= limit * 0.3:
            lines.append(f"- 已用 {used}/{limit} 步，下一步（程序判断）：{self.next_hint()}")
        if self.notes:
            lines.append("- 笔记：")
            lines += [f"  · {k}：{v}" for k, v in self.notes.items()]
        if self.datasets:
            for n, d in self.datasets.items():
                m = self.mapping[n]
                kind = (f"key={d.key}" + (f", link={d.link}" if d.link else "")) if d.key else f"link={d.link}" if d.link else "公司级"
                flt = f" 筛选 {m.filter}" if m.filter else ""
                lines.append(f"- 数据表「{n}」← {m.file}/{m.sheet}{flt}（{kind}）字段：{', '.join(d.fields)}")
        else:
            lines.append("- 尚未定义数据表")
        if self.drafts:
            for s, text in _rules_briefs(self.drafts).items():
                title = "共用部分（合并进每个方案）" if s == SHARED else f"方案「{s}」"
                lines.append(f"- {title}规则摘要（不含出处；修改时按名字只传要改的部分）：\n" + text)
        else:
            lines.append("- 尚未写入规则")
        if self.facts:
            by = {}
            for f in self.facts:      # 带上取值：清空对话后不用为了看数值重读材料
                by.setdefault(f.file, []).append(f"{f.name}={str(f.value)[:40]}" if len(self.facts) <= 150 else f.name)
            lines.append(f"- 参数台账 {len(self.facts)} 条（再登记同一参数请用相同的名字）：")
            lines += [f"  · {k}：{'、'.join(dict.fromkeys(v))}" for k, v in by.items()]
        if self.clauses:
            left = sum(1 for c in self.clauses if not c.where)
            lines.append(f"- 条款清单 {len(self.clauses)} 条" + (f"（{left} 条还没落实）" if left else "") + "，逐条对照，不要遗漏：")
            done = [c for c in self.clauses if c.where]
            if left and done:      # 已落实的只列位置（原文和要点在 cover_clauses 的核对结果里看过），没落实的列全
                lines.append("  已落实：" + "；".join(f"{c.id}→{c.where[:40]}" for c in done))
                lines += [f"  · {c.id} [{c.file}] {c.point} → 未落实" for c in self.clauses if not c.where]
            elif left:
                lines += [f"  · {c.id} [{c.file}] {c.point} → 未落实" for c in self.clauses]
            else:
                lines += [f"  · {c.id} {c.point[:40]} → {c.where[:40]}" for c in self.clauses]
        else:
            lines.append("- 条款清单：还没有。读完每份规则文档就用 record_clauses 逐条摘出计算条款")
        if self.questions:
            lines.append("- 问题：")
            lines += [f"  · {q.id} {q.text} → " + (f"财务答复：{q.answer}" if q.answer else f"已撤回：{q.withdrawn}" if q.withdrawn
                                          else "等待答复" if q.blocking else "提示")
                      for q in self.questions]
        if self.transcripts:
            names = list(self.transcripts)
            lines.append(f"- 已查看图片：{', '.join(names)}" if len(names) <= 12
                         else f"- 已查看图片 {len(names)} 张（{', '.join(names[:6])} 等）")
        for s in self.schemes:
            r = self.results.get(s, {})
            lines.append(f"- 试算「{s}」：" + ("最近修改后尚未试算" if s in self.untested or not r else
                                             f"已试算，奖金池 {fmt(Decimal(r['R0'].pool))}" + (f"，修订 {list(r)[1:]}" if len(r) > 1 else "")))
        if self.schemes and all(s not in self.untested and s in self.results for s in self.schemes):
            left = self._verify_gaps()
            lines.append("- 待独立验算（程序指定，全部通过才能 finish）：" + ("；".join(left[:12]) if left else "全部通过"))
        if self.check_vars:
            pending = bool(self.schemes) and bool(self._verify_gaps())
            lines.append("- 验算辅助变量（已定义，verify 里直接引用，不必再写进 helpers；要改哪个就在 helpers 里重写哪个）："
                         + ("\n" + "\n".join(f"  · {k}[{v.get('scope', 'global')}] = {_clip(v.get('expr', ''), 160)}"
                                              for k, v in self.check_vars.items()) if pending
                            else "、".join(self.check_vars)))
        return "\n".join(lines)


def _tier_text(name: str, t: dict) -> str:
    """把阶梯表翻译成一句话，便于和材料原文对照（如第一档误写成 [0, 0] 时能看出来）。"""
    try:
        steps = [(Decimal(str(a)), Decimal(str(b))) for a, b in t.get("steps") or []]
    except (InvalidOperation, TypeError, ValueError):
        return ""
    excess = t.get("mode") == "excess"
    parts = []
    for i, (lo, r) in enumerate(steps):
        hi = steps[i + 1][0] if i + 1 < len(steps) else None
        num = lambda x: f"{x:,.0f}" if x == x.to_integral_value() else f"{x.normalize():f}"   # noqa: E731  比例档（0.15）不能显示成 0
        rng = f"{num(lo)} 至 {num(hi)}" if hi is not None else f"{num(lo)} 以上"
        parts.append(f"{rng}{'的部分' if excess else '时全部金额'}按 {(r * 100).normalize():f}%")
    return f"{name}（{'超额累进' if excess else '全额累进'}）：" + "；".join(parts)


def _strip_tags(text: str, name: str) -> str:
    """材料包里每段前的 [文件名 · text] 标记去掉文件名（标题已写明）；只剩 text 的整行删掉，页码和表格、图片标记保留。"""
    def sub(m):
        rest = m.group(1).removeprefix(name).strip()
        kind = m.group(2)
        label = " · ".join(x for x in (rest, "" if kind == "text" else kind) if x)
        return f"[{label}]\n" if label else ""
    return re.sub(r"^\[([^\]\n]*?) · (\w+)\]\n", sub, text, flags=re.M)


def _json_data_summary(path: Path) -> str:
    """大 JSON 数据文件的结构摘要：各数组的条数、字段和第一条记录。不是 JSON 或不是数据时返回空。"""
    if path.suffix.lower() != ".json":
        return ""
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    arrays = ({k: v for k, v in obj.items() if isinstance(v, list)} if isinstance(obj, dict)
              else {"（顶层）": obj} if isinstance(obj, list) else {})
    if not arrays:
        return ""
    lines = ["结构摘要（程序生成）："]
    for k, v in arrays.items():
        first = v[0] if v else None
        cols = list(first) if isinstance(first, dict) else []
        lines.append(f"- {k}：{len(v)} 条" + (f"，字段 {cols}" if cols else "")
                     + (f"，第一条 {json.dumps(first, ensure_ascii=False)[:300]}" if first is not None else ""))
    return "\n".join(lines)


def _revisions(revs, strict: bool = False) -> list[dict]:
    """修订版本：[{"name": "R1", "adjust": [{"node": "团队:T01", "amount": 900000, "reason": ...}]}, ...]。
    后一个修订包含前面所有修订的调整（逐步累加），R0 是不做调整的基准。
    没有调整的修订和 R0 一样：strict（set_rules 校验）时报错，否则（旧案卷里存的）直接跳过。"""
    out = []
    for r in revs or []:
        if not isinstance(r, dict) or not r.get("name") or not isinstance(r.get("adjust"), list):
            raise ValueError('revisions 的每一项应为 {"name": "R1", "adjust": [{"node": "团队:T01", "amount": 900000}]}')
        if r["name"] == "R0":
            raise ValueError("R0 是不做调整的基准结果，修订从 R1 开始命名")
        if not r["adjust"]:
            if not strict:
                continue
            raise ValueError(f"修订 {r['name']} 没有任何调整，和 R0 完全一样。只有材料里写了签批调整（某个节点改成多少）"
                             "才建修订；没有调整就不要传 revisions，结果就是 R0")
        for a in r["adjust"]:
            if not isinstance(a, dict) or "node" not in a or "amount" not in a:
                raise ValueError(f"修订 {r['name']} 的调整项应为 {{\"node\": 节点 id, \"amount\": 金额}}，得到 {a}")
        out.append(r)
    return out


def _rules_lines(draft: dict) -> list[tuple[str, str]]:
    """规则摘要的每一行：(规则元素位置, 行)。位置就是 cover_clauses 里写的那个（如 variables.x、payout.y）。"""
    out = []
    for n, t in (draft.get("tables") or {}).items():
        if not isinstance(t, dict):
            continue
        if t.get("kind") == "tier":
            text = _tier_text(n, t) or f"{n}：阶梯 {json.dumps(t.get('steps'), ensure_ascii=False)}"
        else:
            text = f"{n}：查找表 {json.dumps(t.get('mapping'), ensure_ascii=False)}" + (
                f"，查不到时取 {t.get('default')}" if t.get("default") is not None else "，查不到时报错")
        out.append((f"tables.{n}", text))
    for n, v in (draft.get("variables") or {}).items():
        out.append((f"variables.{n}", f"{n}[{(v or {}).get('scope', 'global')}] = {(v or {}).get('expr')}"))
    if draft.get("pool"):
        p = draft["pool"]
        out.append(("pool", f"奖金池[{p.get('scope', 'global')}] = {p.get('expr')}"))
    for lv in draft.get("allocation") or []:
        if isinstance(lv, dict):
            out.append((f"allocation.{lv.get('level')}", f"分配层 {lv.get('level')}（key={lv.get('key')}, parent_key={lv.get('parent_key')}）"
                                                         f"权重 = {lv.get('weight') or '（奖金包所在层，不需要）'}"))
    for p in draft.get("payout") or []:
        if isinstance(p, dict):
            what = f"可发比例 = {p['pay_ratio']}" if p.get("pay_ratio") else f"暂缓比例 = {p.get('hold_ratio')}"
            out.append((f"payout.{p.get('id')}", f"发放 {p.get('id')}[{p.get('scope')}] {what}"))
    for o in draft.get("offsets") or []:
        if isinstance(o, dict):
            out.append((f"offsets.{o.get('id')}", f"抵扣 {o.get('id')}[{o.get('scope')}] 已发金额 = {o.get('expr')}"))
    for c in draft.get("checks") or []:
        if isinstance(c, dict):
            out.append((f"checks.{c.get('id')}", f"校验 {c.get('id')}[{c.get('scope', 'global')}]：{c.get('expr')}"))
    for x in draft.get("patches") or []:
        if isinstance(x, dict):
            body = {k: v for k, v in x.items() if k not in ("source", "desc", "sources") and v}
            out.append((f"patches.{x.get('source')}", f"数据修订 {x.get('source')}：{json.dumps(body, ensure_ascii=False)}"))
    for r in draft.get("revisions") or []:
        if isinstance(r, dict):
            out.append((f"revisions.{r.get('name')}", f"修订 {r.get('name')}："
                        + "，".join(f"{a.get('node')}={a.get('amount')}" for a in r.get("adjust") or [] if isinstance(a, dict))))
    return [(k, f"  {k}  {t}") for k, t in out]


def _rules_briefs(drafts: dict, limit: int = 6000) -> dict[str, str]:
    """各方案的规则摘要；和前面某个方案完全相同的行只列名字，不重复全文（每轮都附在状态里，省 tokens）。"""
    seen: dict[str, str] = {}
    out = {}
    for s, d in drafts.items():
        keep, same = [], {}
        for key, line in _rules_lines(d):
            if s != SHARED and seen.get(line, s) != s:
                same.setdefault(seen[line], []).append(key)
            else:
                seen.setdefault(line, s)
                keep.append(line)
        keep += [f"  与「{x}」相同（不重复列出）：{'、'.join(names)}" for x, names in same.items()]
        text = "\n".join(keep)
        out[s] = text if len(text) <= limit else text[:limit] + "……"
    return out


# ---------------- 主循环 ----------------
def system_prompt() -> str:
    dsl = prompt("extract_rules")
    dsl = dsl[dsl.index("# 规则 DSL") + len("# 规则 DSL"):dsl.index("# 数据字典")]
    dsl = dsl.replace("数据字典（datasets）已固定，见下文，你**只能引用其中的字段名**，不能自造字段。你需要输出：",
                      "表达式只能引用你用 define_dataset 定义过的字段名。规则包括：")
    dsl = dsl.replace("{functions}", describe_functions())
    return prompt("agent").replace("{dsl}", dsl.strip())


JSON_PROTOCOL = """
# 回复格式（本模型不使用原生工具调用）
每次只输出一个 JSON 对象，不要输出其他文字：{"thought": "简短的思考（1～3 句）", "tool": "工具名", "args": {参数}}
可用工具（JSON Schema）：
"""


@dataclass
class AgentResult:
    ws: Workspace
    steps: list[Step]
    ruleset: RuleSet | None
    result: Result | None
    error: str = ""
    outputs: dict[str, Path] = field(default_factory=dict)
    quote_hits: int = 0
    quote_total: int = 0
    recheck: str = ""
    rulesets: dict[str, RuleSet] = field(default_factory=dict)          # 方案 -> 规则
    pending: list[dict] = field(default_factory=list)                   # 尚未答复的问题
    draft_results: dict[str, dict[str, Result]] = field(default_factory=dict)   # 有问题未答复时的试算结果（不输出）
    round: int = 1
    changes: dict | None = None                                         # 本轮相对上一轮的文件变化
    diff_rows: list[list] = field(default_factory=list)                 # 与上一轮逐个对象的金额对比
    rule_changes: list[str] = field(default_factory=list)
    compared: bool = False                                              # 上一轮有结果，做了金额对比
    results: dict[str, dict[str, Result]] = field(default_factory=dict)  # 方案 -> 修订 -> 结果
    reply: str = ""                                                      # 对话时给财务的回复
    deep_steps: int = 0                                                  # 本轮用了几步深度思考

    @property
    def single(self) -> bool:
        """只有一个默认方案、没有修订：输出文件和以前一样（rules.yaml、result.xlsx）。"""
        return list(self.results) == [DEFAULT_SCHEME] and list(self.results[DEFAULT_SCHEME]) == ["R0"]

    @property
    def ok(self) -> bool:
        return self.ws.done and self.result is not None

    @property
    def questions(self) -> list[Question]:
        return self.ws.questions

    @property
    def status(self) -> str:
        """OK：已算出，可交财务复核；NEEDS_CONFIRMATION：有问题等财务答复，本轮不出结果；FAILED：未完成。"""
        if self.ws.done and self.pending:
            return "NEEDS_CONFIRMATION"
        return "OK" if self.ok else "FAILED"


def _size(ms: list[dict]) -> int:
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in ms)


def _is_result(m: dict) -> bool:
    return m["role"] == "tool" or (m["role"] == "user" and str(m.get("content", "")).startswith("【") and "的结果】" in
                                   str(m.get("content", "")).split("\n", 1)[0])


def _shrink(history: list[dict], max_chars: int, keep: int = KEEP_FULL_RESULTS) -> bool:
    """历史超过 max_chars 时整理一次（原地修改），返回是否整理过。

    先把较早的工具结果改成摘要、较早的长参数省略；仍超过上限的一半，就从最早的一轮开始丢弃，
    直到不超过一半，留出空间让之后多轮只追加不改动（前缀不变，缓存可以命中）。
    丢弃时保证剩下的历史以 assistant 消息开头（工具结果不落单）。"""
    if _size(history) <= max_chars:
        return False
    results = [i for i, m in enumerate(history) if _is_result(m)]
    cut = results[-keep] if len(results) >= keep else 0
    for i in results[:-keep] if len(results) > keep else []:
        c = history[i]["content"]
        if len(c) > BRIEF_CHARS + 60:
            history[i] = {**history[i], "content": c[:BRIEF_CHARS] + "\n……（较早的结果已省略。关键信息应已记在笔记里，确需原文可重新调用）"}
    for i, m in enumerate(history[:cut]):
        for tc in m.get("tool_calls") or []:
            if len(tc["function"]["arguments"]) > 600:
                tc["function"]["arguments"] = json.dumps({"省略": "较早的参数已省略，当前规则见工作区状态"}, ensure_ascii=False)
    starts = [i for i, m in enumerate(history) if m["role"] == "assistant"]
    for i in starts:
        if _size(history[i:]) <= max_chars // 2:
            del history[:i]
            return True
    if starts:
        del history[:starts[-1]]
    return True


def _answers_text(answers: list[dict] | None) -> str:
    done = [a for a in answers or [] if a.get("answer") not in (None, "")]
    if not done:
        return ""
    lines = ["\n财务已确认的答复（必须按此处理，不要再就这些问题提问）："]
    for a in done:
        ans = a["answer"]
        if isinstance(ans, int) and 0 <= ans < len(a.get("options") or []):
            ans = a["options"][ans]
        lines.append(f"- {a.get('text', '')} → {ans}")
    return "\n".join(lines)


def run_agent(llm: LLM, paths: list[str | Path], out_dir: str | Path | None = None, *,
              answers: list[dict] | None = None, max_steps: int = 150, max_history_chars: int = 40000,
              max_files: int = MAX_FILES, max_tokens: int | None = 4_000_000, exclude: list[str | Path] = (),
              on_step: Callable[[int, Step], None] | None = None, resume: bool = True,
              should_stop: Callable[[], bool] | None = None, instruction: str | None = None,
              message: str | None = None, by: str = "", chat: list[dict] | None = None,
              reflect: int = MAX_REFLECT, deep: int = DEEP_LIMIT) -> AgentResult:
    """max_steps 和 max_tokens 都是安全上限：模型调用 finish 就结束，正常不会用满。
    reflect：卡住时在本轮内反思、换做法的次数上限（清掉打转的对话历史，带上卡在哪、试过什么，工作区保留），用完再停下。
    deep：按需深度思考的步数上限。平时不思考；上一步出错、被拦下、原样重复或停下来反思后，下一步先分析错在哪、
    该怎么改（附一段分析提示，模型支持时同时打开思考），成功了就切回普通模式。0 表示不用。

    out_dir 是案卷目录：里面已有上一轮的 case.json 时（resume=True），从上一轮的工作区接着做，
    财务答复默认读取案卷里的 questions.yaml。"""
    try:
        files = collect_files(paths, exclude=([out_dir] if out_dir else []) + list(exclude), max_files=max_files)
    except (TooManyFiles, FileNotFoundError) as e:
        raise ToolError(str(e)) from None
    if not files:
        raise ToolError("没有找到可处理的文件（支持 .docx .pptx .pdf .xlsx .xlsm .png .jpg .txt .md .json .csv）")
    ctx = llm.context_window()
    if ctx is not None and ctx < MIN_CONTEXT:
        raise ToolError(f"模型服务的上下文长度只有 {ctx} tokens（max_model_len），智能体每一步的输入就有约 4～5 万 tokens，"
                        f"至少要 {MIN_CONTEXT}，建议 131072。请管理员调大模型服务的上下文长度（vLLM 启动参数 --max-model-len；"
                        "超过模型原生长度的要同时开 YaRN），再重试")
    ws = Workspace(llm, build_inventory(files))
    out = Path(out_dir) if out_dir is not None else None
    case = cs.load_case(out) if out is not None else None      # --new 也保留历次记录，只是不接着上一轮的工作区
    resumed = bool(case and resume and case.get("workspace"))
    prints = cs.fingerprint(files)
    rnd = cs.new_round(case, prints)
    ws.round = rnd["n"]
    if answers is None and resumed and (out / "questions.yaml").exists():
        answers = yaml.safe_load((out / "questions.yaml").read_text(encoding="utf-8")) or []
    last = case["rounds"][-1] if case and case.get("rounds") else None
    changes = None
    if resumed:
        cs.restore(ws, case["workspace"])
        changes = cs.file_changes(last["files"] if last else {}, prints)
        gone = set(changes["修改"]) | set(changes["删除"])
        ws.facts = [f for f in ws.facts if f.file not in gone]       # 这些文件的参数要按新内容重新登记
        ws.clauses = [c for c in ws.clauses if c.file not in gone]   # 条款也按新内容重新摘
        withdrawn = fx.withdraw_stale(ws.facts, ws.conflicts, ws.questions)
    else:   # 首轮就带了答复（如上一次单轮运行的 questions.yaml）：作为已答复的问题放进工作区
        ws.questions = [Question(id=a.get("id") or f"q{i + 1}", text=a["text"], options=a.get("options") or [])
                        for i, a in enumerate(answers or []) if a.get("text") and a.get("answer") not in (None, "")]
    fresh = cs.apply_answers(ws.questions, answers)
    ws.said = _said(chat, message, instruction)
    ws.notes.pop(ANSWER_NOTE, None)          # 上一轮的回答不带到这一轮
    handlers = {t["name"]: getattr(ws, t["name"]) for t in TOOLS}
    ws.transcribe_all()
    ws.triage_all()
    pack = ("\n\n【材料包】以下是本批次材料的内容（程序读取，图片已转写），每轮都附在这里、不会被裁剪。"
            "标了“全文 / 整表”的不要再用工具读；数据表只附了前几行，define_dataset 会读取全部行。\n\n" + ws.briefing())
    if message:             # 对话：财务的一条消息。先按现有规则试算一遍，便于回答和查算式
        ws.chat_by = by or "财务"
        for s in ws._order(ws.schemes):
            try:
                ws._trial(s)
            except Exception:
                pass
        task = _chat_task(ws, message, chat, fresh, (changes if resumed else None))
    elif resumed:
        task = _resume_task(ws, last, changes, fresh, withdrawn)
    else:
        task = ("请完成本批次的奖金计算。文件清单：\n" + ws.list_files() + _answers_text(answers)
                + "\n\n完成后调用 finish。")
    if instruction and not message:      # 上传材料时附带的说明
        task += (f"\n\n财务附的说明（必须照做；不明确或与材料冲突时用 ask_question 提问）：{instruction}"
                 f"\n说明里如果有向你提的问题：先查清楚（金额和算式来自 explain / evaluate，不要自己算），"
                 f"用 note 记下回答，topic 写「{ANSWER_NOTE}」，text 写给财务看的回答；然后接着完成这一轮。没有提问就不用记")
    task += pack
    mode = llm.tool_mode
    sys = system_prompt()
    history: list[dict] = []
    steps: list[Step] = []
    error = ""
    nudges = 0
    seen: dict[str, int] = {}          # 只读工具的调用 -> 第几步，用来发现原地打转
    repeats = 0
    last_write: tuple[str, str] | None = None
    fails: dict[str, int] = {}         # 工具 -> 连续失败次数
    fail_why: dict[str, str | None] = {}   # 工具 -> 上次失败的原因（报错第一行的开头）
    stuck = ""
    undo: dict[tuple[str, str], int] = {}
    # 同一个调用（参数、结果都一样）自上次有新进展以来出现了几次：几个调用轮流原样重复（如 4 个验算轮流提交、每次同样报错）时，
    # 相邻两次并不相同，上面按“连续”计的检测都发现不了，按次数认
    again: dict[tuple[str, str], int] = {}
    same_block, prev_full, long_block, prev_block = 0, set(), 0, set()
    reflected = 0
    idle = 0                           # 连续只查询、没有改动工作区的次数（参数每次不同，上面的重复检测发现不了）
    reached = ws.phase()               # 本轮到过的最远阶段：往前推进到新阶段时清空对话历史；改规则退回前面的阶段再回来不清
    deep_why, deep_used = "", 0        # 下一步要深度思考的原因（空：普通模式）；本轮已用的深度思考步数
    for turn in range(max_steps):
        if should_stop and should_stop():
            error = f"已手动停止（第 {len(steps)} 步）"
            break
        used = llm.usage.prompt_tokens + llm.usage.completion_tokens
        if max_tokens and used >= max_tokens:
            error = f"tokens 用量 {used} 达到上限 {max_tokens}，已停止"
            break
        ws.progress = (len(steps), max_steps)
        ws.budget = f"已用 {len(steps)} 步，上限 {max_steps} 步" + (f"；tokens 已用 {used}，上限 {max_tokens}" if max_tokens else "")
        if _shrink(history, max_history_chars):
            seen.clear()       # 较早的结果已省略，重新读取是合理的
        think = bool(deep_why) and deep_used < deep
        if think:
            deep_used += 1
        tail = [{"role": "user", "content": ws.state() + (_deep_note(deep_why, steps) if think else "")}]
        # 缓存标记：系统提示 + 任务固定不变；历史在两次整理之间只追加，标在历史最后一条上
        body = [{"role": "user", "content": task, "_cache": True}] + history[:-1] \
            + ([{**history[-1], "_cache": True}] if history else [])
        if mode == "json":
            msgs = [{"role": "system", "content": sys + JSON_PROTOCOL + json.dumps(TOOLS, ensure_ascii=False)}] + body + tail
            try:
                text = llm.chat(msgs, json_out=True, temperature=AGENT_TEMPERATURE, **({"think": True} if think else {}))
            except LLMError as e:
                error = str(e)
                break
            reasoning = getattr(llm, "last_reasoning", "") if think else ""
            history.append({"role": "assistant", "content": _short_thought(text)})
            try:
                act = parse_json(text)
                a = act.get("args") or {}
                if isinstance(a, dict) and act.get("thought") and not a.get("thought"):
                    a = {"thought": act["thought"], **a}
                calls = [{"id": "", "name": act["tool"], "arguments": json.dumps(a, ensure_ascii=False)}]
            except (LLMError, KeyError, TypeError, ValueError):
                history.append({"role": "user", "content": '格式不对：请只输出 {"thought": ..., "tool": ..., "args": {...}}'})
                steps.append(Step("（格式不对）", {"_raw": (text or "")[:20000]}, "回复不是 {thought, tool, args} 格式，未执行", False))
                if on_step:
                    on_step(len(steps), steps[-1])
                continue
        else:
            msgs = [{"role": "system", "content": sys}] + body + tail
            try:
                reply = llm.chat_tools(msgs, openai_tools(), temperature=AGENT_TEMPERATURE, **({"think": True} if think else {}))
            except LLMError as e:
                if turn == 0:  # 模型或服务不支持原生工具调用，改用 JSON 协议
                    mode = "json"
                    continue
                error = str(e)
                break
            calls = [{**c, "raw": c["arguments"], "arguments": _fix_arguments(c["arguments"])} for c in reply["tool_calls"]]
            reasoning = (reply.get("reasoning") or "") if think else ""
            history.append({"role": "assistant", "content": reply["content"],
                            **({"reasoning_content": reasoning} if reasoning else {}),
                            **({"tool_calls": [{"id": c["id"], "type": "function",
                                                "function": {"name": c["name"], "arguments": _short_thought(c["arguments"])}}
                                               for c in calls]} if calls else {})})
            if not calls:
                nudges += 1
                if nudges > 3:
                    error = "模型多次没有调用工具"
                    break
                history.append({"role": "user", "content": "请调用工具继续工作；全部完成后调用 finish。"})
                continue
        turn_start, skipped = len(history) - 1, 0     # 本轮 assistant 消息的位置；被拦下的重复调用数
        turn_steps = len(steps)                       # 本轮第一步的位置：深度思考的内容记在它上面，并据本轮结果决定下一步是否思考
        failed_now: set[str] = set()                  # 这次回复里已经算过一次失败的工具
        said = "" if mode == "json" else (reply.get("content") or "").strip()     # 原生工具调用时模型写在正文里的思考
        for c in calls:
            args, note, dup = {}, "", False
            thought, bare = _split_thought(c["arguments"])
            thought = thought or said
            sig = f"{c['name']}:{bare}"          # 思考不同、参数相同，仍是同一个调用
            if c["name"] in READ_ONLY and sig in seen:
                repeats += 1
                stuck = f"反复调用 {c['name']}（参数相同，内容没有变化）"
                result = (f"这次调用与第 {seen[sig]} 步完全相同，内容没有变化，已跳过。不要重复调用；"
                          f"需要更多内容请换参数（如 start_row、offset），否则根据已有信息进行下一步。"
                          f"程序判断的下一步：{ws.next_hint()}")
                steps.append(Step(c["name"], {}, result, False, thought))
                if on_step:
                    on_step(len(steps), steps[-1])
                history.append({"role": "user", "content": f"【{c['name']} 的结果】\n{result}"} if mode == "json"
                               else {"role": "tool", "tool_call_id": c["id"], "content": result})
                skipped += 1
                continue
            try:
                args = json.loads(c["arguments"] or "{}")
                fn = handlers.get(c["name"])
                if not isinstance(args, dict):
                    raise ToolError("参数必须是 JSON 对象")
                if "_invalid_json" in args:
                    raise ToolError(args["_invalid_json"])
                args.pop("thought", None)
                note = args.pop("_note", "")
                args = _coerce_args(c["name"], args)
                note = "\n".join(x for x in (note, args.pop("_fixed", "")) if x)
                if fn is None:
                    raise ToolError(f"没有工具 {c['name']}，可用：{list(handlers)}")
                result, ok = str(fn(**args)) + (f"\n{note}" if note else ""), True
            except ToolError as e:
                result, ok = f"错误：{e}", False
            except json.JSONDecodeError as e:
                result, ok = f"错误：参数不是合法 JSON：{e}", False
            except TypeError as e:
                result, ok = f"错误：参数不对：{_missing_args(c['name'], e)}", False
            except (AttributeError, KeyError, IndexError, ValueError) as e:     # 参数结构和工具说明不一致（如对象写成了列表）
                result, ok = (f"错误：参数结构不对（{type(e).__name__}: {e}）。请按工具说明的格式重写："
                              "要求对象 {...} 的地方不要写成列表 [...]，反之亦然"), False
            if len(result) > MAX_RESULT_CHARS:
                result = result[:MAX_RESULT_CHARS] + "……（已截断）"
            if not thought:
                result += "\n（提醒：每次调用都先在 thought 里写思考：上一步观察到了什么、为什么做这一步、预期看到什么。）"
            step = Step(c["name"], args if isinstance(args, dict) else {}, result, ok, thought)
            if c.get("raw") and c["raw"] != c["arguments"]:     # 模型的原文（格式有误、程序改过或没能执行）留档，便于做回放用例
                step.args = {**step.args, "_raw": c["raw"][:20000]}
            if ok and c["name"] in WRITE_TOOLS:
                ws.changed = True
            if (c["name"] not in READ_ONLY or not ok) and (sig, result) != last_write:   # 紧挨着的原样重复下面另有处理
                key = (sig, result[:300])
                if ok and c["name"] not in READ_ONLY and key not in again:
                    again.clear()              # 写入类工具得到了新的结果：有进展，重新计
                again[key] = again.get(key, 0) + 1
                if again[key] >= MAX_STUCK:
                    repeats = MAX_STUCK
                    stuck = (f"同一个 {c['name']} 调用原样提交了 {again[key]} 次（中间夹着别的调用），结果每次都一样："
                             f"{result.removeprefix('错误：')[:300]}")
                elif again[key] == MAX_STUCK - 1:
                    result += (f"\n注意：这个调用和之前的一次完全相同，结果也一样，原样再提交不会有变化。"
                               f"按上面的结果换一种写法或做法；再原样提交一次就算卡住。下一步（程序判断）：{ws.next_hint()}")
                    step.result = result
            if c["name"] in READ_ONLY and ok:
                seen[sig] = len(steps) + 1
            elif c["name"] not in READ_ONLY:
                if (sig, result) == last_write:      # 写入类工具原样重复、结果也一样：同样算原地打转
                    repeats += 1
                    stuck = f"反复提交相同的 {c['name']}，结果：{result[:300]}"
                    dup = ok
                last_write = (sig, result)
                seen.clear()   # 工作区变了，重新读取是合理的
                if dup:
                    # 成功的一步原样又做了一遍：不把它留在历史里（小模型会照着历史接着重复），改成一句说明和程序判断的下一步
                    result = (f"这一步和上一步的 {c['name']} 完全相同，结果也一样：上一步已经生效，不需要再提交。"
                              f"下一步（程序判断）：{ws.next_hint()}")
                    step.result = result
                    skipped += 1
            # 同一工具因为同一个原因连续失败才算卡住；原因变了说明在往前改（如 finish 先后被不同的检查拦下）
            # 原因相同还要是同一个对象（如同一张表）：先后把三张表各写错一次是三个问题，不是在原地打转；
            # 同一次回复里并排的几个调用，模型还没看到前一个的结果，只算一次
            what = str((args or {}).get("name") or (args or {}).get("node") or (args or {}).get("scheme") or "") \
                if isinstance(args, dict) else ""
            why = re.sub(r"\d+", "", result.removeprefix("错误：").split("\n", 1)[0])[:15] + "|" + what
            if ok:
                fails[c["name"]] = 0
            elif c["name"] not in failed_now:
                fails[c["name"]] = fails.get(c["name"], 0) + 1 if fail_why.get(c["name"]) == why else 1
                failed_now.add(c["name"])
            fail_why[c["name"]] = None if ok else why
            if ok and c["name"] in ("remove_dataset", "remove_facts"):     # 建了又删、删了又建：没有进展的来回改
                what = str((args or {}).get("name", ""))
                undo[(c["name"], what)] = undo.get((c["name"], what), 0) + 1
                if undo[(c["name"], what)] >= MAX_STUCK:
                    repeats = MAX_STUCK
                    stuck = f"「{what}」反复建了又删（{undo[(c['name'], what)]} 次），没有进展"
                elif undo[(c["name"], what)] == MAX_STUCK - 1:
                    result += (f"\n注意：「{what}」已经删了 {MAX_STUCK - 1} 次。先想清楚它该怎么定义再动手，"
                               f"再删一次就算卡住；拿不准就用 ask_question 问财务")
                    step.result = result
            if ok and c["name"] == "set_rules":      # 保存了但阻断问题一直没改掉：同样算没有进展
                # 每次都改掉了几个（阻断问题在减少）是在推进，只是一次没改完，不算卡住；
                # 一个也没改掉才算原地打转。某一个问题一直挂着、别的在改，放宽到两倍次数再停
                now = getattr(ws, "last_blocking", set())
                same_block = same_block + 1 if now and prev_full and not (prev_full - now) else (1 if now else 0)
                stay = now & prev_block if prev_block else now
                long_block = long_block + 1 if now and stay and prev_block else (1 if now else 0)
                prev_full, prev_block = now, (stay if stay and long_block > 1 else now)
                if same_block >= MAX_STUCK or long_block >= 2 * MAX_STUCK:
                    repeats = MAX_STUCK
                    n = same_block if same_block >= MAX_STUCK else long_block
                    stuck = f"set_rules 连续 {n} 次保存后仍有同样的阻断问题，最后一次：{result[:300]}"
                elif same_block == MAX_STUCK - 1 or long_block == 2 * MAX_STUCK - 1:
                    result += ("\n注意：同样的阻断问题已经连续几次没改掉。再出现一次就算卡住；"
                               "如果是规则写法表达不了材料的要求，用 ask_question 问财务或说明卡在哪里")
                    step.result = result
            if fails[c["name"]] >= MAX_STUCK:
                stuck = f"{c['name']} 连续 {fails[c['name']]} 次失败，最后一次：{result[:300]}"
            if c["name"] in QUERY_TOOLS:
                idle += 1
                if idle >= 2 * IDLE_WARN:
                    repeats = MAX_STUCK
                    stuck = f"连续 {idle} 次只用 evaluate / explain 查询，没有改规则、验算或提问，工作区没有进展"
                elif idle == IDLE_WARN:
                    result += (f"\n注意：已经连续 {idle} 次只查询、没有任何改动。根据已查到的信息做决定："
                               "规则错了就 set_rules 改，验算式错了就重写 verify，两边都拿不准就 ask_question 把差异写给财务。"
                               f"再查 {IDLE_WARN} 次仍没有改动就算卡住。下一步（程序判断）：{ws.next_hint()}")
                    step.result = result
            elif c["name"] not in READ_ONLY and ok:
                idle = 0
            steps.append(step)
            if on_step:
                on_step(len(steps), step)
            if mode == "json":
                history.append({"role": "user", "content": f"【{c['name']} 的结果】\n{result}"})
            else:
                history.append({"role": "tool", "tool_call_id": c["id"], "content": result})
        if think:
            for st in steps[turn_steps:]:
                st.deep = True
            if reasoning and len(steps) > turn_steps:
                st = steps[turn_steps]
                st.thought = f"【深度思考】{reasoning.strip()[:DEEP_SHOW]}" + (f"\n{st.thought}" if st.thought else "")
        deep_why = _deep_reason(steps[turn_steps:], skipped, ws, fails)
        if skipped and skipped == len(calls):
            # 整轮都是被拦下的重复调用：不把它留在历史里，否则小模型会照着历史接着重复；只留一句说明
            del history[turn_start:]
            history.append({"role": "user", "content": f"（你刚才又原样调用了 {calls[0]['name']}，和之前完全相同，没有任何变化。"
                                                       f"不要再重复；下一步（程序判断）：{ws.next_hint()}）"})
        if ws.done:
            break
        now = ws.phase() if not message else reached
        if now > reached:
            # 进入新阶段：上一阶段的结论都已存进工作区（台账、条款、规则、试算、验算），对话历史里的读数、试错不再需要，
            # 清掉腾出上下文，让模型只看材料包和工作区状态专心做这一阶段（小上下文的模型尤其需要）
            reached = now
            note = _phase_note(ws, now)
            at = max((i for i, m in enumerate(history) if m["role"] == "assistant"), default=len(history))
            keep = history[at:]          # 刚完成的这一步（如试算结果和抽查）留着，接下来要看
            history.clear()
            history += [{"role": "user", "content": note}] + keep
            repeats, last_write, stuck, idle = 0, None, "", 0
            for d in (fails, fail_why, seen, undo, again):
                d.clear()
            same_block, prev_full, long_block, prev_block = 0, set(), 0, set()
            deep_why = ""
            steps.append(Step("（进入新阶段）", {}, note, True))
            if on_step:
                on_step(len(steps), steps[-1])
            continue
        if (repeats >= MAX_STUCK or any(n >= MAX_STUCK for n in fails.values())) and reflected < reflect:
            # 卡住了先不停：清掉原地打转的对话历史（小模型会照着历史接着重复，也白占上下文），
            # 只留卡在哪、试过什么、程序的判断，让智能体回到材料和数据重新想、换一种做法。工作区（数据表、规则、台账）都保留
            reflected += 1
            deep_why = f"卡住了：{stuck[:300]}"       # 反思后的第一步一定深度思考
            note = _reflection(ws, stuck, steps, reflected, reflect)
            history.clear()
            history.append({"role": "user", "content": note})
            repeats, last_write, stuck, idle = 0, None, "", 0
            for d in (fails, fail_why, seen, undo, again):
                d.clear()
            same_block, prev_full, long_block, prev_block = 0, set(), 0, set()
            steps.append(Step("（反思）", {}, note, True))
            if on_step:
                on_step(len(steps), steps[-1])
            continue
        if repeats >= MAX_STUCK or any(n >= MAX_STUCK for n in fails.values()):
            error = (f"智能体卡住了，已停止以免继续消耗 tokens：{stuck}。"
                     f"已用 {len(steps)} 步" + (f"，中途停下来反思、换做法 {reflected} 次仍没过去" if reflected else "")
                     + "。请把这条信息反馈给开发，或在对话里告诉智能体怎么处理")
            break
    else:
        error = f"达到最大步数 {max_steps} 仍未完成"

    if message and not ws.changed and not fresh and not any((changes or {}).values()):
        # 对话里只是问答、没有改动：不算新的一轮，不改案卷和输出
        ar = AgentResult(ws, steps, None, None, error, round=(last or {}).get("n", 0))
        ar.reply = ws.reply_text or (f"（没有完成）{failure.to_text(error)}" if error else "")
        return ar
    tested = {s: ws.results[s] for s in ws.schemes if s in ws.results and s not in ws.untested}
    rss = {s: ws._ruleset(s) for s in tested}
    draft = {}
    if ws.pending:          # 还有问题等答复：不出正式结果，试算结果只作草稿留在内存
        draft, tested = tested, {}
    first = next(iter(tested), None)
    ar = AgentResult(ws, steps, rss.get(first), tested[first]["R0"] if first else None, error,
                     rulesets={s: rss[s] for s in tested}, results=tested)
    ar.draft_results, ar.round, ar.changes = draft, rnd["n"], changes
    ar.deep_steps = deep_used
    ar.reply = ws.reply_text
    ar.pending = [q.model_dump() for q in ws.pending]
    if last and last.get("digest") is not None and tested:
        ar.diff_rows, ar.compared = cs.diff_results(last["digest"], tested), True
    if last and last.get("drafts") is not None:
        ar.rule_changes = cs.diff_rules(last["drafts"], ws.drafts)
    for rs in rss.values():
        h, t, _ = check_quotes(rs, ws.inv.docs)
        ar.quote_hits, ar.quote_total = ar.quote_hits + h, ar.quote_total + t
    if out is not None:
        ar.outputs = write_agent_outputs(ar, out, answers)
        rnd["inputs"] = [str(f) for f in ws.data_files]
        rnd["outputs"] = {k: [p.name, cs.sha256(p)] for k, p in ar.outputs.items()
                          if k in ("mapping", "rules", "review") or k.startswith(("rules:", "adjust:"))}
        if message:
            rnd["message"] = {"by": ws.chat_by, "text": message}
        rnd.update(status=ar.status, error=error, pending=[q.id for q in ws.pending], summary=ws.summary,
                   answered=[q.id for q in fresh], changes=changes, drafts=json.loads(json.dumps(ws.drafts)),
                   digest=cs.digest(tested) if tested else (last or {}).get("digest"))
        case = case or {"created": rnd["at"], "rounds": []}
        case["rounds"].append(rnd)
        case["workspace"] = cs.snapshot(ws)
        ar.outputs["case"] = cs.save_case(out, case)
        cs.archive(out, rnd["n"], ar.outputs)
    return ar


def _said(chat: list[dict] | None, message: str | None, instruction: str | None) -> str:
    """财务在对话里发过的全部文字（去重，按时间），作为可引用的来源「财务对话」。"""
    out: list[str] = []
    for t in [m.get("text", "") for m in chat or [] if m.get("role") == "user"
                                       and not str(m.get("text", "")).startswith("上传了期望结果")] + [message or "", instruction or ""]:
        t = str(t).strip()
        if t and t not in out:
            out.append(t)
    return "\n\n".join(out)


def _chat_task(ws: Workspace, message: str, chat: list[dict] | None, fresh: list[Question],
               changes: dict | None) -> str:
    lines = ["财务在对话中给你发了一条消息。工作区（数据表、规则、笔记、台账、问题）是上一轮的状态，已按现有规则试算过，"
             "可以直接用 explain / evaluate 查算式和数字。"]
    if chat:
        lines.append("\n之前的对话：")
        lines += [f"- {'财务' if m.get('role') == 'user' else '你'}：{m.get('text', '')[:500]}" for m in chat[-10:]]
    if fresh:
        lines.append("\n财务在问题表里的新答复（必须照做）：" + "；".join(f"{q.id} → {q.answer}" for q in fresh))
    if changes and any(changes.values()):
        lines.append("\n文件变化：" + "；".join(f"{k} {'、'.join(v)}" for k, v in changes.items() if v)
                     + "（新增、修改的规则文档要读并 record_facts 登记参数、record_clauses 摘条款）")
    lines += ["", f"财务的消息：{message}", "",
              "请：",
              "1. 是提问：查清楚后用 reply 回答，金额和算式来自 explain / evaluate，不要自己算；",
              "2. 是要求修改：照做（set_rules、define_dataset 等），他的话本身就是依据；要求不明确、和材料冲突、"
              "或缺信息时不要猜，用 reply 反问；",
              "3. 他明确答复了某个待确认问题：用 answer_question 记下原话，再按答复改规则；",
              "4. 最后调用 reply 结束。改过东西时程序会自动试算并检查，不通过会告诉你原因。",
              "", "当前文件清单：", ws.list_files()]
    return "\n".join(lines)


def _resume_task(ws: Workspace, last: dict | None, changes: dict | None, fresh: list[Question],
                 withdrawn: list[Question] = ()) -> str:
    n = ws.round
    lines = [f"这是同一笔奖金的第 {n} 轮处理（上一轮状态：{(last or {}).get('status', '未知')}）。上一轮的数据表、列映射、规则、"
             "笔记和问题都已恢复到工作区（见工作区状态），没有变化的材料不必重读。", "", "本轮变化："]
    if fresh:
        lines.append("- 财务新答复（必须照做）：")
        lines += [f"  · {q.id} {q.text} → {q.answer}" for q in fresh]
    for k, v in (changes or {}).items():
        if v:
            lines.append(f"- {k}文件：{'、'.join(v)}")
    if withdrawn:
        lines.append("- 程序撤回的问题（冲突所依据的材料已修改，请按新内容重新登记参数）：" + "；".join(q.id for q in withdrawn))
    if not fresh and not any((changes or {}).values()):
        lines.append("- 没有新答复，也没有文件变化")
    if last and last.get("status") == "FAILED" and last.get("error"):
        lines += ["", "上一轮没做完，停在这里（这一轮先针对这个原因换做法，不要原样重复上一轮的写法）：",
                  "  " + str(last["error"])[:600]] + [f"  · {t}" for t in _lessons(str(last["error"]))]
    if ws.pending:
        lines.append("- 仍在等待答复：" + "；".join(f"{q.id} {q.text}" for q in ws.pending))
    lines += ["", "请：",
              "1. 按新答复修改规则（答复只影响它相关的部分，其余保持不变）；",
              "2. 阅读新增、修改的文件，用 record_facts 登记其中的参数（与已有台账同一参数用相同的名字）、record_clauses 摘出条款"
              "（修改过的文件，原来的条款已删除，要重新摘），"
              "取值不一致时程序会自动提问；口径前后矛盾、新增内容不明确时用 ask_question 提问，写明出处，不要自行取舍；",
              "3. 删除的文件如果被数据表用到，改映射或提问；",
              "4. 试算全部方案，确认结果合理后 finish。还有问题等答复时，做完不受影响的部分就 finish 结束本轮。",
              "", "当前文件清单：", ws.list_files()]
    return "\n".join(lines)


def _phase_note(ws: Workspace, k: int) -> str:
    done = "；".join(f"{PHASES[i][0]}（{PHASES[i][2]}）" for i in range(k))
    return (f"【进入第 {k + 1}/{len(PHASES)} 阶段：{PHASES[k][0]}】已完成：{done}。\n"
            "为了腾出上下文，之前的对话已清空（只留下刚完成的最后一步）。材料包仍在任务里；读过的结论在参数台账、条款清单、笔记和规则摘要里（见工作区状态），"
            "直接用，不要从头重读材料。\n"
            f"这一阶段要做：{PHASES[k][1]}。\n完成标准：{PHASES[k][2]}。")


def _segments_text(st, used: set[int] | None = None) -> str:
    """表中间另一段表头的说明：哪一行、哪几列的列名和上面不同。used 给出时只列用到的列。"""
    out = []
    for j, ch in st.segments:
        diff = [f"第{i + 1}列「{a}」在这一段" + (f"叫「{b}」" if b else "没有列名")
                for i, (a, b) in sorted(ch.items()) if used is None or i in used]
        out.append(f"第 {j} 行是另一段数据的表头（不计入数据）" + ("，" + "、".join(diff[:8]) if diff else "，列名和上面一样"))
    return "；".join(out)


_FIGURE = re.compile(r"\d(?:[\d,.]*)\s*(?:%|％|‰|万|亿|元|倍|pcs|PCS|Kg|kg)|系数|比例|阶梯|[<>≤≥]\s*\d")
_EMPTY_IMAGE = re.compile(r"看不清|无法辨认|无可转写|没有可转写|无文字|无内容|空白|无关|没有.{0,8}(?:内容|文字|信息)|标志|商标|logo|LOGO|Logo|装饰|背景图")


def _has_figures(text: str) -> bool:
    """页里有带单位的数、比例、阶梯这类参数痕迹：不折叠。"""
    return bool(_FIGURE.search(text or ""))


def _empty_transcript(t: str) -> bool:
    """转写不出内容的图片（logo、装饰图、“看不清”）：空的，或只用一两句说看不清、是标志、没有相关内容。
    带数字的一律算有内容；很短但没这些字样的（如流程图里的“按季度发放”）也照常给。"""
    s = re.sub(r"\s+", "", t or "")
    if re.search(r"\d", s):
        return False
    return not s or (len(s) <= 60 and bool(_EMPTY_IMAGE.search(s)))


def _doc_pages(d, transcripts: dict[str, str]) -> dict:
    """文档按页的文字（含有内容的图片转写），页码 -> 文字；没有页码的文档整份算一页。"""
    out: dict = {}
    k = 0
    for b in d.blocks:
        p = b.page if b.page is not None else 1
        if b.kind == "image":
            k += 1
            t = transcripts.get(f"{d.name}#{k}", "")
            text = "" if _empty_transcript(t) else t
        else:
            text = b.text or ""
        if text:
            out[p] = (out.get(p, "") + "\n" + text).strip()
        else:
            out.setdefault(p, "")
    return out


def _repeated_lines(d) -> set[str]:
    """每页都重复的页眉、页脚（如“XX 制度 - 某章节”）：作为页首或页尾一行的短行，至少 3 页、过半的页里都有。
    只看页首页尾，避免把正文里反复出现的小标题（如各奖项都有的“考核方式”）当成页眉去掉。"""
    from collections import Counter
    pages: dict = {}
    for b in d.blocks:
        if b.text and b.kind != "table":
            pages.setdefault(b.page, []).extend(ln.strip() for ln in b.text.split("\n") if ln.strip())
    n = len(pages)
    cnt = Counter(ln for lines in pages.values() for ln in {lines[0], lines[-1]} if len(ln) <= 40)
    return {ln for ln, c in cnt.items() if c >= 3 and c >= 0.5 * n and not re.fullmatch(r"[\d.\s/第页共-]+", ln)}


DEEP_SHOW = 4000                 # 步骤记录里留多少思考内容（全文只在当次请求里）
FORMAT_ERRORS = ("错误：参数", "错误：没有工具")    # 参数格式这类小错：程序已经说清楚怎么改，第一次不用深度思考


def _deep_reason(turn: list[Step], skipped: int, ws: Workspace, fails: dict[str, int]) -> str:
    """这一步的结果是否要求下一步深度思考：失败（参数格式小错第一次除外）、被拦下的原样重复、
    程序提醒快要卡住、保存的规则还有阻断问题。返回原因（给模型看的一句话），不需要时返回空。"""
    for st in turn:
        if st.tool.startswith("（"):
            continue
        head = st.result.removeprefix("错误：").split("\n", 1)[0][:300]
        if not st.ok and (not st.result.startswith(FORMAT_ERRORS) or fails.get(st.tool, 0) >= 2):
            return f"{st.tool} 失败：{head}"
        if "\n注意：" in st.result:
            return f"{st.tool} 的结果里有提醒：{st.result.split(chr(10) + '注意：', 1)[1][:300]}"
        if st.ok and st.tool == "set_rules" and getattr(ws, "last_blocking", None):
            return f"set_rules 保存了，但还有阻断问题：{head}"
    if skipped:
        return "又原样做了一遍之前做过的操作，结果和之前一样"
    return ""


def _deep_note(why: str, steps: list[Step] = ()) -> str:
    """深度思考那一步附在工作区状态后面的分析要求（通用，不针对具体材料），带上最近试过、没成功的做法。"""
    tried = []
    for st in reversed(steps):
        if st.tool.startswith("（进入新阶段") or len(tried) >= 6:
            break
        if not st.ok:
            head = " ".join(x.strip() for x in st.result.removeprefix("错误：").split("\n")[:2])[:160]
            tried.append(f"\n  · {st.tool}：{head}")
    seen = "".join(dict.fromkeys(reversed(tried)))
    return ("\n\n【这一步先深度思考，再调用工具】上一步没有成功：" + why +
            (f"\n最近试过、没有成功的做法（不要原样再试）：{seen}" if seen else "") +
            "\n先在思考里把下面几件事想清楚，再只做一个最有把握的改动："
            "\n1. 错误或提示具体指向哪里：规则写法、验算式、对数据的理解（列、取值、编号在哪一层），还是材料本身没写清；"
            "\n2. 前几步是不是已经在同一处改过：是的话说明方向不对，回到材料原文和数据重新核对，不要在原写法上小修小补；"
            "\n3. 这次准备怎么改，为什么能解决；能用 explain / evaluate 看一眼中间数就先看，不要凭印象；"
            "\n4. 口径确实拿不准、或材料之间冲突，用 ask_question 把差异写给财务，不要硬凑。")


def _reflection(ws: Workspace, stuck: str, steps: list[Step], n: int, limit: int) -> str:
    """卡住时的反思提示：卡在哪、最近试过什么、程序的判断，要求先想清楚原因再换一种做法。"""
    tried = []
    for st in steps[-8:]:
        if st.tool.startswith("（"):
            continue
        head = st.result.split("\n", 1)[0][:200]
        why = (st.thought or "").replace("\n", " ")[:120]
        tried.append(f"  · {st.tool}{'（失败）' if not st.ok else ''}：{head}" + (f"｜当时的想法：{why}" if why else ""))
    lines = [f"【停下来反思（第 {n} 次，最多 {limit} 次）】你在同一个地方已经连续几次没有进展：{stuck[:600]}",
             "前面的对话已清掉，工作区（数据表、列映射、规则、台账、条款）都保留，见工作区状态。最近几步：", *tried[-5:],
             "", "不要再按原来的写法小修小补。先在 thought 里写清楚：",
             "1. 报错或阻断问题到底在说什么，前几次改了什么、为什么没解决；",
             "2. 回到材料原文和数据核对真正的要求：相关条款怎么说，数据表里实际是什么（列、取值、哪一层的编号），需要就用工具看一眼；",
             "3. 换一种做法（如换一种写法、拆表或换挂靠的层级、删掉不需要的定义、把复杂的算式拆成辅助变量逐步算），"
             "并说明为什么这次能解决；",
             "4. 确实是材料没写清或规则写法表达不了，用 ask_question 说明卡在哪里，不要硬凑。"]
    lines += [f"提示：{t}" for t in _lessons(stuck)]
    lines.append(f"程序判断的下一步：{ws.next_hint()}")
    if n >= limit:
        lines.append("这是最后一次反思机会，再卡住本轮就会停下。")
    return "\n".join(lines)


def _lessons(error: str) -> list[str]:
    """上一轮停下的原因 -> 这一轮该怎么换做法。"""
    out = []
    if "合法 JSON" in error:
        out += ["上一轮是参数格式写错（多为一次写得太长，或把一个对象里的几项写成了 {\"a\": {...}}, {\"b\": {...}}）。"
                "这一轮 set_rules 分几次调用：每次只写几个变量，再单独写 allocation、pool、payout；同一个对象里的几项用逗号隔开，不要多加 }{"]
    if "同样的阻断问题" in error:
        out += ["上一轮同一个阻断问题反复没改掉：先用 explain / evaluate 弄清原因，确实表达不了的用 ask_question 说明卡在哪里"]
    if "反复" in error or "原样调用" in error:
        out += ["上一轮在原地打转：这一轮先看工作区状态里程序判断的下一步，按它推进"]
    if "verify" in error or "独立验算" in error:
        out += ["上一轮独立验算没对上：先用 explain 看引擎的计算链，逐项和自己的验算比（先核对对象属于哪个小组、部门，待验算清单里写了归属）；跨表的中间数用 verify 的 helpers 定义辅助变量，"
                "不要借用规则变量。确认是规则不符合材料就改规则，拿不准就 ask_question"]
    if "连续" in error and "失败" in error and not out:
        out += ["上一轮同一个工具因为同一个原因连续失败：先在思考里写清失败原因，换一种写法再调用"]
    return out


# ---------------- 输出 ----------------
def write_agent_outputs(ar: AgentResult, out: Path, answers: list[dict] | None = None) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    from bonus_agent import review as rv
    for old in [*out.glob("result*.xlsx"), *out.glob("rules*.yaml"), *out.glob("adjust_*.yaml"),
                out / "待确认问题.xlsx", out / "expected_check.txt", out / rv.FILE]:
        old.unlink(missing_ok=True)       # 上一轮的输出已存档在 history/，不能留在顶层被误用
    ws, paths = ar.ws, {}
    if ws.mapping:
        paths["mapping"] = out / "mapping.yaml"
        paths["mapping"].write_text(yaml.safe_dump({k: v.model_dump(exclude_none=True, exclude_defaults=True)
                                                    for k, v in ws.mapping.items()}, allow_unicode=True, sort_keys=False),
                                    encoding="utf-8")
    for name, rs in ar.rulesets.items():
        key = "rules" if ar.single else f"rules:{name}"
        paths[key] = out / ("rules.yaml" if ar.single else f"rules_{_safe(name)}.yaml")
        paths[key].write_text(rs.dump_yaml(), encoding="utf-8")
        done = []
        for rev in _revisions(ws._merged(name).get("revisions")):
            done += [{"node_id": a["node"], "amount": str(a["amount"]), "reason": a.get("reason", ""), "id": a.get("id", ""),
                      "created_at": f"{len(done) + i:06d}"} for i, a in enumerate(rev["adjust"])]
            paths[f"adjust:{name}:{rev['name']}"] = out / f"adjust_{_safe(name)}_{_safe(rev['name'])}.yaml"
            paths[f"adjust:{name}:{rev['name']}"].write_text(yaml.safe_dump(list(done), allow_unicode=True, sort_keys=False),
                                                             encoding="utf-8")
    qrows = [{"id": q.id, "round": q.round, "text": q.text, "options": q.options, "suggested": q.suggested,
              "related": q.related, "blocking": q.blocking, "answer": q.answer,
              **({"withdrawn": q.withdrawn} if q.withdrawn else {}),
              **({"answered_by": q.answered_by, "answered_at": q.answered_at} if q.answered_by else {})}
             for q in ws.questions]
    paths["questions"] = out / "questions.yaml"
    paths["questions"].write_text(
        "# 财务答复：在 answer 里填选项序号（从 0 开始）或直接写文字；有补充材料放进材料文件夹，\n"
        "# 然后用同一个输出目录再运行一次，智能体会从这一轮接着做（不用 --answers）\n"
        + yaml.safe_dump(qrows, allow_unicode=True, sort_keys=False), encoding="utf-8")
    paths["log"] = out / "agent_log.json"
    paths["log"].write_text(json.dumps({"summary": ws.summary, "error": ar.error, "transcripts": ws.transcripts,
                                        "triage": ws.triage, "steps": [vars(s) for s in ar.steps]}, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    if ar.status == "FAILED":       # 没做完（没通过检查或独立验算）：不出结果文件，免得被当成结果用；过程在 agent_log 里
        return paths
    if not ar.results:
        if ar.pending or ws.questions:     # 本轮不出结果：把问题、规划和工作记录给财务看
            import openpyxl
            from bonus_core.report.excel import _sheet
            wb = openpyxl.Workbook()
            wb.remove(wb.active)
            for title, headers, data, widths in _sheets(ar, qrows):
                _sheet(wb, title, headers, data, widths=widths)
            paths["pending"] = out / "待确认问题.xlsx"
            wb.save(paths["pending"])
        return paths
    paths["review"] = rv.write(ws, out, ar.results)  # 口径单：财务逐条确认后才能确认结果（确认记录按内容保留，见 review.py）
    paths["results"] = out / "results.json"      # 全部金额的明细（应得、暂缓、抵扣、超发、实发），便于程序逐分核对
    paths["results"].write_text(json.dumps(_results_json(ar.results), ensure_ascii=False, indent=1), encoding="utf-8")
    ar.recheck = "；".join(_recheck(ar, paths, name, rev) for name, revs in ar.results.items() for rev in revs)
    if ar.single:
        paths["excel"] = out / "result.xlsx"
        write_excel(ar.ruleset, ar.result, paths["excel"], extra_sheets=_sheets(ar, qrows))
        return paths
    if len(ar.results) == 1 and list(next(iter(ar.results.values()))) == ["R0"]:     # 一个方案、没有修订：只出一份结果
        name = next(iter(ar.results))
        paths["excel"] = out / "result.xlsx"
        write_excel(ar.rulesets[name], ar.results[name]["R0"], paths["excel"], extra_sheets=_sheets(ar, qrows))
        return paths
    for name, revs in ar.results.items():
        for rev, res in revs.items():
            k = f"excel:{name}:{rev}"
            paths[k] = out / (f"result_{_safe(name)}.xlsx" if rev == "R0" else f"result_{_safe(name)}_{_safe(rev)}.xlsx")
            write_excel(ar.rulesets[name], res, paths[k])
    paths["excel"] = out / "result.xlsx"
    _overview(ar, qrows, paths["excel"])
    return paths


def _results_json(results: dict) -> dict:
    """方案 -> 修订 -> {奖金池, 对象: {层级, 名称, 应得, [暂缓, 可发上限, 已发抵扣, 实际抵扣, 超发待核对, 实发]}}；有按人汇总的抵扣时实发取汇总行。
    已发抵扣是已发（预发）全额，和 Excel 发放表同一口径；实际抵扣 = MIN(可发上限, 已发抵扣)，超发待核对 = 已发抵扣 − 实际抵扣。"""
    out = {}
    for s, revs in results.items():
        for rev, r in revs.items():
            objs = {}
            for nid, n in r.nodes.items():
                if n.key:
                    objs[nid] = {"层级": n.level, "名称": n.label, "应得": n.final}
            for nid, p in {**r.payouts, **r.rollups}.items():
                objs.setdefault(nid, {"名称": getattr(p, "label", ""), "应得": p.final})
                objs[nid].update({"暂缓": p.held, "可发上限": p.cap or p.final, "已发抵扣": p.offset,
                                  "实际抵扣": p.deducted, "超发待核对": p.overpaid, "实发": p.paid})
            out.setdefault(s, {})[rev] = {"奖金池": r.pool, "对象": objs}
    return out


def _safe(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in s)


def _overview(ar: AgentResult, qrows: list[dict], path: Path):
    """多个方案 / 修订时的总览：每个对象一行，每个方案（修订）一列。修订是同一方案的不同版本，不能相加。"""
    import openpyxl
    from bonus_core.report.excel import MONEY, _sheet
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    cols = [(n, r) for n, revs in ar.results.items() for r in revs]
    heads = [n if r == "R0" else f"{n} {r}" for n, r in cols]
    rows, seen = [], set()
    for n, revs in ar.results.items():
        for node in revs["R0"].nodes.values():
            if node.id in seen:
                continue
            seen.add(node.id)
            rows.append([node.level, node.key or "", node.label]
                        + [float(ar.results[c][r].nodes[node.id].final) if node.id in ar.results[c][r].nodes else None
                           for c, r in cols])
    _sheet(wb, "各方案结果", ["层级", "编号", "名称"] + heads, rows, money_cols=range(4, 4 + len(cols)))
    summary = [[h, float(Decimal(ar.results[c][r].pool)), f"result_{_safe(c)}" + ("" if r == "R0" else f"_{_safe(r)}") + ".xlsx"]
               for h, (c, r) in zip(heads, cols)]
    ws = _sheet(wb, "方案汇总", ["方案", "奖金池", "明细文件（含计算链）"], summary, money_cols=[2])
    ws.append([])
    ws.append(["说明：同一方案的 R1、R2 是人工调整后的修订版本，与 R0 是同一笔奖金的不同版本，不能相加。"])
    for title, headers, data, widths in _sheets(ar, qrows):
        _sheet(wb, title, headers, data, widths=widths)
    for w in wb.worksheets:
        for row in w.iter_rows(min_row=2):
            for c in row:
                if isinstance(c.value, float):
                    c.number_format = MONEY
    wb.save(path)


def _recheck(ar: AgentResult, paths: dict[str, Path], name: str, rev: str) -> str:
    """从写出的规则、映射（和调整）文件重新读入并重算，逐个对象核对金额，证明结果只来自引擎、可复算。"""
    from bonus_core.data.mapping import load_mapping
    label = "" if ar.single else f"{name}{'' if rev == 'R0' else ' ' + rev}："
    want = ar.results[name][rev]
    try:
        rs = RuleSet.load(paths["rules" if ar.single else f"rules:{name}"])
        data, _ = apply_mapping(rs, ar.ws.data_files, load_mapping(paths["mapping"]))
        adj = []
        if rev != "R0":
            adj = [Adjustment(**a) for a in yaml.safe_load(paths[f"adjust:{name}:{rev}"].read_text(encoding="utf-8"))]
        refs = {f"{s}/{r}": res for s, revs in ar.results.items() for r, res in revs.items() if s != name}
        again = compute(rs, data, adj, refs=refs)
    except Exception as e:  # 复算失败要如实写进结果，不能当作通过
        return f"{label}复算失败：{e}"
    diff = [i for i, n in want.nodes.items() if i not in again.nodes or again.nodes[i].final != n.final]
    if again.pool != want.pool or diff:
        return f"{label}复算不一致：奖金池 {again.pool} / {want.pool}，不一致的对象 {diff[:10]}"
    files = "rules.yaml + mapping.yaml" if ar.single else f"规则、映射{'和调整' if rev != 'R0' else ''}文件"
    return f"{label}一致：按 {files} 重新计算，奖金池和 {len(again.nodes)} 个对象的金额逐分相同"


def _sheets(ar: AgentResult, qrows: list[dict]):
    ws = ar.ws
    status = ("待财务答复（NEEDS_CONFIRMATION）：本轮不出结果。请在 questions.yaml 填写答复（或补充材料）后，"
              "用同一个输出目录再运行一次。问题：" + "；".join(f"{r['id']} {r['text']}" for r in ar.pending)
              if ar.status == "NEEDS_CONFIRMATION" else f"未完成：{ar.error}" if not ar.ok else
              "智能体草稿：规则和数据映射由大模型生成，尚未经财务确认。请核对后再发放。")
    plan = [["状态", status], ["轮次", f"第 {ar.round} 轮"],
            ["方案概述", ws.summary],
            ["计算方案", "、".join(f"{n}（{'、'.join(r)}）" for n, r in ar.results.items()) or "无"],
            ["规则来源", "、".join(d.name for d in ws.inv.docs)]]
    for n, d in ws.datasets.items():
        m = ws.mapping[n]
        kind = f"实体表，主键 {d.key}" if d.key else f"明细表，关联 {d.link}" if d.link else "公司级表"
        plan.append([f"数据表：{n}", f"{m.file}/{m.sheet}" + (f"，筛选 {m.filter}" if m.filter else "") + f"（{kind}）"])
        plan += [[f"  {f}", f"← 「{c}」 {d.fields[f].type}" + (f"，{d.fields[f].unit}" if d.fields[f].unit else "")]
                 for f, c in m.columns.items()]
    plan += [["已忽略", f"{i.get('ref')}：{i.get('reason', '')}"] for i in ws.ignored]
    plan.append(["原文引用命中", f"{ar.quote_hits}/{ar.quote_total}"])
    plan.append(["复算核对", ar.recheck])
    if ar.changes is not None:
        plan.append(["本轮文件变化", "；".join(f"{k}：{'、'.join(v)}" for k, v in ar.changes.items() if v) or "无"])
    if ar.round > 1:
        plan.append(["规则改动（与上一轮比）", "；".join(ar.rule_changes) or "无"])
    q = [[r["id"], r.get("round", 1), r["text"], " / ".join(r.get("options") or []),
          (r["options"][r["suggested"]] if isinstance(r.get("suggested"), int)
           and 0 <= r["suggested"] < len(r.get("options") or []) else ""),
          f"（已撤回：{r['withdrawn']}）" if r.get("withdrawn") else "" if r.get("answer") is None else str(r["answer"])]
         for r in qrows]
    log = [[i, s.tool, json.dumps(s.args, ensure_ascii=False)[:300], ("" if s.ok else "失败：") + s.result[:500]]
           for i, s in enumerate(ar.steps, start=1)]
    out = [("待确认问题", ["编号", "提出轮次", "问题", "选项", "智能体建议（未采用）", "财务答复"], q,
            {"问题": 60, "选项": 40, "智能体建议（未采用）": 20})]
    if ws.facts:
        conflicted = {c.name: c.question for c in ws.conflicts.values()}
        out.append(("参数台账", ["参数", "原文取值", "换算", "文件", "原文", "登记轮次", "冲突问题"],
                    [[f.name, fx.label(f), fx.same_value(f) if fx.number(f.value) is not None else "", f.file, f.quote,
                      f.round, conflicted.get(f.name, "以此为准（原文明确修改）" if f.override else "")]
                     for f in sorted(ws.facts, key=lambda f: f.name)],
                    {"参数": 30, "原文": 60}))
    if ar.compared:
        out.append(("与上一轮对比", ["方案", "修订", "对象", "名称", "上一轮应得", "本轮应得", "差额", "上一轮实发", "本轮实发"],
                    ar.diff_rows or [["（所有对象金额与上一轮相同）"]], {"对象": 16, "名称": 14}))
    if ar.results:
        from bonus_agent import review as rv
        out.append(rv.sheet(rows=rv.build(ws, ar.results)))
    return out + [
            ("智能体规划", ["项目", "内容"], plan, {"项目": 22, "内容": 100}),
            ("智能体工作记录", ["步骤", "工具", "参数", "结果"], log, {"参数": 60, "结果": 90})]
