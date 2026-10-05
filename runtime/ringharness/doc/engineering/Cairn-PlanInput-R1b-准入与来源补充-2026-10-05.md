# Cairn PlanInput/v1 · R1b 准入与来源（非冻结）

状态：代码已接入；本批仅做静态、生成契约与冻结校验。业务 E2E 留待融合完成后统一执行。本文件补充 R1a 接口说明，不修改 v0.5 冻结包。

## 选择模式

`PUT /api/v1/goals/{goal_id}/plan-input-mode` 仅允许认证 operator 在 DRAFT 阶段调用。请求为 `{ "expected_state_revision": 1, "mode": "REQUIRED" }`，返回 `Envelope<GoalResource>`。事务内锁 Goal，验证项目 scope、信任 OPEN、DRAFT 与版本号，再持久化 `plan_input_mode` 并推进 `state_revision`。`OPTIONAL` 为迁移默认值；旧 Goal 沿用原规划流程。`Goal.start` 的既有版本门与 Goal 行锁保证模式选择和启动串行。

## TEMPORAL PLAN 准入

`REQUIRED` 时，Kernel 取当前 Goal 版本与最新未发布候选，要求恰好一份相应的 STAGED PlanInput。Kernel 从原始 `payload_bytes` 复算 PlanInput 摘要，核对 jsonb 投影、Goal 合同版本和摘要、计划修订、候选状态、候选摘要和受限任务摘要。多个当前输入或任何漂移均失败关闭。`get_runtime_actions` 隐藏未满足条件的 READY PLAN 并返回 `PLAN_INPUT_REQUIRED` wait_hint；`admit_runtime_attempt` 在 Goal/Activity 行锁内复核，并把唯一 PlanInput ID 与 digest 写到新 attempt。未产生 attempt、Activity/Goal DONE 或 Plan 发布。

PlanInput 登记行继续 append-only 且为 STAGED。`BOUND` 的事实由 attempt 的不可变 ID/digest 钉扎表达，不更新 R1a 账本状态。

## ContextBundle 与 outcome

ContextCompiler 从 Ring 候选行重建规范 JSON，以候选 digest 物化并读取对象仓字节，校验后把工件作为 `CANDIDATE` InputArtifactRef 写进 ContextBundle。普通 context 写入口无法为 REQUIRED PLAN 跳过这一步。编译、持久化和 bind 分别重核 attempt、Goal、PlanInput、候选与工件目录；失租、摘要漂移或对象仓失败均拒绝。

PLAN outcome 在发布事务内重核 attempt 固定输入与已绑定 ContextBundle，并要求 outcome PlanCreate 正文等于固定候选正文。发布的是新 PUBLISHED Plan 行；旧 CANDIDATE 行仍是候选。新行记录 `source_plan_input_id` 与 `source_plan_input_digest`，数据库触发器禁止发布后改写来源。Goal DONE 仍只归 Kernel 最终屏障。

## 后续接缝与验证边界

- R3：Temporal 对 `PLAN_INPUT_REQUIRED` 的持久等待及新 PlanInput 到达后的唤醒；现有 `NO_READY_WORK` 终止策略须联调，不能把等待误报为 Goal DONE。
- R2：Runner/模型实际消费 ContextBundle 的 `CANDIDATE` 工件，以及模型输出与固定候选一致性的端到端证据。
- 本批固定静态命令：`uv run ruff check <changed Python paths>`、`python3 -m compileall -q <changed paths>`、`uv run python scripts/generate_contracts.py`、`python3 doc/tools/check_spec.py --verify-freeze`、`git diff --check`。这些结果不构成业务 E2E 验收。
