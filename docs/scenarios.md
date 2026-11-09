# 场景：按奖金类型分开计算

每类奖金（提成奖金、开发奖金、年度奖金……）是一个**场景**，各有固定的输入格式和规则。
思路是“智能体帮忙定规则，程序按定好的规则算钱”：

| 用法 | 谁做什么 | 调不调用大模型 |
| --- | --- | --- |
| 编制 | 智能体读场景说明和本期材料，起草规则并试算；财务答复问题、核对口径单，确认后**发布**成场景的规则版本 | 调用 |
| 计算 | 选已发布的规则版本，传本期数据，先按输入模板检查，再直接算 | 不调用 |
| 对话 | 对结果提问、要求修改（修改会生成新一轮，需重新确认、发布） | 调用 |

没有场景的老案卷按原来的通用流程照常使用。

## 场景包

一个场景一个文件夹，文件夹名就是场景编号：

```text
sales_commission/
  scenario.yaml        名称、说明、输入模板、验收用例
  guide.md             场景说明：财务给的规则和口径（编制时作为一份材料交给智能体，可按原文引用）
  acceptance/          验收用例的数据和期望结果（可选）
```

`scenario.yaml` 示例（字段名只是示意，按财务实际的表填写）：

```yaml
id: sales_commission
name: 提成奖金计算
description: 按月计算销售提成。
inputs:                          # 输入模板：每期数据要有的表和列
  - name: 提成核算明细             # 按工作表名或文件名认；认不出时按列名认
    aliases: [核算明细]
    columns:
      - {name: 月份, type: date}
      - {name: 客户简称, nonblank: true}
      - {name: 本位币销售额, type: number}
  - name: 分配名单
    columns:
      - {name: 人员}
      - {name: 角色}
      - {name: 分配比例, type: number, required: false}
outputs: 按人汇总当月提成，单列待分配和冻结的金额。
acceptance:                      # 发布规则版本前必须逐分对上
  - id: 2026年9月
    data: [acceptance/9月数据.xlsx]
    expected: acceptance/9月财务结果.xlsx
```

- 列的 `type` 有 `text`（默认）、`number`、`date`；`required: false` 表示可以没有这一列，`nonblank: true` 表示每行都要填。
- 不认识的字段会直接报错，避免写错了没发现。
- 验收用例的期望结果只用来比对，不会交给智能体。

## 放在哪里

- **本机场景目录**：网页数据目录下的 `scenarios/`（`bonus-agent serve --data D:\bonus_web` 时就是 `D:\bonus_web\scenarios\`），
  或环境变量 `BONUS_SCENARIO_DIRS` 指定的目录。公司真实的场景说明、输入模板和验收数据放在这里，**不进仓库**。
- **仓库自带**：`bonus_agent/scenarios/packs/` 里只有提成、开发、年度三个空骨架（只有名称），本机目录里的同名场景会覆盖它们。
- 发布的规则版本和场景记忆写在本机场景目录下：`<场景>/releases/v1/`、`<场景>/memory.yaml`。

## 编制 → 发布 → 计算

1. 网页“新建案卷”选场景、选“编制”，上传本期材料，运行。智能体会看到：
   - 场景说明（作为一份材料，可以引用原文）；
   - 输入模板和本期材料的检查结果（缺表、缺列、数字列不是数字会先报出来，让财务补正，不让智能体猜）；
   - 场景记忆：以前发布时财务确认过的口径。材料同样适用时可以沿用，拿不准仍要提问。评测时可在新建案卷时取消“参考场景记忆”。
2. 财务答复问题、核对口径单，确认某一轮，冻结成计算模板（原有流程）。
3. 审批人点“发布为场景的新规则版本”。配了验收用例时，先按模板复算每个用例并与期望结果逐分比对，全部一致才发布。
   版本只增不改（v1、v2……）；本案卷里已答复的问题记为场景记忆。
4. 以后每期新建“计算”案卷，选规则版本、上传本期数据：先按输入模板检查，通过后直接算出结果，不调用大模型。
   版本文件被改过会拒绝计算。规则要改时，新建编制案卷，确认后发布新版本。

命令行同样可用：

```bash
bonus-agent auto --scenario sales_commission --period 2026-09 --scenario-dir D:\bonus_web\scenarios --out 案卷 材料文件夹
bonus-agent confirm --out 案卷 --by 财务甲
bonus-agent publish --scenario sales_commission --out 案卷 --by 财务甲 --scenario-dir D:\bonus_web\scenarios
bonus-agent compute --scenario sales_commission --version v1 --period 2026-10 --out 结果 --scenario-dir D:\bonus_web\scenarios 本期数据.xlsx
bonus-agent scenarios --scenario-dir D:\bonus_web\scenarios
```

## 核算期间：任意月份、季度、年度

规则里不写死月份。新建案卷时填核算期间（`2026-09`、`2026Q3`、`2026`、`2026-07~2026-09`、`2026-09-01~2026-09-15`），
计算时每次另选：

- 智能体定义数据表时，给有日期或月份列的业务明细（销售、回款、出货……）写 `period`，程序只取落在期间里的行，
  试算结果里会提示每张表取了几行、期间外几行。名单、预算、系数表这类跨期的表不写 `period`。
- 表达式要用“当期”时写 `period_start()` / `period_end()`，比如按当月取预算：`month(period_end())`。
- 日期写法认不出、空值、或者一行只有一部分落在期间里（如按年记的数据算某个月），会拦下来说明，不会静默算进或丢掉。
- 确认时记下当时的期间；发布后的规则版本换任何期间都能算（`bonus-agent compute --period 2026-10`，网页计算时填期间）。

## 全局能力和场景专属能力

读表格、读 Word/PDF/PPT、看图、分析规则、计算引擎、核对和复算是**全局能力**，所有场景都能用。
场景专属的内容只来自场景包（场景说明、输入模板、验收用例、记忆）；通用提示词里不写任何场景专属的内容
（`tests/test_prompt_leakage.py` 检查）。场景需要额外的技能或外部服务（MCP）时，在 `scenario.yaml` 的 `tools` / `mcp` 里列出，默认都不开。
