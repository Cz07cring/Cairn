# 《AI Agent 架构》Part 10（第 34–45 章）消化：Ringharness Agent Loop 工程增强建议

> 研究范围：Part 10 概述及第 34–45 章；页面标注的源码快照为 Kocoro `origin/main` 的 `4ec6772`（复核日 2026-07-27），因此本文只把机制当作设计输入，不照搬常量。[1]  
> Ringharness 现状依据：`AGENTS.md`、`README.md`、`doc/v0.6/01-开发文档.md` §6–§7、`doc/implementation/进度与验证.md` 最后 100 行，以及对 `packages/context_compiler`、`packages/orchestration`、`apps/runner/src/harness` 的只读核对。状态标签含义：**已实现**=当前代码/进度文已有闭环；**部分**=有相邻机制但缺本章关键不变量；**缺失**=未发现对应实现（不等于全仓绝对不存在，超出必读范围者标“未验证”）。

## 总体判断

Ringharness 已经采用本书推荐的基本形状：**Temporal 确定性外壳 + RunActivation 自适应内核 + Kernel 确定性裁决**，且在持久恢复、失租隔离、UNKNOWN 对账与 Effect Gateway 上比通用 Agent Loop 更严格。主要缺口已从“能不能恢复/调用工具”转向五类工程纪律：**Prompt 载荷治理、阶段化超时、恢复意图时效、卡循环证据、并行调用安全**。任何增强都不得改变以下红线：模型或 Workflow 的成功不等于 DONE；PLAN 工具集仍为空；UNKNOWN 先对账；所有副作用保持同一 `effect_id`；完整审计证据不得因 Prompt 裁剪而丢失。

---

## 第 34 章：从 DAG 到 Agent Loop

### ① 核心论断

- 事前可命名节点、固定边、可确定校验的控制流适合 DAG；下一步依赖运行后观察时才值得使用 Agent Loop。[2]
- 生产形态通常是“确定性外壳 + 自适应内核 + 确定性退出”；能由普通条件判断的检查不应交给模型。[2]
- 长驻 Daemon 不等于长驻 Loop：Loop 应按 Turn 重建；连续性来自执行前持久化 Session 与显式历史快照，而非对象寿命。[2]
- 迭代上限只是失控兜底，不是终止条件；模型宣布“完成”也不是后置条件。[2]

### ② 对 Ringharness 的增强建议

- **已实现**：`packages/orchestration/src/orchestration/temporal_workflows.py::GoalWorkflow` 提供确定性编排外壳，TS `RunActivation` 承载模型回合，Kernel 独占业务裁决；这与本章结构一致，且 DONE 红线更严格。
- **已实现**：PLAN/EXECUTE 的 Activity 准入、Broker 鉴权、VerificationProfile/屏障均位于模型外；继续坚持“不把 schema、权限、DONE 判断塞回 Prompt”。
- **部分**：把“每次 Activation 是新 Loop”的不变量写成 M3 收口契约：`RunActivation` 必须只从 PG/Kernel 签发的 immutable activation snapshot、固定工具注册表和持久水位重建，禁止依赖 TS 进程内缓存跨 Activation 续命。建议落点：`apps/runner/src/temporal/runActivation*`、`apps/runner/src/harness/activation.ts`、`tests/temporal/`。
- **缺失**：为每个 activation 增加显式的确定性终止原因枚举（例如 `GOAL_REQUIRES_REVIEW`、`BUDGET_EXHAUSTED`、`NO_PROGRESS_STOP`、`CANCELLED`），并测试任何原因都不能直接写 Goal DONE。建议作为 **M3.5 稳定性批**，随后 M4 屏障消费这些原因。

## 第 35 章：上下文压缩

### ① 核心论断

