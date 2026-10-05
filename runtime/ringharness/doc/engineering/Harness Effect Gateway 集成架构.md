# Harness Effect Gateway 集成架构

**登记：Hermes（`hermes-c03`）· 2026-09-12。设计输入由用户在第九十七批期间给出，本文件吸收并钉住集成契约。**
权威关系：本文件是**集成架构登记**，不覆盖 `doc/01` / `doc/05` 与 v0.6 修订条款；冲突时以后者为准。字段与路由以 `doc/05` 为唯一权威。

## 1. 架构（用户给定，原文转写）

```text
DeepSeek Harness Agent Loop
            │
            ▼
       Harness ToolRuntime
            │
      注册 Broker-backed tools
            │
            ▼
 Ringharness Effect Gateway
    ├─ create Step
    ├─ prepare effect
    ├─ dispatch
    ├─ 等待/查询可信 receipt
    └─ 转换为 ToolResult
            │
            ▼
  返回 Harness Agent Loop
            │
            ▼
       Qwen 继续下一轮
```

## 2. 这条架构决定了什么（与现状对齐）

**关键翻转：工具回合的循环由 Harness 持有，不由本仓 Control 持有。**

此前实现路径是「Kernel/Runner 主动调模型 → 试图在控制面侧处理 tool_calls」，因而卡在「缺 Control tool_calls 传输」——控制面的 `dispatch` 只发短提示、请求体不带 tools（`probe.py:273-288`、`local_qwen.py:46-52` 已实证）。

按本架构，**不需要**给 Control 的 dispatch 加 tool-call 传输：

| 层 | 归属 | 本仓是否实现 |
|---|---|---|
| Agent Loop（多轮推理、何时调工具） | DeepSeek Harness | 否（上游） |
| ToolRuntime（注册工具、把 tool-call 变成调用） | DeepSeek Harness | 否（上游） |
| 工具的**副作用咽喉**（Step/Effect/dispatch/receipt） | Ringharness | **是** |
| 业务裁决（DONE、预算、审批、验收） | Kernel / 业务 PG | **是** |

对应到已落地代码：

| 架构环节 | 现有实现 |
|---|---|
| create Step → prepare → dispatch | `executeToolHost.ts`（`createExecuteToolHost`）、`brokerToolHttpPorts.ts` |
| 等待/查询可信 receipt | `effectObserveHttpPorts.ts`（`pollEffectStatus`，超时可仍 DISPATCHED，**不**自动写 SUCCEEDED） |
| tool-call 接收 | `cordisExecuteBridge.ts`（`forwardExecuteToolCallsFromChunks`） |
| 工具输入的 content-addressed 工件 | `artifactPutHttpPorts.ts`（`PUT /internal/v1/artifacts/{digest}/content`） |
| Broker-backed 工具注册 | 在途（`brokerBackedHarnessTool.ts`，Cursor 第九十七批后） |

## 3. 不可越的红线（Hermes 验收口径）

本架构把「谁跑循环」交出去了，因此必须把**副作用咽喉与业务裁决**钉死。任何一方向 Harness 侧让步下列任一条，本文件即视为被违反：

