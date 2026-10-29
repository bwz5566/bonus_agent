# 奖金计算智能体（bonus-agent）

为财务部提供季度奖金、年度奖金、过程奖金等场景的计算服务。核心原则：**大模型只负责读懂规则、匹配表头，所有金额由确定性的计算引擎算出**，每一分钱都能追溯到规则原文和数据行。

当前处于**模型选型阶段**：智能体通过百炼 API 调用千问 3.6 / 3.5 的开源多模态模型（最初计划的 Qwen3-VL 32B / 30B-A3B / 8B 已在百炼下线），用评测脚本对比效果，确定后再私有化部署（只改 `bonus_agent/config/models.yaml` 的 provider）。

## 目录

```text
bonus_core/            核心库：纯计算，不依赖大模型和网络
  dsl/                 规则 DSL：数据模型、表达式语法、白名单函数、静态校验
  data/                Excel 表头识别、字段映射、类型转换、数据校验
  engine/              求值器（带计算链）、分配树、锁定重分、保留到分、发放条件、台账补发
  report/excel.py      结果 Excel（含计算链、调整记录、未发放台账）
  golden.py            金标样例（财务手算）回归
  cli.py               命令行 bonus
bonus_agent/           智能体：工具调用主循环（agent.py）、固定流程（auto.py）、文档解析、规则抽取、字段映射推荐
  prompts/             提示词模板
  cli.py               命令行 bonus-agent
evals/                 模型选型评测
  cases/sales_q3/      评测样例：销售季度奖金，Word + 阶梯图片 + PPT 表格 + PPT 系数图片
  cases/annual_2026/   评测样例：公司年终奖，PPT + Word 补充通知 + 含说明页和公司级利润表的 Excel
  run_eval.py          命令行 bonus-eval
bonus_agent/config/models.yaml  模型与服务商配置（随安装包发布；当前目录有 config/models.yaml 时优先用它）
examples/sales_q3/     示例：规则、示例数据（格式故意不规整）、字段映射、调整记录
examples/auth/         服务间鉴权签名头示例（Python 签名与校验、Java 签名），给前后端对接用
tests/                 单元测试、属性测试、端到端测试
```

## 安装

```bash
pip install -e ".[agent,dev]"
python -m pytest          # 38 个测试，应全部通过
```

## 零、全自动模式（推荐先试这个）

把一个批次的规则文档（Word / PPT / 图片）和 Excel 数据放进一个文件夹，内容、格式、文件数量都不限：

```bash
bonus-agent auto --out 输出目录 本批次文件夹
# 例：python evals/cases/annual_2026/make_case.py && bonus-agent auto --out out/annual evals/cases/annual_2026
```

**由大模型主导，不写死流程。** 智能体拿到一套工具，自己决定先看什么、怎么建模、哪里要回头核对：

| 工具 | 作用 |
| --- | --- |
| list_files / read_document / read_sheet / view_image | 看文件清单、文档文字、Excel 原始内容（带行号）、图片（逐字转写） |
| define_dataset / remove_dataset | 定义数据表：选工作表和表头行、命名字段、判断类型和单位；程序会校验列是否存在、数据能否转换 |
| set_rules | 写入或修改规则（阶梯、系数、变量、奖金池、分配层级和权重、发放条件、数据校验、修订），返回静态校验结果；多个方案用 scheme 分开，相同部分写到“共用” |
| trial_run / explain / evaluate | 用真实数据试算、查看某人的算式、计算任意表达式来核对 |
| note | 记笔记：关键规则、参数随时记下，每轮附给模型，不怕长对话被裁剪 |
| record_facts | 登记材料里的参数和口径（原文写法 + 逐字引用），程序核对引用真实存在、比对冲突 |
| ask_question | 有冲突、缺信息或理解不明确时向财务提问，不自行假设；有未答复的问题时本轮不出结果 |
| finish | 完成（要求最近一次修改后已成功试算） |

