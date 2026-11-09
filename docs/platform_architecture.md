# bonus_agent 平台架构落地（存储 · 缓存 · 记忆 · Skill/MCP）

- **日期**：2026-11-06（Asia/Shanghai）
- **依据**：FINAI-FOUNDATION-SPEC、FINAI-UNIFIED-SYSTEM v2、七层蓝图；现仓 `bwz5566/bonus_agent`
- **与财务规则关系**：**本阶段不依赖财务「固定格式/规则」**。场景包可继续 `pending_finance`；平台能力全局可用。
- **原则**：LLM 只编排；金额只走 `bonus_core`；**结构化状态是权威事实**（聊天摘要/向量仅导航）；Redis/进程内存不能当唯一任务存储。

---

## 1. 目标（可验收，不靠财务口径）

1. 会话清空或进程重启后，能从**检查点**恢复当前案卷所需状态（场景、计划进度、已确认决定、待决问题指针）。
2. 材料原件与大工件进**对象/文件存储**；结构化索引与任务状态进**可事务存储**（V1 可用 SQLite/本地 Postgres 适配，接口先定 PostgreSQL 语义）。
3. **结果缓存**按完整指纹命中；命中仍校验权限与版本；缓存不是结算依据。
4. **三层记忆**可读写：工作记忆（当前轮）、情景记忆（案卷）、长期记忆（租户偏好/列别名/已发布规则指针）。
5. **Skill 注册表 + MCP 客户端骨架**存在；调用经场景 `allowed_tools` / `allowed_mcp` 过滤（无场景时仅允许全局只读探测类）。
6. 每轮对模型只注入 `context_manifest`（摘要 + 引用 ID），明细不进上下文。

---

## 2. 模块落点（对现仓）

| 能力 | 包路径（建议） | 说明 |
| --- | --- | --- |
| 存储抽象 | `bonus_agent/infra/storage/` | `ObjectStore`（本地目录先）、`StateStore`（SQLite→PG） |
| 任务/案卷状态 | `bonus_agent/infra/state/` | case/session/run/checkpoint/result_revision |
| 缓存 | `bonus_agent/infra/cache/` | 指纹键：输入+规则包版本+引擎/数值模式+代码版本；可选 Redis，默认真地盘 |
| 记忆 | `bonus_agent/memory/` | working / episodic / long_term；权威在 StateStore |
| 编排注入 | `bonus_agent/memory/context_manifest.py` | 构造每轮注入卡 |
| Skill | `bonus_agent/skills/` | 注册 + 加载说明书；不执行金额 |
| MCP | `bonus_agent/mcp/` | 客户端适配骨架 + 白名单 |
| 场景白名单 | 现有 `scenarios/packs/*/pack.yaml` | 增加 `allowed_tools` / `allowed_mcp` 字段（可空=仅全局默认） |

V1 **不强制**：Kafka、向量库、微服务拆分；向量检索可后加且不得当规则权威源。

---

## 3. 数据与记忆模型（精简）

**StateStore 表/集合（逻辑）**
- `sessions`：session_id、tenant、created_at、last_manifest
- `cases`：case_id、scenario_id、status、freeze_fingerprint
- `checkpoints`：run_id、step、payload_ref、fingerprint、created_at
- `decisions`：案卷内已确认映射/答复（只追加，替代关系显式）
- `result_revisions`：引擎产出指针 + validation 标记
- `memory_items`：layer、scope、key、value_ref、expires

**缓存键** = hash(scenario_id, rule_package_version|pending, input_fingerprint, engine_version, numeric_profile, code_digest)

**检查点**：节点成功后写；恢复时校验指纹与组织范围；失败不复用跨版本缓存。

---

## 4. 规则库分层（已确认，仍不填财务内容）

1. 现有 DSL/算子 → **通用规则库**（场景无关）
2. 财务材料到后 → **映射绑定**到场景包
3. 冲突 → **财务场景规则覆盖**通用库

