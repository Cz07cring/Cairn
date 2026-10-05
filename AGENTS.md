# Ringharness · Agent 工作约定

面向在本仓库改代码的 agent。人类可读总览见 [README.md](README.md)；冻结规格与实现进度分别见下方文档指针。

## 0. 红线（最高优先级，任何任务不得违反）

> 移植自全局 Agent 约定的红线；与本文件其他条款冲突时，以本节为准。

### 测试纪律

- 永远不要在编写代码后编写单元测试。
- 强烈优先使用端到端测试作为唯一的测试机制。使用它们来验证复杂功能是否正常工作。在端到端测试结束时，生成一个可验证且可重复的工件。
- 如果你必须孤立地测试一个系统, 首先写下它可能失败的所有方式, 然后再编写代码。
- 开发期间不要运行全套 e2e, 要在结束的时候再执行。
- 冗余测试被认为是有害的。
- 变更检测测试被认为是有害的。
- 不要在没有真正的行为测试差距的情况下为错误修复创建回归测试。

#### 测试准入与清理

- 新行为默认写在 `tests/e2e/**`。不得把端到端场景拆成一组 mock 单元测试后宣称行为已验证。
- 孤立测试只能用于 E2E 难以稳定注入或观测的故障，例如协议代数、状态机非法迁移、幂等/恢复中断点、安全失败关闭。写测试前必须在批次记录中列出：可能的失败方式、为什么 E2E 不能可靠覆盖、测试要观测的公开不变量。缺一项就不得新增。
- 禁止保留仅检查常量/默认值/字段形状、私有函数调用顺序、源码快照、mock 成功路径、测试专用包装层或实现细节的测试。覆盖率与测试数量不是验收证据。
- 审计存量孤立测试时，逐条映射到「真实故障模式 + 最近 E2E 验收缝」。找不到 E2E 遗漏且独立可观测的真实 bug 时删除；后续 E2E 覆盖了同一故障时也删除。不得把被删的低价值单元测试平移成同等低价值的集成测试。
- E2E 工件必须包含固定输入或种子、完整可复跑命令、环境边界、通过/失败/跳过结果、原始输出路径与 SHA-256。未运行、跳过、非目标范围必须显式记录。

### 证据纪律（用词即口径，说错=伪造证据）

- `observed` = 日志/链上/API 看到了, 未证明整体正确
- `verified` = 按固定输入可复现地跑通 (贴命令+输出)
- `done` = 全部验收缝绿 + 独立复核, 不是"我改完了"
- 禁止: "应该可以" "大概率没问题" — 要么 verified, 要么标未验证+依据

- 宣称"已接通/已完成"前, grep 写入方和调用方, 不是定义处。字段有默认值 ≠ 有人设它; 函数存在 ≠ 有路径调用它。找不到写入方, 结论是"未接通", 不是"已完成"。
- 谁写谁不验: 自己改的代码, 验证结论交给独立路径 (fresh subagent / 重跑测试 / 换工具复核), 不自报"已通过"。
- 失败要贴两次运行的对比: 报 bug 时给复现命令+输出; "本地绿但 CI 红"必须附失败集 diff, 不许只贴局部绿。

### 反幻觉

- "应该可以" = 没验证。跑测试/查链上/看源码, 确认再报。
- 引用的每个数字要能指出处, 找不到标"存疑"。
- 报问题必须带: 影响 → 路径 → PoC/证据 → 修复建议。缺一样就不算报完。
- 假洞 = 白干。你付不起幻觉的代价。

> 注：本节测试纪律与 §4.10「新 API/不变量先写失败测试再实现」并存时，「测试」默认指端到端行为测试。只有通过上述孤立测试准入后才能先写隔离红灯；禁止事后补写单元测试。

## 1. 项目是什么

Ringharness 是完整 V1 无人值守系统：Manager 规划、Executor 提交候选、Auditor 独立验证，由 ControlKernel 按规则判定 DONE。技术栈为 Python 控制面 + TypeScript Runner/Harness adapter + React Web；权威状态在 PostgreSQL。

当前 **0.1.0 部分实现**，不是完整无人值守系统。已有工程骨架、项目/配置/Skill API、Goal DRAFT 持久化、Content v3 编码、工件只读查询与对象仓适配；Goal.start/Task/DAG、租约、Harness/Broker 执行、OIDC、完整 UI、E2E/100h 尚未完成。

## 2. 文档权威（先读再改）

