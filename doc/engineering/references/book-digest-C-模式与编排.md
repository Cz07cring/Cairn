# 《AI Agent 架构》1–19 章与附录 B/C：对 Ringharness 开发流程的增强建议

> 研究范围：逐章读取第 1–19 章、附录 B/C；项目侧只读 `AGENTS.md`、`doc/01-开发文档.md` §1、`doc/v0.6/01-开发文档.md` §3–4、`doc/implementation/进度与验证.md` 最后 80 行，并对 ContextCompiler、Skill、Memory、Temporal 编排相关文件做只读定位。状态判断是本次快照，不等同于完整代码审计。
>
> 判断基准：**已实现**＝必读材料或定位到的代码/测试明确证明；**部分**＝已有领域对象/切片但闭环不完整；**缺失**＝本次材料明确未落地或未定位到；**不适用**＝与当前目标/红线不匹配，不建议为“架构完整”而引入。

## 总体结论

书中最有价值的不是再给 Ringharness 增加一种“聪明模式”，而是五条工程约束：模式按任务形态选择；所有循环都必须有预算、超时、收敛和最大轮次；上下文、记忆和交接必须可追溯且隔离；高级推理只产生候选意见；最终正确性仍由确定性验证器裁决。Ringharness 的 Kernel、VerificationProfile、证据三分离、EffectIntent、租约/fencing 与角色隔离，比书中许多教学实现更严格，应保留并作为上位规则。

书中若干做法不能原样采用：① “LLM 说完成”不能成为停止即 DONE；② Hook 队列满即丢只适用于临时遥测，不能丢审计/审批/效果事件；③ 对远程调用自动重试不能覆盖非幂等副作用，UNKNOWN 必须先对账；④ Reflection 失败返回初稿不能绕过验收；⑤ Swarm Lead 不能自行发布计划、派发副作用或判 DONE；⑥ CoT/启发式 confidence 不是证据。

---

## 第 1 章：Agent 的本质

### ① 核心论断
- Agent 是“目标 + LLM + 工具 + 记忆 + 自主循环”，生产可用还必须补预算、权限、审批、审计与沙箱；自主越高，不确定性越大。[1]
- 适合 Agent 的任务应目标明确、可拆解、结果可验证；不可逆副作用和高风险决策应设置确认点。[1]
- 模型只是概率性提案者，系统质量更多取决于工具、错误处理与护栏，而非单纯模型强弱。[1]

### ② 对 Ringharness 的增强建议
- **[已实现]** `GoalContract`、固定 `VerificationProfile`、Approvals、EffectIntent、沙箱边界与 Kernel-only DONE 已把“自主”限制在受控提案/执行域。
- **[部分]** `AGENTS.md` 明确仍是部分实现；继续以 `contracts/implementation-coverage.json` 诚实标识能力，禁止把模型连通、Activity 成功或 Workflow 完成宣传成 Agent 目标完成。
- **[增强]** 在 `doc/engineering/当前开发方案.md` 的每个 M 批次认领模板增加 `success_oracle`、`irreversible_effects`、`human_gate` 三字段；验收检查每个新增能力是否有机械判据和副作用策略。

## 第 2 章：ReAct 循环

### ① 核心论断
- ReAct 是 Reason→Act→Observe 的交织循环；观察应记录客观事实，下一轮再判断。[2]
- 生产停止条件包括用户中断、预算、超时、最大轮数、无进展/结果收敛；“模型自报完成”只是信号，不是正确性保证。[2]
- 应限制 observation window，并要求研究类任务确实调用工具、有观察证据，防止首轮“偷懒完成”。[2]

### ② 对 Ringharness 的增强建议
- **[部分]** Runner/Harness、ModelInvocation、预算、StopRequest/Receipt 与 Temporal 观察路径已形成切片；完整可持久恢复的 EXECUTE ReAct 循环仍应按当前 M 轨推进。
- **[增强]** 在 `apps/runner/src/harness/**` 产出结构化 `reason_code/action_request/observation_ref/progress_fingerprint`，但不保存私有 CoT；`packages/control_kernel` 根据连续 observation/effect/artifact 指纹判定 NO_PROGRESS，而非让模型判定。
- **[增强]** 测试加入：重复同一工具+同一参数+同一结果达到阈值后进入可解释 BLOCKED/重规划；预算耗尽停止；模型输出 “done” 不改变 Goal 状态。