工作区状态（笔记、数据表、规则摘要、问题、试算结论）每轮都会附给模型。对话历史超过上限（默认 40000 字）才整理一次：较早的工具结果只留摘要，仍太长就丢掉最早的几轮；两次整理之间只追加不改动，请求前缀不变，私有化 vLLM 的前缀缓存和百炼显式缓存（`cache: explicit`）都能命中。
金额始终由计算引擎按模型写的规则算出，每一分钱可追溯；模型只负责“读懂并建模”，不做任何计算。写出结果后，程序会从写出的规则和映射文件重新读入、重新计算，逐个对象核对金额（结果里的「复算核对」）。

命令行会实时打印每一步，失败的步骤打印完整原因。输出目录里有：`result.xlsx`（结果 +「待确认问题」「智能体规划」「智能体工作记录」）、`rules.yaml`、`mapping.yaml`、`questions.yaml`、`agent_log.json`。
一批里有多个方案或修订时：`result.xlsx` 是总览（每个对象一行、每个方案 / 修订一列），各方案的明细和计算链在 `result_方案[_修订].xlsx`，规则在 `rules_方案.yaml`，修订的人工调整在 `adjust_方案_修订.yaml`。

有期望结果时（如验收测试的标准答案），用 `--expected 期望结果.xlsx` 在算完后逐个对象核对，结果写入 `expected_check.txt`。这个文件只用来核对，会自动排除在输入之外，不会交给智能体。

**多轮处理（案卷）**：`--out` 目录就是这笔奖金的案卷。材料有冲突、缺信息或理解不明确时，智能体直接提问，不自行假设；
本轮状态为 `NEEDS_CONFIRMATION`（返回码 3），不出结果，只输出 `questions.yaml` 和 `待确认问题.xlsx`。
财务在 `questions.yaml` 的 `answer` 填写答复（选项序号或文字），有补充材料放进材料文件夹，然后**用同一个 `--out` 再运行**：

```bash
bonus-agent auto --out 案卷目录 本批次文件夹
```

智能体从上一轮的工作区（数据表、映射、规则、笔记、问题）接着做，只处理本轮变化：新答复、新增 / 修改 / 删除的文件（按 sha256 识别），
不重读没变的材料；新材料与已有规则冲突时继续提问。算完后 `result.xlsx` 的「与上一轮对比」列出金额有变化的对象，
「智能体规划」列出规则改动；每轮输出存档在 `history/第N轮/`，案卷状态在 `case.json`。`--new` 忽略上一轮从头开始。

**冲突自动检测**：模型把每份材料里的参数（比例、金额、阶梯每一档、系数、门槛、口径）登记进参数台账，写原文写法和逐字引用；
程序核对引用确实在该文件里（文档文字、图片转写、Excel 单元格）、取值确实在引用里，然后**由程序比对**：同名参数换算后取值不同
（5% 与 0.06、30万元 与 280000）就自动生成阻断问题，列出各处原文，不让模型取舍。只有原文明确写了“修改为 / 以本通知为准”的那一条
（quote 必须包含这类字样）才以它为准、不算冲突。财务答复后新材料又出现新取值会再次提问；冲突依据的文件被修改或删除时，未答复的问题由程序撤回、按新内容重新比对。
完成前程序还检查：每份规则文档都登记过参数（无关的要写明原因）、登记的数值都在规则里出现（有冲突的按财务选定的值查）。
结果和待确认问题里都有「参数台账」。

**确认冻结**：财务核对某一轮结果无误后确认，冻结成计算模板，以后每期换数据直接算，不再调用大模型：

```bash
bonus-agent confirm --out 案卷目录 --by 确认人 [--note 说明] [数据文件...]
bonus run-template --template 案卷目录/template --out 结果目录 本期数据.xlsx ...
```

