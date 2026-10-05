# Ringharness

> **进行中（v0.6 Temporal 轨道）。** 方案见 [v0.6 Temporal 十份修订文档](doc/v0.6/README.md)。原 v0.5 冻结文档保持不变；当前事实以代码、[接口覆盖清单](contracts/implementation-coverage.json)及[开发记录](doc/implementation/进度与验证.md)为准。接口存在、测试通过或 Workflow COMPLETED 都不等于完整无人值守或 Goal DONE。

完整 V1 正在开发。冻结设计位于 `doc/`，实际实现进度见 [开发记录](doc/implementation/进度与验证.md)，Agent 工作约定见 [AGENTS.md](AGENTS.md)。当前已不只是项目 API 骨架，但尚未达到完整无人值守系统的发布验收。

## 协作与第三方联合开发

- 贡献流程：[CONTRIBUTING.md](CONTRIBUTING.md)
- 安全披露：[SECURITY.md](SECURITY.md)
- 与 Harness / Temporal / Verifier / Broker 等对接：[doc/engineering/第三方联合开发.md](doc/engineering/第三方联合开发.md)
- Issue 请用 GitHub 模板（缺陷 / 功能 / **第三方集成**）；PR 须通过 `.github/workflows/foundation.yml`

## 当前已有代码路径

- Python/TypeScript workspace、依赖锁、生成 OpenAPI 与 TS 类型。
- Control API 已包含项目与配置、Goal/Task/Activity、Effect、审批、审计、最终屏障和 ReadModel 等路由；具体覆盖以生成清单为准，路由存在不代表其所有业务场景已验收。
- PostgreSQL 保存业务状态、事件、租约、回执和完成屏障；Temporal 负责持久编排，TypeScript Runner/Harness adapter 与 Broker-backed 工具路径已有实现和窄范围验收缝。
- Content v3、S3兼容字节存储、SHA256完整性校验、项目工件目录与授权下载已经落地；大对象流式处理及完整生命周期仍按开发记录推进。
- React 工作台已有 Goal、活动、效果、审计、最终化等观察面；完整产品交互与生产身份接入仍需发布验收。

## 本地验证

`uv sync --frozen --python 3.12` 和 `pnpm install --frozen-lockfile` 安装依赖。

`uv run python scripts/test_local.py` 使用名为 ringharness-development-pg 的独立测试容器和 ringharness-development-s3 独立对象存储及本机 `.runtime/postgres.env`、`.runtime/minio.env`，执行迁移与 Python 测试。该容器是开发时单独创建的，不使用任何已有业务库。新机器也可以设置 RING_DATABASE_URL/RING_TEST_DATABASE_URL 指向专用 PostgreSQL，依次执行 `uv run alembic upgrade head`、`uv run pytest -q`。对象存储测试还需 RING_TEST_S3_ENDPOINT、RING_TEST_S3_ACCESS_KEY、RING_TEST_S3_SECRET_KEY 指向独立S3测试服务，账号须能创建/删除随机测试bucket。依赖模板在 deploy/compose.test.yaml；映射端口由 docker compose port 查询。未配置测试依赖时相应集成测试会明确 skip，不能算集成验收通过。

`pnpm test` 执行 Runner 行为与防御规则测试；`pnpm build` 类型检查并构建 Web；`uv run python scripts/generate_contracts.py` 生成当前契约。coverage 文件只说明路由覆盖，不证明 live、恢复、长时稳定性或 Goal DONE。

在本机已有专用容器的环境中，`uv run python scripts/serve_local.py` 启动 API（127.0.0.1:58101），`pnpm --filter @ring/web dev --port 58102 --strictPort` 启动 Web。端口已占用时退出，不终止其他服务。

联调栈中的 Runner 不能只看端口或 PID 文件判断存活。运行 `uv run python scripts/probe_runner_health.py`，同时核对精确 Runner 进程、`ring-runner` Activity task queue 的近期 poller 及二者进程树关系；退出码非零表示当前没有可证明的持久 Runner。

浏览器 OIDC/session 已有 API 路径，但生产身份提供方、部署配置和完整浏览器验收仍须按环境验证。CLI 身份需要显式设置 RING_JWT_PUBLIC_KEY、RING_JWT_ISSUER、RING_JWT_AUDIENCE，项目列表另需至少32字节 RING_CURSOR_SECRET；仓库别名在 RING_REPOSITORY_REFS JSON 数组中预登记。不得把私钥、数据库密码或 token 写入前端。缺少配置时接口关闭，不以开发万能身份绕过。

## 明确尚未通过完整发布验收

当前仍缺单一固定发布候选上的完整门禁证据：默认 CI live、真实故障恢复覆盖、长时间运行以及 100 小时验收尚未完成。Runner 多条执行入口、部分大模块和当前能力文档仍在收敛；生产 OIDC、运维探针、完整 UI 及证据生命周期需按发布环境继续验证。短时 E2E、局部 live、接口覆盖或某次 Goal DONE 只能证明对应验收缝，不能替代完整 V1 验收。

原 v0.5 文档及快照保持不变，其中“尚未实现”是冻结当时状态，当前进度以开发记录为准。新增 U01/U02/E01 是独立补充，未自动纳入冻结。

工件下载配置：RING_S3_ENDPOINT、RING_S3_BUCKET、RING_S3_ACCESS_KEY、RING_S3_SECRET_KEY（仅服务端）、RING_S3_REGION。bucket由运维预建；应用不自动创建。RING_ARTIFACT_MAX_BYTES默认16MiB，最大64MiB；单段读取仍校验整个对象。本地serve_local不自动使用测试root凭据。未配置时内容下载503。