## 第 3 章：工具调用基础

### ① 核心论断
- 工具应有明确 metadata、JSON Schema、参数约束和结构化结果；description 的使用场景、格式与示例直接影响选工具/填参质量。[3]
- 生产工具必须有超时、限流、成本、危险级别、认证与沙箱属性，并按任务只暴露最小工具集。[3]
- LLM 参数容错可以规范化常见格式，但不能用宽松强转掩盖危险或越权输入。[3]

### ② 对 Ringharness 的增强建议
- **[已实现]** PLAN 零工具、Broker 再鉴权、Policy/EffectIntent/Approvals、预算与工作区隔离优于普通 Tool Registry。
- **[部分]** `apps/runner/src/harness/**` 已有 Broker bridge；建议把工具说明扩展为版本化 `ToolCapabilityManifest`：schema digest、effect class（READ/REVERSIBLE/IRREVERSIBLE）、idempotency/reconcile strategy、timeout、network/path allowlist、result size ceiling。
- **[增强]** 在 `VALIDATE_SKILL` 同时做 tool-description fixture：代表性请求应选中允许工具、拒绝近似危险工具；禁止自动 clamp 金额、路径、次数等安全敏感参数，必须 schema 失败关闭。

## 第 4 章：MCP 协议详解

### ① 核心论断
- MCP 统一工具发现、调用与授权，但普通 HTTP adapter 不能冒充完整 MCP；协议兼容需 transport-level 测试。[4]
- 远程工具必须防 SSRF、超大响应、超时、提示注入、lookalike server 和工具组合攻击；Registry 元数据不能替代 allowlist 与隔离。[4]
- 自动重试、熔断适合无副作用或可安全幂等操作，不能假定所有远程调用都可重复。[4]

### ② 对 Ringharness 的增强建议
- **[不适用/缺失]** 当前内部工具少且已有 Broker，不应为生态标签立即引入 MCP。
- **[增强]** 若未来接 MCP，只允许作为 Broker 后端 adapter，配置写入不可变版本并固定 server identity、协议版本、transport、tool schema digest、域名/证书、响应上限；MCP Client 不得获得业务 DB 写权或绕过 EffectIntent。
- **[冲突处理]** 书中指数退避重试必须按 effect class 分流：只读可重试；幂等写复用同一 `effect_id`；UNKNOWN/不可安全重试写先 reconcile，失败则 BLOCKED。

## 第 5 章：Skills 技能系统

### ① 核心论断
- Skill 把 system instructions、工具白名单、参数/预算约束和工作流知识版本化复用；能力边界与工作流步骤应分离。[5]
- 渐进式披露分元数据、正文、引用文件/脚本三层，避免在启动时把所有工具和说明塞入上下文。[5]
- 危险 Skill 的可调用者、可用工具、预算应三层独立控制；Skill 可声明适合单 Agent 还是多 Agent 路径。[5]

### ② 对 Ringharness 的增强建议
- **[已实现]** Skill/SkillSet API、固定版本、CANDIDATE→VALIDATING→ACTIVE/REJECTED/REVOKED、`VALIDATE_SKILL` 与 role scopes 已落地。
- **[部分]** ContextCompiler 会绑定 ACTIVE Skill 工件，但渐进式披露、支持文件按需装配和模式声明未验证。
- **[增强]** 在 SkillVersion schema 增加不可变 `execution_shape`（single/react/dag，仅路由建议）、`risk_class`、`required_capabilities`、`supporting_ref_digests`；激活时验证，运行时只能从已冻结 refs 选择，不能动态扩大工具权限。

## 第 6 章：Hooks 与事件系统