`confirm` 只能确认最新一轮且该轮已出正式结果（状态 OK，没有待答复问题）；会核对该轮输出的规则、映射、调整文件没被手工改过，
再按模板复算、与该轮结果逐分一致，才写入 `template/`（`manifest.yaml` 记录确认人、时间、轮次、方案计算顺序、各文件 sha256）。
`run-template` 运行前核对模板文件指纹，被改过就拒绝计算；按顺序算各方案和修订（被引用的方案先算），结果里有「确认信息」。
规则要改时，在案卷上再走一轮智能体处理并重新确认。

其他：`--max-steps`（默认 80）和 `--max-tokens`（默认 200 万）是安全上限，模型调用 finish 就结束，正常用不满；重复调用会被拦截，同一问题重复或连续失败 3 次直接停止；`--pipeline` 改用固定流程（规划 → 抽取 → 计算），给工具调用能力弱的模型用；模型不支持原生工具调用时会自动改用 JSON 动作协议。

支持的方案结构：奖金池 + 多层按权重分配；奖金包按部门（或其他层级）分别计算再往下分；“每人按公式直接算”的方案（总包 = 各人金额之和）；公司级数据表（如各月利润）；某些人不参与（权重为 0）；按比例暂缓发放；一批多个方案（季度、过程、年度）和人工调整修订（R1、R2）。

多表关联：项目、销售单这类被其他表按编号引用的对象建成“关联实体”，可以定义项目层变量（如项目毛利率、有效销售额）；其他表的行按编号直接取到项目的字段和变量（参与比例表 → 项目，回款 → 销售单 → 项目）；按日期期间筛选（`between(recognition_date, "2026-07-01", "2026-09-30")`、`years_between`）；一张表混放多类数据时按列筛选拆成几张表；数据校验规则（如参与比例合计为 1）不通过时阻止计算。样例见 `evals/cases/acceptance_q3`，结果与独立的参考实现逐分一致。

发放与抵扣：按回款比例发放（`pay_ratio`，可发上限 = ROUND(应得 × 比例, 2)，其余暂缓、不重新分配）；扣减已发金额（`offsets`，本次实发 = MAX(可发上限 − 已发, 0)，超发只登记不追回）；一个方案扣另一个方案某修订的实发用 `scheme_paid("QUARTER", "R2")`，试算时自动先算被引用的方案，被引用的方案改过后引用它的方案要重算。

材料要求“确认前不得计算 / 发放”的口径找不到财务答复时，智能体记为阻断问题，结果标为 `NEEDS_CONFIRMATION`（命令返回码 3），只是草稿、不得发放；在 `questions.yaml` 填写答复后重跑。

完整验收样例见 `evals/cases/syn_acceptance`（季度 R0/R1/R2、过程奖、年度奖扣预付和季度 R2）：`rules/` 是手写的标准规则，`reference.py` 是不经过引擎的独立实现，`run_case.py` 用引擎算并核对；
`python evals/cases/syn_acceptance/reference.py evals/cases/syn_acceptance/expected/05_期望奖金结果.xlsx` 可独立验证期望结果本身。

## 一、计算引擎（不需要大模型）

```bash
cd examples/sales_q3
python make_data.py                                   # 生成示例数据 data.xlsx
bonus validate --rules rules.yaml                     # 静态校验
bonus test --rules rules.yaml                         # 跑财务手算样例，精确到分
bonus detect data.xlsx                                # 看表头识别结果
bonus suggest --rules rules.yaml data.xlsx            # 启发式推荐字段映射
bonus run --rules rules.yaml --mapping mapping.yaml \
          --adjust adjustments.yaml --out result.xlsx data.xlsx
```

`result.xlsx` 包含：汇总、各层级明细（含权重算式）、个人发放（应发/实发/挂起）、调整记录、未发放台账、计算链、异常与提示、规则与数据版本。

已实现的业务规则：

