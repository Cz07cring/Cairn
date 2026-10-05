# Shannon 恢复 / Checkpoint / Temporal 演变 / 控制信号研究

> 研究对象：`/tmp/shannon-study`（Kocoro-lab/Shannon）  
> 对照对象：`/Users/ring/Documents/code/ringharness`  
> 方法：静态只读源码与文档核查；未运行 Shannon 集成测试。以下只写实际读到的证据；全仓未命中之处用“未发现”而非断言不存在。

## 结论先行

1. Shannon 的真正崩溃恢复底座是 **Temporal event history + replay**；其名为 `StateChannel.Checkpoint` 的快照只是进程内 `map`，使用非确定性 `time.Now/uuid.New`，没有 PostgreSQL 落盘或服务重启后的恢复入口，不能当作 durable checkpoint。
2. 未发现 Shannon 对“崩溃 Turn”做 PostgreSQL 索引发现或启动扫描，也未发现恢复三道门（恢复开关、过期窗口、尝试次数上限）的完整实现；相应常量为 **未发现/不适用**，不是“0”。
3. Workflow 演变大量使用 `workflow.GetVersion`；有 history 导出/replay 工具和 CI 步骤，但仓库当前未发现 `tests/histories/*.json`，因此 CI 可直接跳过，历史回放门禁可能是空门。
4. 通用 control handler 实现 pause/resume/cancel/inject，但取消是“发 child signal → sleep 1s → Cancel”，且未设置 `WaitForCancellation`、未见 detached cleanup，存在清理未完成即父流程结束的风险。
5. Shannon 有请求级 Redis 幂等、数据库 `ON CONFLICT` 和不同 Activity retry 档位，但未发现 Ringharness 所要求的稳定 `effect_id` + `UNKNOWN` + reconciliation 闭环。因此只能借鉴分层思路，不能照搬其副作用保证。
6. Shannon server 将 Temporal `WORKFLOW_EXECUTION_STATUS_COMPLETED` 映射为任务 `COMPLETED`；这与 Ringharness“DONE 只走 Kernel”红线冲突，明确不应移植。

---

# ① 逐问题发现（文件:行号 + 关键代码）

## 1. Checkpoint 与恢复

### 1.1 Shannon 有三种名字相近、语义不同的“checkpoint”

#### A. 应用内状态快照：只在内存，不是持久恢复点

`go/orchestrator/internal/state/channels.go:12-20`：

```go
type StateChannel struct {
    ...
    checkpoints map[string]Checkpoint
}
```

`channels.go:47-58` 初始化普通 Go map；`channels.go:102-123` 将当前状态 JSON 化后，以 `uuid.New()` 为 ID 写入该 map；`channels.go:126-151` 的 Restore 也只从同一 map 取数据。没有 DB/Redis/Temporal Activity 持久化。

```go
checkpointID := uuid.New().String()
sc.checkpoints[checkpointID] = checkpoint
```

`channels.go:53-54,89,112-116` 还在 Workflow 可达代码中使用 `time.Now()` 和随机 UUID，而不是 Temporal 的确定性时间/side effect。若该逻辑在 replay 时执行，存在非确定性风险；未见 `workflow.SideEffect` 包装。

实际生产 Workflow 用例仅明确找到 Streaming：

- `go/orchestrator/internal/workflows/streaming_workflow.go:88-92`：执行前 checkpoint，metadata phase=`pre-execution`；
- `streaming_workflow.go:172-176`：成功结果生成后 checkpoint，phase=`completed`；
- `streaming_workflow.go:247-248`：仅把 checkpoint ID 列表塞回结果 metadata。

因此它确实位于阶段边界，但**未发现失败后调用 `Restore` 的 Workflow 入口，也未发现跨进程持久化**。错误分支 `streaming_workflow.go:132-148` 只更新内存状态并返回失败，没有 checkpoint。

#### B. Pause checkpoint：控制状态标记，不是业务状态快照

`go/orchestrator/internal/workflows/control/handler.go:34-63` 建立 pause/resume/cancel/inject signal 和 `control_state` query；`handler.go:97-148` 的 `CheckPausePoint` 在协作式安全点消费 cancel/pause，并用 `workflow.Await` 等待 resume/cancel。`handler.go:165-171` 将 phase 和 workflow time 记到 `LastCheckpoint`：

