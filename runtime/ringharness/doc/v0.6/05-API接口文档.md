# API接口文档：Temporal 内部边界修订

> v0.6 / DESIGN_REVISION · 2026-09-11。Temporal 技术栈修订，尚未实现、未冻结、未完成运行验收。本文与链接的 v0.5 基线合读：本文明确列出的替换规则优先，其余领域、安全及验收要求全部继承；不把历史“未开始”状态当作当前代码状态。

继承基线：[05-API接口文档.md](../05-API接口文档.md)。

## 1. 公共兼容

继承v0.5 Goal/Task/Activity/Effect/Command、身份、三态与错误信封。用户命令继续经API与Kernel；不暴露Temporal start/signal/cancel/reset管理端口给浏览器或模型。START返回命令受理结果，Temporal不可用则待投递；不谎称运行已开始。

公共 GoalResource 新增两个只读编排元数据字段（已实现并进入生成契约）：`orchestration_backend:LEGACY/TEMPORAL`（默认 LEGACY，存量与幂等缓存缺字段时不误切）与 `owner_epoch:Epoch`（默认"1"）。二者是 Resource 展示字段，不进入 Goal 合同 Content 与任何签名摘要；浏览器只读，不能经写请求提交或修改（提交未知写字段按 v0.5 规则拒绝）。Temporal 状态不改写 GoalStatus 枚举。

现有路由并不因文档变更删除；LEGACY路径保留到迁移完成。新增内部签名以下是设计接口，不是已存在HTTP端点，不进入当前OpenAPI implemented清单。

## 2. 新内部契约（JSON，待生成schema）

所有UUID/digest/timestamp沿用原规则；epoch用十进制字符串。除标为nullable的字段外均必填。

| 类型 | 字段与约束 |
|---|---|
| OrchestrationBinding | project_id:uuid, goal_id:uuid或null, budget_scope_id:uuid, backend:LEGACY/TEMPORAL, owner_epoch:Epoch, namespace:string, workflow_id:string, active_run_id:string或null, worker_build_id:string, contract_digest:Digest |
| RuntimeActionRef | project_id:uuid, goal_id:uuid或null, activity_id:uuid, action_id:uuid, owner_epoch:Epoch, binding_digest:Digest；只含引用，不含模型正文或凭据 |
| RuntimeAttemptLink | activity_id:uuid, attempt_id:uuid, namespace:string, workflow_id:string, run_id:string, temporal_activity_id:string, temporal_attempt:int≥1, fencing_epoch:Epoch, owner_epoch:Epoch |
| DeliveryReceipt | command_id:uuid, workflow_id:string, run_id:string或null, delivery_status:PENDING/ACKNOWLEDGED；ACK只代表编排收到 |

Workflow ID是稳定业务ID派生的内部标识，Continue-As-New保持Workflow ID，run_id变化。运维活动没有Goal时以operation_id构造Workflow ID；预算仍绑定project运维scope。不要将run_id用作effect幂等键。

## 3. 服务调用面

| 设计调用 | 输入 | 输出/拒绝 |
|---|---|---|
| ensure_workflow | Binding + command_id | DeliveryReceipt；已存在但项目/owner不一致拒绝 |
| get_runtime_actions | Binding + expected业务版本 | RuntimeActionRef[]及持久等待条件；Kernel计算依赖但不直接派发 |
| admit_runtime_attempt | RuntimeActionRef + RuntimeAttemptLink关联字段 | 原ActivityLease；拒旧owner、旧fence、预算/信任/停止未决 |
| commit_runtime_outcome | 原Lease + 原kind outcome + command_id | 原业务结果；相同提交幂等，冲突拒绝 |
| reconcile_runtime | Binding + 原action/attempt引用 | 已受理结果、继续等待或需要安全核对；禁止自动换effect |

认证必须来自受信Worker身份与服务端持久绑定；不能信请求自报role/project。Temporal task token不等于Kernel授权。既有无目标全局claim对TEMPORAL Goal拒绝；只允许具名准入。kind/target/role/Scope仍按原固定映射。

## 4. 跨系统一致性

PG command、binding、outbox原子提交；唯一(command_id,event_kind)避免多次生成派发事件。Relay通过稳定workflow_id/command_id重投；Workflow对命令去重且Continue-As-New携带消费水位，PG保留权威消费记录。不得仅凭进程内set去重。

PG outcome成功后网络超时，重放同command_id查询旧回执。空响应、404、超时均不能推断动作未执行。技术attempt只能更新transport映射，只有Kernel核对后创建新的业务attempt与fence。

业务Activity/Attempt/Effect不因Workflow reset、重试、Continue-As-New重置。修复历史必须走受控新命令或原效果核对，禁止Temporal UI reset自动绕过业务审批。

## 5. 兼容与生成门槛

当前Content v3/Event v1保持原字节格式；编排元数据先作为独立内部契约，不能悄悄塞进旧签名内容。需要新增Content字段时另升级schema版本和向量。M0必须补齐上述契约JSON Schema、数据库唯一键/FK、错误码、Python/TS JSON交叉测试，才可冻结v0.6。

公共新增运维诊断接口尚未定稿，暂不增加成功stub或标已覆盖；Temporal状态不会改写原GoalStatus枚举。
