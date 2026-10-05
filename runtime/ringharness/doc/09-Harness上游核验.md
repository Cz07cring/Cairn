# DeepSeek Harness 上游核验与v0.5接入约束

文档基线：v0.5。上游事实沿用固定提交的已核验结果；本轮更新自有接入契约，不宣称重新安装或运行上游。

核验日期：2026-09-11（Asia/Shanghai）。核验对象：官方 `deepseek-ai/deepseek-harness`。固定提交：[`c291e7961a515f6d7af9304e7fd1d257929aef26`](https://github.com/deepseek-ai/deepseek-harness/commit/c291e7961a515f6d7af9304e7fd1d257929aef26)，提交时间为 2026-09-10 14:17:09 UTC。本附录是静态官方资料和选定源码核验，不是 ringharness 的实现证明，也不是上游完整审计或运行验收。

## 1. 可用于工程设计的结论

| 主题 | 已证实事实 | 对 ringharness 的含义 |
|---|---|---|
| 技术栈 | 根清单使用 ESM，声明 `pnpm@11.7.0`、Node `^22.19.0 \|\| >=24.0.0`，具有 TypeScript 构建和 workspace 配置。[根清单](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/package.json) | 接入层按 Node/TypeScript 边界设计；不能把上游描述为 FastAPI 应用。 |
| 扩展机制 | Cordis 提供 service、typed event 与可撤销 effect；模型适配器、工具注册、session log、agent loop 均可组合替换。[架构](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/docs/architecture.md#cordis) | 自有权限、证据和调度集成优先做插件/adapter，避免维护私有 loop 分叉。 |
| 配置启动 | 官方应用通过 `dsh` 加 profile 启动；`sdk`、`headless`、`sdk-minimal`、`acp` 启动时应用配置层，`web` 支持 live patch reload。[启动和 profile](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/docs/architecture.md#application-launch) | Runner 必须保存 profile、配置摘要、版本；不可默认运行中修改 SDK 插件树。 |
| Skills | `dsh-skill` 管注册表，`dsh-skill-filesystem` 做目录发现，`dsh-tool-skill` 提供目录与按需加载工具。[包族](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/packages/skill/README.md) | 用户要求的 Skill 接入有官方原生基础，不需要把全文长期塞进提示词。 |
| 会话持久化 | session facts 可持久化；agent live events 与流式 chunk 不能直接当作 durable facts。[Session log](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/docs/architecture.md#session-log) | 自有 Task 状态提交、证据落盘和 UI 临时流必须分开。 |
| 后台 jobs | 默认 `LocalJobRegistry` 使用内存 Map，属于进程本地生命周期注册表。[实现](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/packages/jobs/jobs-local/src/index.ts) | 不能把 `ctx.jobs` 当成跨进程 durable 调度数据库。 |
| Subagent | 可选能力，有多个 provider；支持一次性路径和 continuable child，provider 能力不完全相同。[Subagent](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/docs/subsystems/subagent.md) | 调度前做 capability negotiation，不能静默丢弃结构化输出、深度、工具过滤等要求。 |
| Agent Teams | 官方架构声明实验性、显式启用的 `ctx.agentTeams`，有 durable roster/task board/mailbox。[能力边界](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/docs/architecture.md#capability-seams) | 不得声称上游完全没有持久化协作；是否复用需要独立适配和故障验收。 |

## 2. Skill 接入的精确边界

官方 `SkillProvider` 提供异步 `list(options)` 与 `get(candidate, options)`；`ctx.skills.registerProvider(...)` 注册时同步安装 provider，其远端发现放在 `list` 内。注册按 host/per-scope 分层；最近 scope 的同名技能优先。未完整发现的目录不能当作权威空目录缓存。[Provider contract](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/docs/subsystems/skills.md#provider-registry)

本地目录顺序为项目 `.dsh/skills`、项目 `.agents/skills`、自定义目录、用户 DSH skills、用户 agents skills、配置的 bundled 目录。支持 `<name>/SKILL.md` 和 `<name>.md`，不支持递归 `**/SKILL.md`；名称要求 kebab-case。[发现规则](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/docs/subsystems/skills.md#local-discovery-priority)

**ringharness 设计要求，尚非上游保证：** 每次 ActivityAttempt 固定 `skill_id + version + content_digest + source`，记录加载证据；普通技能升级只能影响新 attempt 或显式重规划；紧急撤销须阻止在途 attempt 的后续动作并保存/核对进度；Skill 文本本身不能提升工具权限、扩大网络范围或批准外部副作用。权限由ControlKernel和ExecutionBroker强制执行；上游工具注册必须全部接到该Broker，不能保留可绕过它的本地写工具。

Planner 接入特例（自有强制设计）：PLAN profile 不加载任何模型可调用工具，包含 dsh-tool-skill、文件读取、搜索及 subagent/team 工具。ContextCompiler 预加载批准且固定版本的规划 Skill；宿主调用模型和处理 lease 不是 Planner 的工具权限。adapter 必须拒绝意外 tool call，Kernel/Broker 再按服务身份拒绝 PLAN 工具请求。上文“所有工具接 Broker”只适用于其他已授权角色，不能被理解成 Planner 可以经 Broker 使用只读工具。

## 3. 持久化与恢复不能混为一谈

上游架构明确：完整 assistant settlement 写入后用于 fork/resume；若进程在 settlement 前硬退出，本次未提交的 stream 不构成 durable attempt。`SessionPersistence` 是可替换 seam，提供 `create/open/stat/list/export`；这意味着持久化具备扩展点，并不自动证明具备业务任务的租约、fencing、资源预算或 exactly-once 副作用。[Session log 与持久化扩展](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/docs/architecture.md#session-log)

上游 jobs 状态是 `running/stopping/completed/killed/failed`。`JobHooks.done` 表示 producer 资源已释放后结算；清理异常可以使注册表记录失败，但不能据此声称外部进程已经停下。owner 释放会取消并等待 job。[Job 生命周期](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/docs/subsystems/jobs.md)

**ringharness 设计要求：** PostgreSQL 保存 Goal/Task/Activity/ActivityAttempt/EffectIntent/Approval/Barrier 的权威业务状态；Harness session 保存模型上下文和工具历史；两者通过固定 activity_id/attempt_id/session_ref 关联。恢复先读取已有证据并对账，再决定继续、重试或人工介入。会话可恢复不等于外部操作可重放。

## 4. v0.5采用的插件与adapter映射（待实现）

以下为工程设计映射，非已存在的 ringharness 包：

| ringharness 责任 | 官方 seam | 设计约束 |
|---|---|---|
| 模型路由 | `ctx.llm` | 保存实际 model/provider，不能只记配置默认值。 |
| 工具策略与执行 | `ctx.tools`、`tools/*` | 所有提案进入ExecutionBroker；Kernel检查lease、effect_id、审批消费和write_epoch；不得用ctx.jobs的ID替代业务effect。 |
| 沙箱 | `ctx.fs`、`ctx.subprocess`、`ctx.sandbox` | 文件和进程处于同一执行世界，沙箱隔离须实测。 |
| 证据记录 | `SessionEventMap`、`session/event` | session事件是上下文事实；工程证据由可信Broker采集进入EvidenceLedger，不能由模型流自报成功生成。 |
| Skill 目录 | `ctx.skills` | ContextCompiler为每个ActivityAttempt固定Skill目录与digest，禁止用户home/工作区自动发现绕过批准registry。 |
| 委派 | `ctx.subagents` | 先验证provider能力；子代理若有长动作必须关联Kernel Activity，保存lineage，不能后台自行形成第二任务队列。 |
| 同会话目标 | `ctx.goals` | 不替代 ringharness 的跨任务 GoalContract、DAG 与预算账本。 |

接口名称来源：[官方扩展映射](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/docs/architecture.md#where-new-behavior-goes)。此处只确定 seam，具体注册签名应在 adapter 实现时从锁定版本的类型声明生成，不凭本文手写兼容层。

## 5. 开发前仍需通过的验证

这些检查尚未执行，不能写成已通过：

1. 固定该 commit 或对应发布包，安装构建并记录实际 Node/pnpm/OS；根清单版本号不等于已验证的 npm 发布包版本。
2. SDK 建立 session、运行最小工具、读取持久化历史、进程终止后重新打开；验证真实返回类型与错误码。
3. 对启用的每种 subagent provider 分别测试取消、继续、结构化结果、权限过滤和资源回收。
4. 模型流中途、工具执行前后、结果已生成但控制面未提交时强制中断，验证不重复副作用。
5. Skill scope 隔离、同名覆盖、目录更新、失效缓存和恶意 Skill 越权测试。
6. 默认不启用实验性Agent Teams作为业务调度来源。若启用仅作受控协作adapter，逐活动映射Kernel Activity并验证不能绕开budget/effect/屏障。
7. 实测 100 小时 soak、预算耗尽、控制面切换和 worker 崩溃恢复后，才可声明无人值守运行验收完成。

## 6. 核验方法与限制

使用官方 GitHub 页面、官方 commit API 和固定 SHA 源文件；已查看 `LocalJobRegistry` 的内存实现与根依赖清单，并核对官方架构、Skills、Jobs、Subagent 文档。本次源码 clone 未完成，部分 raw 下载超时，未执行上游安装或测试。结论按以上已读取资料限定，不用网络获取失败推断功能不存在。上游 README 标记为 developer preview，后续升级需要重新核验接口与回归。[官方说明](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/README.md)

## 7. 自有协议的明确替代关系

| 上游对象/能力 | ringharness v0.5用法 | 验证 |
|---|---|---|
| session / activation | ActivityAttempt关联的可重建上下文，不拥有业务状态 | AT03/AT08 |
| local job | 仅在当前Runner内追踪执行；失败后由Kernel根据原effect/证据恢复 | AT01/AT03 |
| tools / subprocess | Broker强制准入、可信collector、隔离工程与验证scope | AT01/AT02/AT04 |
| continuation | 恢复上下文，不能重新分配原logical_step_id/effect_id | AT01/AT05 |
| skills | 固定批准版本；普通升级留给新attempt，撤销拒后续调用 | T20 |
| agent teams / subagents | 可选adapter；不能持独立业务调度权 | AT03/AT08 |

外部interface采用[05](05-API接口文档.md)的ActivityLease/Outcome。Runner内统一包含fake与Harness两种adapter，取消旧的Python Worker→TS adapter重复协调层；三权角色仍使用独立身份、context和沙箱。心跳只更新lease，不要求业务state_revision；失租回执仍交原effect的reconciliation inbox。

上游源码中的fencing/签名/最终完成能力不能从此表推断存在；这些都是ringharness自有设计，必须由T01—T24与AT01—AT08和真实100h验收验证。

## 8. 本轮细节审计对adapter的要求

以下均属ringharness自有实现要求，不声称上游原生提供：PLAN checkpoint允许无workspace/session；收到重连后先读Kernel步骤，不能从Harness工具历史重建第二份effect；周期Critic输出GoalReviewResource，最终审核输出global_audits数组。fake与Harness两种adapter必须对上述联合类型、Planner零工具和旧身份重传运行同一组测试。上游session记录不能替代已发布计划、审批消费或签名引用闭包。


## 9. 截图与上游能力的证据边界

用户SONIC MEA截图说明一种编排设计，不能作为DeepSeek Harness已实现这些隔离的源码依据。Manager持久记忆、新轮Executor session、独立Auditor、保护基线、唯一状态写入和三态审核均须由本项目adapter/Kernel/Broker/Ledger验证。上游可恢复session仅在C01规则满足时复用；不能因为有fork/resume能力就共享不同角色上下文。图中的sha1表述不改变本项目SHA256/JCS内容协议。


## 10. MEA验收补充

截图中的“跑完销毁推理轨迹”需要本项目会话存储生命周期适配，不能仅调用新建session就宣称旧轨迹被删除。adapter必须区分结构化结果/工具证据与临时推理，提供撤销访问、幂等清理及真实结果记录；上游是否支持物理删除需在实现时针对锁定SHA核验，不预设存在原生删除API。若无法保证本地轨迹不持久化/清理，D01不能标通过，须选择满足约束的session存储实现。

三态经Kernel持久反馈到Manager，不能直接拼接Executor会话或依靠上游内存mailbox作为唯一通道。D02—D04验证跨重启的审计汇总与消费去重。


## 11. v0.5 adapter冻结边界

adapter将ctx.llm接入受信ModelInvocation路径，PLAN模型不注册工具；不能拿ctx.tools绕过零工具规则完成云审批。上游取消事件只能成为StopReceipt观察输入，必须由宿主确认具体实例退出/隔离，不能直接等价为资源已释放。activate前固定ExecutionBinding和ContextBundle，版本失配拒绝执行。会话存储删除能力仍需W01针对固定上游SHA做能力探测；不满足即拒用该配置并选择可满足清理契约的自有存储adapter，不能降低冻结的隔离规则。


## 12. v0.5 adapter数据合同

固定Content v3与审计round/VerificationRun引用；Skill验证结果提交SkillValidationRecord业务内容，不能伪造Goal候选Audit。宿主是验证器来源边界，Harness模型输出只作待校验提案。审批始终按EFFECT/MODEL_INVOCATION区分。上述为自有接口升级，上游核验仍沿用本文件固定SHA，未重新运行上游测试。

## 13. v0.5接管与失效边界

Harness接管读取全部原验证义务和持久判定，不能只携带新attempt成功run。宿主的工具/模型调用受project trust屏障约束，Skill事件丢失不能绕过Broker准入；可信回执仍可落盘。此为自有协议要求，未重新运行上游验证。