```go
h.State.LastCheckpoint = map[string]interface{}{
    "phase": phase,
    "timestamp": workflow.Now(ctx),
}
```

这只是 Temporal history 内的控制状态，优势是 replay-durable；没有完整步骤产物、effect receipt 或 schema version。

#### C. Swarm 的 2 分钟 checkpoint timer：周期性 Lead 监控，不是快照

`go/orchestrator/internal/workflows/swarm_workflow.go:2411-2415`：

```go
checkpointInterval := 2 * time.Minute
```

`swarm_workflow.go:2637-2641` timer 到期产生 `LeadEvent{Type:"checkpoint"}`，用于定期唤醒 Lead。注释“reduced from 60s to 120s”自相矛盾，但代码常量是 **120 秒**。它不是写快照；更接近阶段 Watchdog tick。

### 1.2 防抖、持久化与崩溃 Turn 发现

- **Checkpoint 防抖**：未发现通用 debounce。Streaming 只在前/后两个边界打内存快照；Swarm 是固定 120 秒周期 tick。
- **持久化**：业务 task/agent/tool execution 有 PostgreSQL 表（`migrations/postgres/002_persistence_tables.sql:6-98`），事件有 PostgreSQL event log（`004_event_logs.sql:6-27`）；但 `StateChannel.checkpoints` 未接这些表。
- **崩溃 Turn 重新发现**：Temporal 自身会凭 event history 重新投递 Workflow/Activity task；Shannon 应用层未发现按 PostgreSQL RUNNING/turn 索引扫描并重启遗留 Turn 的实现。server 的 `watchAndPersist` 是启动请求后的 goroutine watcher（`go/orchestrator/internal/server/service.go:3701-3756`），不是服务启动时的全局 orphan scan。
- migrations 中未发现 `checkpoint`/`recovery_attempts`/`intent_valid_until` 等恢复专表或索引。

### 1.3 “恢复三道门”核查

对全仓检索 `intent_valid_until`、`checkpoint_schema_version`、`recovery_attempts`、`RECOVERY_ENABLED/recovery_enabled`、`RecoveryMaxAge` 等结果为 0。故 Shannon 未发现以下应用级恢复门：

| 门 | Shannon 证据 | 常量/位置 |
|---|---|---|
| 显式恢复开关 | 未发现 | 未发现 |
| 意图过期窗口 | 未发现 | 未发现 |
| 自动恢复次数上限 | `AgentState.RetryCount < 3` 是普通失败重试资格，不是 durable recovery gate（`state/types.go:114-118`） | 3，但**不应误写成恢复门常量** |
| checkpoint schema compatibility | StateChannel 写死 Version `1.0.0`，Restore 不检查 checkpoint schema（`state/channels.go:47-58,126-151`） | 无门禁 |

结论：三道门并非 Shannon 的可直接复用实现，反而是 Ringharness 必须补齐/固化的不变量。

## 2. Temporal 工作流演变

### 2.1 使用 `GetVersion`，未发现 Temporal Patch API

Shannon Go Workflow 大量用 `workflow.GetVersion(ctx, changeID, DefaultVersion, n)`。例如：

- 控制信号兼容：`control/handler.go:31-36`，change ID=`pause_resume_v1`；旧历史直接不安装信号处理。
- 新增预算 Activity：`orchestrator_router.go:460-465`，change ID=`force_research_budget_v1`，明确说明防止 in-flight 旧历史 replay 时遇到新 Activity。
- 多版本演进：`strategies/domain_analysis_workflow.go:170-172` 使用 `domain_prefetch_discover_only_v1`（max version 3）等。

未发现 Go SDK 的 `Patch/DeprecatePatch`；主要演变手段是 `GetVersion`。

### 2.2 历史 replay 测试存在，但门禁可能为空

