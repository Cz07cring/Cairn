# 本机 Temporal（仅 TM / M0 联调用）

**不要**把本目录并入 `scripts/serve_local.py`；默认开发 runtime 保持不变。
本机业务 `.runtime` 配置也不应被本模板改写。

## 版本

见 [VERSIONS.md](./VERSIONS.md)。Python SDK 已钉 `temporalio==1.32.0`；
TS SDK 已钉 `@temporalio/*@1.23.0`；Server 镜像为候选 tag，**digest 待联调锁定**。

## 启动（显式、可选）

```bash
cd deploy/temporal
docker compose up -d
# 默认 Temporal Frontend: 127.0.0.1:7233
# Temporal 自用 PG（与业务库隔离）: 127.0.0.1:15433
# UI: 127.0.0.1:8233
```

停止：

```bash
docker compose down
```

镜像按 **digest** 钉扎（见 VERSIONS.md）；`dynamicconfig/` 挂载进容器。宿主 **5433** 常被其他栈占用，故自用 PG 用 **15433**。

## Worker

控制面（`ring-control`）：

```bash
export RING_TEMPORAL_TARGET=127.0.0.1:7233
# 可选：Kernel Activities 业务库（与 Broker 不同，Worker 允许持有）
# export RING_DATABASE_URL=...
uv run python -m workflow_worker
```

Runner（`ring-runner`，仅 `RunActivation` Activity，无 TS Workflow）：

```bash
export RING_TEMPORAL_TARGET=127.0.0.1:7233
pnpm --filter @ring/runner temporal-worker
# 冒烟：pnpm --filter @ring/runner temporal-worker -- --once
```

未设置 `RING_TEMPORAL_TARGET` 时：Python Worker 打 `workflow-worker-idle`、TS Runner 打 `runner-temporal-idle`，并以 0 退出（`--once`），不假装已接入。

## 恢复 / 联调验证（pytest）

```bash
# Worker 失联接续（start_local）+ GoalWorkflow observe-timeout Continue-As-New 水位
uv run pytest tests/temporal/test_temporal_worker_recovery.py \
  tests/temporal/test_goal_workflow_continue_as_new.py -q

# 本机 compose Server（须先 docker compose up；不可连则 skip）
export RING_TEMPORAL_TARGET=127.0.0.1:7233
uv run pytest tests/temporal/test_tm_docker_compose_goal_workflow.py \
  tests/temporal/test_tm_compose_real_kernel.py \
  tests/temporal/test_tm_compose_ts_run_activation.py \
  tests/temporal/test_tm_compose_kernel_and_ts.py -q
```

Worker recovery 使用 `start_local`（真 CLI）；compose 联调使用钉扎 digest 的 auto-setup。
`test_tm_compose_real_kernel` 走**真** Kernel Activities（RunActivation stub 写 SUCCEEDED 供观察）；
`test_tm_compose_ts_run_activation` 拉起真 TS Worker（stub Kernel）；
`test_tm_compose_kernel_and_ts` 二者皆真 → `PENDING_ENV` + `OBSERVE_TIMEOUT`（不 stub 写库终态）。
三者都不标 Goal DONE、不 fallback LEGACY。

## 边界

- Workflow 确定性：禁止在 Workflow 内直接 SQL/HTTP/文件；IO 只在 Activity。
- 不得把 Goal 标为 DONE；不得 fallback LEGACY。
- FakeTemporal 路径继续用于无 Server 的单测；真实 Client 仅当 target 可连时使用。
- `observe_activity_status` 只读 `activities.status`，**不等于**验收 PASS / Goal DONE。
- 生产 CAN（`enable_continue_as_new`）默认关闭；测试/后续 env→payload 显式开启。

## 可选：创建 Goal 默认 TEMPORAL / 强制 TEMPORAL

```bash
# 仅当显式设置时，Goals.create INSERT 写入 TEMPORAL；缺省仍 LEGACY
export RING_ORCHESTRATION_DEFAULT_BACKEND=TEMPORAL

# 强制 TEMPORAL（须同时有 RING_TEMPORAL_TARGET）：新 Goal 默认 TEMPORAL，
# 并拒绝 START 仍为 LEGACY 的 Goal；全局 claim 亦不领取任何 Goal 活动（防双调度）。
# 存量 LEGACY 排空须显式：
#   export RING_LEGACY_CLAIM_DRAIN=1
export RING_ORCHESTRATION_REQUIRE_TEMPORAL=1
export RING_TEMPORAL_TARGET=127.0.0.1:7233
```

不要在默认测试环境打开上述变量，以免翻转既有 LEGACY 用例。
