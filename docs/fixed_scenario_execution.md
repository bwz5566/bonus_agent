# 固定奖金场景：规则发布与批次计算

## 1. 这次调整了什么

默认网页和 `bonus-agent auto` 不再进入通用智能体建模循环。批次必须绑定一个已注册场景、一个已发布规则版本和一个批准的期间。
规则、固定映射、输入快照和程序版本决定计算结果；模型不能改变它们。

- 提成 `sales_commission`、开发 `eng_dev_bonus`、年度 `annual_bonus` 分别发布各自的规则包。
- 每个场景可以有不同字段、层级、公式和输出对象，不强制使用季度样例的结构。
- `pending_finance` 是场景初始材料状态。真正可运行的财务版本保存在发布库，不依赖旧迁入样例的 `evals/` 文件。
- 规则包尚未提供时显示未配置，不自动推导制度，不以产品批准替代财务发布。
- 本次测试使用临时合成数据验证技术流程，没有为真实业务填入任何奖金公式。

## 2. 财务需要交付的规则包

目录内包含：

```text
release.yaml         场景、版本、适用期间、验收用例
rules.yaml           使用现有 RuleSet DSL 的确定性规则
mapping.yaml         固定文件、工作表、表头行与字段映射
valid.xlsx           财务确认的正向验收数据
expected.json        财务确认或独立实现得到的完整期望结果
duplicate.xlsx       指定异常的反向验收数据
```

`release.yaml` 示例（只有文件引用和发布信息，不代表具体业务规则）：

```yaml
scenario: annual_bonus
version: v1
periods: ['2026']
rules: rules.yaml
mapping: mapping.yaml
cases:
  - id: finance_example
    data: [valid.xlsx]
    expected: expected.json
  - id: duplicate_employee
    data: [duplicate.xlsx]
    error_code: key_dup
allowed_tools: [describe_scenario, explain_object, list_issues]
```

发布要求：

1. 至少一个正向独立期望结果用例和一个反向阻断用例；不能只检查总额。
2. `expected.json` 的结构为 `R0 → nodes / payouts / rollups`。`nodes` 覆盖所有引擎节点（包括“全部”）；发放及汇总对象必须覆盖
   `final、held、cap、offset、deducted、paid、overpaid` 全部指标。缺失、额外或不一致的编号都拒绝发布。
3. 反向用例必须产生指定的错误代码，不能因为别的异常而算通过。
4. 映射覆盖规则的全部字段，指定 `file、sheet、header_rows`。第一版采用固定单行表头，字段顺序与映射顺序一致。
   不自动跳过合计、重复表头，不接纳分段换列、动态展开和模糊编号匹配。遇到这些材料应先制作经财务确认的固定模板。
5. 对应数据表不得使用 `unmatched: ignore`。必填、类型、主外键及规则中的业务校验失败时直接阻断。
6. 本阶段运行时没有可改金额参数入口；阈值、系数和公式变更统一发布新版本，防止任意对话改数。

完整的合成示例见 `tests/test_fixed_scenario_execution.py::fixture_package`，仅供开发和验收使用。

## 3. 发布与版本

```bash
bonus-agent publish-scenario --source 财务规则包 --release-dir /srv/bonus/releases --by 财务审批人
```

该管理命令应由获得财务审批授权的发布管理员在服务器执行。发布库目录的写权限只授予发布管理员；网页经办人没有发布接口。
`--by` 是审批留档信息，不是身份认证或电子签名系统的替代品。

所有验收通过后，一次性生成不可覆盖的 `场景/版本/` 目录。记录审批人、时间、全部文件指纹、验收报告和程序指纹。
再次发布同名版本会失败。文件修改会使版本失效；计算程序发生变化也必须重新验收并发布新版本。
版本不能在运行期间切换，系统不自动选择“最新版”。

目录复制到运行环境时要保留整个版本目录。生产运行不依赖仓库内的业务样例文件。

## 4. 执行与复核

```bash
bonus-agent auto --scenario annual_bonus --rule-version v1 --period 2026 \
  --release-dir /srv/bonus/releases --out /srv/bonus/batches/2026-annual 本批数据.xlsx
```

也可使用核心命令（计算过程相同）：

```bash
bonus run --scenario annual_bonus --rule-version v1 --period 2026 \
  --release-dir /srv/bonus/releases --from-pack --out result.xlsx 本批数据.xlsx
```

输入文件名可以按期更换，但必须能按固定工作表唯一绑定；所有上传文件都必须属于该版本的输入契约。
输入快照保存后，程序执行“固定映射 → 计算 → 金额勾稽 → 导出”。中间值和行来源沿用现有引擎追踪链。

任务目录包含 `inputs/、mapped.json、computed.json、validated.json、result.xlsx、run.json`。
失败时保留已完成的检查点，不发布可下载的正式结果。校验及计算成功后导出，最后提交完成状态。

网页新建案卷绑定场景、发布版本及期间；未配置场景不能启动计算。
材料上传或删除后，当前结果标记失效。审批人复核金额和过程后，可确认具体任务快照；确认过程重新核对输入及输出指纹，不能确认已经变化的结果。
确认不执行付款，不会把试算草稿转成业务规则。

## 5. 重复提交和故障恢复

- 一个案卷或命令行 `--out` 表示一个业务批次，不在不同批次间复用。
- 任务标识包含场景、发布版本及指纹、期间、输入内容、程序版本和精度策略。
- 相同输入重复提交返回已完成结果，且再次检查结果文件指纹。不同期间、输入或规则版本产生不同任务。
- SQLite 事务领取同一任务的执行租约；同时提交时只有一个执行者计算。租约每 10 秒续期，失去租约的执行者不能覆盖新执行者状态。
- 普通故障重试恢复映射、计算检查点；例如导出失败后不再次计算已保存金额。进程崩溃后的未续期租约最多等待 60 秒即可恢复。
- 该实现面向同一服务器、本地持久化磁盘的多进程运行，不支持把 SQLite 任务库放到跨机器共享文件系统上作为分布式调度器。

## 6. 只读智能体

批次计算完全不初始化模型。网页对话才按需调用模型，允许的工具仅为：

- `describe_scenario`：读取当前发布规则。
- `explain_object`：读取指定对象的引擎金额和计算链。
- `list_issues`：读取已完成结果的异常提示。

即使模型请求 `set_rules`、修改数据或其他未授权工具，执行器也直接拒绝，不会转发到旧 Workspace。
一次对话最多 5 次工具交互，工具结果有上下文大小上限。对话不能触发计算、付款、审批或规则发布。
对话只能引用通过完整性检查的当前结果；材料已经变化时不会把旧结果当成当前结果回答。

旧通用智能体仅保留 `bonus-agent auto --explore` 开发入口；旧迁入规则的核心命令必须显式使用 `--preview-migrated`。
旧 `confirm`、`approve-settlement` 命令同样必须显式使用 `--explore`，其草稿产物不能进入固定计算。
`create_app(exploration=True)` 仅供历史流程迁移测试，正常 `serve` 不开放这一选项。生产页面不会调用产品批准或冻结草稿的旧接口。

## 7. 本阶段仍需业务交付

三类实际奖金规则、固定数据模板、有效期间和独立验收结果仍由财务提供。
发布过程的审批职责需要与公司的授权管理配套；本次不臆造业务制度，也不宣称已完成真实业务或百万行性能验收。