### ① 核心论断
- Hooks 用于看（观测）、管（暂停/审批）、扩（插件逻辑）；暂停应发生在明确 checkpoint，长任务宜用持久信号而不是轮询。[6]
- 事件需要分级：高频 partial/heartbeat 与审计级状态事件采用不同保留策略。[6]
- 审批超时应拒绝；阻塞 Hook 会拖垮主流程，临时遥测可异步丢弃。[6]

### ② 对 Ringharness 的增强建议
- **[部分]** append-only journal/outbox、ReadModel snapshot/events/SSE、Approvals、Temporal、StopRequest/Receipt 已覆盖多数机制；部分 pause/cancel/stop seam 与完整 UI 仍在推进。
- **[关键冲突]** 不能照搬“队列满即丢”：effect、approval、lease/fencing、verification、finalization 事件必须与业务状态同事务持久化；仅 LLM partial、UI typing、可重建指标允许丢。
- **[增强]** 建立 `EventDurabilityClass` 合同与测试矩阵，并在 `packages/read_model`/outbox relay 做故障注入：断流后 SSE 可从序号恢复，审计事件零缺口，临时事件允许采样。

## 第 7 章：上下文工程

### ① 核心论断
- 上下文工程不是润色 prompt，而是管理 instructions、history、memory、retrieval、tools 与 output contract 的有限预算。[7]
- 四策略是 Write、Select、Compress、Isolate；压缩有损，应保留 URL/文件路径等可恢复引用和失败经验。[7]
- 上下文越长并不必然更好；JIT 检索、最小工具集、稳定前缀和隔离子上下文更重要。[7]

### ② 对 Ringharness 的增强建议
- **[已实现]** ContextCompiler 默认预算 8192、超限拒绝；合同、Skill、binding 与 PlanningFeedback 以引用/摘要装配，不静默裁掉合同。
- **[部分]** 进度记录显示早期无记忆/代码检索装配；现有完整 JIT 检索、压缩、每分区预算状态未验证。
- **[增强]** `ContextBundle` 增加逐分区 token ledger、selection reason、source digest、excluded reason；合同/权限/VerificationProfile 永不压缩，旧工具输出只替换为内容寻址引用，失败摘要保留可恢复路径。

## 第 8 章：记忆架构

### ① 核心论断
- 长期/语义记忆应与工作/会话记忆分层；语义检索需 recent + semantic + summary 融合、去重和来源标记。[8]
- 向量相似度不是事实证明；阈值、分块、MMR、租户/Agent 隔离和 PII 保留策略都需评测。[8]
- 成功策略与失败模式可成为记忆，但低价值内容不应污染召回。[8]

### ② 对 Ringharness 的增强建议
- **[部分]** `/memories`、PROPOSED→VERIFIED、INDEX_MEMORY 排队/claim/outcome 已实现诚实晋升切片；进度明确无 `memory_indexes` 表、向量服务与正式 worker。
- **[增强]** 优先实现“权威 PG 记录 + 可重建向量索引”，索引条目携带 memory_id/status/type/source_evidence_ids/role_scope/model_version；只检索 VERIFIED fact/decision，hypothesis/question 必须显式标记，不能混成事实。
- **[冲突处理]** 书中“错误消息不存”不适用于 Ringharness 的 failure memory；应去除噪声堆栈和秘密，但保留结构化 failure fingerprint、已试路径和反证，防止重复失败。

## 第 9 章：多轮对话设计

### ① 核心论断
- 多轮系统需要持久 Session、token-window/摘要、租户隔离、防 session hijack、PII 脱敏与幂等派生字段。[9]
- 无权访问与不存在应尽量使用不可枚举的响应；标题等派生模型调用应只生成一次并有确定性降级。[9]
- 缓存故障的“创建空会话”降级可能改善聊天体验，但会牺牲连续性与正确性。[9]

### ② 对 Ringharness 的增强建议
- **[部分]** activation 身份、会话、工具集、工作区隔离是红线；Harness session/Checkpoint 已设计，完整跨恢复闭环仍在推进。
- **[增强]** 给角色会话建立 `(project, goal, role, activation, attempt)` 复合绑定与反劫持测试；跨角色只传批准的 artifact/evidence/feedback，不传对方聊天史或推理文本。
- **[冲突处理]** Redis/会话存储故障不得静默创建“空业务会话”继续执行；Ringharness 应失败关闭或恢复到可信 checkpoint，避免丢失 effect/attempt 上下文。