- `go/orchestrator/tests/replay/workflow_replay_test.go:17-58`：读取 `tests/histories/*.json`，注册若干 Workflow 后 replay；若目录不存在或无 JSON，测试 `Skip`。
- `go/orchestrator/tools/replay/main.go:26-46`：注册 10 个 Workflow，通过 `ReplayWorkflowHistoryFromJSONFile` 检测 nondeterminism。
- 根 `Makefile:146-165`：`replay-export` 从 Temporal 导出历史；`Makefile:167-180`：`replay` 和 `ci-replay`；无 history 时明确打印 `no histories found, skipping`。
- `.github/workflows/ci.yml:60-67`：Go build/test 后运行 `make ci-replay`。
- 实际枚举 `/tmp/shannon-study/tests` 与 `go/orchestrator/tests` 未发现 JSON history fixture。因此“CI 配了 replay”已验证，“CI 当前拥有固定历史语料”未验证，静态结果显示为空。

另一个覆盖缺口：replay tool 注册列表不含 `SwarmWorkflow`、`AgentWorkflow`、`DomainAnalysisWorkflow` 等近期复杂 Workflow（`tools/replay/main.go:27-38`）。

### 2.3 Continue-As-New：生产并未真正续跑

全仓唯一明确 history-length 逻辑位于 `swarm_workflow.go:3610-3617`：超过 **8000 events** 后设置 `historyTruncated=true` 并 break 去 synthesis；注释明确写着“full ContinueAsNew with snapshot in Phase 3.4”。未调用 `workflow.NewContinueAsNewError`。

因此 Shannon 当前：

- 不携带 watermark；
- 不携带 inbox/已消费 signal ID；
- 不携带 in-flight Activity identity/status；
- 不会在新 run 续观测在途 Activity；
- 只是提前结束并综合部分结果。

这比 Ringharness 已落地的 CAN 水位和 carried Activity 观察弱，**不适用直接移植**。

### 2.4 RetryPolicy 按 Activity 用途有区分，但不是统一类型矩阵

代表性配置：

- 事件/旁路记录：`simple_workflow.go:40-43`、`opts/opts.go:14-19`，`MaximumAttempts=1`；
- Simple 主执行：`simple_workflow.go:71-76`，2 次；
- Streaming 主执行：`streaming_workflow.go:94-101`，5 分钟、heartbeat 30 秒、2 次；
- Agent：`agent_workflow.go:58-80`，普通 5 分钟/heartbeat 3 分钟，LP agent 10 分钟/heartbeat 5 分钟；指数退避 1 秒起、系数 2、最大 1 分钟、最多 3 次；
- Swarm：主 activity 2/3 次，P2P/emit 多为 1 次（`swarm_workflow.go:496-516,1747-1769,1817-1819,1873-1876`）。

优点是区分“主要计算 / best effort 事件 / P2P”；不足是未看到基于“只读、幂等写、不可逆副作用、对账”的中央策略表，且 `NonRetryableErrorTypes` 使用很少/未形成矩阵。

## 3. 控制信号（pause / resume / cancel / inject）

### 3.1 统一 handler + 协作式安全点

`control/signals.go:3-8` 定义 pause/resume/cancel/inject 名称；`control/handler.go:31-90`：

- `pause`：设置 `Paused=true`、记录 reason/time；
- `resume`：清 pause；
- `cancel`：设置 `Cancelled=true`、记录 reason；
- `inject`：把 payload 追加到 `InjectedContext`；
- query `control_state` 暴露当前控制状态。

`handler.go:97-148` 的 `CheckPausePoint` 才真正执行控制：先非阻塞消费 cancel/pause；暂停时 `workflow.Await` 到 resume/cancel。意味着 pause/cancel 不是抢占，延迟取决于 Workflow 是否在合理位置调用 pause point。

调用点例子：

- `streaming_workflow.go:110-113`：主 Activity 前；
- `simple_workflow.go:155-158,218-220,293-295,365-367`：memory 后、执行前/后、持久化前；
- `template_workflow.go` 多阶段边界也调用。

这正是可借鉴的“阶段边界安全点”。

### 3.2 注入只累计；如何被业务消费并不统一

`handler.go:84-89` 仅 append context；`handler.go:183-194` 提供 GetInjectedContext/ClearInjectedContext。未发现通用合并规则、去重 ID、schema 校验、TTL 或 actor/审计字段。直接移植会让 replay-safe injection 变成无治理输入。

### 3.3 子流程信号传播有实现，但 cancel 清理不可靠

`handler.go:218-225` 动态登记 child ID；`handler.go:229-244` 通过 `SignalExternalWorkflow` 转发信号。

