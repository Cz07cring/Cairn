# 《AI Agent 架构》第 20–26 章消化：Ringharness 生产化流程增强建议

> 研究范围：Part 7 概述、20–22 章、Part 8 概述、23–26 章。项目判断依据：`AGENTS.md`、`doc/v0.6/01-开发文档.md`、`doc/v0.6/05-API接口文档.md`、`doc/implementation/进度与验证.md` 最后 100 行，以及任务给定项目背景。本文只提出流程与设计增强，不修改仓库。
>
> 状态口径：**已实现**＝指定材料明确记录代码/验证已落地；**部分**＝已有边界或骨架，但缺完整闭环/专项验收；**缺失**＝材料明确未完成，或未见对应机制；**未验证**＝指定材料不足以判断，不能据此断言代码不存在。

## 0. 两个 Part 的总判断

Part 7 将生产架构归纳为控制/执行/LLM 分层、Temporal 持久工作流和指标/追踪/日志三支柱。[1] Ringharness 不应照搬 Go/Rust/Python 的语言处方；它已经以 Python Kernel/Workflow、TS Runner/Harness、Python Broker 建立了更符合自身约束的职责分层。真正值得吸收的是：**职责与凭据隔离、持久恢复、跨层关联标识、分层超时、可观测性门禁**。

Part 8 将企业级能力归纳为三级预算、策略治理、能力型沙箱和多租户全链路隔离。[5] Ringharness 已有更严格的固定 Kernel 裁决、不可变配置、Broker 准入和失败关闭红线，但预算结算、策略发布证据、沙箱逃逸测试、多租户负向矩阵仍需形成可执行验收。

---

## 1. 第 20 章：三层架构设计

### ① 核心论断

1. 单体 Agent 的主要生产风险是工具与主进程共享安全边界、并发互相拖累、内存状态随崩溃丢失、单工具资源耗尽拖垮整体；分层的目的不是语言炫技，而是拆开编排、安全执行和 LLM 生态。[2]
2. 编排层只决定执行者、顺序、预算和综合，不直接执行不可信任务；执行层负责沙箱、资源上限、超时、工具白名单与参数校验；LLM 层负责 Provider 适配和模型调用。[2]
3. 跨层必须传播 Workflow ID/Run ID 以关联日志、成本和资源归属；超时应“外长内短”，避免外层先结束而内层继续浪费资源。[2]
4. 分层会带来部署、调试和通信成本；小规模系统未必需要多语言。跨层不存在天然原子事务，应通过持久工作流、幂等和恢复协议处理。[2]
5. 健康端点应尽早可用，服务启动要区分依赖连接与 Worker 注册；不同优先级队列可隔离实时任务与后台任务。[2]

### ② 对 Ringharness 的增强建议

- **[已实现] 保持“职责分层而非语言分层”。** `doc/v0.6/01` §1–2 已明确 Python Workflow 只做确定性编排、TS Runner 承载 activation、Broker 受控执行、Kernel/PG 裁决业务事实；这比照搬 Go/Rust 更符合“不新增业务语言”约束。建议把第 20 章的语言表改写成 Ringharness 的“能力/凭据矩阵”，并作为每批审查项写入 `AGENTS.md` §7.1：组件、可持凭据、可写事实、可发起副作用、禁止行为。
- **[部分] 把分层超时变成机器可校验的合同。** 当前已知本地 Qwen timeout 180s、`RunActivation` start-to-close 360s，且有 Kernel lease TTL/心跳；但尚未见“Goal/Workflow → Activity → Harness 模型 → Broker 工具”全链的外长内短静态检查。建议 M3 收口时在 `doc/v0.6/01` §6 和 `doc/02-测试文档.md` 增加超时不变量，并在 `tests/temporal/**`/Runner 单测验证：`tool_timeout < model_round_timeout < RunActivation start_to_close < lease_ttl`，同时为 StopReceipt/QUARANTINED 保留独立恢复窗口。
- **[部分] 统一跨层关联信封。** `RuntimeAttemptLink` 已含 workflow/run/activity/attempt/fence/owner，且 `RuntimeActionRef` 只传引用；建议在 `doc/v0.6/05` 增补内部 `CorrelationContext` 或规定所有 Kernel Activity、Runner、Broker 日志统一携带 `project_id/goal_id/activity_id/attempt_id/action_id/effect_id/workflow_id/run_id/trace_id`，但禁止把这些高基数字段做 Prometheus label。
- **[缺失/未验证] 建立组件级 readiness 依赖表。** `AGENTS.md` 显示 Web 有真实 `/health/live`，Broker/Workflow Worker 有 idle/health 行为，但指定材料未证明 DB、Temporal、模型、对象仓、Broker 的 critical/non-critical 分类已闭环。建议 M5 前为 `apps/control`、`apps/workflow-worker`、`apps/execution-broker`、Runner 定义 live/ready/degraded 语义；关键授权/业务库缺失必须 503，非关键观测后端失败只降级观测，不得扩大执行权限。
- **[不采纳书中直接做法] 不新增 Rust Agent Core，也不做静默降级。** Ringharness 当前规模和约束下，隔离可由 Broker + OS/container/WASI 后续实现；若 Broker/沙箱不可用，不能回退为 Runner 本地直执行或 LEGACY 双调度。