- 上下文保护应有三道独立闸门：主动阈值、请求前 Preflight、Provider 超限后的单次 Reactive；每道处理前一道的不同失效方式。[3]
- 压缩保留“开场 + 摘要 + 有硬下限的近期尾部”，并修复 `tool_use/tool_result` 配对切片边界。[3]
- 摘要器本身会空返、超时或溢出，故其输入要独立设界；Reactive 标志在一次 Run 内不可复位，最多一次。[3]
- 压缩只塑造模型视图，不是留存策略；原始审计记录必须另外完整保存。稳定截断边界还应避免持续打散 Prompt Cache。[3]

### ② 对 Ringharness 的增强建议

- **已实现（更保守）**：`packages/context_compiler/src/context_compiler/compile.py` 默认 `TokenBudget.max_input_tokens=8192`，估算超限即 `CompileRejected`，明确禁止静默裁剪合同/Skill。这条红线不能被“自动摘要后继续”绕过。
- **冲突与折中**：书中透明压缩若直接改写合同/证据，会与 Ringharness“超限拒绝、固定 VerificationProfile、审计完整”冲突。折中是引入**显式派生视图**：原始 binding/证据保持不变；Compiler 只接受 Kernel 预登记、带来源 digest、算法/模型版本和损失声明的 `ContextViewArtifact`。若派生视图仍超 8192，继续拒绝。
- **缺失**：在 Compiler 与实际 Provider dispatch 之间增加两次计量：compile-time 预算与 wire preflight；Provider 报 context overflow 时只允许生成一次“需要重编译”的类型化结果，不得在 Runner 内偷偷裁剪后重试。落点：`packages/context_compiler/**`、`apps/runner/src/harness/cordisLlmBridge.ts`、Kernel model invocation 协议；批次建议 **M3.5 Context Budget v1**。
- **缺失**：遥测区分 `compile_rejected`、`wire_preflight_rejected`、`provider_context_overflow`、`derived_view_failed`，并记录估算/实测 token、bundle digest；M5 用边界样例验收。

## 第 36 章：Tool Result 预算与外溢

### ① 核心论断

- 截断是销毁，外溢是搬家；大结果应保留 rune 安全的预览与可取回的持久指针。[4]
- 必须同时限制“单个结果”和“单 Turn 聚合量”；并行批次可能每项都合规而总量溢出。[4]
- 外溢替换及 Seen 状态必须跨 Turn/Checkpoint 持久化，否则每轮会重复外溢同一原结果。[4]
- 本来已有可恢复地址的读取类工具应自我设界，而非再复制一份；总量预算是目标，不是假定绝不超过的硬天花板。[4]

### ② 对 Ringharness 的增强建议

- **部分**：Ringharness 已有 S3 字节仓、Artifact 目录、SHA256 完整性与授权下载，也已有 Broker ToolResult/Effect receipt；但未发现“Tool Result → Artifact + bounded preview + durable reference”的 Prompt 外溢策略。
- **建议（最高优先）**：在 `apps/runner/src/harness/brokerBackedHarnessTool.ts` 返回模型前，按 ToolPolicy 执行单结果预算；完整结果通过现有 Artifact PUT 路径落库/对象仓，Prompt 只放 `artifact_id`、digest、字符数、内容类型、rune 安全预览与“如何精确取回”。禁止写 Runner 本机明文 spill 目录，以免绕过项目授权、GC 与审计。
- **建议**：RunActivation 在每个工具批结束后再施加 aggregate budget，从最大可外溢项开始；将 `tool_call_id → artifact_id/preview_digest/seen` 放进 Kernel 事实或 activation checkpoint，CAN 后重建同一替身。
- **红线**：外溢 Artifact 只证明完整性/来源，不证明 PASS；取回仍走 Broker/Kernel 准入，PLAN 不能因此获得 read 工具；UNKNOWN 结果不得包装成正常内容。
- **验收批次**：**M3.5 Tool Result Envelope**，路径 `apps/runner/src/harness/**`、`packages/control_kernel/**/artifacts/effects`、集成测；覆盖中文 rune、十个中等结果聚合、CAN 后不重复上传、无权限无法取回、外溢不改变 DONE。

## 第 37 章：分层压缩

