# 贡献指南

感谢参与 Ringharness。本仓库面向 **Kernel / Temporal 编排 / Harness Runner / Broker** 的联合开发；第三方集成请先读 [第三方联合开发](doc/engineering/第三方联合开发.md)。

## 开始之前

1. 阅读 [AGENTS.md](AGENTS.md)（实现硬规则：DONE 权威、三权分立、诚实覆盖）。
2. 设计权威：`doc/v0.6/`（Temporal 修订）+ 继承的 `doc/01`–`doc/10`（v0.5 冻结基线）。
3. 进度以代码与 [doc/implementation/进度与验证.md](doc/implementation/进度与验证.md) 为准，不以文档冒充已实现。

## 开发环境

```bash
uv sync --frozen --python 3.12
pnpm install --frozen-lockfile
uv run python scripts/test_local.py   # 独立 PG/S3 容器；缺依赖则 skip，不算验收通过
pnpm test && pnpm build
uv run python scripts/generate_contracts.py
uv run ruff check apps/control packages tests scripts migrations
```

密钥与 `.runtime/` 不得提交。本机身份见 README（`RING_JWT_*`、`RING_CURSOR_SECRET` 等）。

## 分支与 PR

| 约定 | 说明 |
|---|---|
| 默认分支 | `main`（创建远程后生效） |
| 功能分支 | `feat/…`、`fix/…`、`docs/…`、`test/…` |
| PR | 必须通过 `.github/workflows/foundation.yml`；说明动机、风险、如何验证 |
| 契约 | 改 API/协议须同步 `doc/contracts` → 生成 OpenAPI/TS；禁止手改 `packages/api-client/src/generated.ts` |

PR 模板见 `.github/PULL_REQUEST_TEMPLATE.md`。Issue 请用模板（缺陷 / 功能 / **第三方集成**）。

## 代码边界（第三方必读）

- **Kernel（业务 PG）**：Goal/Task 状态、授权、预算、证据、DONE；Workflow 不得直接写 DONE。
- **Temporal**：只做持久编排；不可用时命令待投递，**禁止** fallback LEGACY。
- **Runner / Harness**：只承接 activation；不持业务 DB 写凭据。
- **Broker**：只执行已授权 effect；不扫 Task 做调度。
- 语言：交流与 `error.message` 用简体中文；协议标识保持英文。

## 不要做的事

- 为未实现能力生成成功 stub 或把 ready scope 写成完整无人值守。
- 用模型成功、shell exit 0、前端按钮冒充 Goal/Task DONE。
- 把生产库、共享业务容器或密钥写进仓库。
- 未获合同授权默认切云（`RING_CLOUD_MODE` 默认 DENY）。

## 安全问题

请勿在公开 Issue 中贴密钥或可复现的越权步骤。见 [SECURITY.md](SECURITY.md)。