| 何时 | 读什么 |
|---|---|
| 任意实现任务开始前 | [doc/README.md](doc/README.md) 导航；领域不变量 [doc/01-开发文档.md](doc/01-开发文档.md)；字段/路由 [doc/05-API接口文档.md](doc/05-API接口文档.md) |
| 判断「现在做到哪」 | [doc/implementation/进度与验证.md](doc/implementation/进度与验证.md)；以代码与 [contracts/implementation-coverage.json](contracts/implementation-coverage.json) 为准，进度文可能滞后 |
| 改目录/契约生成 | [doc/engineering/工程目录与契约生成补充.md](doc/engineering/工程目录与契约生成补充.md) |
| 改前端/命令进度展示 | [doc/engineering/前端技术栈与命令进度协议补充.md](doc/engineering/前端技术栈与命令进度协议补充.md)、[doc/04-前端文档.md](doc/04-前端文档.md) |
| 接上游 Harness | [doc/09-Harness上游核验.md](doc/09-Harness上游核验.md)；固定 SHA，禁止另立调度权威；live 测优先接本机 PM2 托管的本地 Qwen 开放接口（见 §9） |
| 验收口径 | [doc/02-测试文档.md](doc/02-测试文档.md)、[doc/03-收敛文档.md](doc/03-收敛文档.md) |

冻结规格为 **v0.5 / FROZEN_SPEC**。`doc/` 编号文档与 `doc/contracts/` 是设计权威；改协议须同步文档/schema/迁移/生成客户端/测试，不能只改文档冒充已实现。U01/U02/E01 是独立补充，未自动纳入冻结包。

## 3. 仓库地图（已落地 vs 空壳）

```text
apps/control          # FastAPI：项目/策略/模型配置/验证配置/工件读取
apps/runner           # 目前仅 PLAN 工具拒绝等本地防御单测
apps/web              # React 工作台：真实 /health/live，创建目标禁用
apps/execution-broker # broker_app 长驻入口（idle/health；非调度权威，无业务库写凭据）
apps/workflow-worker  # Temporal Worker 骨架（缺 RING_TEMPORAL_TARGET 则 idle；可持业务库供 Kernel Activities）
packages/control_kernel   # protocols + storage（项目/配置/工件目录）
packages/evidence_ledger  # Content v3 编码 + S3Objects 字节仓
packages/api-client       # 由 OpenAPI 生成，禁止手改 generated.ts
packages/{execution_broker,context_compiler,read_model,observability,orchestration}  # orchestration 钉 temporalio==1.32.0
migrations/versions   # 0001_projects … 0016_finalization（及后续编排迁移）
contracts/            # 生成/同步产物；coverage 列出未实现公共路由
deploy/temporal       # 本机 Temporal 候选模板（digest TBD；不并入 serve_local）
scripts/              # generate_contracts / serve_local / test_local
tests/                # unit + 真实 PG/S3 集成（缺环境则 skip）
```

依赖边界：`packages` 不导入 `apps`；`domain` 无网络/文件/DB IO；Runner/Web 不持业务 DB 或宿主凭据；Broker 不得直连业务库写权限；workflow-worker 可为 Kernel Activities 持有 `RING_DATABASE_URL`（经 control_kernel）。

## 4. 实现硬规则

1. **诚实覆盖**：只实现真实行为；OpenAPI/`implementation-coverage.json` 只反映已实现路由，不为未完成能力生成成功 stub。
2. **完成判定**：模型成功、shell exit 0、Activity SUCCEEDED、前端按钮态，都不能写成 Goal/Task DONE。DONE 只走 Kernel + 固定 VerificationProfile + 屏障。
3. **三权分立**：PLAN activation 工具集必须为空；工具路径由 Kernel/Broker 再鉴权，不信任请求自报身份。
   **新增写入口/回执入口时必须绑定 attempt 持有者身份**：仓内既有守卫 `storage/effects.py:_require_owner_attempt`（校验 `activity_attempts.worker_id == 提交 subject 的 worker`），`create_step`/`prepare_effect` 等 12 处已正确使用。2026-09-12 发现 `apply_effect_receipt` 与 `apply_stop_receipt` **绕过该守卫**，导致任意已注册 worker 可伪造他人 Effect/Stop 回执（Issue #17），其中 Stop 侧会**提前释放 QUARANTINED 资源**。
   两条约束：① 回执路径**不得**照搬「attempt 必须 ACTIVE」——失租后的迟到回执须按 `doc/01:164` 收进 reconciliation，不得丢弃唯一真实结果；② 身份不符时**零副作用写入**（不落 receipt、不改状态、不释放资源/预算）。