### ① 核心论断

- “年龄”只是相关性的代理；第一次处理时可分为近期完整、中期语义/机械摘要、远期元数据，但正文型工具应有保内容下限。[5]
- 已压缩结果应保持终态、字节稳定，不反复重写；语义压缩按**尝试次数**设预算，失败时机械降级。[5]
- 所有重写必须维持工具调用/结果配对，并对缓存影响打点。[5]
- 若任务会回看很早证据，年龄模型不成立，应改用持久外溢与按需取回。[5]

### ② 对 Ringharness 的增强建议

- **缺失但暂不应抢跑**：在 8192 硬拒绝和 Tool Result 外溢尚未完成时，先上模型摘要会增加不透明损失。顺序应是第 36 章外溢 → 确定性 tier/stub → 最后才评估语义摘要。
- **建议**：M4 前仅实现确定性 aging view：保留最近若干 ToolResult envelope；旧项保留 `tool_call_id`、tool、参数 digest、artifact pointer、receipt/evidence ids、终态与可取回说明。因为原文仍在 Artifact 中，这不是删除证据。
- **红线**：模型生成摘要不得成为审计证据、Verification PASS 或 DONE 依据；任何摘要必须携带 source artifact digest 与 lossiness 标识。
- **验收**：同一 checkpoint 重组两次字节完全一致；Provider 配对合法；读取当前编辑文件不会只剩不可取回元数据；语义摘要尝试上限可配置且失败关闭。

## 第 38 章：Deferred Tool Loading 与 Tool Search

### ① 核心论断

- 工具 Schema 应按 token 成本而非工具数量预算；Deferred 触发可由预算超限或 always-defer 类别触发。[6]
- 高频开场工具不应延迟；Warm Set 为 session 级冷启动，并按完整 Schema 指纹而非名称失效。[6]
- Deferred Loading 只是能力发现优化，不是授权；实际权限必须在执行时强制。[6]
- 缓存在稳定前缀内的常驻 Schema 成本可跨会话轮次摊薄，不能只看单次大小。[6]

### ② 对 Ringharness 的增强建议

- **已实现（安全侧）**：Runner `authorizeToolProposal`、Broker/Kernel 再鉴权，且 PLAN 工具集为空；这正确地把授权放在执行期。
- **冲突**：PLAN 不得拥有 `tool_search`，哪怕它看似只读；因此 Deferred/Tool Search 仅能用于 EXECUTE（未来 AUDIT/INTEGRATE 也须按固定角色政策），不能作为扩展 PLAN 工具面的理由。
- **部分**：目前受控工具注册表/Pin Gate 已有，但未发现 Schema token 预算、always-defer、session Warm Set 或 schema fingerprint。
- **建议（M4 后性能批）**：先记录每个 activation 实际发送的 schema bytes/tokens 与开场调用分布；超过预算后才在 EXECUTE 引入 deferred catalog。Warm key 至少绑定 tool schema digest、ToolPolicy version、role/kind、tenant/project scope；即使模型猜中隐藏名字也照常走准入拒绝。
- **验收**：相同工具名但 schema 改变时 warm miss；未经授权的隐藏工具永不执行；PLAN request 的 tools 始终为空；首个常用工具不增加搜索往返。

## 第 39 章：Prompt Cache 稳定性

### ① 核心论断

- Prompt Cache 是实际 wire 字节前缀契约，语义等价或对象相等不够；排序、null/空对象、时间戳都会造成漂移。[7]
- 稳定边界应按共享范围与变化频率分配；易变数据必须放在稳定前缀之后。[7]
- 工具授权在执行期拒绝，不应靠每轮删改 Schema 破坏稳定前缀。[7]
- Fork 应复制实际 dispatched request 并只追加，不应从高层状态重建；每次有意重写应记录 old/new hash 以归因 cache miss。[7]

### ② 对 Ringharness 的增强建议

