# Web 驾驶舱 E2E 联调验收矩阵

> 更新：2026-09-13  
> 用途：把“页面看起来完整”和“业务真的跑通”分开。每一项必须同时给出浏览器行为、真实后端事实和可复验的证据，才可从 `待联调` 改成 `已验证`。

## 1. 状态口径

| 状态 | 含义 |
|---|---|
| 已验证 | 固定输入下，浏览器与真实服务完成可重复场景；只代表该场景通过 |
| 部分验证 | 已接真实接口，但身份、数据或长程条件尚不完整 |
| 待联调 | 页面或后端单独存在，尚无跨边界 E2E 证据 |
| 未开始 | 所需实现仍缺失 |

`observed`、`verified`、`accepted`、`completed` 与 `done` 不混用。Goal `DONE` 只能由 ControlKernel 在固定 VerificationProfile 和最终屏障后裁决；浏览器成功、HTTP 2xx、Workflow COMPLETED、Harness 返回、命令退出 0 都不能代替它。

## 2. 用户功能板块

| 用户板块 | 前端行为 | 后端/运行时事实 | 当前状态 | 自动化证据 | 收敛条件 |
|---|---|---|---|---|---|
| 系统入口与健康 | 桌面/移动端可打开；展示“服务可响应”的有限结论 | `GET /health/live` 真实返回 `{alive:true}` | 已验证 | `e2e/workbench.spec.ts`：桌面 Chromium + Pixel 7 | 保持失败关闭；不得把 liveness 写成 readiness 或 100h 通过 |
| 一级导航与快捷前往 | 侧栏、移动端横向入口、`⌘/Ctrl+K` 搜索可达 | 路由由同一 Workbench 状态模型驱动 | 已验证 | 导航、键盘搜索、刷新保持 E2E | 新增页面必须同步导航清单与可访问名称 |
| 登录边界 | 未登录时不生成演示数据；开发 Bearer 明确标记为调试身份 | `GET /api/v1/auth/session` + 短期 RS256 Bearer | 部分验证 | 匿名 E2E + 隔离短期 operator E2E | 增加完整 OIDC 登录、过期、退出与 401 恢复 |
| 目标管理 | 已以真实 operator 创建项目并在目标工作台读回；Goal 草稿/启动仍待联调 | Project/Goal/START 与 PG 权威状态 | 部分验证 | `e2e/authenticated.spec.ts` | 浏览器完成“建草稿→启动→刷新仍存在”，并观察真实 Task/DAG |
| 任务流程图 | 总览显示真实 Goal/Task 数量；按 `depends_on` 分层并查看步骤详情 | `GET /api/v1/goals/{goal_id}` + `/tasks` | 部分验证 | 只读共享开发数据预览 + 图布局单测 | 在隔离业务 E2E Goal 上验证完整状态变化、循环失败关闭和任务详情 |
| 执行记录 | 筛选、列控制、分页、选择、打开单次运行 | Goal/Activity/Effect/ModelInvocation 来自 PG 读模型 | 待联调 | 视图模型单测 | 真实 activation 运行时，浏览器观察状态与 API/PG 一致且无伪造行 |
| 命令与工具进度 | 展示 phase、命令、开始/结束、输出摘要和失败原因 | Harness ToolCall → Broker Effect → receipt/reconcile | 待联调 | 后端 E2E-1/2 与 Runner 测试已有独立证据 | 浏览器看见同一 `activation_id/logical_step_id/effect_id` 的完整链并可追溯 |
| 暂停、继续、取消 | 用户动作有确认与反馈，失败后可恢复 | Control Command、Stop、QUARANTINED 与恢复规则 | 待联调 | 后端已有窄缝测试 | 浏览器真实提交命令；断网/刷新/重复点击不重复副作用 |
| 需要我处理 | 展示审批、隔离与待处理原因 | Approval、VerificationObligation、UNKNOWN/QUARANTINED | 待联调 | 组件单测 + 后端隔离测试 | operator 决策后状态可追溯；审批不直接等于 Effect 或 DONE |
| 证据与审计 | 浏览 hash、来源、验收裁决和失败项 | 字节仓、catalog、Auditor 独立上下文、三态裁决 | 待联调 | 后端 E2E-3/三层审计用例 | UI 同时展示完整性、来源可信度、验收正确性，不能把 hash 当 PASS |
| 验收与交付 | 展示最终屏障、导出排队与 ack | Audit → Integrate → Finalize → Kernel DONE | 待联调 | 后端 E2E-4/5 用例 | 浏览器观察真实最终屏障；交付须有 ack；崩溃恢复不得制造假 DONE |

## 3. 全业务长程路径