---

## 2. 第 21 章：Temporal 工作流

### ① 核心论断

1. Workflow 只能包含确定性决策，所有 HTTP、SQL、文件、随机数和真实时间等副作用必须进入 Activity；恢复依赖历史重放，已完成 Activity 结果从历史取回。[3]
2. Activity 必须按场景配置 StartToClose、ScheduleToClose、HeartbeatTimeout、指数退避和不可重试错误；长时执行依赖心跳，但重试不能忽略副作用幂等。[3]
3. 工作流演进必须使用具名版本门；新增 Activity、修改决策序列而无版本门会让旧历史发生 non-determinism。[3]
4. 暂停/恢复/取消适合用 Signal，查询不改变状态；大 payload 只传引用，无限历史用 Continue-As-New；复杂并行可用 Future/Selector/子工作流。[3]
5. Temporal UI、历史导出和 replay 测试用于“时间旅行”定位；取消必须被执行代码响应，否则会泄漏资源。[3]

### ② 第 21 章专项对照表

| 书的建议 | Ringharness 现状 | 状态 | 差距/增强动作 |
|---|---|---|---|
| Workflow 纯确定性，IO 全进 Activity | `doc/v0.6/01` §1 明确 Workflow 不直接模型/HTTP/SQL/文件；Kernel/Runner 均为具名 Activity | 已实现（设计+已有测试） | 给 `packages/orchestration/**` 增加 CI 静态禁用清单（网络、系统时间、随机、环境变量、直接 DB 客户端），避免后续回归 |
| 版本门保护旧历史 | 已落地 `b086-temporal-recovery-v1`、`m3-execute-orchestration-v1`，已有 TM03/旧历史 replay | 已实现 | 建立“任何 Workflow 控制流变更必须同时提交历史 fixture + patch ID 清单”的 PR 门；patch 命名进入 `doc/v0.6/05` 或专门演进登记表 |
| Activity 有场景化超时/重试/不可重试分类 | `RunActivation` 技术重试上限 1 次；失败后 reconcile；本地 Qwen 与 Activity timeout 已配置 | 部分 | 为 Kernel 纯幂等查询、业务提交、模型调用、Broker effect 分别定义 retry matrix；权限/预算/合同/旧 fence 必须 non-retryable；UNKNOWN 必须转 reconcile，不能自动新 effect |
| 心跳检测长任务 | Temporal 心跳 + Kernel lease 双心跳，单宿主协调；失租停止工具准入 | 已实现/部分验收 | M3 增加故障注入：只断 Temporal 心跳、只断 Kernel 续租、两者乱序、completion 丢失；检查无陈旧写和无预算提前释放 |
| Signal 暂停/恢复/取消 | 已设计暂停/取消先 Kernel StopRequest，再 Workflow 唤醒 Runner/Broker；Temporal cancel 不等于 StopReceipt | 部分 | 明确 Signal 只是唤醒，不是业务事实；增加重复/乱序 Signal、Workflow 已 CAN、Runner 不响应取消的测试 |
| 查询只读状态 | 浏览器不暴露 Temporal 管理端口；公共诊断 API 未定稿 | 缺失/未验证 | M4 诊断 API 只从 Kernel/PG 返回业务权威状态，可附 Temporal 传输诊断；禁止用 Query/Workflow COMPLETED 改写 GoalStatus |
| 大数据只传引用 | `RuntimeActionRef` 明确只含引用，不含模型正文/凭据；工件进 Ledger/S3 | 已实现（设计） | 增加 payload 大小门和敏感字段 schema 测试，避免模型正文/密钥进入 Temporal History/Memo |
| Continue-As-New 控制历史 | 已落地水位恢复，业务 Activity/Attempt/Effect 不随 CAN 重置 | 已实现 | M5 做高水位/反复 CAN 测试，验证 command 消费水位、RUNNING attempt 观察和 patch marker 全保留 |
| 历史导出与确定性 replay | 已有 TM03 与 9 个 Temporal 专项测试的独立复跑证据 | 已实现/部分 | CI 固化来自“旧版本真实/合成历史”的 replay 语料库；每个 patch 至少一条旧路径、一条新路径 |
| Fire-and-Forget 用于日志/指标/审计 | 书建议非关键持久化可不等待 | 与红线冲突 | 仅 telemetry 可 best-effort；业务 journal、预算、审计事实、证据目录绝不能 fire-and-forget，必须走 PG/outbox/幂等回执 |
| 子工作流/优先队列 | 当前固定 `ring-runner`、`ring-control`，清理需独立受信恢复 Workflow | 部分 | 暂不按租户/优先级盲目拆队列（队列不是权限边界）；优先先落地独立 RECONCILE/CLEANUP Workflow，再以容量数据决定队列隔离 |