- **部分**：Content v3 规范编码、bundle digest、工具执行期授权为稳定字节提供了基础；未发现 actual wire request 的 canonical snapshot/hash 与 cache rewrite 事件。
- **建议（M4/M5 成本可观测批）**：在 `cordisLlmBridge.ts` / model invocation 端记录脱敏后的 `stable_prefix_digest`、`tools_schema_digest`、`history_view_digest`、provider cache read/create tokens。测试对象必须是序列化后的实际请求字节。
- **建议**：工具注册表稳定排序；时间、租约 TTL、工作目录等易变数据不得进入跨 activation 稳定前缀。工具权限仍由 Broker 执行时判定，不能为缓存而放松权限，也不应为权限每轮重排 Schema。
- **注意**：Prompt Cache 是成本/延迟优化，不可成为业务状态或 DONE 信号；当前本地 Qwen 是否支持、如何计费未验证，先做字节稳定性与遥测，不承诺收益。

## 第 40 章：持久化 Agent Loop

### ① 核心论断

- Checkpoint 应位于模型完成、工具批结束、摘要完成等阶段边界并防抖，而非每迭代或只在终点。[8]
- 中断 Turn 应通过持久标记索引发现；`InProgress ⇔ marker exists` 必须覆盖每条保存/补丁路径。[8]
- 自动恢复需要开关、意图过期窗口、尝试次数上限；尝试计数必须在模型调用前持久化。[8]
- 恢复一律按无人值守，且恢复不是幂等：外部副作用必须另有幂等键；状态还需版本化与取消清标记。[8]

### ② 对 Ringharness 的增强建议

- **已实现/领先**：Temporal 已接管持久编排；CAN 从持久水位恢复在途 Activity；`RunActivation` 最大技术尝试 1；`effect_id` 跨 Worker 不变；UNKNOWN 对账；取消/失租经 StopReceipt 与 QUARANTINED，不盲目释放。
- **部分**：CAN 有 generation/prior_run_id、consumed command ids 等水位，但未在必读范围内发现“恢复意图过期窗口”“checkpoint schema version/不可读时带事件放弃”“全局恢复开关”的完整闭环。
- **建议（M3 立即补门）**：在 Workflow 输入/Kernel Activity snapshot 增加 `created_at`、`intent_valid_until`、`checkpoint_schema_version`、`recovery_attempts`；每次重新调用模型/Runner 前原子递增尝试。过期只转 `RECOVERY_ABANDONED/REVIEW_REQUIRED`，绝不 DONE，也不自动重发 Effect。
- **与项目红线的折中**：书中“恢复运行”在 Ringharness 必须更保守：凡已进入 `DISPATCHED/UNKNOWN/QUARANTINED` 的 Effect，恢复路径只允许 reconcile/observe/stop，不允许重新 prepare 一个新 `effect_id`。
- **验收**：Temporal time-skipping/replay 测试覆盖过期、版本不兼容、模型调用前崩溃、取消后重启、同 RouteKey 并发；断言副作用 dispatch 次数为 1、Goal 非 DONE。

## 第 41 章：运行中操控 Agent

### ① 核心论断

- 追问有 accepted、committed、completed 三个不同状态；入队、进入模型上下文、回复交付分别拥有状态。[9]
- 追问应在迭代决策边界注入；最终排空与关闭注入窗口必须在同一所有权锁下，消灭“accepted 但无人拥有”的竞态。[9]
- 一次物理 Run 可有多个逻辑用户轮次，因此回复和 delivery ack 必须按入站消息 ID 寻址；回复成功后才确认。[9]
- 追加、撤回、中断是三种操作；真正取消应走取消信号，而非 Prompt 文本。[9]

### ② 对 Ringharness 的增强建议