- 阶梯提成区分**全额累进**（`mode: whole`）和**超额累进**（`mode: excess`），边界是否包含用 `lower_inclusive` 显式标明；
- 分配层级数和权重由规则定义；
- 调整奖金包时**总额不变**：被调整的节点锁定，同级其余节点按原始权重重分，之前调整过的节点保持锁定，可撤销；
- 金额**保留到分**，尾差用最大余数法分配，保证每层下级之和严格等于上级；
- 未收款按比例挂起，进入台账；回款后**按当时的规则版本**补发（`bonus_core.engine.ledger.release`）；
- 除零、查表未命中、字段为空等情况一律报错，不静默补 0。

## 二、模型选型

1. 在阿里云百炼开通服务，拿到 API Key。
2. 写入 Key：`cp config/secrets.example.yaml config/secrets.yaml`，填 `api_key`（该文件已加入 .gitignore，不会提交）；也可以设置环境变量 `DASHSCOPE_API_KEY`，环境变量优先。
3. 核对模型能否调用：`bonus-agent models --remote`，每个已配置模型后面会标“可用 / 不可用”，并列出账号下所有 VL、3.5、3.6 相关的模型 ID。
4. 生成评测样例文档并运行评测：

```bash
python evals/cases/sales_q3/make_case.py
bonus-eval --fake                                     # 先不调模型，演练整条流程
bonus-eval --repeats 3                                # 四个候选各跑 3 次
bonus-eval --models open,reference --repeats 3        # 全部开源模型 + 商业版上限参考
bonus-eval --modes two_step,direct --repeats 3        # 同时对比“先转写图片”和“直接看图”
bonus-eval --modes agent --repeats 3                  # 大模型主导的智能体，从原始文件开始
bonus-eval --modes agent,auto --repeats 3             # 对比智能体和固定流程
```

已配置的模型分组（`bonus_agent/config/models.yaml`，2026-10-28 用 `--remote` 实测）：

| 分组 | 模型 | 说明 |
| --- | --- | --- |
| candidates | qwen3.6-35b-a3b / qwen3.6-27b / qwen3.5-35b-a3b / qwen3.5-27b | 百炼在售、权重开源，可私有化部署 |
| open | 以上 + qwen3.5-122b-a10b | 开源模型全集 |
| reference | qwen3-vl-plus / qwen3.6-flash | 商业版，不能私有化，只看效果上限 |
| legacy | qwen3-vl-32b / 30b-a3b / 8b | 已在百炼下线，仅作记录 |
| deepseek（默认 deepseek-flash） | deepseek-flash / deepseek-v4-pro | DeepSeek 官方 API；flash 能看图，v4-pro 只有文本（图片交给 flash 转写） |

**换用其他模型**：任何 OpenAI 兼容接口（百炼、DeepSeek、vLLM、其他云厂商）都可以用。在 `models.yaml` 的 `providers` 里加服务商地址和 Key 的环境变量名，
在 `models` 里加模型，运行时用 `--model 模型名`，或把 `default` 改掉。例：`DEEPSEEK_API_KEY=sk-... bonus-agent auto --model deepseek-flash --out 案卷目录 材料文件夹`。
只有文本的模型（如 deepseek-v4-pro）写 `vision: false`，或写 `vision: 另一个能看图的模型名` 由它转写图片；不能看图又没配看图模型时，
图片里的规则由智能体向财务提问，不会猜。不支持原生工具调用的模型会自动改用 JSON 动作协议。

报告在 `evals/reports/`：Markdown 汇总、CSV 明细，以及每次调用的原始输出、图片转写和抽取出的规则。

评测指标：

| 指标 | 含义 |
| --- | --- |
| 可用规则率 | 输出能通过格式和静态校验的比例（含最多 2 轮自动修正） |
| 结构检查得分 | 阶梯档位、全额/超额、图片里的系数、PPT 表格、分配层级、挂起条件等检查项的通过比例 |
| 个人金额算对率 | **最重要**：用抽取出的规则在标准数据上实算，与标准答案逐人比对（精确到分） |
| 原文引用命中率 | 规则出处是否真实存在于文档中，低说明在编造 |
| 对歧义提问 | 净利润一条故意没写全额还是超额，看模型会不会提问 |
| 耗时、tokens | 用于估算部署后的速度和资源 |