### ③ 对开发流程的直接增强

- **Workflow 变更清单化**：PR 模板新增 `changes_workflow_command_sequence`、`patch_id`、`old_history_fixture`、`replay_command`、`CAN_state_fields` 五项；未填不得合并。
- **重试语义评审前置**：每个新增 Activity 在 `doc/v0.6/05` 旁登记“技术重试键、业务幂等键、不可重试错误、UNKNOWN 核对动作、取消后的资源状态”。尤其不能采用章节示例的 `WorkflowID + ActivityID + Attempt` 作为逻辑计费键：Temporal attempt 变化可能导致重复计费；Ringharness 应绑定稳定 `ModelInvocation/effect_id/action_id`。
- **取消不是释放证明**：继续坚持 Ringharness 的更严格规则；M3/M5 验收必须同时观察 `StopRequest → StopReceipt/UNKNOWN → QUARANTINED/释放`，不能只看 Temporal canceled。

---

## 3. 第 22 章：可观测性

### ① 核心论断

1. Metrics 看聚合趋势，Traces 看单请求全链路，Logs 查具体上下文；三者应通过 `trace_id` 关联。[4]
2. 指标应按工作流、Agent/Pattern、Token/成本、依赖等层分类，采用 Counter/Histogram/Gauge；标签必须低基数，用户 ID、任务 ID、时间戳不能做 Prometheus label。[4]
3. OpenTelemetry 应传播 W3C `traceparent`，每一跳创建 span 并记录错误；生产需采样，错误/关键路径可提高采样率。[4]
4. 健康检查要区分 critical 与 non-critical，并区分 liveness/readiness/degraded，避免缓存/观测抖动触发重启。[4]
5. 告警必须分级，并覆盖失败率、P95/P99 延迟、异常 Token、关键依赖；仪表盘至少展示吞吐、成功率、延迟、成本和错误。[4]

### ② 对 Ringharness 的增强建议

