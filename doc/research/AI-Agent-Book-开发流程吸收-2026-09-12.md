# AI Agent Book 开发流程吸收（2026-09-12）

> 性质：研究与流程改进提案，不覆盖 `doc/01`、`doc/05` 或 v0.6 修订规格。若本文与冻结不变量冲突，以冻结规格为准。
>
> 协作登记：GitHub Issue #12，`owner=Codex`，`batch=codex-b100-agent-book-process`，仅新增本文。

## 1. 吸收结论

这本书与 Ringharness 的总体方向一致：生产级 Agent 不是一个无限循环的模型进程，而是一个可恢复、可裁决、可审计的复合系统。对本项目最重要的表达是：

```text
确定性外壳（Temporal + Kernel）
        ↓
自适应内核（DeepSeek Harness AgentLoop）
        ↓
受控副作用（Effect Gateway + Broker）
        ↓
确定性退出（Auditor + VerificationProfile + Finalization）
```

这不要求替换现有架构，而是要把以下能力收进每批开发的可验收门禁：

1. 按阶段边界持久化，不用进程存活假装连续性。
2. 恢复必须经过开关、过期时间和尝试上限三道门。
3. Watchdog 按等待阶段和推进责任方分开，不用一只整轮计时器。
4. 卡循环检测是多信号证据聚合，且必须从提醒升级为强制停止。
5. 工具并发安全是“本次调用”的属性，不是工具名的静态属性。
6. `accepted`、`committed`、`completed`、`delivered` 分开，任何一个时点都不能冒充 Goal DONE。
7. “模型说完成”不是退出条件；只能使用可重现的验收后置条件。

## 2. 来源与证据边界

### 2.1 书中明确陈述的模式