4. **状态权威**：业务事实在 PG（journal/outbox）；向量库、Harness session、上游 local job 不能接管。
5. **幂等与租约**：HTTP `Idempotency-Key` 去重请求；`effect_id` 跨 Worker 不变；UNKNOWN 先对账，禁止为「无人值守」重复副作用。
6. **证据三分离**：hash 完整性、可信来源、验收正确性分开；工件目录行 ≠ PASS 裁决。
7. **配置不可变**：策略/模型/验证配置用版本行 + DB 拒绝 UPDATE/DELETE；`BLOCKED` 信任态不接受新配置。
8. **失败关闭**：缺 JWT/库/分页密钥/仓库别名/对象仓时接口 503，禁止开发万能身份或空实现绕过。
9. **契约单向**：`doc/contracts` → 包内/根 `contracts` 逐字节同步 → OpenAPI → TS 客户端。禁止从 TS 反推 Python 模型。
10. **TDD 边界**：新 API/不变量先写失败的行为测试再实现，默认使用 E2E；孤立测试必须先满足 §0 的故障模式准入。集成/E2E 用独立测试库和测试桶，永不碰生产或其他业务库。

## 5. 常用命令

```bash
uv sync --frozen --python 3.12
pnpm install --frozen-lockfile
uv run python scripts/test_local.py          # 专用容器 ringharness-development-pg / -s3 + .runtime/*.env
uv run alembic upgrade head && uv run pytest -q   # 已自备 RING_*_DATABASE_URL / S3 时
pnpm test && pnpm build
uv run python scripts/generate_contracts.py  # 后检查 contracts 与 generated.ts 无漂移
uv run ruff check apps/control packages tests scripts migrations
python3 doc/tools/check_spec.py --verify-freeze
uv run python scripts/serve_local.py         # API 127.0.0.1:58101
pnpm --filter @ring/web dev --port 58102 --strictPort
```

**批次收工自检（推送前跑一次，替代凭记忆逐条敲）**：

```bash
bash scripts/check_batch.sh                  # 一次跑齐门禁（ruff / 单测 / **Temporal 编排测试** / 契约幂等 / 冻结 / 前端三件套）
bash scripts/check_batch.sh --tests tests/integration/test_x.py   # 加跑指定集成测试
bash scripts/check_batch.sh --skip-frontend  # 纯 Python 批次
bash scripts/check_batch.sh --fix            # 自动重生成契约 / 重算冻结包
bash scripts/check_batch.sh --live           # 额外加跑 live（--require-live --isolated-db；烧配额，不默认）
```

它会自动定位共享树并补 `.runtime` / `node_modules`（隔离 worktree 缺前者 ⇒ live 用例**静默 skip**；
缺后者 ⇒ `tsx` 退出 254 ⇒ **假红**）；venv 缺失时**失败关闭**，不伪装成代码红。

**`--live`（可选，第 7 步）**：加跑 `run_live_suite.sh --require-live` ——
`foundation.yml` 不设 chat env ⇒ 7 个 `*.live.test.ts` **静默 skip**，
「全绿」里它们一次都没跑（本会话的 live 通路回归正是这样躲过 CI）。
该开关把「跳过」判为失败。**默认不开**：需真实凭据、烧配额、并与他人 live 争用。

**2026-09-13 补第 2b 步：`pytest tests/temporal`**。此前门禁只跑 `tests/unit`，
而 `tests/temporal/**`（29 文件）**完全不在门禁内** —— 它是编排面（workflows / activities /
演变门 / 恢复语义）的主测试目录。实测后果：一个 `NameError: name 'Path' is not defined`
在该目录**潜伏整整一轮无人发现**（单跑该文件才暴露）。
判据：真失败 ⇒ ❌；**全 skip ⇒ ❌ 并标注「无证据」**（本仓「skip ≠ pass」口径）。
退出码非 0 表示有门未过。**注意**：它不代替 CI，也不跑全量 `pytest`（本地全量不可信，见 §7.1 规则 16）。

本机身份需显式配置 `RING_JWT_PUBLIC_KEY` / `RING_JWT_ISSUER` / `RING_JWT_AUDIENCE`；列表分页需 ≥32 字节 `RING_CURSOR_SECRET`；仓库别名在 `RING_REPOSITORY_REFS`；对象仓为 `RING_S3_*`。私钥、库密码、token 不得写入前端或提交进库（`.runtime/` 已 gitignore）。

## 6. 语言与可读性（本仓库约定）

为方便人类审阅，本仓库统一：