- **部分**：暂停/取消已按 Kernel 关准入 → 持久 StopRequest → 编排唤醒 Runner/Broker → StopReceipt；这比 Prompt“停止”正确。未发现 live follow-up mailbox、三态 delivery 或每消息回复寻址。
- **项目折中**：Ringharness 的 activation snapshot、VerificationProfile、工作区归属是安全边界；追问不得在运行中悄悄修改 Goal 合同、CWD、工具政策或验证配置。普通说明可作为新 `Command` 在下一决策边界提交；改变验收/副作用范围的追问必须创建新 revision/activation，经 Kernel 重新准入。
- **建议（M4 用户控制批）**：PG 建持久 command inbox，至少含 `message_id`、`accepted_at`、`committed_activation_id`、`reply_artifact_id/delivered_at`、`revoked_at`；Temporal Signal 只负责唤醒，不承担事实权威。
- **验收**：用故障注入覆盖 accepted 后 Worker 崩溃、commit 前后崩溃、最终回复竞态、重复 delivery；每条消息要么由当前 activation 持有，要么产生新 activation，绝不丢失/双写，且 delivery ack 不影响 Goal DONE。

## 第 42 章：Agent 超时与 Watchdog

### ① 核心论断

- 超时必须绑定显式阶段与等待所有者；整轮墙钟无法区分正常长工具、审批等待和卡死 LLM。[10]
- Soft Idle 只负责可见性、每个阶段实例一次；Hard Idle 负责类型化取消并预留取消/清理传播时间。[10]
- 流间隙是传输层独立时钟，工具也有自己的超时；不同所有者的活动不能互相重置。[10]
- 嵌套摘要等远端调用要临时借用 LLM 阶段；若阶段跟踪器结构失真，生产环境不应据不可信状态贸然取消，同时要暴露覆盖失效。[10]

### ② 对 Ringharness 的增强建议

- **部分**：已有 Kernel 许可续期、Temporal heartbeat/cancel、`RunActivation start_to_close=360s`、本地 Qwen 180s 等；但这是分散时限，未见统一 `TurnPhase`、Soft/Hard、SSE chunk gap 与类型化部分结果。
- **建议（最高优先）**：在 `RunActivation` heartbeat details 中上报 `{phase, phase_epoch, phase_started_at, last_progress_at, tool_call_id/effect_id}`；至少区分 setup、awaiting_llm、executing_tool、awaiting_effect_observation、injecting_stop、finalizing。Temporal timeout 不替代 Kernel stop/reconcile。
- **建议**：LLM provider 增加 stream-gap watchdog；Hard Idle 产出类型化 activation outcome 与部分文本 Artifact，随后关闭工具准入并进入 Stop/Observe/Reconcile；不得因 timeout 自动产生新 Effect 或 DONE。
- **验收（M3.5 Watchdog）**：time-skipping/假 Provider 覆盖“LLM 无首 token”“半流卡死”“合法长工具持续心跳”“审批等待”“阶段 epoch 重入”；断言只有正确所有者超时、Soft 不取消、Hard 有原因/阶段/部分输出、UNKNOWN 路径不重放。

## 第 43 章：卡循环检测

### ① 核心论断

- 卡循环检测是多个“无进展”证据的聚合，不是单一重复规则；裁决至少有 Continue、有限 Nudge、ForceStop。[11]
- 重复失败通常比重复成功更值得宽容；Nudge 若不升级就只是装饰，ForceStop 应做一次无工具总结。[11]
- 检测只能限制重复，挡不住第一次破坏性调用；第一防线是工具对必填字段与真实结果诚实，且在系统调用前失败。[11]
- 探测器应由事故驱动并跟踪误报；删除探测器前要证明故障已被其他信号覆盖。[11]

### ② 对 Ringharness 的增强建议