**数据安全**：调用云端 API 时只使用脱敏后的规则文档和数据；评测样例都是生成的模拟数据。

### 用真实规则文档试用

```bash
bonus-agent models
bonus-agent extract --model qwen3-vl-32b --datasets examples/sales_q3/rules.yaml \
    --out draft.yaml 激励方案.docx 系数表.pptx
# draft.yaml 是规则草稿，draft.questions.json 是问题卡和校验结果，交财务逐条确认
bonus-agent map --model qwen3-vl-32b --rules draft.yaml 数据.xlsx
```

### 增加评测样例

在 `evals/cases/` 下新建目录，放规则文档、`case.yaml`（参考 `sales_q3/case.yaml`：标准答案规则、计算数据、映射和检查项）。建议用脱敏的历史规则文档做 30–50 个样例，覆盖图表、扫描件、多层分配等情况。

## 三、私有化部署（选型之后）

### 内网网页服务

财务不用编辑 yaml、不用命令行：在网页上建案卷、传材料、看问题、填答复，点“运行下一轮”；审批人确认结果后，每期上传本期数据按模板计算。

```bash
pip install -e ".[agent,web]"
bonus-agent user add --data /srv/bonus 张会计账号 --roles editor --display 张会计       # 口令在终端输入，只存 PBKDF2 哈希
bonus-agent user add --data /srv/bonus 李经理账号 --roles approver --display 李经理
bonus-agent serve --data /srv/bonus --host 127.0.0.1 --port 8080 [--model local]
```

- 对话：案卷页的“对话”框可以直接问结果（智能体用 explain / evaluate 查算式，不自己算），也可以让它修改（改系数、改口径、答复问题）。
  改了规则、数据表或台账时，程序自动试算全部方案并做完成前检查，通过才生成新的一轮；只是提问不生成新的一轮。要求不明确或与材料冲突时智能体反问，不猜。
  对话里答复的问题记为“某某（对话）”，对话内容记入 `chat.jsonl` 和操作记录。
- 角色：`editor`（经办）传材料、答复问题、运行、按模板计算；`approver`（审批）确认结果；其他登录用户只读。
- 答复记录答复人和时间（写进 questions.yaml）；新建、上传（含 sha256）、删除、答复、运行、确认、按模板计算、下载都记入案卷的 `audit.jsonl`，页面上可查。
- 每个案卷同时只跑一轮，运行中不能改材料和答复；全局最多同时跑 `--max-parallel` 轮。
- 安全：默认只监听本机，内网访问请放在 HTTPS 反向代理（nginx 等）后面；网页登录（服务端会话，HttpOnly Cookie，8 小时过期，右上角可退出；同一用户名 5 分钟内输错 5 次暂停登录），表单带防伪码；上传只收支持的格式、只取文件名、单个不超过 50 MB；下载限制在案卷目录内，不提供 case.json；不开放 API 文档页。
- 数据目录结构见 `bonus_agent/web.py` 开头的说明；材料、结果都只存在这台服务器上。生产环境请在 `models.yaml` 里把默认模型改成本地 vLLM，数据不出内网。

### 本地模型

在 `bonus_agent/config/models.yaml`（或自己的 `config/models.yaml`）的 `local_vllm` 里填 vLLM 地址，把 `default` 改成本地模型即可；本地 vLLM 支持 `json_mode: json_schema` 约束解码，输出更稳定。

## 下一步

- 计算说明 Word 生成（模板 + 数字占位符校验）
- 问题分工（按类型指定答复人）、更细的权限
- LangGraph 状态机与人工确认节点