- **[缺失/未验证] 将 `packages/observability` 从目录占位提升为 M5 发布门。** 指定材料只证明该包存在，未证明 OTEL/Prometheus/告警落地。建议新增 `doc/02` 的 OBS 验收编号：API→outbox relay→GoalWorkflow→Kernel Activity→RunActivation→Harness→Broker→Kernel commit 全链 trace 可检索。
- **[部分] 业务关联字段与 telemetry 分层。** 结构化日志可携带 `goal_id/action_id/effect_id/attempt_id/fence/workflow_id/run_id/trace_id`；Prometheus 仅允许 `component/activity_kind/outcome/error_class/model_tier/orchestration_backend` 等有限枚举。增加 cardinality 单测/审查脚本，禁止 `project_id/user_id/goal_id/trace_id` 作 label。
- **[缺失] 建立“无人值守真正需要”的告警，而不只照搬通用成功率。** 至少包括：outbox age、Workflow start ack lag、stuck RUNNING lease、heartbeat/lease 分歧、UNKNOWN backlog/age、QUARANTINED 资源数、reconcile 失败、预算预留与结算差、错误 DONE 尝试、Temporal history/CAN 水位。
- **[部分] 可观测性不得成为事实权威。** 指标/trace 丢失只能影响诊断，不能改变 DONE、预算结算、证据可信度或停止回执；业务审计必须仍在 PG journal/outbox。此点比章节“审计追踪可 FNF”更严格。
- **[缺失] M5/真实 100h 使用 SLO 驱动验收。** 在 `doc/03-收敛文档.md` 定义可执行阈值：命令 ACK P95、activation 排队/执行 P95、UNKNOWN 最大年龄、对账最终收敛率、重复 effect=0、跨 fence 陈旧写=0、业务终态与 Workflow 状态错配=0；100h 报告必须附查询或仪表盘快照。

---

## 4. 第 23 章：Token 预算控制

### ① 核心论断

1. 单一预算不足，需 Task、Session、Agent 多级限制；软阈值用于预警，硬阈值立即阻断，高价值任务可暂停等待审批。[6]
2. 每次执行应走“执行前估算/检查 → 执行 → 按实际 input/output tokens 分别计价和记录”；估算有误差，应预留综合缓冲。[6]
3. 接近预算可用 Workflow durable timer 做背压，不能在 Activity 内 sleep 占住 Worker；连续超限可熔断，防止级联故障。[6]
4. Token 使用记录必须幂等，避免 Temporal completion 丢失后重试导致重复计费；预算启用/禁用的双记录路径必须有 guard。[6]
5. 预算指标应覆盖 token、成本、超限、背压延迟和熔断状态；超支时优先明确降级而非无界继续。[6]

### ② 对 Ringharness 的增强建议

- **[部分] 将现有 `ModelInvocation(max_output_tokens/max_cost_usd/data_categories)` 扩成“预留—结算—核对”三阶段。** 在 `packages/control_kernel` 与 `doc/v0.6/05` 定义稳定 `model_invocation_id`：准入时原子预留最大成本；Provider 回执后按 input/output 实际量结算；超时/UNKNOWN 保留预留并进入 reconcile，不能先退款再重试。
- **[部分] 预算层级应贴合 Ringharness 领域，而非照搬 Session。** 建议层级为 `project/contract budget_scope → Goal → Task/Activity → ModelInvocation/EffectIntent`，Manager/Executor/Auditor 各有角色子配额；GoalReview 与重规划次数也计入。预算权威在 Kernel/PG，Runner 只接收本次已批准上限。
- **[缺失] 并发预算需用数据库原子约束验证。** 增加集成测试：多个 activation 同时预留，合计不得越过硬限；completion 丢失/Activity 重试只结算一次；UNKNOWN 不产生第二个 invocation；配置降额时既有预留如何处理必须固定规则。
- **[冲突修正] 不以 Temporal `attempt` 作为幂等身份。** 章节文本声称 `WorkflowID+ActivityID+Attempt` 在重试时相同，但 Temporal attempt 通常会变化，此处至少存在表述风险。Ringharness 应坚持 `effect_id`/稳定 `ModelInvocation`/command ID 跨 Worker 与技术重试不变，`temporal_attempt` 只做运输审计。
- **[谨慎采纳] 背压不得模糊硬拒绝。** 可用 Workflow timer 处理低优先级节流，但当预算/授权不足时必须 Kernel 明确 BLOCKED/拒绝；禁止“睡一会儿后绕过旧准入”。任何降级（换模型、缩短输出、减少审查）必须在不可变合同允许范围内，且绝不能削弱固定 VerificationProfile/DONE 屏障。