## 第 10 章：Planning 模式

### ① 核心论断
- Planning 是分解→执行→覆盖评估→补充的有界循环；子任务应声明 dependencies、produces/consumes 与 in/out scope。[10]
- 无依赖并行、有依赖走 DAG；LLM 的覆盖判断必须被最大轮次、关键缺口等确定性护栏覆盖。[10]
- 过度分解、范围重叠、循环依赖和追求 100% 覆盖都会增加失败与成本。[10]

### ② 对 Ringharness 的增强建议
- **[部分]** GoalContract/TaskContract、动态 DAG、criterion coverage、PlanningFeedback 和版本化重规划已设计/有切片；端到端 activation 与发布闭环仍不能按文档概念视作全实现。
- **[增强]** Plan activation 后、发布前增加 `PlanStaticValidator`：环检测、悬空依赖、producer/consumer 完整性、边界重叠、每条 criterion 至少一条验证路径、预算可行性；PLAN 仍零工具，只产提案。
- **[验收]** 构造环、缺 producer、无 criterion coverage、越预算四类计划均拒绝，且拒绝不会创建工程 effect 或开放 EXECUTE 准入。

## 第 11 章：Reflection 模式

### ① 核心论断
- Reflection 是评估→具体反馈→有限重生成，适合高价值且有客观标准的输出；MaxRetries 通常 1–2。[11]
- 自评有偏见，最好用独立模型/确定性规则；Reflection 不能保证正确，也不应成为核心依赖。[11]
- 教学实现的“Reflection 失败返回初稿”只适用于低风险生成，不适合硬验收任务。[11]

### ② 对 Ringharness 的增强建议
- **[已实现/更严格]** Auditor 独立验证、GoalReview、PlanningFeedback、VerificationProfile 将自我反思提升为独立批评与机械验收。
- **[增强]** 把 Reflection 限定为“候选改进器”：Auditor 输出结构化 criterion→finding→evidence_ref→recommended_change，经 Kernel 形成不可变 PlanningFeedback；Executor 可生成新 candidate，但旧 candidate 不原地修改。
- **[冲突处理]** 评估失败不得返回初稿并继续 finalization；必须 BLOCKED/REWORK/REVERIFY，且模型评分永远不能写 PASS。

## 第 12 章：Chain-of-Thought

### ① 核心论断
- 结构化中间论断可能帮助多步任务，但生成解释不是模型私有推理的忠实窗口。[12]
- 必须区分私有推理、用户可见理由与可验证证据；高风险审计只应依赖工具结果、引用、计算、测试和状态后置条件。[12]
- 按连接词/步骤数计算的 confidence 只是启发式，不能当校准概率或正确性证明。[12]

### ② 对 Ringharness 的增强建议
- **[适用但无需新模式]** 不需要存储或展示完整 CoT；ModelInvocation 只登记必要元数据，避免角色间推理泄漏与敏感信息扩散。
- **[增强]** 统一模型输出为 `decision + concise_rationale + claims[] + evidence_refs[] + uncertainty[]`；Auditor 对 claims 逐条验证，rationale 只供诊断。
- **[验收]** 测试“解释文本包含 PASS/置信度 0.99”但缺 EvidenceEnvelope 时，Kernel 必须拒绝；日志/ReadModel 不暴露隐藏推理。

## 第 13 章：编排基础

### ① 核心论断
- 编排器职责是分解、分发、协调与综合；多 Agent 只有在并行、专业化或容错收益高于协调成本时才值得。[13]
- 并发必须有上限，结果综合要去重、保留冲突和失败信息，预算按任务分层。[13]
- 暂停/恢复/取消应传播到子工作流并在 checkpoint 生效。[13]