| 主题 | 书中模式 | 来源 |
|---|---|---|
| 学习方法 | 模式优先、框架其次；从一个带工具的最小循环起步 | [前言](https://waylandz.com/ai-agent-book/%E5%89%8D%E8%A8%80/) |
| DAG 与 Loop | 事前可知控制流用图；依赖观察的控制流用循环；生产上采用混合形态 | [第 34 章](https://waylandz.com/ai-agent-book/%E7%AC%AC34%E7%AB%A0-%E4%BB%8EDAG%E5%88%B0Agent-Loop/) |
| 持久循环 | Checkpoint 在阶段边界防抖落盘；恢复有 enable/expiry/max-attempt 三道门；尝试次数在模型调用前落盘 | [第 40 章](https://waylandz.com/ai-agent-book/%E7%AC%AC40%E7%AB%A0-%E6%8C%81%E4%B9%85%E5%8C%96Agent-Loop/) |
| 运行中输入 | 分离 accepted/committed/completed；在迭代边界注入新消息；原子关闭运行尾部竞态 | [第 41 章](https://waylandz.com/ai-agent-book/%E7%AC%AC41%E7%AB%A0-%E8%BF%90%E8%A1%8C%E4%B8%AD%E6%93%8D%E6%8E%A7Agent/) |
| 超时 | 显式 TurnPhase；LLM 流、工具、审批、压缩各有自己的计时和推进责任方 | [第 42 章](https://waylandz.com/ai-agent-book/%E7%AC%AC42%E7%AB%A0-Agent%E8%B6%85%E6%97%B6%E4%B8%8EWatchdog/) |
| 卡循环 | 聚合多种重复/无进展信号，输出 Continue/Nudge/ForceStop；首次危险调用仍靠工具契约拦截 | [第 43 章](https://waylandz.com/ai-agent-book/%E7%AC%AC43%E7%AB%A0-%E5%8D%A1%E5%BE%AA%E7%8E%AF%E6%A3%80%E6%B5%8B/) |
| 并发工具 | 按本次调用的并发安全性分批；未知分类失败关闭；结果按 `tool_use_id` 配对 | [第 44 章](https://waylandz.com/ai-agent-book/%E7%AC%AC44%E7%AB%A0-%E5%B9%B6%E8%A1%8C%E5%B7%A5%E5%85%B7%E6%89%A7%E8%A1%8C/) |
| Temporal | Workflow 保持确定性，副作用进 Activity；长 Activity 用 heartbeat；变更需要版本策略 | [第 21 章](https://waylandz.com/ai-agent-book/%E7%AC%AC21%E7%AB%A0-Temporal%E5%B7%A5%E4%BD%9C%E6%B5%81/) |

### 2.2 一手资料交叉核验

- Temporal 官方文档要求 Workflow 以相同输入生成相同命令序列，并将 LLM、HTTP、DB 等非确定操作放在 Activity：[Workflow Definition](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/workflow/workflow-definition.mdx)。
- Temporal 官方文档说明 Activity heartbeat 同时承担活性证明与恢复进度载荷，但它不替代外部副作用幂等：[Detecting Activity Failures](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/detecting-activity-failures.mdx)。
- 本仓冻结规格已把 effect/receipt、UNKNOWN 对账、资源隔离和 DONE 屏障定义为独立不变量；本文不使用书中样例覆盖这些更严格的本地约束。

### 2.3 Ringharness 工程推论

下文对 M0–M5、Kernel/Broker/Harness 和四方协作的映射是本项目的工程推论，不是原书规格。未经冻结文档修订与测试落地前，不得宣称相应能力已实现。

## 3. 对 M0–M5 的流程增强

| 阶段 | 新增开发门禁 | 必须产生的证据 |
|---|---|---|
| M0 契约 | 每个切片先画出确定性外壳/自适应内核/副作用咽喉/确定性退出；说明哪个层拥有每个状态 | 不变量表、状态所有者、错误分类、幂等键、非目标 |
| M1 编排 | 事前确定的步骤留在 Temporal/DAG；需要观察才能决定下一步的行为留在 Harness Loop | replay test、版本门、中断点矩阵，且 Workflow history 无 prompt/token/secret |
| M2 模型循环 | 先证明最小真实闭环：模型发 tool call → ToolResult 回到下一轮模型；PLAN 工具集仍为空 | 固定 Harness SHA、真实 Qwen 轨迹、两轮模型证据、PLAN 零 Step/Effect |
| M3 执行/恢复 | Checkpoint 按阶段防抖；恢复经 enable/expiry/max-attempt；尝试在模型调用前记账；恢复一律 unattended | prepare/dispatch/receipt 各 crash point；恢复后仍为同一 `logical_step_id/effect_id`；过期恢复被拒绝 |
| M3 并发 | 根据调用参数、workspace、资源、命令形状进行调用级分类；未知一律串行或拒绝 | 冲突调用不并发；异主体/过期 fence 拒绝；结果绑定 `tool_call_id` |
| M4 可观测/操控 | 显式 TurnPhase；LLM stream、tool、approval、compaction 独立 Watchdog；运行中输入只在迭代边界原子接收 | typed timeout reason、部分输出、责任方、accepted/committed/completed/delivered 时间线 |
| M4 收敛 | 卡循环聚合重复调用、连续失败、无新证据、预算消耗等信号；Nudge 必须有升级上限 | Continue/Nudge/ForceStop 决策轨迹；ForceStop 不跳过 StopReceipt/UNKNOWN 对账 |
| M5 验收 | 100h 不只统计“活着”，还要覆盖过期 checkpoint、迟到输入、流间隙卡死、连续 Nudge、并发误分类 | 固定 seed 的故障注入报告、UNKNOWN 数量、重复副作用=0、未证实完成=0 |

## 4. 统一开发批次流程

后续批次建议在现有“认领→实现→审查→验收→合并”上增加可验证产物：

```text
Hermes 立项
  ↓
写明 pattern / invariant / owner / non-goal
  ↓
Issue 认领路径与固定 depends_on SHA
  ↓
先写 RED（至少一个 happy path + 一个 crash/race/UNKNOWN path）
  ↓
最小实现
  ↓
窄测 + replay/crash/fencing 证据
  ↓
Codex 固定 SHA 审计（假 DONE / 重复副作用 / 恢复）
  ↓
Hermes 独立验收
  ↓
Cursor 合并
  ↓
进度账本记录 observed / verified / pending
```

### 4.1 新增的 Issue/PR 必填字段

现有 `owner/batch/owned_paths/depends_on/verification_owner/status/PR/merged_by` 保留，建议追加：

| 字段 | 内容 |
|---|---|
| `pattern` | 本批采用 DAG、Agent Loop、Handoff、Reflection 或固定流程的原因 |
| `invariants` | 不得被实现绕过的状态/安全约束 |
| `non_goals` | 本批明确不宣称完成的能力 |
| `crash_points` | 实测的中断点：调用前、prepare 后、外部成功回执前等 |
| `authority` | Workflow / Harness / Kernel / Broker / Auditor 中谁拥有该状态 |
| `evidence` | 测试、receipt、artifact、history、日志或 UI 实测的精确位置 |
| `residual_risk` | 未测外部依赖、真实设备/模型/长时间运行边界 |

### 4.2 证据用词限制

| 词 | 只能在何时使用 |
|---|---|
| `observed` | 某个日志/API/状态已观察，但尚未证明整体正确 |
| `verified` | 指定场景按固定输入与可复现方式通过 |
| `accepted` | 命令/消息已被可靠持久层受理 |
| `completed` | 局部执行单元完成，不代表 Task/Goal DONE |
| `delivered` | 结果成功交付给指定接收者，必须有 ack |
| `done` | 只能是 Kernel 在固定 VerificationProfile 与最终屏障上的业务裁决 |

## 5. 四方吸收任务

| 参与方 | 需吸收的内容 | 建议下一个产物 |
|---|---|---|
| Cursor | AgentLoop/ToolRuntime 真实闭环、调用级并发、`tool_call_id` 归属、abort signal | M3 中先做单工具两轮真实 Harness E2E，再开并发 |
| OpenCode | 本地 Qwen 真实流、流间隙、无进展信号和上下文增长 | 只读 probe：输出 TurnPhase、chunk gap、token 增长和重复 tool-call 指纹 |
| Codex | Temporal replay、crash point、恢复期限、副作用幂等、最终屏障 | 固定 SHA 只读审计；获得路径认领后再补 temporal RED |
| Hermes | 将模式变成项目门禁，防止文档或局部绿灯冒充实现 | 裁定本文哪些字段/门禁并入 AGENTS、v0.6 或进度账本 |

## 6. 当前项目立即适用的门禁

对当前 Harness Effect Gateway 线，不等待新协议也可以立即执行：

1. 上游官方 AgentLoop 未真实调用前，不宣称“Harness 接入完成”。
2. ToolResult 没有回到下一轮模型前，不宣称“多轮工具循环完成”。
3. 独立 Runner/Broker 身份下未完成 PREPARED → DISPATCHED → SUCCEEDED + evidence 前，不宣称“受控工具 live 通路完成”。
4. prepare/receipt 窗口 crash 后无法定位原 `logical_step_id/effect_id` 时，不开启自动恢复副作用。
5. `UNKNOWN`、缺 evidence、未确认 Stop 或隔离资源存在时，不开启后续工具准入或最终屏障。
6. 未完成调用级并发分类前，保持同 activation/workspace 工具串行。
7. 并发实现不能先于串行闭环、恢复不重复副作用和 typed timeout 的证据。

## 7. 建议验收场景

| ID | 场景 | 通过条件 |
|---|---|---|
| AB01 | 真实 Qwen 产生一次 `read_file` | Broker 执行后 ToolResult 按 `tool_call_id` 返回，模型第二轮引用实际结果 |
| AB02 | prepare 后 Runner 被杀 | 恢复先找原 Step/effect，零额外外部动作 |
| AB03 | 外部成功但 receipt 丢失 | 进入 UNKNOWN，对账前不重试、不继续工具、不 DONE |
| AB04 | checkpoint 已过期 | 旧意图不自动恢复，产生可观测的 abandoned/block 结果 |
| AB05 | LLM 流中断但工具仍在进展 | 流间隙 Watchdog 只影响 LLM 所有阶段，不误杀正常工具 |
| AB06 | 相同失败调用连续发生 | 先 Nudge，到阈值后 ForceStop；计入预算，不无限提醒 |
| AB07 | 两个读调用命中同一可变资源 | 不因“只读”标签自动并发，保持资源序列 |
| AB08 | 运行尾部到达新用户消息 | 消息要么被当前 Turn 原子接管，要么生成新 Turn，不丢失也不双跑 |
| AB09 | effect 对账成功但 Stop 未 CONFIRMED | 原工程结果可记录，但新写入准入和最终屏障仍关闭 |

## 8. 采用边界

本文可作为当前切片的 review checklist，但正式改变以下内容时仍须按项目规则完成单向同步和验证：

- 增加 API/字段/状态：先修订 `doc/contracts` 与 `doc/05`，再迁移、代码、OpenAPI 和 TS 客户端。
- 改变 Temporal Workflow 命令序列：先设计 patch/version 门和旧 history replay 测试。
- 增加工具并发：先冻结调用级分类合同和结果归属，再改 Runner/Broker。
- 增加自动恢复：先有 expiry/max-attempt/unattended 政策与过期反例。
- 新增“完成”类状态：必须证明它不会绕过 Kernel 的 DONE 裁决。