1. **Harness 是执行闭环，不是调度权威。** 它不得决定 Task/Goal 状态、不得写 DONE。DONE 仍只走 Kernel + 固定 VerificationProfile + 屏障（`doc/01`）。
2. **一切工具副作用必须经 Effect Gateway。** 禁止 Harness 侧绕过 Kernel 的 effect 准入直连宿主（文件系统、网络、上游 API）。工具要么是 Broker-backed，要么不被注册。
3. **ToolResult 不等于业务成功。** 模型拿到 `{"ok":true}` 只代表工具动作被受理/派发；`dispatch` 成功、`SUCCEEDED` 字样、Agent Loop 正常结束，均**不得**写成 Goal/Task DONE，也不得写成 effect SUCCEEDED（须有可信 receipt）。
4. **receipt 必须可信且幂等。** 同一逻辑动作跨重试/跨 Worker 复用同一 `effect_id`；UNKNOWN 先对账，禁止为「无人值守」重复副作用。长时间未见终态时应回报「仍在途」，不得自动判成功。
5. **身份与凭据隔离。** Harness 进程不得持有业务库写凭据；工具调用须带可验证的 activity/attempt/fencing 身份，工具路径由 Kernel/Broker 再鉴权，**不信任请求自报身份**。
6. **会话/工具集/工作区隔离。** 同一模型可服务多角色，但 activation 身份、会话、工具集、工作区必须隔离（`doc/01`）。
7. **上游版本钉扎。** Harness checkout / 镜像一律固定 SHA 或 digest，禁止 `latest`；接入前按 `doc/09` / v0.6-09 核验。
8. **模型正文与形状不由控制面假定。** 模型可能返回 tool_calls 或散文；Harness 侧负责解析，本仓不因「拿到非预期形状」而伪造成功。

## 4. 对现有批次判定的影响

- **M3 的「缺 Control tool_calls 传输」不再是阻塞项。** 该缺口在本架构下不存在：控制面不承担工具回合。已落地的 `cordisExecuteBridge` / `executeToolHost` / `effectObserveHttpPorts` / `artifactPutHttpPorts` 恰好是本架构中「Effect Gateway」一侧的实现，方向正确。
- **Runner 的 `RunActivation` 定位需收敛。** 它仍可作为「宿主侧一次 activation 的执行与观察」入口，但**不应**被理解为「本仓负责跑完整工具多轮循环」。若后续批次试图在 `runActivation` 内部自建完整 agent loop 以达到 E2E 绿，属越界，Hermes 会退回。
- **live/compose EXECUTE E2E 的形态变了。** 真实验收应验证「Harness 的 tool runtime 经 Effect Gateway 产生 Step/Effect 并拿到可信 receipt」，而非「本仓 Control 侧发 tools 给模型」。请据此写 M3 的 E2E 用例。
- **与 Issue #7 同源**：`dsh web` 就是本架构里的 Harness Agent Loop，它在 8001 上装配 16 万级 prompt 属其自身行为，与本仓无关（见 #7 最终归因）。

## 5. 待办（按优先级）

1. **Cursor**：完成 `brokerBackedHarnessTool`（ToolRuntime 注册面），并在账本写明其与 `executeToolHost` / `effectObserveHttpPorts` 的边界。
2. **Cursor**：按 §4 改写 M3 EXECUTE E2E 用例形态（Harness 侧驱动 → Gateway 出 Step/Effect → 可信 receipt）。
3. **Hermes**：对上述两项做门禁（重点 §3.1–§3.4：假 DONE、绕准入、receipt 可信与幂等）。
4. **Codex**：审计本架构下的重复副作用路径（重试 / CAN / 失租后重领是否复用同一 `effect_id`）。
5. 文档侧：若确认要正式纳入设计，须同步 `doc/v0.6/01`（编排职责）与 `doc/v0.6/09`（Harness 上游核验），由 Cursor 在收口批次并入；本文件在此之前仅为集成登记。

## 6. 未决问题（需裁定后回填）

1. Harness 与本仓之间是 HTTP 拉取（本仓提供 Effect Gateway 端点）还是上游回调注册？只读诊断显示当前是 Runner 侧主动经 Control 内部路由，需与上游确认是否引入反向通道。
2. 工具白名单由谁持有：Kernel 的 Skill/工具集，还是 Harness 的 ToolRuntime？按三权分立应在 Kernel 侧裁决，Harness 仅执行注册结果 —— 待确认。
3. 「等待/查询可信 receipt」的等待形态：Harness 侧阻塞轮询还是本仓异步回调？超时语义须与 `RunActivation` 的 `start_to_close` 一并定。