---

## 5. 第 24 章：策略治理（OPA）

### ① 核心论断

1. 声明式策略把安全规则从散落的 if/else 中抽出，可预编译、版本化、集中测试，并记录 allow/deny 原因和策略 hash。[7]
2. 安全策略应默认拒绝、deny 优先；决策输入必须包含所有影响结果的上下文，缓存键也必须覆盖这些字段。[7]
3. 策略发布可经历 off/dry-run/enforce，并按稳定主体做 canary；每次评估记录 query hash、有效模式、policy version 和延迟。[7]
4. 策略引擎异常时 fail-open/fail-closed 是业务选择；章节倾向一般生产可用性场景使用 fail-open + 告警，但同时承认安全敏感系统应 fail-closed。[7]
5. 策略只能回答“是否允许”，不能替代执行沙箱和资源隔离。[7]

### ② 对 Ringharness 的增强建议

- **[已实现/部分] 不急于引入 OPA，先把固定 Kernel 规则变成可审计决策合同。** Ringharness 已有不可变策略版本、Worker 身份、具名准入、Broker 二次鉴权与 DONE 固定屏障。建议在 `doc/v0.6/05` 增加 `PolicyDecisionRef`：`decision_id/policy_version_id/policy_digest/input_digest/reason_codes/mode/evaluated_at`；所有 admit/deny/approval 写 PG journal，避免只留日志。
- **[缺失] 增加策略版本发布流水线。** 新策略版本必须：schema/静态校验 → 单元策略向量 → 历史 journal 离线模拟（dry-run，不改变业务状态）→ 测试租户/项目 enforce → 全量启用。配置不可 UPDATE/DELETE；回滚是发布新版本并保留旧决策可重放，而非热改原行。
- **[冲突] 明确拒绝章节的通用 `FailOpen` 建议。** Ringharness 红线要求缺 JWT/库/密钥 503、RING_CLOUD_MODE 默认 DENY、BLOCKED 不收新配置。因此策略引擎不可用或输入不全必须 fail-closed；仅“观测导出器”等非授权依赖可 fail-open/degraded。
- **[冲突] dry-run 不能把危险请求实际放行到真实 Broker。** 可在影子评估路径记录“新策略本会 deny”，但真正执行仍由当前已生效固定策略 enforce。canary 只能选择不同已批准策略版本，不能随机削弱底线。
- **[缺失] 决策缓存必须绑定不可变上下文。** 若将来缓存，键至少含 `policy_digest/project_id/role/kind/target/scope/data_categories/budget_scope/state_revision/owner_epoch/fencing_epoch/trust_state`；任何状态修订、停止请求或配置版本变化都使旧缓存无效。

---

## 6. 第 25 章：安全执行（WASI 沙箱）

### ① 核心论断

1. WASI 使用 capability 模型：默认无文件、网络、环境变量等权限，只显式开放所需目录和能力。[8]
2. 安全执行应是三道防线：输入校验 → 沙箱执行 → 输出审核；沙箱本身不能证明结果正确。[8]
3. 文件边界需 canonicalize 后再校验，防 symlink/path traversal；宿主环境变量不继承，目录尽量只读，stdin/stdout/stderr 有大小限制。[8]
4. 资源限制需同时覆盖内存、实例/表、CPU fuel 和墙钟 epoch timeout；同步执行必须与异步事件循环隔离。[8]
5. 必须用逃逸、外联、无限循环、fuel、内存耗尽等负向测试实测；WASI 的兼容性/生态限制意味着不是所有工具都适用。[8]

### ② 对 Ringharness 的增强建议

