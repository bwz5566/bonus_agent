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
bonus_agent/           智能体：文档解析、规则抽取、字段映射推荐
  prompts/             提示词模板
  cli.py               命令行 bonus-agent
evals/                 模型选型评测
  cases/sales_q3/      评测样例：Word + 阶梯图片 + PPT 表格 + PPT 系数图片
  run_eval.py          命令行 bonus-eval
config/models.yaml     模型与服务商配置
examples/sales_q3/     示例：规则、示例数据（格式故意不规整）、字段映射、调整记录
examples/auth/         服务间鉴权签名头示例（Python 签名与校验、Java 签名），给前后端对接用
tests/                 单元测试、属性测试、端到端测试
```

## 安装

```bash
pip install -e ".[agent,dev]"
python -m pytest          # 27 个测试，应全部通过
```

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
