# Ringharness 前端驾驶舱：Hatchet UI/UX 吸收基线

> 基线日期：2026-09-13；**产品面裁定补记 2026-09-14**  
> Hatchet 固定版本：`315d43a72fd771b049b304b865a81dbab98c466c`  
> DeepSeek Harness 固定 SHA：`c291e7961a515f6d7af9304e7fd1d257929aef26`  
> 范围：吸收信息架构、交互模式和可用性原则；不复制 Hatchet 业务状态语义，不把执行完成映射成 Ringharness Goal DONE。

## 0. 三面分工（2026-09-14 用户裁定）

```text
Hatchet 风格  →  管理面（目标列表、需要我处理、执行记录筛选、系统健康）
DeepSeek Harness Web  →  执行面（对话、工具轨迹、composer、activation 实时工作）
Ringharness Control/Kernel  →  可信治理面（合同、租约、Effect、审批、屏障、DONE）
```

| 面 | 体验归属 | 权威数据 | 禁止 |
|---|---|---|---|
| **管理** | `apps/web` Hatchet 驾驶舱 IA / token | Goal/Task/Attention/屏障只读投影 | 用管理 UI 冒充执行细节或 DONE |
| **执行** | 固定 SHA 的官方 Harness Web（样式与交互以此迭代） | session / tool trail / live stream | session 成功冒充 Goal DONE；绕过 Broker 的本地写工具 |
| **治理** | Control API + Kernel 裁决；驾驶舱只读/发命令 | PG journal、EffectIntent、VerificationProfile、屏障 | 前端或 Harness 自判 DONE |

接线原则：

1. **管理壳用本仓 Agent 聊天交流**——`#run-live`（「执行交流」）投影 Kernel 权威的模型摘要与工具回执，类 Codex 跟流；**不**嵌入官方 `dsh web`，不编造私有推理。用户消息入 activation 的受控 API 未接通前 Composer 失败关闭。
2. **执行面不持业务写权**——工具路径仍经 Runner → Broker → Kernel；DeepSeek Harness 固定 pin 只作为**执行腿**（后端）。
3. **治理面不下沉到 Harness CSS**——审批、隔离 inbox、最终屏障、NO_PROGRESS 复盘留在 Ringharness 驾驶舱。
4. **官方 Web 深链可选诊断**——`HarnessWebExecutionPanel` / `serve_harness_web.sh` 可保留，**不是**产品主路径。

本文件其余章节描述**管理面**如何吸收 Hatchet；执行面视觉以 DeepSeek Harness `packages/client/ui-*` 与 `apps/web` 为准，在固定 pin 上迭代，禁止另立第二套聊天皮肤。

## 1. 产品目标

Ringharness 面向长程无人值守任务。用户打开**管理面**时，必须先回答四个问题：

1. 系统现在正在做什么？
2. 是否偏离 GoalContract？
3. 是否需要我处理？
4. 当前结果是否已满足交付条件？

Effects、Activities、Plans、ModelInvocations、Commands 等属于回答这些问题的证据，不是一级导航。新版工作台将原来的单页资源清单收敛为三层：

```text
今日总览
  ├─ 目标
  ├─ 需要我处理
  └─ 执行记录
       ├─ 概览          ← 管理（Hatchet）
       ├─ Harness 执行  ← 执行（DeepSeek Harness Web）
       ├─ 执行链路      ← 治理投影（Kernel 事实）
       ├─ 日志
       └─ 证据与验收    ← 治理（屏障 / 验收）
```

## 2. Hatchet 值得吸收的模式

### 2.1 稳定的运行列表入口

Hatchet 的 Workflow Runs 页面把状态统计、时间范围、工作流、状态、元数据筛选和运行表格放在同一上下文。Ringharness 对应页面应称为“执行记录”，按 Goal、Task、状态、时间和异常类型过滤。

吸收：

- 状态摘要与筛选同屏；
- 表格行可以进入运行详情；
- 过滤条件进入 URL，刷新和分享后仍能复现；
- 空状态给出下一步，读取失败保留错误诊断入口；
- 批量动作只在明确选择后出现。

Ringharness 约束：

- 普通用户首页不直接显示高密度表格；
- “重放”必须经过 ControlCommand、Effect Gateway、UNKNOWN 和 Stop 门禁；
- “取消请求已受理”不得显示成已停止。