- **[部分] Broker 边界已正确，沙箱强度仍需专项验收。** 已落地 Broker Effect Gateway、Runner 不持业务 DB 写凭据、工具 prepare→终态→ToolResult、失租停止准入；但指定材料未证明 WASI/container/cgroup 的具体隔离已经实现。M3 应把“进程隔离、文件系统视图、网络 egress、CPU/内存/进程数/输出字节上限”写入每个 ToolCapability/VerificationProfile。
- **[缺失] 建立 `tests/security` 的沙箱红队矩阵。** 至少测试：`../` 与 symlink 逃逸、读取 `.runtime`/SSH/环境变量、DNS/公网/metadata endpoint 外联、fork bomb/无限循环、内存与磁盘填满、超大 stdout/stderr、子进程遗留、取消后继续写、失租后陈旧 fence 写。验收必须同时检查宿主无泄露、Broker 回执为固定错误类、资源最终 StopReceipt/QUARANTINED 收敛。
- **[冲突] 不接受“有 WASI 所以 dangerous=false”。** 沙箱降低影响面，但不改变操作语义；代码执行、文件写、shell、网络仍应保持危险等级并经过 Kernel/Broker 准入。沙箱成功更不能成为证据正确性或 DONE 的依据。
- **[部分] 输出审核接入证据三分离。** Broker 可签可信来源/回执并验证 hash，但 Auditor/固定验证器必须独立判断验收正确性；stdout、生成文件和系统调用摘要应作为不同证据引用，不可把“沙箱 exit 0”直接映射 PASS。
- **[缺失/流程] 先做 threat model/spike，再决定 WASI。** 在不新增业务语言的前提下，可比较 WASI、rootless container、macOS/Linux 平台沙箱。选择标准是实测隔离、工具兼容和停止可证明性，而不是章节给出的启动速度数字；任何实现都不得让 Broker 获得业务库写凭据。

---

## 7. 第 26 章：多租户设计

### ① 核心论断

1. 多租户不是补一个 SQL `WHERE`，而是认证、会话、工作流、向量存储、数据库五层都强制租户绑定；任一遗漏都可能泄露数据。[9]
2. 租户 ID 必须来自受信认证上下文，不能相信请求字段；跨租户资源访问应返回 NotFound，避免泄露存在性。[9]
3. 隔离可选共享表行级、独立 schema、独立数据库，成本与隔离强度递增；共享表要求每个业务表 tenant_id 非空并在所有查询中约束。[9]
4. Temporal 需携带租户元数据以便检索/诊断，但工作流访问仍要复核归属；向量检索必须使用租户 namespace/filter。[9]
5. 数据隔离之外还需故障与配额隔离，并在 CI 运行专门的跨租户负向用例。[9]

### ② 对 Ringharness 的增强建议

- **[部分] 将现有 project 归属校验固化为全链 scope invariant。** `RuntimeActionRef/Binding` 已带 `project_id`，b086 已做项目/Goal/Activity/action/owner/fence 六项归属检查，认证不信请求自报 role/project；这是良好基础。建议每个业务表/对象键/outbox/event/evidence/ModelInvocation/EffectIntent 都有可证明的 project（若未来引入 organization/tenant，则同时绑定 tenant→project）。
- **[未验证] 不把 `project_id` 自动等同 `tenant_id`。** 指定材料未显示独立 Tenant/Organization 模型和组织级配额；在产品需求确认前，不应为“企业级”盲目迁移所有表。先在 `doc/01` 冻结隔离层级与共享资源（公共 Skill/模型配置是否允许跨项目复用）。
- **[缺失] 增加跨项目/未来跨租户矩阵测试。** 对同一资源 ID 分别测试 API 读写、Kernel admit/commit/reconcile、outbox relay、Temporal binding/CAN、Broker artifact/effect、S3 object key、列表分页 cursor；攻击请求必须 NotFound 或固定拒绝，且审计记录存在但响应不泄露资源存在性。
- **[部分] Temporal Memo/Search Attributes 只能用于诊断，不能作为授权权威。** 章节依赖 Memo 检查租户，但 Ringharness 应继续以 PG `OrchestrationBinding` + Worker JWT + Kernel 复核为权威；Memo/Search Attribute 可存 project/tenant 的不可敏感检索字段，读取后仍需与绑定比对。
- **[已实现/需保持] 开发万能身份不可进入生产。** 章节示例包含 `skipAuth` 开发模式，而 Ringharness 已明确缺 JWT 503、禁止万能身份。继续要求启动时发现绕过配置即失败，集成测试只使用独立测试库/桶和显式测试签发身份。
- **[缺失] 配额与 noisy-neighbor 隔离并入 M5 100h。** 除 Token 预算外，按 project/tenant 限制并发 activation、Broker 工具并发、对象/日志输出、UNKNOWN/QUARANTINED 数量；压测一个 project 达限时，另一个 project 的 ACK/执行 SLO 不应显著恶化。

