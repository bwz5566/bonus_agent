# 奖金计算智能体（bonus-agent）

为财务部提供季度奖金、年度奖金、过程奖金等场景的计算服务。核心原则：**大模型只负责读懂规则、匹配表头，所有金额由确定性的计算引擎算出**，每一分钱都能追溯到规则原文和数据行。

当前处于**模型选型阶段**：智能体通过百炼 API 调用千问 3.6 / 3.5 的开源多模态模型（最初计划的 Qwen3-VL 32B / 30B-A3B / 8B 已在百炼下线），用评测脚本对比效果，确定后再私有化部署（只改 `config/models.yaml` 的 provider）。

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
config/models.yaml     模型与服务商配置
examples/sales_q3/     示例：规则、示例数据（格式故意不规整）、字段映射、调整记录
examples/auth/         服务间鉴权签名头示例（Python 签名与校验、Java 签名），给前后端对接用
tests/                 单元测试、属性测试、端到端测试
```

## 安装

```bash
pip install -e ".[agent,dev]"
python -m pytest          # 36 个测试，应全部通过
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
| set_rules | 写入或修改规则（阶梯、系数、变量、奖金池、分配层级和权重、发放条件），返回静态校验结果 |
| trial_run / explain / evaluate | 用真实数据试算、查看某人的算式、计算任意表达式来核对 |
| ask_question | 记录需要财务确认的问题，按建议理解继续 |
| finish | 完成（要求最近一次修改后已成功试算） |

工作区状态（数据表、规则、问题、试算结论）每轮都会附给模型，对话变长时只保留最近的历史，模型不会忘记已经做过的事。
金额始终由计算引擎按模型写的规则算出，每一分钱可追溯；模型只负责“读懂并建模”。

命令行会实时打印每一步。输出目录里有：`result.xlsx`（结果 +「待确认问题」「智能体规划」「智能体工作记录」）、`rules.yaml`、`mapping.yaml`、`questions.yaml`、`agent_log.json`。

文档没写清楚的地方先按模型建议的理解计算。财务在 `questions.yaml` 的 `answer` 填写答复（选项序号或文字）后重跑：

```bash
bonus-agent auto --answers 输出目录/questions.yaml --out 输出目录2 本批次文件夹
```

确认无误后，`rules.yaml` + `mapping.yaml` 就是固定的计算模板，以后每期换数据直接 `bonus run`，不再调用大模型。

其他：`--max-steps` 限制智能体轮数（默认 40）；`--pipeline` 改用固定流程（规划 → 抽取 → 计算），给工具调用能力弱的模型用；模型不支持原生工具调用时会自动改用 JSON 动作协议。

支持的方案结构：奖金池 + 多层按权重分配；“每人按公式直接算”的方案（总包 = 各人金额之和）；公司级数据表（如各月利润）；某些人不参与（权重为 0）；按比例暂缓发放。

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

已配置的模型分组（`config/models.yaml`，2026-10-28 用 `--remote` 实测）：

| 分组 | 模型 | 说明 |
| --- | --- | --- |
| candidates（默认） | qwen3.6-35b-a3b / qwen3.6-27b / qwen3.5-35b-a3b / qwen3.5-27b | 百炼在售、权重开源，可私有化部署 |
| open | 以上 + qwen3.5-122b-a10b | 开源模型全集 |
| reference | qwen3-vl-plus / qwen3.6-flash | 商业版，不能私有化，只看效果上限 |
| legacy | qwen3-vl-32b / 30b-a3b / 8b | 已在百炼下线，仅作记录 |

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

在 `config/models.yaml` 的 `local_vllm` 里填 vLLM 地址，把 `default` 改成本地模型即可；本地 vLLM 支持 `json_mode: json_schema` 约束解码，输出更稳定。

## 下一步

- 计算说明 Word 生成（模板 + 数字占位符校验）
- FastAPI 服务与数据库（按技术方案的接口和表结构）
- LangGraph 状态机与人工确认节点