```text
用户创建目标
  → Manager/Planner 只产出计划（PLAN tools=[]）
  → Temporal 持久编排
  → Harness 执行循环
  → Broker 唯一副作用出口
  → receipt / UNKNOWN 对账
  → 候选封存
  → Auditor 独立验证
  → 集成与最终屏障
  → ControlKernel 裁决 Goal DONE
  → 前端显示证据、裁决和交付 ack
```

| 阶段 | 当前可复验入口 | 当前结论 |
|---|---|---|
| 脚本化全业务链 | `bash scripts/run_business_e2e.sh` | 后端具备 E2E-0～5 场景集；仍需独立环境复跑，不能由前端批次代验 |
| 真实模型单次链 | `RING_E2E4_LIVE_CHAT=1 bash scripts/run_business_e2e.sh live 1` | 依赖固定 Harness checkout、真实 chat、隔离 PG/S3 与 Temporal；缺任一项必须失败关闭 |
| 10～20 个不同任务 | `bash scripts/run_convergence_soak.sh --runs 10` | 尚未形成独立验收结论 |
| 8h / 24h / 100h | `scripts/run_convergence_soak.sh` 的长跑参数与结果目录 | 尚未完成；在真实时长、故障注入、指标和证据包齐全前不得写“可交付 100h” |

### 3.1 2026-09-13 本机已有运行观察

这些目录来自共享开发运行时，只能证明“实际跑过并产生了结果”，不能替代隔离环境和独立验收：

| 证据目录 | 观察结果 | 可得结论 |
|---|---:|---|
| `.runtime/soak-verify-20260913T061938Z` | 3/3 通过，每次约 40 秒 | 固定 live 场景曾连续通过三次 |
| `.runtime/soak-soak-20260913T102432Z` | 8/10 通过 | 仍存在 20% 场景失败，未达到稳定交付 |
| `.runtime/soak-soak-20260913T103212Z` | 1/5 通过 | 同一场景对模型行为敏感，稳定性不足 |
| `.runtime/soak-final-20260913T104249Z` | 1/1 通过；对应 Goal 为 `DONE` | 只证明该 Goal 经 Kernel 完成，不能外推整体成功率 |

失败日志显示两类主要机制：模型长时间重复 `read_file` / `write_file` 后仍缺 `seal_candidate`；连续无效写入触发 `NO_PROGRESS_FORCE_STOP` 并关闭工具准入。强停是正确的安全行为，但也说明自主执行的任务完成率仍需优化。

同期指标文件明确给出共享库污染警告：窗口内包含大量测试或中断 Goal，整体成功率不能作为验收依据；历史窗口也曾残留未对账 `UNKNOWN`。后续长跑必须使用独立数据库、独立对象桶和本批唯一 `project_id/goal_id` 取样，禁止用共享窗口统计宣布收敛。

## 4. 交付门槛

1. 桌面与移动浏览器 E2E 通过，失败时保留 trace、截图和视频。
2. 使用隔离测试身份与隔离 PG/S3/Temporal；禁止连接生产或其他业务库。
3. 从目标创建到最终裁决的所有关键 ID 可贯穿查询：`goal_id`、`task_id`、`activation_id`、`attempt_id`、`logical_step_id`、`effect_id`、`artifact_id`、`verification_run_id`。
4. 覆盖正常、拒绝、超时、失租、receipt 丢失、UNKNOWN、重启、重复点击和取消恢复。
5. 前端显示必须与 API/PG 权威事实一致；日志与 Harness session 只作观察源。
6. 100 小时验收必须输出逐轮日志、失败分类、四项业务指标和最终证据清单，并由未参与实现的一方复跑确认。

当前前端批次只声明：**驾驶舱交互、真实 liveness、未登录失败关闭和跨设备可达性已验证**。带身份的业务面联调与 100 小时验收仍按上表推进。

## 5. 运行方式

基础浏览器门连接当前 `127.0.0.1:58101` Control API：

```bash
pnpm --filter @ring/web test:e2e
```

认证联调必须显式提供已经隔离的 PostgreSQL 数据库；脚本会先迁移数据库，再动态生成十分钟有效的 RSA 身份，同时启动独立 Control API 与前端：

```bash
RING_WEB_E2E_DATABASE_URL='postgresql+psycopg://.../ring_web_e2e' \
  pnpm --filter @ring/web test:e2e:auth
```

私钥和 Bearer 只存在于当次 Playwright 进程环境，不写入仓库或测试报告。

查看当前开发库中真实活动 Goal 时，使用只读 `viewer` 身份启动预览；脚本自动选择一个带 Task 的活动 Goal，总览和任务流程每 5 秒回读权威状态：

```bash
RING_WEB_PREVIEW_DATABASE_URL='postgresql+psycopg://.../ring_test' \
  pnpm --filter @ring/web preview:readonly
```
