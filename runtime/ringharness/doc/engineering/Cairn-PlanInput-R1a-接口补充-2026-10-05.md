# Cairn PlanInput/v1 · R1a 接口补充（非冻结）

状态：**R1a 已实现**；R1b 的代码与接口见
[准入与来源补充](Cairn-PlanInput-R1b-准入与来源补充-2026-10-05.md)，R2/R3 未实现。
本文是 `doc/05-API接口文档.md` 冻结基线之外的增量接口说明；按
AGENTS §7.1 红线，`doc/05` 与 `doc/releases/v0.5` 冻结字节不随本批改动，待批次
合并时由文档权威方统一并入。

权威契约来源：Cairn fork `04a7647`
`cairn/src/cairn/server/integration/plan_snapshot.py:seal_snapshot`；消费缝设计见
Cairn `docs/integration/plan-consumption-seam.md`。Ring 侧实现：
`packages/control_kernel/src/control_kernel/protocols/plan_inputs.py`、
`storage/plan_inputs.py`、`apps/control/src/control_api/routes/plan_inputs.py`、
迁移 `0043_plan_inputs`。

## 路由

| 方法与路径 | 角色 | 请求 | 返回 |
|---|---|---|---|
| POST /api/v1/goals/{goal_id}/plan-inputs | O | PlanInputPayload + `Idempotency-Key` | 201 Envelope\<PlanInputResource\> |
| GET /api/v1/goals/{goal_id}/plan-inputs/{plan_input_id} | V/O/A/admin | 无 | 200 Envelope\<PlanInputResource\> |

失败关闭：401 未认证；403 角色不符；404 Goal/输入不存在、跨项目、或 ID 与路径
goal 不符（统一口径，不泄漏存在性）；409 `IDEMPOTENCY_CONFLICT`（同键异体）或
`INVALID_STATE`（信任非 OPEN）；422 `VALIDATION_ERROR`（协议/校验失败，message
带 `PLAN_INPUT_*` 机器码）；503 依赖缺失。

## PlanInputPayload（固定 schema，禁止增删字段）

与 Cairn `seal_snapshot` payload 逐字段一致；extra=forbid。边界与 Cairn 同值：
canonical JSON（`sort_keys`、紧凑分隔符、非转义）≤ **8192 B**；单条文本 ≤
**2000 B**（禁控制字符与凭据样式文本）；候选摘要 tasks ≤ **32**、coverage ≤
**64**。`source_fact_ids` 最多 32 条，`hint_ids` 最多 32 条，均排序去重；
`selected_text` 按一个 Intent、所选 Facts、所选 Hints 的顺序逐一对应，
最多 65 条。每个 ID 最多 100 字符；总量仍受 8192 B 字节闸门约束。

字段：`schema`（恒 `"PlanInput/v1"`）、`cairn_project_id`、`ring_project_id`、
`ring_goal_id`、`graph_digest`、`selected_intent_id`、排序去重的
`source_fact_ids`/`hint_ids`、`selected_text[]（id/text/source_kind∈intent|fact|hint）`、
`candidate_plan_id`、`candidate_content_digest`、`candidate_summary
（task_count/tasks[]（id/objective/depends_on 排序）/coverage[]）`、
`goal_contract_revision`、`goal_contract_digest`、`expected_plan_revision`、
`created_by`。

## Ring 侧事务内核验（全部失败关闭，任一不符即 422/404/409，零副作用）

1. Goal 存在（404）+ 项目 scope（`check_scope`，404）+ 信任 OPEN（409）。
2. `ring_goal_id`＝路径 Goal；`ring_project_id`＝Goal 项目；`created_by`＝认证
   subject（不信自报主体）。
3. `goal_contract_revision`/`goal_contract_digest`/`expected_plan_revision` 必须
   等于 `goals` 行当前值（合同修订、合同摘要、计划修订三重一致）。
4. Goal 状态 ∈ {PLANNING, RUNNING}（与 CANDIDATE 提交同门；DRAFT 预登记归 R1b）。
5. **候选以 Ring DB 重取为准**：`plans` 行存在、同项目同 Goal、`status=CANDIDATE`
   且 `plan_revision IS NULL`；`content_digest` 与 payload 声明一致；受限摘要由
   DB 行**重算**（objective/depends_on 排序、coverage 四元组、边界）后与
   `candidate_summary` 深度比对。Cairn 复制的正文/digest 一律不作数。
6. 幂等：scope＝(goal, subject, POST, path, key)，body digest＝canonical payload
   （含 `candidate_plan_id`）。同键同体回放缓存；同键异体 409，绝不静默换绑定。

## 存储与读取

`plan_inputs`（append-only，UPDATE/DELETE 触发器拒绝）：

- `payload_bytes bytea`——**canonical 原始字节账本**，≤8192 B CHECK 兜底；
  `content_digest = sha256(payload_bytes)`，读回时逐字节复现校验，损坏即失败关闭。
- `payload jsonb`——可查询投影（非权威，jsonb 规范化不承诺字节一致）。读回统一
  从字节账本解析 payload，并核对投影与字节解析结果深度相等；旁路写坏投影同样
  失败关闭，绝不返回 digest 证明不了的 payload。
- CANDIDATE 钉扎列：`candidate_plan_id`、`candidate_content_digest`、
  `goal_contract_revision`、`expected_plan_revision`、`created_by`、`status`
  （R1a 恒 `STAGED`；`BOUND/STALE` 归 R1b admit 钉扎）。
- 事件：`project_events(PLAN_INPUT_RECORDED)` + `goal_events(PLAN_INPUT_RECORDED)`。

## R1a 明确不做

R1a 不含 PLAN admit、`plan_input_mode=REQUIRED`、ContextBundle `InputArtifactRef`、Runner
读取端口、Kernel outcome 来源关系或 Temporal。这些能力分属 R1b/R2/R3。CANDIDATE 不因
登记而变 `PUBLISHED`；输入登记 ≠ Evidence PASS；不写 Goal/Task DONE。
