# Temporal 版本矩阵（M0 本机联调钉扎）
#
# Python / TS SDK 与 Server 镜像 digest 已写入；禁止部署 latest。

## 钉扎状态

| 组件 | 版本 | 状态 |
|---|---|---|
| Python SDK | `temporalio==1.32.0` | 已钉（精确 pin，禁止 latest） |
| Temporal Server image | `temporalio/auto-setup@sha256:607d68caa111338d754771efb876c92dfcdae06d056e4530bb31cd0f37406e6a`（tag 对照 `1.28.1`） | **本机联调已核验 digest**（2026-09-12）；≠ 生产冻结 |
| Temporal UI | `temporalio/ui@sha256:28bb3ea5a6ea3e09f16b521f32ab727c96470f7f1e420c66a6cbfb02001a8aa2`（tag 对照 `2.31.2`） | 本机联调钉扎 |
| Temporal 自用 Postgres | `postgres@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94`（tag 对照 `16`） | 与业务 PG 隔离；宿主端口 **15433** |
| TS SDK | `@temporalio/{worker,activity,client,workflow}@1.23.0` | 已钉（精确 pin，禁止 latest） |
| Worker build id (Python) | `m0-py-1.32.0-dev` | 控制面 `ring-control` |
| Worker build id (TS Runner) | `m0-ts-1.23.0-dev` | Runner `ring-runner` |

## 任务队列

| 队列 | 进程 | 职责 |
|---|---|---|
| `ring-control` | `apps/workflow-worker`（Python） | GoalWorkflow + Kernel 控制 Activities |
| `ring-runner` | `apps/runner` Temporal Worker（TS） | 仅 `RunActivation` Activity；**不**注册 TS Workflow |

GoalWorkflow 在 PLAN admit 成功后派发 `execute_activity("RunActivation", …, task_queue="ring-runner")`，
再经 Kernel Activity `observe_activity_status` 观察 PLAN 库内终态（`plan_terminal_statuses`；超时 `OBSERVE_TIMEOUT`）。
**observe ≠ acceptance / Goal DONE。**
TS PLAN：缺 checkout/Control URL/JWT 时 `PENDING_ENV` + `pending_harness`；env 就绪且注入 Cordis 端口时可走零工具桥 → `ACTIVATION_SUBMITTED`（仍 **≠** Goal DONE / ≠ live Qwen 100h）。
`pending_harness` **不**使 Workflow 失败、**不**标 Goal DONE。

## 原文摘要

```
Python SDK: temporalio==1.32.0
Temporal Server: temporalio/auto-setup@sha256:607d68caa111338d754771efb876c92dfcdae06d056e4530bb31cd0f37406e6a (1.28.1)
Temporal UI: temporalio/ui@sha256:28bb3ea5a6ea3e09f16b521f32ab727c96470f7f1e420c66a6cbfb02001a8aa2 (2.31.2)
Temporal PG: postgres@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94 → 127.0.0.1:15433
TS SDK: @temporalio/worker@1.23.0（及 activity/client/workflow 同版精确 pin）
Worker build id (Python): m0-py-1.32.0-dev
Worker build id (TS Runner): m0-ts-1.23.0-dev
Task queues: ring-control (Kernel) / ring-runner (RunActivation)
```

## 说明

- 本文件是开发机联调矩阵，**不等于**已通过 TM01–TM12 全量或冻结 v0.6。
- `deploy/temporal/docker-compose.yml` 使用 digest 引用；tag 仅作人类对照。
- 默认 `scripts/serve_local.py` **不**拉起 Temporal，避免改动本机默认 runtime。
- Runner：`pnpm --filter @ring/runner temporal-worker`（缺 `RING_TEMPORAL_TARGET` 打 `runner-temporal-idle` 并以 0 退出）。
- **测试 Server 路径**：
  1. `WorkflowEnvironment.start_local()` — SDK CLI（非 compose digest）
  2. `tests/temporal/test_tm_docker_compose_goal_workflow.py` — 本机 compose `127.0.0.1:7233`（不可连则 skip）
- 下载/端口失败时联调用例 `pytest.skip`，不冒充通过、不 fallback LEGACY。
