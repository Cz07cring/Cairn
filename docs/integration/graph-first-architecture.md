# Origin → Fact/Intent → Goal：融合后的产品边界

日期：2026-10-05（Asia/Shanghai）。这是用户对融合方向的修正。当前代码只实现
部分接线，以下“应当”是待实现的产品约束，不是运行事实。

## 结论

Cairn 项目创建时就有两个端点：`origin` 是已知起点，`goal` 是要达到的终点。
逐步生长的是两者之间的 Fact/Intent 因果路径。Ringharness 加入后，这张图
仍是用户理解问题、提出方向、观察新事实的主界面。Ring 的 GoalContract
承载同一个终点的执行与验收约束，但不预先规定整条探索路径。

```mermaid
flowchart LR
  O[Origin 已知起点] --> F[Fact 当前已知]
  F --> I[Intent 下一步提议]
  I --> P[封存本轮图与候选]
  P --> K[Ring Kernel 准入与计划]
  K --> E[分权 Runner / Broker 执行]
  E --> A[独立 Evidence / Auditor]
  A -->|可信新观察| F
  F -->|满足终点并过最终屏障| G[Goal]
```

## 每轮权限

| 对象 | 产生与作用 | 不具备的权限 |
|---|---|---|
| Cairn Origin/Goal | 创建项目时固定起点和终点；后续图围绕两端生长 | 不代表 Ring Goal 已启动或已 DONE |
| Fact | 用户提供或从 Ring 证据投影；需标注来源与信任状态 | 描述文字不能冒充独立验收证据 |
| Intent | 人或零工具 Reason 根据当前图提出下一步方向 | 不能直接变成 Task、领取租约或调用外部工具 |
| PlanInput | 封存一个 Intent、所选 Fact/Hint 与候选摘要及版本 | `STAGED/BOUND` 不代表计划发布或 Goal 完成 |
| Ring Goal/Task | Kernel 按合同、预算、路径与能力控制长程执行 | Workflow/模型/候选不得自行写 DONE |
| Evidence/Auditor | 回传有来源的结果，驱动下一轮 Fact 与最终屏障 | 单条成功回执不能证明整个终点达成 |

## OODA 循环如何保留

| Cairn 阶段 | 融合后的实际动作 | 唯一权威 |
|---|---|---|
| Observe | 读当前图、Ring 状态和有来源的新 Fact；缺失或断线显示 UNKNOWN | 图由 Cairn 保存；执行事实由 Ring 回读 |
| Orient | 零工具 Reason 根据 Origin、Goal、Fact、Hint 解释当前差距 | Reason 仅提出解释，不产生可信 Fact |
| Decide | 选一个 Intent，封存图版本和该 Intent 的执行约束 | Cairn 记录方向；Kernel 校验合同与准入 |
| Act | Ring Manager 把所选方向细化为可执行 Task，Executor 经 Broker 执行，Auditor 核验 | Kernel/Runner/Broker/Auditor 分权 |
| 回环 | 已验证的结果作为新 Fact 加入图，再次 Observe/Orient | 不能用模型总结替代证据 |

当前实现把 bound project 排除于 Cairn dispatcher，且 B2 要求人工输入完整
`PlanCreate`。因此它暂停了自动 OODA，只是一条安全的过渡接线。恢复自动
循环时，不能把原 Cairn CLI 探索权限重新打开；应由零工具 Reason 产出
Intent，并由 Ring 的租约与 Effect Gateway 承接 Act。

Ring Manager 对所选 Intent 只能做受控细化。当前 R1b 只校验 Goal 合同；
它没有确定性的“输出计划仍执行所选 Intent”约束。后续须给本轮 Intent
定义结构化执行锚点与预期证据，Kernel 检查计划覆盖该锚点、来源 ID/digest、
预算、路径和能力；无法证明关联时保持待审或阻断。自然语言相似度不能当作
独立验收。允许模型细化计划，不允许它静默改选另一探索方向。

## 对当前实现的影响

现有 Ring 产品入口先绑定**已经存在**的 Ring Goal，绑定时还拒绝任何未结论
Intent；绑定后关闭 Cairn 原 dispatcher，而 B2 让人手工提交完整
`PlanCreate`。这能验证一条受控接线，但不是最终默认体验。若停在这里，
Cairn 会失去“读图 → 提 Intent → 探索 → 加 Fact → 再读图”的主循环。

下一批须补“图优先”路径：用户仍从 Origin 和 Goal 创建项目；受控创建或
选择 Ring Goal 时，只冻结终点合同、预算和验证配置，不冻结未知探索路径。
本轮 Reason 在零工具权限下从当前图提出 Intent。选中的 Intent 经 PlanInput
进入 Ring；Kernel 发布本轮合法计划与 Task，Broker 执行外部效果，Auditor
核对。受权投影只把有来源的观察追加为 Fact，随后 Reason 再读新图。
单轮计划结束后若终点尚未验收，继续下一轮；只有 Ring Kernel 的最终屏障
与 ReleaseManifest 能使页面显示 Goal 已验收。

## 验收红线

最终 E2E 固定至少两轮：`Origin + Goal → Intent 1 → Ring Effect/Evidence →
Fact 1 → Intent 2 → Ring Effect/Evidence → Fact 2 → Goal DONE`。两轮之间
必须证明第二个 Intent 使用了新增 Fact；第一轮成功时 Goal 仍未 DONE。
断线/重启后不重复外部副作用；未经验证的 Fact 不升级为 Evidence；
没有 Kernel DONE 与匹配 ReleaseManifest 时页面不得显示完成。

当前尚未实现自动零工具 Reason、自动 Goal 草稿/合同映射、受权 Fact 回流和
两轮最终 E2E。这些缺口必须先补代码，再执行最终 E2E。