### ② 对 Ringharness 的增强建议
- **[部分]** Temporal GoalWorkflow 已接 PLAN/EXECUTE 准入与恢复观察，且源码明确不写 DONE；AUDIT/VERIFY/finalization 的完整持久编排状态本次未验证。
- **[增强]** 编排路由必须是版本化策略输入，输出只为 Kernel action IDs；Temporal 只消费 Kernel 准入结果，不自行从 LLM complexity score 直接派发。
- **[增强]** 每批 Temporal 变更必须包含 replay test、Continue-As-New 水位不丢、父子取消传播、并发槽/预算守恒、`marks_goal_done is False` 五类门禁。

## 第 14 章：DAG 工作流

### ① 核心论断
- DAG 用静态无环依赖表示哪些任务并行、哪些等待；图结构固定、依赖清晰时优于动态 Swarm。[14]
- Temporal Workflow 必须使用确定性 API；依赖等待要有超时和状态观察，不能把 SDK retry 当业务 attempt。[14]
- 环检测、依赖结果传递、并发上限与失败传播是启动前和运行时的关键约束。[14]

### ② 对 Ringharness 的增强建议
- **[部分]** 领域已定义 Task DAG，Temporal 接管持久编排；`temporal_attempt` 与业务 `attempt_id/fencing_epoch/execution_round` 已明确分离。
- **[增强]** 固化 DAG 发布前 validator 和 replay fixture；每条边绑定 plan_version，重规划生成新图，旧许可不复活。
- **[冲突处理]** Temporal 自动 retry 只能重复技术投递，稳定 business action/effect ID 不变；新的业务 attempt 必须经 Kernel 确认旧执行失效、资源隔离和效果可处理。

## 第 15 章：Swarm 模式

### ① 核心论断
- Swarm 适合任务数量/结构无法预先确定、需要运行时人类输入和 Agent 通信的场景；Lead 事件驱动协调 Workers。[15]
- 纯去中心化在模块化任务上有效，但共享单体问题容易互相踩踏；验证器和环境设计比增加 Agent 数更关键。[15]
- Workspace 应增量读取，Worker 要有收敛检测、idle/reassign 与全局终止条件。[15]

### ② 对 Ringharness 的增强建议
- **[不适用（V1 核心）]** 当前应优先完成固定 DAG + Kernel 裁决，不引入可自行 spawn/revise/dispatch 的自由 Swarm。
- **[可选未来]** 若引入，只能作为 PLAN 域中的“提案群”：Lead 无工具，spawn/reassign/revise_plan 均转为 Kernel 校验的提案；Worker 工作区、activation、工具集仍隔离，串行集成候选。
- **[红线]** 共享 Workspace 不能成为业务事实源；PG/journal/outbox 仍权威，任何 file_read“零成本验证”只能检查存在/格式，不能替代来源可信与验收正确性。

## 第 16 章：Handoff 机制

### ① 核心论断
- Handoff 从 previous_results、Workspace 到 P2P 逐级增加复杂度；大多数固定链路不需要 P2P。[16]
- Plan IO 的 produces/consumes、producer 集合验证、数据大小限制、内容引用与增量序号是可靠交接的基础。[16]
- 交接必须有超时、确定性时间和明确接收者；大数据存对象仓，只传引用。[16]

### ② 对 Ringharness 的增强建议
- **[部分]** Artifact/EvidenceEnvelope/CandidateManifest、ExecutionBinding、ContextBundle、PlanningFeedback 已提供可追溯交接材料，角色身份和工作区隔离已明确。
- **[缺失/增强]** 增加统一 `HandoffEnvelope`：from/to role、goal/task/activity/attempt、plan/contract/config/skill/profile versions、artifact/evidence refs+digests、open risks、known failures、effect reconciliation state、budget remainder；接收方必须 ACK 校验。
- **[验收]** 版本陈旧、hash 不符、跨 role 未授权字段、UNKNOWN effect 未对账、缺关键 evidence 任一情况均拒收且不启动下一 activation。

## 第 17 章：Tree-of-Thoughts

### ① 核心论断
- ToT 适用于解空间大、早期决策影响大、且中间状态可评估的问题；通过分支、评分、剪枝和回溯搜索。[17]
- 分支因子×深度会爆炸，必须有 exploration budget；关键词式评分容易被套话欺骗。[17]
- ToT 成本高，优先先用简单模式，只有方向选择失败才升级。[17]