本 PR 只留接口/`RuleLibrary` 占位与文档，不编造业务规则。

---

## 5. 实施切片（本波 PR）

1. `infra/storage` + `infra/state`：本地实现 + 接口
2. `memory` 三层 API + context_manifest
3. `infra/cache` 指纹缓存
4. checkpoint 读写接到 case/run 缝（最小侵入）
5. `skills/` + `mcp/` 骨架与白名单钩子
6. 文档 `docs/platform_architecture.md` + 单测（重启恢复、缓存未授权不命中、manifest 不含明细行）

**明确不做**：填真实财务契约；上生产 Postgres/Redis 集群（接口预留即可）。


---

## 6. 已落地（本分支实际实现）

| 能力 | 文件 | 要点 |
| --- | --- | --- |
| 存储 | `infra/storage/__init__.py` | `ObjectStore`：SHA-256 内容寻址、原子写、读时校验；`SQLiteStateStore`：每次写一个事务、每次查询带 scope，`decisions/checkpoints/result_revisions` 由触发器保证只追加 |
| 任务状态 | `infra/state/__init__.py` | `TaskState`：session/case/decision/checkpoint/result_revision；`resume()` 取该 case+run 最新检查点，指纹不符抛错 |
| 检查点接缝 | `infra/checkpoints.py` + `case.py` | `save_case()` 写完 case.json 后镜像一份检查点（case_id=案卷目录指纹，run_id=`round-N`，指纹=本轮输入文件 sha256 表）；`load_case()` 在 case.json 丢失时回落到最近检查点。**仅在设置 `BONUS_AGENT_STATE_DIR` 时启用**（可选 `BONUS_AGENT_STATE_SCOPE`，默认 `local`），检查点写失败只告警不影响本轮；`agent.py` 未改动 |
| 缓存 | `infra/cache/__init__.py` | 全指纹键 + scope；`authorized=False` 一律不命中；校验和不符不命中；缓存只存引用，不作结算依据 |
| 记忆 | `memory/__init__.py` | working/episodic/long_term 三层，值进 ObjectStore，索引进 StateStore（`memory_items`），支持 TTL |
| 上下文注入 | `memory/context_manifest.py` | 白名单投影：只含 ID、状态、轮次、待决问题 ID、检查点 id/run/step/fingerprint；不遍历明细行、金额、答复值、记忆值 |
| 场景白名单 | `scenarios/__init__.py` | `PackSchema.allowed_tools` / `allowed_mcp`，默认空列表；仍 `extra=forbid`。`eng_dev_bonus` 与 `annual_bonus` 保持 `pending_finance`。`sales_commission` 为 `migrated_draft` |
| Skill / MCP | `skills/__init__.py`、`mcp/__init__.py` | 默认拒绝；无场景时只放行全局只读 `list_files`；MCP 需精确 `server/tool`，只写 server 不放行 |
| 测试 | `tests/test_platform_storage.py` | 重启恢复/指纹不符、案卷接缝、缓存未授权不命中、manifest 不含明细、记忆隔离、Skill/MCP 拒绝 |

仍未做：PostgreSQL / Redis 适配器（接口已按 `StateStore` 协议预留）；`RuleLibrary` 占位；agent 内部节点级（非轮次级）检查点与基于检查点的跳步续跑。

## 7. 迁入草稿的产品书面批准

`sales_commission` 的正式结算不把包标成 `published`。批准记录的 `kind` 是
`product_settlement_approval`，`finance_signed` 固定为 false，并带当前绑定指纹。

- 未设置 `BONUS_AGENT_STATE_DIR` 时，案卷输出目录里的 `product_approval.json` 是结构化记录。
- 设置了该目录时，同一条记录追加进 `decisions`（`case_id` 为 `scenario:<pack>`）。状态库是权威；文件必须带上同一 `decision_id`，正文不一致就拒绝结算。
- 聊天摘要和网页提示不是批准。试算不读这条记录。
