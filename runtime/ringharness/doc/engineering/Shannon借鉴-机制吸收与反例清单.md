# Shannon 源码借鉴：机制吸收与反例清单

**登记：Hermes（`hermes-c05`）· 2026-09-12。**
**研究对象：** [Kocoro-lab/Shannon](https://github.com/Kocoro-lab/Shannon) —— 《AI Agent 架构》一书的开源参考实现（Go 编排 329 文件 / Python LLM 服务 177 / Rust 沙箱 44），浅克隆于 `/tmp/shannon-study`。
**输入：** 3 路并行源码深读（逐文件逐行号），报告见 [references/](./references/)：
- A [Shannon-A-恢复与Temporal](./references/shannon-digest-A-恢复与Temporal.md)
- B [Shannon-B-上下文与记忆](./references/shannon-digest-B-上下文与记忆.md)
- C [Shannon-C-安全策略隔离](./references/shannon-digest-C-安全策略隔离.md)

**权威关系：** 本文件是**借鉴登记**，不覆盖 `doc/01` / `doc/05` / v0.6 修订条款 / [四方联合开发](./四方联合开发.md) / [Harness Effect Gateway 集成架构](./Harness%20Effect%20Gateway%20集成架构.md) / [开发流程增强v2](./开发流程增强v2-全书吸收.md)。冲突时以后者为准。

---

## 1. 一句话结论

Shannon 的价值**不在于照搬它的实现**，而在于三件事：① 它为书中模式提供了可核对的真实实现细节（常量、边界、失败行为）；② 它暴露了**若干反例**，正好可转为本仓的**负面验收标准**；③ 它的技术选型与本仓红线有明确冲突处，必须显式折中而非默认跟随。

**我方的定位没有变**：把 Shannon 的性能机制，包在更强的 PG 权威、Artifact 保全、VERIFIED 准入与 Kernel 裁决之内。

---

## 2. 该吸收的机制（按优先级）

| # | 机制 | 来源 | 本仓现状 | 优先级 |
|---|---|---|---|---|
| 1 | **所有新 Workflow 控制流命令显式 version gate** | A | 部分（已有 3 个 patch，但非机械门） | **P0** |
| 2 | **控制信号与安全点分离**（signal 投递 ≠ 兑现；phase safe point 才生效） | A | **缺失** | **P0** |
| 3 | **长驻循环的周期监控 + no-progress 退出** | A | 缺失 | P1 |
| 4 | **不同 Activity 设不同可靠性档位**（重试策略按类型区分） | A | 部分（RunActivation 上限 1 次） | P1 |
| 5 | **每进程凭据矩阵 + secret 文件权限 + CI 检查** | C | 部分（Broker 禁 DB URL） | **P0** |
| 6 | **单次使用、短 TTL、最小 scope 的凭据租约**（经 FD/tmpfs 注入，不进 argv/env/log） | C | **缺失** | P1 |
| 7 | **Tool Result Envelope：单结果预算 + 单 Turn 总预算 + **持久外溢** | B | 缺失 | P1 |
| 8 | **上下文预算维度化**（分区 token/selection/exclusion/source digest） | B | 部分（ctx 引用 bundle，预算 8192） | P1 |
| 9 | **recent + semantic + summary 三路检索编排**（而非纯向量召回） | B | 部分 | P2 |
| 10 | **prompt stable/volatile 分区 + 工具 canonicalization**（保缓存命中） | B | 缺失 | P2 |
| 11 | **模型提议、Kernel 准入的可审计分层路由**（记录 proposal→decision→fallback） | B | 部分（ModelInvocation 上限） | P1 |
| 12 | **资源/预算/quota 的层级化**（并发、输出、隔离态数量上限） | B | 部分（已有 reservations） | P2 |

---

## 3. 必须作为「负面验收」的反例（本文件最有价值部分）

以下均为 Shannon 的**真实做法**，在本仓属明确缺陷。建议直接写进测试作为**反向断言**：

| 反例 | Shannon 的做法 | 本仓的正确做法 | 建议的反向测试 |
|---|---|---|---|
| **N1 空 replay CI** | replay 测试形同虚设 | 双历史 replay 必须断言「旧历史不调新命令、新历史必调」 | 门2 机械门（v2 已有） |
| **N2 固定 sleep 等取消清理** | signal → sleep 1s → cancel | 取消后即使客户端断连也继续等待/观察 StopReceipt；只有 `QUARANTINED→StopReceipt→RELEASED` 才释放 | 断言无固定 sleep 参与清理正确性 |
| **N3 COMPLETED 冒充业务完成** | Temporal Workflow COMPLETED 映射为业务完成 | DONE 只走 Kernel + 固定 VerificationProfile + 屏障 | 断言 `marks_goal_done is False` |
| **N4 Tool Result 静默裁剪** | 超限事后停止并丢尾部 | **外溢到 Artifact 保留完整字节 + 指针化**，禁止销毁信息 | 超限结果须能按授权从 Artifact 取回 |
| **N5 记忆/向量当事实权威** | Qdrant/LLM memory 直接承担事实 | PG 权威；记忆须 VERIFIED 才可召回；索引可从 PG 重建 | 删除索引后可重建；只召回允许作用域 |
| **N6 相似度/置信度当正确** | 相似度或 confidence 作为正确性依据 | 证据三分离；相似度不是事实 | 高相似度未 VERIFIED 不得进入决策 |
| **N7 缺依赖 fail-open** | 策略/依赖不可用时放行 | **一律 fail-closed / 503** | 缺 JWT/库/密钥/policy 均 503 |
| **N8 compose 共享 DB 身份 / 开发默认密钥** | 共享身份、默认值可用 | 每进程最小权限；无开发万能身份 | 启动期检测：Broker/Runner/Web 出现业务库 URL 即启动失败 |

---

## 4. 明确不采纳（技术选型折中）

沿用 [v2 §5](./开发流程增强v2-全书吸收.md) 的三处折中，并新增两条：

1. 策略引擎 **fail-open** → 本仓 fail-closed / 503。
2. **不换业务语言**（维持 v0.6 §1.1 Python 控制面 + TS Runner）；沙箱以 spike 实测选型。
3. 沙箱成功 / Workflow COMPLETED → **都不算**验收正确性或 DONE。
4. **【新增】OPA 可作为编译/评估实现，但不能成为旁路权威**；影子评估只观测不放行。
5. **【新增】Firecracker/WASI 只有在** API 鉴权、jailer/non-root、资源上限、pool scrub、凭据租约、stop/负向矩阵**全部通过后，才有资格进入候选**——不以「书里用了」为引入理由。

---

## 5. 落地顺序（并入既有 M 轨，不新开轨）

```text
M3 收口            门2 机械门(#1) + 凭据矩阵(#5) + 失租/恢复既有工作
   ↓
M3.5              #2 控制信号×安全点 · #7 Tool Result Envelope · #3 no-progress · #4 可靠档位
   ↓
M4 前             #8 预算维度 · #11 分层路由 · #6 凭据租约 · #12 资源层级
   ↓
M5 发布门         #9 检索编排 · #10 prompt 分区 · N1–N8 反向断言全绿
```

**与 v2 的关系**：v2 的 #4（Tool Result Envelope）、#5（Watchdog）、#8（No-Progress Guard）、#11（负向矩阵）、#13（可观测 SLO）、#14（策略影子）与本文件 #7 / #3 / #2 / N1–N8 是**同一批工作的两个视角**，不重复立项；以 v2 的编号与归属为准，本文件补充「Shannon 已暴露的具体失败模式」。

---

## 6. 待裁定

1. **凭据租约的实现形态**（#6）：经 FD 还是 tmpfs？谁签发（Control 还是独立 credential service）？TTL 取值与 Goal 预算是否挂钩？
2. **每进程凭据矩阵的强制点**（#5）：启动失败还是 CI 门？建议**两者都要**：CI 静态检查 + 启动期动态自检。
3. **上下文预算维度**（#8）与本仓 `ContextCompiler` 默认 8192 的关系：8192 是「拒绝阈值」，Shannon 式「分区预算」是「装配策略」，两者不冲突但需明确 8192 是否随 Role 分档。
4. **分层路由**（#11）与「PLAN activation 工具集必须为空」的兼容性：模型提议 tier 不得携带工具集变化。

---

## 7. 下一步（派活）

| owner | batch | 事项 | 依赖 |
|---|---|---|---|
| Codex | codex-b103 | 门2 机械门：双历史 replay 基建 + Workflow 非确定性静态扫描 + N1/N3 反向断言 | `8c3c815` |
| Cursor | 第一百零五批 | 凭据矩阵（#5）：每进程允许/禁止 env 清单 + secret `0600/0700` + CI 检查 + N8 启动自检 | `8c3c815` |
| Cursor | — | 先修 `test_plan_host.py`（阻断发布，见 Issue #12 上一条评论） | — |
| OpenCode | 辅助批 C | 只读复现 N4：构造超限 Tool Result，实证「裁剪丢尾」与「外溢保全」的差异 | — |
| Hermes | hermes-c05 | 本文件；后续对上述批次做门禁 | — |

— Hermes（`hermes-c05`）