### ② 对 Ringharness 的增强建议
- **[缺失/非近期必需]** 不应在 EXECUTE 内让多个分支真实调用副作用工具。
- **[可选增强]** 把 ToT 收敛为 PLAN 候选生成：多个隔离 Planner activation 各产不可变 PlanCreate，确定性 validator 先筛，再由独立 Auditor/人类按预登记 trade-off 比较；任何分支都无工具。
- **[验收]** exploration budget、分支数、版本和候选 hash 可追溯；未选分支不能留下 effect intent/lease/工作区写入。

## 第 18 章：Debate 模式

### ① 核心论断
- Debate 用对立视角暴露确认偏差和论证漏洞；视角必须真实对抗，轮数应有限，并保留少数派风险。[18]
- 主持人、投票或 confidence 只能综合意见，关键词论证评分不证明事实正确。[18]
- 有客观答案的任务不应使用 Debate；机械测试优先。[18]

### ② 对 Ringharness 的增强建议
- **[缺失/选择性适用]** 只适用于高风险架构取舍、VerificationProfile 变更评审或模糊 GoalContract 澄清，不适用于测试结果、hash、状态迁移。
- **[增强]** 设 `proposer/critic/risk` 三种隔离 activation，输出 claim/evidence/counterexample/minority_risk；Moderator 只能生成建议，Kernel/审批者按版本化规则决定是否接受。
- **[红线]** 同一底座模型可多角色但不等于独立性；必须配合机械验证、保留测试和工作区/会话/工具隔离，Debate 多数票不能判 DONE。

## 第 19 章：Research Synthesis

### ① 核心论断
- 系统研究是规划→并行研究→覆盖评估→缺口补充→综合报告，不是搜索结果拼接。[19]
- 覆盖必须区分“找到实质信息”和“承认未知”；多源交叉验证、冲突与限制需要显式保留。[19]
- LLM 覆盖判断需最大轮次等确定性护栏，输出事实需内联引用和来源清单。[19]

### ② 对 Ringharness 的增强建议
- **[部分]** EvidenceEnvelope、criterion coverage、Candidate/Release Manifest 和三层审计可承载该模式；通用 Research workflow 未验证。
- **[增强]** 为文档/调研类 Goal 建 `EvidenceCoverageMatrix(criterion, claim, source_ref, source_class, corroboration, contradiction, freshness)`；Auditor 检查真实覆盖而不是字数/模型分数。
- **[验收]** “未找到”不得计覆盖；相互冲突来源不得被综合器静默抹平；关键 claim 少于规定可信来源或来源未 fetch/未 hash 时 finalization 失败。

## 附录 B：模式选择指南

### ① 核心论断
- 黄金法则是从最简单模式开始，只有质量、效率或方向性瓶颈被评测证明后才升级。[20]
- ReAct/Planning 覆盖大多数任务；Reflection、ToT、Debate 与动态 Supervisor 成本高、延迟高、难调试。[20]
- 模式选择必须结合依赖是否静态、是否需要外部工具、是否有客观答案、延迟/成本/质量约束。[20]

### ② 对 Ringharness 的增强建议
- **[增强]** 新增版本化 `PatternSelectionPolicy`，但它只选择“如何生成候选/反馈”，不选择 DONE 语义和权限；默认 `planning+dag+independent_audit`，高级模式须显式 policy/approval。
- **[增强]** 在批次验收中比较同一固定基准集的完成率、token、wall-clock、重规划次数、BLOCKED 率、错误副作用数；没有显著收益不得升级模式。

## 附录 C：FAQ

### ① 核心论断
- 多 Agent 并不天然优于单 Agent；先跑通单 Agent/工具链，再按任务拆分、专业化与并行收益升级。[21]
- 预算、最大迭代、超时是防失控三道基础护栏；质量评估还需正确性、效率与安全基准集。[21]
- Prompt injection、沙箱、租户隔离、记忆分层、真实工具调用与回归评测是生产常见问题。[21]

