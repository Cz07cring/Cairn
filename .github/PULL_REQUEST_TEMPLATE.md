## 摘要

<!-- 为什么需要这个改动？解决什么问题？ -->

## 变更类型

- [ ] 缺陷修复
- [ ] 功能 / 协议
- [ ] 文档
- [ ] 测试 / 举证
- [ ] CI / 工程化
- [ ] 第三方集成（Harness / Temporal / Verifier / Broker 适配）

## 与规格的关系

- 相关文档：`doc/…`（章节或条目）
- 是否改动冻结契约 / OpenAPI / Content Schema：是 / 否  
  - 若是：已运行 `uv run python scripts/generate_contracts.py`，且 `contracts/` 与 `generated.ts` 无意外漂移

## 验证

- [ ] `uv run python scripts/test_local.py`（或说明 skip 原因）
- [ ] `pnpm test && pnpm build`（若触及 TS）
- [ ] `uv run ruff check …`（若触及 Python）
- [ ] 未把密钥 / `.runtime/` 写入仓库
- [ ] 未将 Goal/Task DONE 写成模型成功或 shell exit 0

## 风险与回滚

<!-- 双调度、LEGACY fallback、证据语义变更等 -->

## 第三方联合开发（如适用）

- 对接组件：
- 版本 / SHA / 镜像 digest：
- 本侧改动的边界（Kernel / Runner / Broker / Workflow）：