---

## 8. Top 5 最该立刻做的流程增强

### Top 1：把 Workflow 演进门升级为“patch + 双历史 replay + CAN 状态清单”

- **为什么**：第 21 章把确定性、版本门、历史 replay、Continue-As-New 视为 Temporal 生产底线；Ringharness 已有两个优秀 patch，但需要从个人纪律变成每次变更的机械门。[3]
- **改什么**：`AGENTS.md` §7.1 PR 字段、`doc/v0.6/01` §5、`doc/v0.6/05` §4、`tests/temporal/**`；任何修改 `packages/orchestration/**` 的批次提交 patch ID、旧/新历史 fixture、CAN 携带字段清单。
- **验收标准（可执行检查）**：CI 执行全部历史 replay；每个 patch 至少断言旧历史不调新 Activity、新历史调新 Activity；连续两次 CAN 后 `command watermark/owner_epoch/running attempt/action identity` 不丢；扫描 Workflow 模块不得直接网络/DB/环境变量/非确定性时间随机。

### Top 2：落地预算“原子预留—实际结算—UNKNOWN 核对”

- **为什么**：第 23 章指出多级预算、input/output 分价与重试幂等是成本防火墙；当前 ModelInvocation 已登记上限，但完成丢失和并发预留仍是无人值守成本风险。[6]
- **改什么**：`packages/control_kernel/**`、ModelInvocation/预算迁移与 schema、`doc/v0.6/05`；稳定幂等键使用 `model_invocation_id/effect_id/action_id`，绝不用 `temporal_attempt`。
- **验收标准（可执行检查）**：并发 N 个 invocation 的预留总额不越硬限；同一 completion 重放 10 次只结算一次；超时/404/空响应进入 UNKNOWN 且保留预留；对账后恰好一次 commit/refund；降级不得改变 VerificationProfile 或绕过 DONE 屏障。

### Top 3：将全链可观测性与无人值守 SLO 设为 M5 发布门

- **为什么**：第 22 章要求 metrics/traces/logs 联动；Ringharness 的真正风险不是一般 HTTP 错误，而是 outbox 卡住、心跳与租约分歧、UNKNOWN 不收敛、错误释放和状态错配。[4]
- **改什么**：`packages/observability/**`、各服务结构化日志、`doc/02`/`doc/03`、100h 报告模板；统一 correlation envelope，Prometheus 只用低基数枚举。
- **验收标准（可执行检查）**：给定 command/effect 能从 API trace 追到 Kernel commit；告警能在故障注入后命中 outbox age、stale lease、UNKNOWN age、QUARANTINED、预算差额；100h 中重复 effect=0、陈旧 fence 写=0、错误 DONE=0，且 cardinality 检查无 ID 类 label。

### Top 4：建立不可变策略版本的影子评估与 fail-closed 发布流水线