- **部分**：`cordisExecuteBridge` 已验证空 path/未知字段/过长参数；Effect 观察禁止假 SUCCEEDED；UNKNOWN 关闭本 activation 后续工具准入。这些是正确的第一防线。未发现跨工具调用的 no-progress detector。
- **建议（与 DONE 红线一致）**：由 Kernel/Runner 计算确定性进展信号：新 Artifact digest、新可信 receipt、测试状态变化、工作区 manifest 变化、Task/Step 状态推进；模型“我有进展”不算。按 `tool + canonical args + observed outcome` 建滑窗，先 Nudge 一次，再 ForceStop。
- **ForceStop 折中**：可允许一次**零工具**总结并保存为诊断 Artifact，但它不能是 Audit PASS、Completion 或 DONE；若模型不可用，也必须能确定性停止。
- **建议**：对所有 Broker 工具做注册表级 required-field contract test：缺字段、空字段、未知字段必须在 create Step/prepare/dispatch 之前失败；这样避免第一下就造成副作用。
- **验收（M3.5 Loop Guard）**：相同成功调用、相同校验失败、交替 read/edit 无 manifest 变化、正常批处理不同资源、UNKNOWN 后停机；检查 Nudge 有界、ForceStop 无 tools、dispatch 次数不增加、误报指标可见。

## 第 44 章：并行工具执行

### ① 核心论断

- 并发安全与只读不是同一属性；分类应针对每次调用及参数，不应只按工具名。[12]
- Shell 分类必须保守：未知命令、未知子命令、任意元字符（含换行）均降为串行；假阴性损失延迟，假阳性损坏状态。[12]
- 并发生命周期/结果必须按 `tool_use_id` 配对；消费者支持身份关联后才能默认启用并发。[12]
- 并行前处理审批，并限制批大小；并行会加速 Tool Result 总量溢出并降低可调试性。[12]

### ② 对 Ringharness 的增强建议

- **部分**：Broker-backed Harness Tool 已使用 `callId`，测试中有 `Promise.all` 场景；但未发现生产级“按调用并发安全 + 资源冲突”分区器，当前并发语义未验证。
- **建议（M3.5 Parallel Tool Batch，放在第 36 章 envelope 后）**：ToolPolicy 新增 `concurrency_class` 与按参数解析的 `resource_keys`；只有所有调用均 `SAFE_PARALLEL` 且资源键不冲突才并行，否则稳定排序后串行。副作用类默认串行，即便资源看似不同；UNKNOWN 立即关闭后续准入并等待已在途调用对账。
- **Shell**：在达到成熟分类器前一律串行；以后如开放，仅允许 AST/严格 token 白名单，换行、管道、重定向、替换、后台符号全部 fail closed。分类结果不能替代 Broker 准入。
- **身份**：贯通 `tool_call_id/callId → step_id → effect_id → receipt_id → ToolResult/UI event`；结果顺序可变，但组装回模型时按原 tool call 顺序确定化。
- **验收**：并发两个不同只读 Artifact、同资源读锁冲突、任意 shell 元字符、两个需审批调用、一个 UNKNOWN + 一个仍在途、乱序完成；证明配对不串、无重复 effect、聚合预算生效、Goal 不被并发结果直接 DONE。

## 第 45 章：Computer Use 上下文管理

### ① 核心论断

- GUI Loop 同时增长文本观察与图像，至少需独立控制：单观察大小、文本观察窗口、浏览器截图窗口、全局含图消息上限。[13]
- 激进裁剪应按数据生成方限定：浏览器旧视口与用户上传/批量视觉素材的生命周期不同。[13]
- 陈旧载荷应替换成明确、稳定、幂等的占位文本，保留 `tool_use_id` 与调用/结果配对。[13]
- Prompt 裁剪只是模型视图，不能顺手删除底层截图、审计事件或用户素材；每种生命周期有独立所有者。[13]

### ② 对 Ringharness 的增强建议

- **缺失/未到阶段**：当前 M3 重点为 Broker 工具、心跳、暂停、失租与 UNKNOWN；未发现正式 Computer Use 工具面。不要为本章抢先扩大工具权限。
- **建议（未来 Computer Use 专项，M4/M5 后）**：在协议设计期就给 ToolResult 标记 `payload_origin`（GUI observation / browser screenshot / user upload / generated artifact）、`media_artifact_id`、digest 与 retain policy；ContextCompiler 依据来源分别施加四预算。
- **红线**：Prompt 中旧图可替换为稳定 placeholder，但完整图仍在证据账本/对象仓并按项目授权和 GC；占位不等于证据删除，截图也不等于验证 PASS。
- **验收**：巨大中文 accessibility tree、12 步浏览器导航、12 张用户比较图、重复过滤、checkpoint 恢复；检查 rune 安全、旧浏览器图只有模型视图被移除、用户图不被误裁、字节幂等、调用结果配对合法。