### 2.2 Run 详情页保持上下文

Hatchet 把 Header、Overview、Traces、Logs 和步骤详情组织在一个 Run 页面。点击图中节点时打开侧栏，而不是跳到另一页丢失上下文。

Ringharness 对应：

| Hatchet | Ringharness | 权威边界 |
|---|---|---|
| Workflow Run | Goal 执行记录 | Goal 状态来自 Kernel |
| Task Run | Task / ActivityAttempt | Task 与尝试不得混为一个状态 |
| Run Status | 分层状态 | Workflow、Harness、Kernel、Auditor 分开显示 |
| Replay | 恢复 / 重验请求 | 只能提交命令，回读后确认 |
| Cancel | 安全暂停 / Stop | 请求、回执、资源释放分开显示 |
| Output | Candidate Artifact | 候选不是交付包 |
| Completed | 局部执行完成 | 不等于 Goal DONE |

### 2.3 DAG + 步骤 Inspector

Hatchet 的图只负责定位结构；选中步骤后，Inspector 展示状态、尝试、输入输出、日志和时间线。Ringharness 的 DAG 还必须表达：

- Task 依赖边与动态计划版本；
- 当前 execution round；
- RUNNING、VERIFYING、BLOCKED、RECOVERING、UNKNOWN；
- 选中节点的 ActivityAttempt、Effect、Receipt 和 Artifact；
- 该节点是否挡住最终屏障。

当前前端直接读取 TaskContract.depends_on 展示真实前置关系，并提供可点击的步骤 Inspector。复杂图布局、关键路径和跨计划版本关系仍等待专用 Read Model；禁止按数组顺序推断依赖关系。

### 2.4 Logs 与 Traces 分层

Hatchet 的 Logs 支持分页、查询、级别和 attempt 过滤；Traces 使用 OTel 层次和时间轴，并在运行结束后停止高频刷新。

Ringharness 对应规则：

- 日志：命令 stdout/stderr、Goal 事件和系统诊断；
- Trace：Harness activation、模型调用、工具调用、Effect、Receipt；
- 验收：VerificationRun、VerificationObligation、审计三态；
- 三类数据分别展示，不能用绿色 Trace 代替绿色验收；
- 长日志分页或虚拟化；默认只显示摘要，原始输入输出按需展开；
- 运行中优先 SSE，断线时 2–5 秒回退刷新；终态停止或降到低频；
- 不采用 300ms 全局轮询，避免多目标长程运行时放大控制面负载。

### 2.5 URL 可恢复状态

Hatchet 把标签、选中任务、Span 和查询写入 URL。Ringharness 第一阶段已经把一级页面和 Run 标签写入 hash，并兼容旧锚点：

| 旧入口 | 新入口 |
|---|---|
| `#tasks` / `#plans` | `#run-overview` |
| `#activities` / `#effects` | `#run-trace` |
| `#commands` / `#goal-events` | `#run-logs` |
| `#task-evidence` / `#goal-reviews` / `#finalization` | `#run-evidence` |
| `#quarantine-inbox` / `#approvals` | `#attention` |

后续将 `goal_id`、选中 `task_id`、时间范围、日志查询和 Inspector 开合状态加入 URLSearchParams。敏感原始输入输出不得进入 URL。

## 3. Ringharness 最终信息架构

### 今日总览

服务于负责人和首次使用者：当前目标、阶段、偏离诊断、需要处理、近期交付。没有 Goal 时显示创建/选择下一步。

### 目标

创建 Goal DRAFT、发起 START、选择观察 Goal，并查看 Task 与 Plan。表单成功只显示“命令已受理”或权威回读状态。

### 需要我处理

仅聚合必须由人判断的事项：

- UNKNOWN Effect；
- Stop 未确认；
- QUARANTINED VerificationObligation；
- 审批；
- 预算/信任阻断；
- 最终屏障要求 REVERIFY / REWORK。

每条事项必须显示原因、负责人、继续条件和动作影响。

### 执行记录

采用 Hatchet 的 Run detail 骨架：

- 概览：Goal 摘要、Task/DAG、Plan、Activity；
- 执行链路：ModelInvocation、Activity、Effect、Memory；
- 日志：Command 与 Goal Event；
- 证据与验收：Artifact、GoalReview、Finalization、Release。