- **为什么**：第 24 章的默认拒绝、deny 优先、版本 hash、dry-run/canary 值得吸收；但其通用 fail-open 建议与 Ringharness 安全红线冲突，必须明确采用更严格折中。[7]
- **改什么**：策略配置 schema/Kernel decision journal、`doc/v0.6/05` 的 `PolicyDecisionRef`、PR 模板；流程固定为静态校验→测试向量→历史 journal 影子评估→受控项目 enforce→全量，新旧版本均不可改写。
- **验收标准（可执行检查）**：策略缺失/编译失败/输入字段缺失均拒绝或 503；同一输入可由 `policy_digest+input_digest` 重放出相同 reason code；dry-run 只写影子结果、不让当前策略本应拒绝的 effect 进入 Broker；回滚产生新版本且旧审计仍可验证。

### Top 5：在 M3 收口前跑“沙箱 × 停止 × 跨项目”负向矩阵

- **为什么**：第 25 章要求输入—沙箱—输出三道防线和资源/逃逸实测，第 26 章要求每层隔离与跨租户 CI；这些是 Broker 接通真实工具后最迫近的风险。[8][9]
- **改什么**：`apps/execution-broker/**`、Runner tool host、`tests/security/**` 与集成测试；先冻结 capability/egress/fs/resource/output 合同，不强制新增 Rust，WASI 与 rootless container 以 spike 实测选择。
- **验收标准（可执行检查）**：路径穿越/symlink、宿主环境、外网与 metadata、fork/CPU/内存/磁盘/stdout 攻击均失败；取消或失租后旧进程不能产生受理写，资源进入 StopReceipt 或 QUARANTINED；用另一 project 身份访问 API/PG binding/Broker/S3/cursor 一律不返回数据且留下安全审计。

---

## 9. 结论与取舍

本书最有价值的不是“换成 Go/Rust/OPA/WASI”这一技术清单，而是把生产要求转化为可验证机制：确定性历史、稳定幂等身份、预算闭环、策略版本证据、能力最小化和全链隔离。Ringharness 在业务事实权威、三权隔离、DONE 屏障、失租 UNKNOWN 和 Temporal patch/CAN 上已经比书中通用示例更严格；下一步应优先把这些优势机械化为 CI/故障注入/100h 门禁，而不是扩展新功能面。

特别需要保留三处折中：**不为三层而新增业务语言；授权链失败一律 fail-closed；沙箱/Temporal/模型成功都不提升为验收正确性或 DONE。**

## Sources

[1] https://waylandz.com/ai-agent-book/Part7%E6%A6%82%E8%BF%B0 — Part 7：生产架构
[2] https://waylandz.com/ai-agent-book/%E7%AC%AC20%E7%AB%A0-%E4%B8%89%E5%B1%82%E6%9E%B6%E6%9E%84%E8%AE%BE%E8%AE%A1 — 第20章：三层架构设计
[3] https://waylandz.com/ai-agent-book/%E7%AC%AC21%E7%AB%A0-Temporal%E5%B7%A5%E4%BD%9C%E6%B5%81 — 第21章：Temporal工作流
[4] https://waylandz.com/ai-agent-book/%E7%AC%AC22%E7%AB%A0-%E5%8F%AF%E8%A7%82%E6%B5%8B%E6%80%A7 — 第22章：可观测性
[5] https://waylandz.com/ai-agent-book/Part8%E6%A6%82%E8%BF%B0 — Part 8：企业级特性
[6] https://waylandz.com/ai-agent-book/%E7%AC%AC23%E7%AB%A0-Token%E9%A2%84%E7%AE%97%E6%8E%A7%E5%88%B6 — 第23章：Token预算控制
[7] https://waylandz.com/ai-agent-book/%E7%AC%AC24%E7%AB%A0-%E7%AD%96%E7%95%A5%E6%B2%BB%E7%90%86 — 第24章：策略治理（OPA）
[8] https://waylandz.com/ai-agent-book/%E7%AC%AC25%E7%AB%A0-%E5%AE%89%E5%85%A8%E6%89%A7%E8%A1%8C — 第25章：安全执行（WASI沙箱）
[9] https://waylandz.com/ai-agent-book/%E7%AC%AC26%E7%AB%A0-%E5%A4%9A%E7%A7%9F%E6%88%B7%E8%AE%BE%E8%AE%A1 — 第26章：多租户设计