### ② 对 Ringharness 的增强建议
- **[已实现方向正确]** Ringharness 已把这些问题升级为 Policy、Approvals、sandbox、budget ledger、role isolation、Memory 状态机和验证屏障。
- **[增强]** 建立跨模式回归 corpus：首轮假完成、循环调用、prompt injection、工具组合外泄、角色串话、过期 handoff、UNKNOWN effect、记忆污染、Workflow COMPLETED 假 DONE；每次模式/Skill/ContextCompiler 变更必跑。

---

## 对照表 1：附录 B 模式选择 vs Ringharness 当前模式

| 模式 | 书中选择条件 | Ringharness 当前采用方式 | 状态 | 建议边界 |
|---|---|---|---|---|
| ReAct | 需要工具、任务相对简单 | EXECUTE 由 Harness/Runner 提案工具，Broker/Kernel 准入 | 部分 | 模型“完成”仅结束 activation，不是 DONE；加 progress fingerprint/收敛门 |
| Planning | 多步、有依赖、结构化输出 | Manager/Planner 产 PlanCreate，PlanningFeedback 驱动版本化重规划 | 部分 | PLAN 工具集永远为空；发布/派发由 Kernel |
| DAG | 可提前规划的部分依赖 | Goal/Task DAG + Temporal 持久编排 | 部分 | 发布前静态验证；技术 retry 不创建新业务 attempt |
| Reflection | 高价值、有客观标准 | 独立 Auditor/GoalReview，而非同一 Agent 自评 | 已实现方向/部分闭环 | 反馈只生成新 candidate；不可自评 PASS |
| Handoff | 专业角色交接、需上下文传递 | ContextBundle、Artifact/Evidence、PlanningFeedback、ExecutionBinding | 部分 | 增加 HandoffEnvelope+ACK；不传私有推理/未授权历史 |
| Debate | 争议性、多视角 | 当前非核心 | 缺失/选择性适用 | 仅设计/策略提案；少数风险保留；票数不决定 DONE |
| ToT | 多解空间、可评估中间状态 | 当前非核心 | 缺失/非近期必需 | 仅 PLAN 候选分支，所有分支零工具、无副作用 |
| Research Synthesis | 系统调研、覆盖+引用 | 证据/criterion/manifest 可承载，通用流程未验证 | 部分 | 用 EvidenceCoverageMatrix 和可信来源门，不用字数/模型分数 |
| Swarm/Supervisor | 结构动态、实时协调 | 与 V1 固定权威链不匹配 | 不适用（近期） | 若未来引入，Lead 仅提案，Kernel 仍是唯一调度/裁决权威 |

## 对照表 2：Skills / Hooks / MCP vs Ringharness Skill API 与证据审计

| 书中机制 | 书中目的 | Ringharness 对应 | 状态 | 必须增加/保留的审计要求 |
|---|---|---|---|---|
| Skill 元数据+正文+支持文件渐进加载 | 节省上下文 | SkillVersion/SkillSet + ContextCompiler artifact bindings | 部分 | 每层内容 digest、选择理由、role scope；运行时不得扩权 |
| Skill 工具白名单/预算/risk | 最小权限 | role scopes、Policy、预算、PLAN 零工具 | 已实现方向 | 激活验证 tool set 与 role；dangerous Skill 走审批消费 |
| Skill 验证与版本 | 复用可靠流程 | VALIDATE_SKILL、ACTIVE/REJECTED/REVOKED | 已实现 | 验证记录绑定固定 VerificationProfile 与证据；在途版本不漂移 |
| Hook 生命周期/工具事件 | 观测与扩展 | journal/outbox、ReadModel events/SSE、ModelInvocation | 部分 | 审计事件同事务、序列可恢复；不得因背压丢失 |
| Hook 暂停/恢复/审批 | 人机控制 | Temporal、Approvals、StopRequest/Receipt | 部分 | checkpoint、超时拒绝、信号传播；StopIntent≠StopObserved |
| MCP tools/list/call | 外部工具复用 | 当前 Broker adapter 可承载 | 缺失/非必需 | 固定 server/schema/transport 身份；allowlist、响应上限、注入隔离 |
| MCP 自动 retry/熔断 | 网络容错 | EffectIntent、幂等键、reconcile | 仅条件适用 | 只读可重试；写复用 effect_id；UNKNOWN 先对账，绝不盲重放 |
| MCP/工具输出 | 给模型观察 | EvidenceEnvelope/Artifact refs | 部分 | 工具输出是非可信数据；hash 完整性、来源可信、验收正确性三分离 |