---

# Top 5 最该立刻做的流程增强

## 1. M3.5：Tool Result Envelope + 双层预算 + 持久外溢

- **为什么**：第 35–37 章共同表明，单个大结果和并行聚合都能击穿上下文；静默截断会销毁信息，未持久化替换会在每轮重复工作。[3][4][5]
- **改什么**：`brokerBackedHarnessTool.ts`/RunActivation 工具批组装、Artifact PUT/Kernel Artifact 协议、ContextCompiler；增加单结果预算、单 Turn 聚合预算、Artifact 指针、rune 预览、持久 Seen/Replacement。
- **验收标准**：构造 10 个各自低于单项阈值但总量超标的中文 ToolResult；验证完整字节可按授权从 Artifact 取回，Prompt 落回目标，CAN 后不重复上传，`tool_call_id` 配对不破坏，UNKNOWN/PLAN/DONE 红线不变。

## 2. M3.5：阶段化 Watchdog，而不是继续堆整轮 timeout

- **为什么**：第 42 章指出“等待多久”只有绑定阶段与所有者才有意义；LLM、stream、工具、审批需要不同的时钟。[10]
- **改什么**：`RunActivation` heartbeat details、`cordisLlmBridge.ts`、Temporal/Kernel activation outcome；引入 phase + epoch、Soft/Hard Idle、stream gap、类型化取消及部分输出 Artifact。
- **验收标准**：假 Provider 分别模拟无首 token、半流卡死与慢但持续输出，另跑合法长工具心跳；只有相应所有者超时，Soft 仅告警，Hard 关闭准入并走 Stop/Observe/Reconcile，且不自动重试 Effect、不写 DONE。

## 3. M3：补齐恢复意图时效、版本与前置尝试计数

- **为什么**：第 40 章最危险的失败不是恢复不了，而是无人值守恢复过期的破坏性意图；恢复也不自动提供幂等。[8]
- **改什么**：`GoalWorkflow` payload/Kernel activation snapshot 增加 `intent_valid_until`、`checkpoint_schema_version`、`recovery_attempts`、恢复开关；模型/Runner 调用前原子递增，过期/不兼容带事件放弃。
- **验收标准**：Temporal time-skipping/replay 覆盖过期、版本不兼容、调用前后崩溃、取消后重启；断言旧意图进入 REVIEW/ABANDONED，`DISPATCHED/UNKNOWN` 只 reconcile，dispatch 总数不增加，Goal 非 DONE。

## 4. M3.5：确定性 No-Progress Guard + 全注册表必填字段契约

- **为什么**：第 43 章证明循环检测挡不住第一次坏调用；必须先让工具诚实，再用多信号、有限 Nudge、ForceStop 控制重复。[11]
- **改什么**：Broker 工具注册表契约测；Runner/Kernel 进展滑窗，以 Artifact/receipt/manifest/test 状态等可信变化为信号；一次 Nudge 后零工具 ForceStop 总结。
- **验收标准**：缺字段在 Step/prepare/dispatch 前失败；重复成功、重复校验错、无 manifest 变化的 read/edit 循环会升级停止；正常批处理不误停；ForceStop 不带工具、不产生 PASS/DONE。

## 5. M3.5：先贯通调用身份和结果预算，再开放保守并行