### 验收与交付

直接进入执行详情的“证据与验收”标签。这里固定展示：

1. hash 完整性；
2. 来源可信度；
3. 验收正确性；
4. Auditor 三态；
5. UNKNOWN / Stop / 隔离状态；
6. FinalizationBarrier；
7. ReleaseManifest；
8. Kernel Goal 状态。

### 系统健康

分开呈现 liveness、readiness、依赖健康和业务验收。API 进程在线只能证明 liveness。

## 4. 状态与文案冻结

| 内部事实 | 用户文案 | 禁止文案 |
|---|---|---|
| 命令返回 202 | 请求已受理 | 操作完成 |
| Harness 工具返回 | 工具调用已返回 | 任务已完成 |
| Activity SUCCEEDED | 本次活动完成 | Goal 已完成 |
| Workflow COMPLETED | 编排运行结束 | 目标已交付 |
| Artifact hash 匹配 | 内容完整性通过 | 证据有效 |
| Auditor PASS | 本项验收通过 | 系统已 DONE |
| Auditor INSUFFICIENT | 证据不足 | 失败 / 通过 |
| Auditor FAIL | 验收失败 | 系统错误 |
| Kernel DONE + Release | 已交付 | 仅写“执行成功” |

## 5. 组件边界

```text
AppShell
├─ SideNavigation
├─ PageHeader
├─ OverviewPage
├─ GoalsPage
├─ AttentionPage
├─ RunPage
│  ├─ RunHeader
│  ├─ RunTabs
│  ├─ GoalPicker
│  └─ ExistingObservePanels
└─ HealthPage
```

既有 ObservePanel 保留真实 API 接线。页面层只负责组合与渐进披露，不在多个组件重复取数后推导新业务事实。后续聚合数据应由 Read Model API 提供，避免浏览器拼装权威状态。

## 6. 后续 API 缺口

按优先级补充只读 Read Model，不改变 Kernel 写权威：

### P0：Run 驾驶舱可用

- `GET /api/v1/goals/{goal_id}/workbench-summary`
  - Goal 状态、block_reason、阶段、偏离诊断、预算、Task 计数、UNKNOWN、待处理数、屏障；
- `GET /api/v1/goals/{goal_id}/graph`
  - 在现有 TaskContract.depends_on 之上补充关键路径、跨版本关系和聚合当前节点；
- `GET /api/v1/goals/{goal_id}/attention-items`
  - 类型、原因、负责人、继续条件、允许动作。

### 已落地：Hatchet 核心交互

- 执行记录状态统计、搜索、状态筛选和可点击行；
- Light/Dark 色彩 token、侧栏收起、悬停/按下/选中反馈；
- Run 四标签和旧深链兼容；
- TaskContract.depends_on 关系卡片与步骤 Inspector；
- 所有工程术语下沉到“技术详情”。

### P1：执行定位

- Trace 聚合读取：activation → model turn → tool call → effect → receipt；
- 日志游标分页、级别/attempt/时间过滤；
- 步骤 Inspector 聚合读取；
- URL 可恢复选择状态。

### P2：长程效率

- 保存的过滤器；
- 列表列可见性；
- 键盘快捷键；
- 多 Goal 对比；
- 大日志虚拟化；
- 用户角色化首页组件。

## 7. 前端刷新策略

| 数据 | 活跃时 | 终态/不可见时 |
|---|---:|---:|
| Goal 事件 | SSE | 重连时游标续传 |
| Workbench summary | 2–5 秒回退 | 30 秒或停止 |
| Task/Activity | 2–5 秒 | 30 秒 |
| Command log | 流式/游标 | 用户手动刷新 |
| Trace | 5 秒 | 终态后短暂宽限再停止 |
| 证据/屏障 | 状态变化时失效重取 | 不轮询 |

所有定时器必须随页面可见性、Goal 选择和组件卸载而停用。

## 8. 可访问性与响应式验收

- 侧栏、主导航、Run 标签均有语义名称和 `aria-current`；
- 键盘可进入全部入口，焦点环清晰；
- 960px 下侧栏收窄为图标；680px 下改为顶部横向导航；
- 状态不只依赖颜色，必须同时显示文本；
- Inspector 打开后聚焦标题，Esc 关闭并恢复原节点焦点；
- 日志自动追加不得抢走用户滚动位置；
- `prefers-reduced-motion` 下禁用非必要动画。