取消路径 `handler.go:254-280`：

```go
for _, childID := range h.State.ChildWorkflowIDs {
    SignalExternalWorkflow(..., SignalCancel, ...).Get(...)
}
workflow.Sleep(ctx, time.Second)
for _, childID := range ... {
    RequestCancelExternalWorkflow(...).Get(...)
}
```

问题：

1. 固定 sleep 1 秒不是 cleanup receipt/ack；
2. `RegisterChildWorkflow` 仅保存 ID，没有 run ID；
3. actual child options 仅设置 `ParentClosePolicy: REQUEST_CANCEL`（如 `orchestrator_router.go:517-519`），未设置 `WaitForCancellation=true`；
4. 文档示例却宣称 `WaitForCancellation: true`（`docs/multi-agent-workflow-architecture.md:243-252`），文档与实际实现不一致；
5. 未见 disconnected context/detached cleanup activity。

因此可借鉴“先业务取消信号、再 SDK cancel”的两阶段意图，但不能照搬 1 秒睡眠；Ringharness 必须继续以 StopReceipt/资源释放证据为准。

### 3.4 避免把 Workflow COMPLETED 当业务完成：Shannon 没有守住

- 文档将 `WORKFLOW_COMPLETED` 直接描述为“Workflow finished successfully”（`docs/event-types.md:64-76`）。
- Agent Workflow 在 Activity 成功后发 `StreamEventWorkflowCompleted`（`agent_workflow.go:152-165`）。
- server watcher 在 Temporal status completed 时构造 `TaskStatusCompleted` 并持久化（`internal/server/service.go:3725-3756`）。

Swarm streamer 对最终答案有更细一层：只有 `AgentID=="final_output"` 的 LLM_OUTPUT 才作为 canonical output（`cmd/gateway/internal/openai/streamer.go:283-326`），这是好的传输层分流；但它仍不是独立审计/Kernel 验收。

对 Ringharness：**Workflow COMPLETED、Activity SUCCEEDED、模型 done 都只能是技术/候选状态，绝不可成为 DONE 输入的捷径。**

## 4. 幂等与副作用

### 4.1 HTTP 请求幂等

`go/orchestrator/cmd/gateway/internal/middleware/idempotency.go:18-25` 默认 TTL 为 24 小时；`idempotency.go:73-117` 对带 `Idempotency-Key` 的 POST 用 Redis `SetNX` 建 processing lock；`idempotency.go:122-149` 命中 completed response 时回放；`idempotency.go:195-224` 缓存状态码/header/body。

边界：这是 gateway request 去重，不保证 Worker 重试后外部 effect exactly-once。

### 4.2 数据库幂等是局部的

- `internal/activities/record_query.go:87-99` 生成 query 记录 `IdempotencyKey`；
- `internal/activities/persistence.go:44-80` 写 task execution；
- `internal/db/task_writer.go` 多处 `ON CONFLICT`，部分更新使用 `ON CONFLICT(id) DO UPDATE`。

优点是数据库写入有 key/PK 冲突保护；不足是 key 来源分散，部分来自调用参数/业务字段，未形成跨 Worker 稳定 effect identity 契约。

### 4.3 外部副作用、重试后去重、UNKNOWN/对账

未发现类似 Ringharness 的统一 `effect_id` 生命周期：`PREPARED → DISPATCHED → SUCCEEDED/FAILED/UNKNOWN → RECONCILED`。全仓检索 UNKNOWN/reconciliation 只出现少量非该语义上下文，未发现针对“请求可能已送达但响应丢失”的对账 Activity/状态机。

Temporal 默认是 at-least-once Activity；Shannon 对部分 emit/persistence 设置 1 次 retry，能降低重复概率，但**MaximumAttempts=1 不是副作用 exactly-once，也不能解决 timeout 后 UNKNOWN**。

结论：Ringharness 的“effect_id 跨 Worker 不变；UNKNOWN 先对账；禁止无人值守重复副作用”比 Shannon 更强，应保留，不可降级。

---

# ② 与 Ringharness 差距

## 2.1 Shannon 相对 Ringharness 的明显缺口