| 类别 | 约定 |
|---|---|
| 与用户/agent 交流、本文件、新增说明文档 | **简体中文** |
| 代码注释（含模块 docstring、关键逻辑旁注） | **简体中文**；说明「为什么 / 边界」，不复述标识符字面意思 |
| 用户可见错误提示 `error.message`、前端提示文案 | **简体中文** |
| 协议标识：HTTP path、JSON 字段、`error.code`、表名、环境变量、Python/TS 类型与函数名 | **保持英文**（与冻结 OpenAPI / Content Schema / 生成客户端一致） |
| 测试名、提交说明 | 可用中文描述意图；测试函数名可用英文 snake_case 以贴合 pytest 习惯 |

不要把类名、函数名、字段改成拼音或汉字标识符——会与 `doc/05`、生成契约和上游适配层断裂。需要可读性时，用中文注释和中文 `message` 表达。

已有英文注释在改到该文件时顺手改成中文；不必为「统一语言」做整库无行为重命名。

## 7. 当前推进顺序（v0.6 M0→M5，取代 W01→W07）

开发入口与运行编排权威见 [doc/engineering/当前开发方案.md](doc/engineering/当前开发方案.md)：Temporal 接管持久编排，Kernel 保留业务裁决，顺序为 [v0.6/01](doc/v0.6/01-开发文档.md) §7 的 M0–M5。**新工作不得继续给旧 PG dispatcher（全局 claim/READY 扫描）扩功能**；LEGACY 路径仅供存量 Goal 排空。

W01→W07 是 v0.5 时代的历史顺序（OIDC/领域校验 → Goal/Task/Activity → Harness 受控执行 → 验收屏障与 UI → T/AT/E 与 100h），其未完成项全部并入 M 轨道继续，不删减。

未实现能力保持禁用或显式 pending；ready 范围文案保持 `project-config-api-only` 一类诚实 scope，直到调度真正就绪。

### 7.1 四方联合开发约定（Hermes 编排验收 · Cursor 主开发 · OpenCode 辅 · Codex 收敛）