---

## Top 5 最该立刻做的流程增强

### 1. 发布前 PlanStaticValidator 闸门
- **为什么**：Planning/DAG/Handoff 都要求依赖、produces/consumes、边界、覆盖和预算可验证；模型覆盖判断不稳定。[10][14][16]
- **改什么**：在 M 轨 PLAN activation→Kernel 发布之间加入确定性 validator；触及 `packages/control_kernel` 的 plan/graph 准入、合同 schema 与对应集成测，按正常契约单向流程生成。
- **验收标准**：环、悬空依赖、无 producer、边界冲突、criterion 无验证路径、预算不可能六类 fixture 全部失败关闭；合法 DAG 发布；失败时零 effect、零 EXECUTE lease，PLAN tools 仍为空。

### 2. 统一 HandoffEnvelope + 接收 ACK
- **为什么**：多角色系统最危险的不是“不会交接”，而是版本、证据、效果状态或身份在交接中丢失。[9][16]
- **改什么**：为 PLAN→EXECUTE→AUDIT/VERIFY 定义带版本/hash/effect reconciliation/open risks 的交接合同；ContextCompiler 只从该 envelope 和权威 PG 编译下一 activation。
- **验收标准**：过期 plan/config/skill/profile、artifact hash 错、跨角色字段泄漏、UNKNOWN effect 未对账、缺必需 evidence 均拒收；ACK 幂等，重放不重复副作用。

### 3. ToolCapabilityManifest 与组合攻击门
- **为什么**：工具质量、最小权限、远程身份与 retry 语义决定 Agent 能否安全执行；MCP 也不能替代这些边界。[3][4][5]
- **改什么**：给 Broker 工具登记 schema digest、effect class、idempotency/reconcile、timeout、path/network allowlist、response ceiling；VALIDATE_SKILL 检查工具选择 fixture 和危险组合（如 read secret + outbound HTTP）。
- **验收标准**：未登记/漂移 schema、越域 URL、超大响应、危险参数强转、禁用工具组合全部失败；非幂等写超时后同 `effect_id` 对账，不出现第二次外部效果。

### 4. Context/Memory provenance 预算账本与检索基准
- **为什么**：上下文应 JIT 选择且可恢复；语义相似度不是事实，记忆污染会跨轮次放大。[7][8][9]
- **改什么**：ContextBundle 增加分区 token/selection/exclusion/source digest；补“PG 权威记录+可重建索引”的 memory worker，并按 project/goal/role/status/type 隔离；PII/secret 在持久化前处理。
- **验收标准**：超 8192 仍拒绝且不裁合同；只召回允许作用域的 VERIFIED facts/decisions；hypothesis 明示；删除索引可从 PG 重建；检索集报告 recall/重复率/污染率；角色串话为 0。

### 5. 审计级事件不丢 + 高级推理只产提案的跨模式回归门
- **为什么**：Hooks 的异步丢弃只适合临时遥测；Reflection/ToT/Debate/Swarm 的结论都不能替代独立证据和 Kernel 裁决。[6][11][12][15][17][18][21]
- **改什么**：定义事件耐久等级；把 Reflection/Debate/ToT 输出限定为 versioned proposal/PlanningFeedback；建立跨模式安全 corpus 并纳入每批验收。
- **验收标准**：outbox/SSE 断连重放后 effect/approval/lease/verification/finalization 事件零缺口；LLM “done/PASS”、多数票、高 confidence、Workflow COMPLETED 均无法直接改变 Goal DONE；仅固定 VerificationProfile + finalization barrier 可完成。

## Sources