| 机制 | Shannon | Ringharness 对照 |
|---|---|---|
| durable checkpoint | StateChannel 是进程内 map | PostgreSQL `checkpoints` 已有内容寻址、lease/context/effect 校验（`migrations/versions/0024_checkpoints.py:13-39`；`storage/checkpoints.py:89-148`） |
| CAN 水位 | 8000 events 后提前 synthesis；未 CAN | `GoalWorkflow` 已携带 consumed command、skip-admit activity、attempt、generation/prior run 并续观测（`temporal_workflows.py:305-342`） |
| workflow evolution | 大量 GetVersion，有空 replay CI 风险 | 已有 `workflow.patched` + Replayer 测试（`temporal_workflows.py:177-180`；`test_tm03_history_replay.py:351-440`） |
| 副作用未知态 | 未发现 UNKNOWN/reconcile 闭环 | 已有 effect receipt、失租 DISPATCHED→UNKNOWN→对账基线 |
| DONE 权威 | Temporal completed 会写 task completed | `GoalWorkflow` 明确 `marks_goal_done=False`（`temporal_workflows.py:345-363`） |
| cancel cleanup | signal + 1s + cancel，无 receipt 等待 | 已有 QUARANTINED→StopReceipt→RELEASED 基线，应继续强化 |

## 2.2 Ringharness 任务上下文与当前工作树之间的状态差异

任务说明称恢复安全门缺失；但本次只读工作树已看到**部分实现**：

- `packages/orchestration/src/orchestration/temporal_workflows.py:25-29`：schema=1、max attempts=3、默认 intent TTL=4h；
- `temporal_workflows.py:140-165`：generation>0 时检查 schema、attempts、deadline，危险动作前返回 `RECOVERY_ABANDONED`；
- `temporal_workflows.py:316-342`：CAN 前持久携带 recovery attempts/schema/deadline；
- `tests/temporal/test_recovery_safety_gate.py:115-259`：过期、schema 不兼容、次数超限三类 time-skipping 测试。

但仍有真实差距：

1. **无独立 recovery enabled 开关**：`enable_continue_as_new` 是 CAN 开关，不等价于“允许恢复危险动作”；generation>0 只要通过其余门就恢复。
2. 这些字段主要存在于 CAN payload；`RealTemporalClient` 可转发字段名单仅到 `generation/prior_run_id`（`client.py:108-120`），没有显式转发 `checkpoint_schema_version/recovery_attempts/max_recovery_attempts/intent_valid_until` 供外部恢复入口使用。
3. PostgreSQL checkpoint v3 协议与 CAN recovery payload schema=1 是两套版本，尚未见统一恢复决策记录/ABANDONED 审计表。
4. 未见阶段化 Watchdog 与独立 No-Progress Guard 落地；ControlKernel 搜索只见失租/heartbeat 基础。

因此报告建议应按“部分落地、需收口”，而不是重复从零实现三门。

---

# ③ 可直接借鉴的机制清单