## 9. 验收场景

1. 未登录：总览可读，写入口提示 OIDC，不能暴露 Service Key。
2. 无 Goal：目标页给出创建步骤；空列表不显示 DONE。
3. RUNNING Goal：Run 标签可切换，旧深链仍可进入对应新页面。
4. UNKNOWN Effect：需要我处理显示阻断，后续写动作关闭。
5. Stop 请求已受理但无回执：显示“等待停止确认”，不能显示“已停止”。
6. Artifact hash 匹配但 Auditor INSUFFICIENT：显示完整性通过、证据不足、屏障未释放。
7. Workflow COMPLETED 但 Kernel RUNNING/BLOCKED：页面以 Kernel 状态为 Goal 主状态。
8. Kernel DONE 且 ReleaseManifest 有效：才显示“已交付”。
9. API 断线：保留最后更新时间并标为陈旧，不把空响应解释为零。
10. 移动端：一级任务可达，执行详情标签可横向滚动，不截断关键状态。

## 10. 来源

- Hatchet 仓库：https://github.com/hatchet-dev/hatchet
- Run 页面：https://github.com/hatchet-dev/hatchet/blob/315d43a72fd771b049b304b865a81dbab98c466c/frontend/app/src/pages/main/v1/workflow-runs-v1/%24run/index.tsx
- Run Header：https://github.com/hatchet-dev/hatchet/blob/315d43a72fd771b049b304b865a81dbab98c466c/frontend/app/src/pages/main/v1/workflow-runs-v1/%24run/v2components/header.tsx
- Runs 页面：https://github.com/hatchet-dev/hatchet/blob/315d43a72fd771b049b304b865a81dbab98c466c/frontend/app/src/pages/main/v1/workflow-runs-v1/index.tsx
- URL 状态：https://github.com/hatchet-dev/hatchet/blob/315d43a72fd771b049b304b865a81dbab98c466c/frontend/app/src/pages/main/v1/workflow-runs-v1/hooks/use-run-detail-search.tsx
- Logs：https://github.com/hatchet-dev/hatchet/blob/315d43a72fd771b049b304b865a81dbab98c466c/frontend/app/src/pages/main/v1/workflow-runs-v1/%24run/v2components/workflow-run-logs.tsx
- Observability：https://github.com/hatchet-dev/hatchet/blob/315d43a72fd771b049b304b865a81dbab98c466c/frontend/app/src/pages/main/v1/workflow-runs-v1/%24run/v2components/step-run-detail/observability/observability.tsx

## 11. RuoYi Vue3 后台交互补充基线

> 固定版本：`d50306c98e1ca7cb4a49e35514662bf1b24689eb`（TypeScript 分支，2026-09-13 核验）

RuoYi 的价值主要在普通用户已经熟悉的企业后台操作习惯。本项目吸收以下交互：

- 固定侧栏、面包屑式页头和右侧常用工具；
- 搜索、刷新、显示/隐藏列集中在表格工具栏；
- 表格多选后才出现批量操作提示；
- 分页始终同时展示当前范围与总数；
- 显示列、主题和侧栏状态在浏览器本地保存；
- 危险动作必须明确解释结果，命令受理与执行完成分开；
- 移动端收起侧栏和非关键表格列。

没有引入 Vue、Element Plus、Pinia、Spring Security 或 RuoYi 的用户/部门/岗位等业务模块。Ringharness 继续使用 React、生成的 API Client、OIDC/JWT 和 Kernel 权威状态。

实现新增：SVG 图标体系、`⌘/Ctrl + K` 快速前往、可恢复列显示、表格选择与分页、按真实依赖分层的任务图、任务详情内部标签和键盘关闭。

来源：

- 仓库：https://gitcode.com/yangzongzhuan/RuoYi-Vue3/tree/typescript
- Navbar：`src/layout/components/Navbar.vue`
- HeaderSearch：`src/components/HeaderSearch/index.vue`
- RightToolbar：`src/components/RightToolbar/index.vue`
- Pagination：`src/components/Pagination/index.vue`
- 定时任务表格：`src/views/monitor/job/index.vue`