经 GitHub Issue / PR / worktree 协作；**禁止多方共写同一 worktree 的同一文件**。全局 persona **不**覆盖本仓库 [AGENTS.md](AGENTS.md) / `doc/` 不变量。权威协同公告见 [doc/engineering/四方联合开发.md](doc/engineering/四方联合开发.md) 与 [Issue #12](https://github.com/Cz07cring/ringharness/issues/12)；若与本表冲突，以 Issue / 四方文档 §7 为准并在收口批次并入本文。

| 参与方 | 身份 | 主职责 | 禁止 |
|---|---|---|---|
| **Hermes** | **主力开发（编排面）+ 立项裁定** | 与 Cursor 并行开发；持有 `packages/orchestration/**`、`tests/temporal/**`、`doc/engineering/**`、`AGENTS.md`；并负责批次立项/优先级闸门、路径裁定、冲突仲裁、红线否决 | **不验收自己写的代码**（交由 Codex）；不改 DONE 语义；不手改 `generated.ts`；不越入 Cursor 持有的路径 |
| **Cursor** | **主力开发（Kernel/Harness 面）+ 集成合并** | `apps/**`、`packages/control_kernel/**`、其余 `packages/*`；主功能、Runner/Harness 接线、集成、合并执行、进度闸门 | 不得自判 done；不得另立调度权威；不越入 Hermes 持有的 `packages/orchestration/**` |
| **Codex** | **独立验收 + 红队审计** | **验证双方的全部业务产物**（按 AB 台账逐条给结论）；假 DONE / 重复副作用 / 恢复正确性 / 三权隔离的红队审计；PR 合并门禁；Temporal 恢复语义审计 | 改业务码须先认领 `owned_paths`；用 Workflow COMPLETED 冒充 Goal DONE |
| **OpenCode** | 探针与诊断 | `scripts/probe_*`、只读诊断、复现与日志、窄范围红绿单测 | 改业务码须书面分派；不改 DONE 语义；不与他人并行全量库测 |

**三权分立（2026-09-12 调整，用户裁定）**：开发方 = Hermes + Cursor（双主力，按路径隔离）；**验收方 = Codex（独立，不参与被验收对象的开发）**；探针 = OpenCode。
**谁写谁不验**：Hermes 与 Cursor 均不得验收自己写的代码；Hermes 保留**红线否决权**（违反 DONE/幂等/三权/失败关闭等不变量时可阻断合并）——否决权是守门，不是验收。
**路径硬边界**：`packages/orchestration/**` 与 `tests/temporal/**` 归 Hermes（原 Codex 持有，因 Codex 转验收而移交）；`apps/**` 与 `packages/control_kernel/**` 归 Cursor。任一方需越界改对方路径，先在 Issue 认领并等他方明确释放。


**批次认领字段**（写在 Issue 正文或进度文表格，缺一不可）：

| 字段 | 含义 |
|---|---|
| `owner` | Hermes / Cursor / OpenCode / Codex |
| `batch` | 批次号（如第八十六批 / 辅助批 B / codex-b086 / hermes-c03） |
| `owned_paths` | 本批可改路径 glob（越权先停） |
| `depends_on` | 依赖的已合并 PR / 批次（宜写 commit 短 SHA） |
| `verification_owner` | 谁跑验证（默认同 owner；全量 `test_local` 须问 Cursor） |
| `reviewed_by` | 审计方（Codex 或对位方） |
| `status` | claimed / in_progress / blocked / pr_open / done |
| `PR` | GitHub PR 链接（有则填） |
| `merged_by` | 谁执行合并（默认 Cursor） |

**批次内容字段**（合并 Cursor 模式卡片、Codex 附录 §4.1、Hermes v2 —— **只此一份规范清单**；原三份清单作废，各自条目已并入下表）：

| 字段 | 含义 |
|---|---|
| `pattern` | 本批采用哪个模式（DAG / Agent Loop / Handoff / Reflection / 固定流程）及**为什么** |
| `invariants` | 不得被实现绕过的约束（**含副作用咽喉路径**；PLAN 须为空） |
| `authority` | 该状态归谁：Workflow / Harness / Kernel / Broker / Auditor |
| `done_authority` | 本批**绝不**写 Goal/Task DONE 的证明方式（测名或断言） |
| `non_goals` | 本批明确**不宣称**完成的能力 |
| `seam` | 可独立红绿的验收缝（E2E/集成/live；孤立测试须先过 §0 准入）；禁止「顺手绿一片」 |
| `crash_points` | 实测的中断点（调用前 / prepare 后 / 外部成功回执前 …） |
| `evidence` | 测试、receipt、artifact、history、日志的**精确位置**（路径/用例名） |
| `residual_risk` | 未测的外部依赖、真实设备/模型、长时间运行边界 |

**流程硬规则：**

1. 共享账本：[doc/implementation/进度与验证.md](doc/implementation/进度与验证.md) + GitHub Issue；**先认领再动工**，收工补「本批验证」。
2. **优先级闸门**：Hermes 立项与排期；Cursor 执行合并。Temporal 接入与恢复 > 新业务面（EXECUTE/UI/Verifier 产品化）。禁止 Workflow COMPLETED / 模型成功冒充 Goal DONE。
3. **路径隔离（2026-09-12 更新）**：`packages/orchestration/**` + `tests/temporal/**` 归 **Hermes**；`apps/**` + `packages/control_kernel/**` + 其余 `packages/*` 归 **Cursor**；OpenCode 可写仅探针与只读诊断；Codex 转独立验收，改业务码须先认领。**任一方不得改对方在途路径**（越界先停并公告）。
4. **同一时刻只允许一方**跑全量 `uv run pytest -q` / `scripts/test_local.py`（共用开发容器库与运行中的 Temporal）；默认 Cursor；Hermes 跑全量前须先在 Issue 公告并等让位。
5. 契约单向：触及 API/模型的一方跑 `generate_contracts.py`，确认无 drift；`generated.ts` 禁止手改。
6. 每批一 commit（或一 PR），message 带批次号；`.runtime/`、v0.5 冻结字节与 `doc/releases/` 任何一方不得改。
7. 本机 Qwen（`omlx-flashnext:8001`）Cursor / OpenCode 共用；并发打满时 Cursor 优先保留 probe / Harness live 配额。
8. **合并与验收**：`merged_by`（默认 Cursor）按 Hermes 裁定的顺序合并；**业务码的独立验收归 Codex**（按 AB 台账逐条给结论；含三权隔离、假 DONE、重复副作用、恢复正确性）；Hermes 保留**红线否决权**但不验收自己或 Cursor 的代码。**谁写谁不验**。
9. **模式优先切片**（吸收 [《AI Agent 开发实战》](https://waylandz.com/ai-agent-book/%E5%89%8D%E8%A8%80/)）：每批认领须带**批次内容字段**（见上表 9 条，原「模式卡片」七字段已并入）；验收四问见 [开发流程增强-模式优先切片](doc/engineering/开发流程增强-模式优先切片.md)，Hermes 增补第五问「恢复与幂等是否成立」及七道流程门见 [开发流程增强 v2-全书吸收](doc/engineering/开发流程增强v2-全书吸收.md)。**禁止**在本仓 Control/`runActivation` 自建完整多轮 Agent Loop；循环归 Harness，副作用归 Effect Gateway（见 [集成架构](doc/engineering/Harness%20Effect%20Gateway%20集成架构.md)），DONE 只归 Kernel。
10. **批次自证必须同时贴 `pnpm run build` 与 `pnpm test` 输出**：vitest **不做类型检查**，`tsc -p`（build）才暴露类型错（2026-09-12 两次 main 红均由此而来）。只贴 vitest 视为证据不全。
11. **共享 worktree 中禁止裸 `git commit`**：本仓多方可写同一工作目录时，他人可能已把在途文件 `git add` 到索引；裸 `git commit` 会把他人的未完成改动扫进你的批次，且**其暂存版本可能落后于工作树**，导致 main 因 lint/类型错误变红。**必须显式指定路径**：
    `git add <你的文件> && git commit -- <你的文件>`（或 `git commit <path>`）。
    同理：**改完立即提交**，未提交的改动会被并发写入覆盖（2026-09-12 同一裁定被覆盖两次）。
12. **宣布 done 的前置是全量绿**：`uv run pytest -q` 与 `pnpm run build` 均通过。只跑相关文件不构成证据——当日累计 7 次「局部绿、main 红」。
13. **证据用词限制**（采纳 [研究附录采纳裁定](doc/engineering/研究附录采纳裁定.md) §3）：
    `observed`＝某日志/API/状态已观察，未证明整体正确；`verified`＝指定场景按固定输入与可复现方式通过；`accepted`＝已被可靠持久层受理；`completed`＝局部执行单元完成，**不代表** Task/Goal DONE；`delivered`＝交付成功**须有 ack**；`done`＝**只能是** Kernel 在固定 VerificationProfile 与最终屏障上的业务裁决。
    **禁令**：`accepted`/`completed`/`delivered` **不得**替代 `done`；`verified` 不得用于未经独立复跑的自报结果。
14. **七条"不宣称"门禁**（采纳附录 §6，今日生效）：
    ① 上游官方 AgentLoop 未真实调用前，不宣称"Harness 接入完成"；
    ② ToolResult 未回到下一轮模型前，不宣称"多轮工具循环完成"；
    ③ 独立 Runner/Broker 身份下未完成 `PREPARED → DISPATCHED → SUCCEEDED + evidence` 前，不宣称"受控工具 live 通路完成"；
    ④ prepare/receipt 窗口 crash 后无法定位原 `logical_step_id`/`effect_id` 时，**不开启自动恢复副作用**；
    ⑤ `UNKNOWN`、缺 evidence、未确认 Stop 或隔离资源存在时，**不开启后续工具准入或最终屏障**；
    ⑥ 未完成调用级并发分类前，保持同 activation/workspace 工具串行；
    ⑦ 并发实现**不得先于**串行闭环、恢复不重复副作用与 typed timeout 的证据。
15. **AB 验收场景台账**：AB01–AB09 见 [研究附录采纳裁定](doc/engineering/研究附录采纳裁定.md) §4（唯一台账，逐条挂状态与证据位置）。**顺序硬约束**：AB07/工具并发不得先于 AB01（真实闭环）与 AB02/AB03（恢复不重复副作用）。
16. **本地全量绿当前不可信（2026-09-12 实测）**：同一命令、同一环境、只换代码，两次全量的失败集**完全不同**——本地 HEAD 失败 `test_broker_read_file` + 3× `test_recovery_safety_gate`；`origin/main` 失败 `test_tm09_plan_tools` + 2× compose。根因是**共用开发库残留状态 + 本机常驻 Temporal（:7233）**，CI 用临时库故为绿。
    → **正式门禁以 CI 为准**；本地全量结果**不得**单独作为 done/退回依据。本地发现失败时，先在 `origin/main` 复跑同一用例判定是回归还是不稳定；报失败时必须附「两次运行的失败集对比」。
17. **消费一个字段/权威/旋钮前，必须找到它的写入方或调用者（2026-09-12 教训，两方独立命中）**：字段有定义、有初值、语义清楚，**不等于**有东西在写它；配置项有默认值、有上界，**不等于**有东西在设它。
    - Codex 在 #19 复核中查出：`elapsed_wall_seconds` 有字段、有初值 0、语义清楚，但**全仓无任何推进路径** → 编排层接入后恒得「全额预算」，PR 声称「收口」却**零行为效果**。
    - Hermes 在 c28 自查中命中同族：`activation_budget_seconds` 已实现、工作流也读取它、历史级测试证明它进得了派发命令 —— 但既**不在启动载荷透传白名单**里（调用方传了也**静默丢弃**），**全仓也无任何 setter**。三条测试全绿仍「不可达」。
    - 同族第三次：`RING_LEASE_TTL_SECONDS`（默认 90，上界 600）**可配但全链路无人设置**，生效值恒为默认。
    **做法**：声称「已支持/已收口」前，`grep` **写入方与调用方**（不是定义处）；新增旋钮须同时登记全部透传层（本仓为 **调用方 → 载荷白名单 → 工作流体 → 派发命令属性** 四层，每层都会静默丢弃），并为白名单加**伞形往返测试**；找不到写入方时，结论只能是「**未接通**」，不是「已完成」。

**当前活跃认领（2026-09-12，对齐 Issue #12）：**

| owner | batch | owned_paths | depends_on | verification_owner | status | PR |
|---|---|---|---|---|---|---|
| Hermes | hermes-c03 | `doc/engineering/**`、进度文编排/验收段 | — | Hermes | in_progress | #12 |
| Cursor | 第一百一十四批 | `packages/orchestration/**/temporal_workflows.py`、`client.py`、`tests/temporal/test_recovery_safety_gate.py` | `e26f074` | Cursor | done | — |
| Cursor | 第一百一十三批 | `packages/control_kernel/**/effects.py`、`storage/stops.py`、`tests/integration/test_effects.py`、`tests/integration/test_stops.py` | `e5b63e8` | Cursor | done | — |
| Cursor | 第一百一十二批 | `packages/control_kernel/**/tool_capability_manifest*`、`storage/effects.py`、`apps/control/**/effects.py`、`tests/**/test_tool_capability*`、`tests/integration/test_effects.py` | `d04f823` | Cursor | done | — |
| Cursor | 第一百一十一批 | `packages/control_kernel/**/handoff_envelope*`、`storage/orchestration.py`、`tests/unit/test_handoff_envelope.py` | `e202899` | Cursor | done | — |
| Cursor | 第一百一十批 | `packages/control_kernel/**/plan_static_validator*`、`storage/plans.py`、`tests/unit/test_plan_static_validator.py` | `23bba06` | Cursor | done | — |
| Cursor | 第一百零九批 | `apps/runner/src/harness/activationGuards*`、`cordis*Bridge*`、`temporal/runActivation*` | `8454dad` | Cursor | done | — |
| Cursor | 第一百零八批 | `apps/runner/src/harness/noProgressGuard*`、`brokerBackedHarnessTool*` | `1db426f` | Cursor | done | — |
| Cursor | 第一百零七批 | `apps/runner/src/harness/activationWatchdog*`、`cordisLlmBridge*`、`cordisExecuteBridge*`、`llmStreamWatchdog*` | `9c5536a` | Cursor | done | — |
| Cursor | 第一百零六批 | `apps/runner/src/harness/activationWatchdog*`、`brokerBackedHarnessTool*` | `30a55fd` | Cursor | done | — |
| Cursor | 第一百零五批 | `apps/runner/src/harness/toolResultEnvelope*`、`brokerBackedHarnessTool*` | `9d7e7d1` | Cursor | done | — |
| Cursor | 第一百零四批 | `packages/control_kernel/**/probe.py`、`packages/orchestration/**/temporal_workflows.py`、`tests/integration/test_cloud_mode_dispatch.py` | `a25061a` | Cursor | done | — |
| Cursor | 第一百批 | `packages/control_kernel/**/claims.py`、`stops.py`、`tests/integration/test_lease_recovery.py` | `7d8c4ed` | Cursor | done | — |
| Cursor | 第九十九批 | `packages/control_kernel/**/claims.py`、`reconcile.py`、`stops.py`、`tests/integration/test_execute_lease_unknown.py` | `7379dbe` | Cursor | done | — |
| Cursor | 第九十八批 | `apps/runner/src/harness/brokerBackedHarnessTool*` | `0d1fa7b` | Cursor | done | — |
| Cursor | 第九十七批 | `apps/runner/src/harness/cordisExecuteBridge*` | `ea86e0a` | Cursor | done | — |
| Cursor | 第九十六批 | `apps/runner/src/temporal/runActivation*`、`cordisExecuteBridge*` | `cb7af34` | Cursor | done | — |
| Cursor | 第九十五批 | `apps/runner/src/temporal/runActivation*`、`cordisExecuteBridge*` | `eb8f732` | Cursor | done | — |
| Cursor | 第九十四批 | `apps/runner/src/temporal/runActivation*` | `fe4821b` | Cursor | done | — |
| Cursor | 第九十三批 | `packages/orchestration/**`、`apps/workflow-worker/**`、`tests/temporal/test_m3_*` | `030afaa`（#9） | Cursor | done | — |
| Cursor | 第九十二批 | `apps/runner/src/harness/artifactPutHttpPorts*`、`executeToolHost*` | `bd61206` | Cursor | done | — |
| Cursor | 第八十五–九十一批（收口） | `apps/runner/src/harness/**` | PR #11 / `9dc1efe` | Cursor | done | — |
| OpenCode | Issue #7 | 只读诊断 + 可选 `scripts/probe_*` | 第84批租约修复 | OpenCode | claimed | #7 |
| Codex | b086 | `packages/orchestration/**`、`tests/temporal/**`（独立 worktree） | `c05997e` | Codex 窄测 + Cursor 集成 | done | #9 → `030afaa` |

## 8. 完成标准（改完自检）

- [ ] 行为与 `doc/01`/`doc/05` 不变量一致；未把未实现路由写成已覆盖
- [ ] 相关测试通过；缺外部依赖时是明确 skip，不假装绿。新增孤立测试须附 §0「测试准入与清理」三项记录
- [ ] 若触及 API/协议：`generate_contracts.py` 后无意外 drift；需要时跑冻结校验
- [ ] 新注释与 `error.message` 为中文；协议级英文标识未被改写
- [ ] 未把密钥写入仓库；未用生产库或共用业务容器做实验

## 9. Harness 与 chat 后端（本机 Qwen / DeepSeek 覆盖）

真实 Harness 接入仍属 W03/M2 待实现；live / 模型探测默认走 OpenAI 兼容 chat 连接器（环境变量历史名仍为 `RING_LOCAL_QWEN_*`）。

- **本机 Qwen（默认）**：PM2 `omlx-flashnext`（`127.0.0.1:8001`），由 `.runtime/qwen.env` 注入。OpenCode 开发也常用同一端口。
- **与 OpenCode 争用时**：Cursor / ringharness live **改走 DeepSeek**，写 gitignore 的 `.runtime/chat.env`（覆盖 `RING_LOCAL_QWEN_BASE/API_KEY/MODEL`）。推荐从 Hermes `~/.hermes/profiles/coder/.env` 取 `DEEPSEEK_API_KEY`：
  ```bash
  export DEEPSEEK_API_KEY=...   # 或直接依赖已写好的 .runtime/chat.env
  uv run python scripts/use_deepseek_chat.py
  uv run python scripts/probe_local_qwen.py   # 应显示 provider=deepseek
  ```
  恢复本机 Qwen：删除 `.runtime/chat.env` 后重启 `serve_local`。模板见 `deploy/local/chat.env.example`。
- `scripts/serve_local.py` / `test_local.py`：合并顺序为 **进程显式 export > chat.env > qwen.env**（显式环境不被 dotenv 压过）。
- 本机 MLX 慢：`RING_LOCAL_QWEN_TIMEOUT` 默认 **180s**；DeepSeek 覆盖时常用 **120s**。GoalWorkflow `RunActivation` `start_to_close` 为 **360s**。
- 现场 model id：本机以探针为准；DeepSeek 当前探针常见 `deepseek-flash` / `deepseek-v4-pro`（写入 `ModelProfileCreate.model_id` / `RING_LOCAL_QWEN_MODEL`，禁止写死进协议）。
- **云闸门（失败关闭）**：dispatch 外呼前取更严者（`RING_CLOUD_MODE` × `ModelProfile.cloud_mode`）。`DENY` 只允许 loopback；远程须 `PREAUTHORIZED` 且 `provider_ref ∈ cloud_provider_refs`（开发 DeepSeek 常用 `deepseek:api`）。连接器 URL ≠ 合同授权；`chat.env` 写入 `RING_CLOUD_MODE=PREAUTHORIZED` 只开部署层，Profile 仍须同步允许。
- Probe 必须实测 model_id；activation 身份 / 会话 / 工具集 / 工作区隔离见 `doc/01`。
- adapter 未落地前，不为「能连上模型」单独加假调度或绕过 Kernel/Broker 的捷径。