| 机制 | 分类 | 判定 | 对 Ringharness 的用法 / 边界 |
|---|---|---|---|
| Temporal event history + deterministic replay | Temporal 原生 | **可移植（已采用，继续强化）** | 把控制状态、signal、水位留在 history；业务事实仍在 PG；不能替代 effect receipt |
| `GetVersion`/patch gate 包住“新增命令” | Temporal 原生 + Shannon 用法 | **可移植** | 每次新增/重排 Activity、timer、child、signal handler 都先打 patch；绝不靠代码注释兼容 |
| 固定生产 history fixture + CI replay | Shannon 工具链 | **需改造** | 借导出/批量 replay，但禁止“无 fixture 跳过”；CI 至少含成功、失败、取消、CAN 跨 run 历史 |
| 统一 control state/query + 阶段安全点 | Shannon 自建 + Temporal signal | **需改造** | pause/resume/inject 只改变控制状态；在 phase 边界执行；signal 必须有 command_id、actor、schema、TTL、审计 |
| parent→child signal propagation | Shannon 自建 | **需改造** | 保存 workflow ID+run ID；传播返回 ACK；cancel 后等待 StopReceipt/child cancellation completion，而非 sleep 1s |
| 两阶段 cancel：业务 signal 后 SDK cancel | Shannon 自建 | **需改造** | 可保留意图顺序，但第二阶段由 deadline/receipt 触发，不用固定 1 秒；清理运行于可取消性明确的 Activity |
| 按 Activity 类别配置 timeout/heartbeat/retry | Shannon 用法 | **可移植** | 中央表按 PURE_READ / IDEMPOTENT_WRITE / EXTERNAL_EFFECT / OBSERVER / TELEMETRY 分类；外部 effect 最大 1 次后 UNKNOWN+对账 |
| Swarm 120 秒周期监控 tick | Shannon 自建 | **需改造** | 借为阶段化 Watchdog 调度，不叫 checkpoint；阈值按 PLAN/EXECUTE/AUDIT/STOP 各自设置 |
| 连续 3 次永久工具错误停止 | Shannon 自建 | **需改造** | `swarm_workflow.go:1616-1638`；改为 Kernel 可解释 BLOCKED/FAILED，不得当完成 |
| 连续 3 次无工具动作判定收敛 | Shannon 自建 | **需改造** | `swarm_workflow.go:1650-1679`；Ringharness 应比较业务进展指纹，而非只看“是否调用工具” |
| Streaming 前/后阶段 checkpoint | Shannon 概念 | **需改造** | 边界选择可借；必须落 PG、内容寻址、schema/version、effect IDs，且恢复前过门 |
| StateChannel 内存 map checkpoint | Shannon 自建 | **不适用** | 非 durable，且 `time.Now/uuid.New` 有 replay 非确定性风险 |
| 8000 events 后提前 synthesis | Shannon 当前实现 | **不适用** | 不是 CAN；会丢在途状态语义。Ringharness 已有更强 carried observation |
| Temporal COMPLETED→业务 COMPLETED | Shannon server | **不适用（红线冲突）** | DONE 只能由 Kernel 固定 VerificationProfile 判定 |
| Redis HTTP response cache 作为 effect 幂等 | Shannon gateway | **不适用单独使用** | 只能做请求层；不能替代稳定 effect_id/UNKNOWN/reconcile |

---

# ④ Top 5 建议（为什么 / 改哪个文件 / 验收标准）

## Top 1：把当前恢复安全字段收口成一个可审计的 `RecoveryPolicy`，补独立总开关

**为什么**：Shannon 没有三道门，说明 Temporal 自动 replay 本身不会阻止“陈旧意图恢复后继续危险动作”。Ringharness 当前工作树虽已有 TTL/schema/次数，但无独立恢复开关，且字段主要困在 CAN payload。

**改动位置**：

- `packages/orchestration/src/orchestration/temporal_workflows.py`：在任何 admit/RunActivation 前统一调用纯确定性 gate；新增 `recovery_enabled`，默认 false（生产显式开启）。
- `packages/orchestration/src/orchestration/client.py`：完整转发 policy 字段，拒绝隐式万能默认。
- 新增 migration（**不得修改** `0024_checkpoints.py`）：记录 recovery generation、intent deadline、attempt count、schema、decision/reason。
- `tests/temporal/test_recovery_safety_gate.py`：补 switch-off、刚好等于上限、非法/无时区 deadline、崩溃后 attempt 不清零。

**验收标准**：`generation>0 && recovery_enabled!=true` 时 0 次 admit、0 次 RunActivation，结果为 `RECOVERY_ABANDONED/RECOVERY_DISABLED`；过期/schema 不兼容/次数超限同样 fail closed；每次决策有 PG 审计行；任何分支 `marks_goal_done=False`。

## Top 2：实施阶段化 Watchdog + 业务进展指纹 No-Progress Guard

**为什么**：Shannon 的 120 秒 Lead tick 和“连续 3 次无工具/永久错误”证明长驻流程需要主动收敛，但其“有工具调用=有进展”太弱。Ringharness 应以不可伪造业务事实判断进展。

**改动位置**：

- `packages/orchestration/src/orchestration/temporal_workflows.py`：按 PLAN/EXECUTE/AUDIT/STOP/RECONCILE 设置 heartbeat/no-progress timer。
- `packages/control_kernel/src/control_kernel/storage/claims.py`：提供 progress fingerprint（activity status rev、checkpoint digest、candidate/effect/verification revision）。
- 新增 `tests/temporal/test_phase_watchdog.py`、`test_no_progress_guard.py`。