- **为什么**：第 44 章强调并发安全属于具体调用而非工具名，且并行会放大结果体积与乱序归因风险。[12]
- **改什么**：ToolPolicy 增加 `concurrency_class/resource_keys`；贯通 `tool_call_id → step_id → effect_id → receipt_id`；默认副作用和 shell 串行，安全只读且资源不冲突才小批并行。
- **验收标准**：乱序完成仍正确配对；同资源冲突串行；含换行/管道/重定向的 shell 不并行；UNKNOWN 后不再准入新调用且在途项逐个对账；同一 `effect_id` 不因并发/恢复改变。

## 实施顺序建议

`#3 恢复安全门（当前 M3） → #1 Tool Result Envelope → #2 Watchdog → #4 Loop Guard → #5 Safe Parallel`。原因是并行会放大上下文、超时和对账复杂度，必须最后开放。第 38–39 章的 Deferred Tool Loading/Prompt Cache，以及第 45 章 Computer Use，应在这些正确性基础完成后作为 M4/M5 的成本与新工具面专项，不应抢跑安全闭环。

## Sources

[1] https://waylandz.com/ai-agent-book/Part10-Agent-Loop%E5%B7%A5%E7%A8%8B — Part 10 概述
[2] https://waylandz.com/ai-agent-book/%E7%AC%AC34%E7%AB%A0-%E4%BB%8EDAG%E5%88%B0Agent-Loop — 第 34 章：从 DAG 到 Agent Loop
[3] https://waylandz.com/ai-agent-book/%E7%AC%AC35%E7%AB%A0-%E4%B8%8A%E4%B8%8B%E6%96%87%E5%8E%8B%E7%BC%A9 — 第 35 章：上下文压缩
[4] https://waylandz.com/ai-agent-book/%E7%AC%AC36%E7%AB%A0-Tool-Result%E9%A2%84%E7%AE%97%E4%B8%8E%E5%A4%96%E6%BA%A2 — 第 36 章：Tool Result 预算与外溢
[5] https://waylandz.com/ai-agent-book/%E7%AC%AC37%E7%AB%A0-%E5%88%86%E5%B1%82%E5%8E%8B%E7%BC%A9 — 第 37 章：分层压缩
[6] https://waylandz.com/ai-agent-book/%E7%AC%AC38%E7%AB%A0-Deferred-Tool-Loading%E4%B8%8ETool-Search — 第 38 章：Deferred Tool Loading 与 Tool Search
[7] https://waylandz.com/ai-agent-book/%E7%AC%AC39%E7%AB%A0-Prompt-Cache%E7%A8%B3%E5%AE%9A%E6%80%A7 — 第 39 章：Prompt Cache 稳定性
[8] https://waylandz.com/ai-agent-book/%E7%AC%AC40%E7%AB%A0-%E6%8C%81%E4%B9%85%E5%8C%96Agent-Loop — 第 40 章：持久化 Agent Loop
[9] https://waylandz.com/ai-agent-book/%E7%AC%AC41%E7%AB%A0-%E8%BF%90%E8%A1%8C%E4%B8%AD%E6%93%8D%E6%8E%A7Agent — 第 41 章：运行中操控 Agent
[10] https://waylandz.com/ai-agent-book/%E7%AC%AC42%E7%AB%A0-Agent%E8%B6%85%E6%97%B6%E4%B8%8EWatchdog — 第 42 章：Agent 超时与 Watchdog
[11] https://waylandz.com/ai-agent-book/%E7%AC%AC43%E7%AB%A0-%E5%8D%A1%E5%BE%AA%E7%8E%AF%E6%A3%80%E6%B5%8B — 第 43 章：卡循环检测
[12] https://waylandz.com/ai-agent-book/%E7%AC%AC44%E7%AB%A0-%E5%B9%B6%E8%A1%8C%E5%B7%A5%E5%85%B7%E6%89%A7%E8%A1%8C — 第 44 章：并行工具执行
[13] https://waylandz.com/ai-agent-book/%E7%AC%AC45%E7%AB%A0-Computer-Use%E4%B8%8A%E4%B8%8B%E6%96%87%E7%AE%A1%E7%90%86 — 第 45 章：Computer Use 上下文管理