**验收标准**：冻结 workflow 时间后，阶段超时触发可解释事件；同一 fingerprint 连续 N 个窗口不自动重跑危险 effect，而进入 WAITING/BLOCKED 或 UNKNOWN→RECONCILE；任一真实 revision 变化会重置计数；PLAN 工具集仍为空；Watchdog 不产生 DONE。

## Top 3：把 history replay 变成非空强制发布门

**为什么**：Shannon 虽有 export/replay/CI，但无 fixture 时直接 skip，形成“看起来有门、实际上空门”。Ringharness 已有 Replayer 测试，应进一步固定真实历史语料。

**改动位置**：

- `tests/temporal/test_tm03_history_replay.py`：保留合成历史测试，并加入固定历史加载矩阵。
- 新增 `tests/temporal/histories/`：成功、RunActivation 失败、cancel、旧 patch、CAN+在途 Activity 五类脱敏 history。
- CI workflow/测试脚本：history 数量为 0 或目标 Workflow 未覆盖时直接失败。

**验收标准**：新代码 replay 全部固定历史为 0 nondeterminism；Replayer 不再执行 Activity；故意交换 Activity 顺序测试必然报 `NondeterminismError`；fixture=0 时 CI 红灯；`b086`、`m3` marker 前后历史都覆盖。

## Top 4：建立中央 Activity 可靠性档案，严格区分“重试”和“对账”

**为什么**：Shannon 的 1/2/3 次 RetryPolicy 分档值得借鉴，但缺 effect 语义。Ringharness 应明确：纯读可重试、幂等 DB 写可重试、外部副作用只投递一次，超时即 UNKNOWN 后对账。

**改动位置**：

- `packages/orchestration/src/orchestration/temporal_workflows.py`：抽出字面量可靠性 profile；继续保持 `RunActivation maximum_attempts=1`。
- `packages/orchestration/src/orchestration/kernel_activities.py` 与 TS Runner `RunActivation` 定义：贯穿稳定 effect_id/attempt_id/fencing epoch。
- `tests/temporal/test_tm03_history_replay.py`、`test_tm05_effect_receipt.py`：扩展超时但外部已成功的 UNKNOWN 场景。

**验收标准**：Temporal history 证明外部 effect 只 Schedule 一次；响应丢失后状态为 UNKNOWN，不二次 dispatch；reconciler 使用同一 effect_id 查询并收敛；read-only observer 可按 profile 重试；任何 Activity SUCCEEDED 都不直接 DONE。

## Top 5：引入统一控制 handler，但取消完成以 StopReceipt/资源释放为准

**为什么**：Shannon 的统一 pause/resume/cancel/inject + phase safe point 很清晰；但 signal→sleep 1s→cancel 会丢清理。Ringharness 应借接口，不借等待方式。

**改动位置**：

- `packages/orchestration/src/orchestration/temporal_workflows.py`：增加有 schema 的 pause/resume/cancel/inject command handler、query state 和 phase safe point。
- `apps/control` 新增/扩展控制 API：Idempotency-Key、actor、command_id、reason、TTL；缺 JWT/库/密钥继续 503。
- Stop/effect 相关 storage 与 `tests/temporal/test_tm06_stop_receipt.py`、`test_tm07_disconnect_semantics.py`：验证 child/runner 清理。

**验收标准**：重复 command_id 只消费一次；pause 仅在安全点生效且重启/replay 后仍 paused；inject schema/TTL 不合法 fail closed；cancel 后即使客户端断连也继续等待/观察 StopReceipt，只有 QUARANTINED→StopReceipt→RELEASED 才释放资源；超时则保持隔离/UNKNOWN，不伪造完成。

---

## 最终判断

最值得从 Shannon 吸收的不是它的内存 checkpoint 或完成状态映射，而是四个结构性习惯：**所有新 Workflow 命令显式 version gate、控制信号与安全点分离、长驻循环必须有周期监控和 no-progress 退出、不同 Activity 设置不同可靠性档位**。与此同时，Shannon 暴露的三个反例应作为 Ringharness 的负面验收：**空 replay CI、固定 sleep 等取消清理、Temporal COMPLETED 冒充业务完成**。Ringharness 已有更强的 Kernel DONE、effect UNKNOWN/reconcile、CAN carried observation 与 StopReceipt，不应因借鉴而降级。