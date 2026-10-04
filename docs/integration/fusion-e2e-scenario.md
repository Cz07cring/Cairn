# Cairn × Ring 第一条融合 E2E：隔离订单幂等修复

状态：**固定场景与最终验收方案；融合 E2E 未运行，复跑脚本未实现**。编写日期：2026-10-04（Asia/Shanghai）。本文件只规定输入、门禁、观测和工件，不报告验收通过。唯一认领路径是本文件；Ring 源码、fixture、测试及脚本均未改动。

## 1. 场景与固定输入

在隔离 Git 仓库中修复 `order_service/order_service/store.py` 的重复建单缺陷。业务调用输入取自 Ring `tests/fixtures/business_e2e/order_service/FIXED_INPUT.json`：`sku=SKU-001`、`quantity=2`、`unit_price="19.90"`、`idempotency_key="checkout-20260912-001"`。初始库存为 10。相同 key 顺序或并发调用两次，应只产生一张订单并使库存为 8；不同 key 两次应产生两张订单并使库存为 6。订单 ID 本身是随机 UUID，只比较同 key 两次的 ID 相等，不固定其字节值。

这是一项**软件修复**，没有交易所、资金或生产订单。fixture 的 `app.py` 是 Python 调用门面，没有 HTTP 服务。公开检查在 `tests/`；并发和不同 key 的受保护检查在 `tests_hidden/`。独立 Auditor 持有后者，Executor 工作区不得包含 `tests_hidden/` 或 `reference_fix/`。`reference_fix/store.py` 只用于 fixture 自检，不作为 Executor 提示或融合验收结果。最终验收须由真实 Broker/验证器在隔离工作区执行公开和受保护检查；单独运行 fixture 的 pytest 文件不构成融合 E2E。

`scripts/seed_business_e2e.py --run-id <id>` 会在 `.runtime/e2e/<id>/` 建立 bare `source.git`、两个 detached worktree 和 `artifacts/run-summary.json`。脚本复制公开源码、公开检查、`pyproject.toml` 与固定输入，排除受保护检查和参考修复；随后仅向 Auditor worktree 加入受保护检查。默认 seed 会删除同名 run 目录后重建，因此每次用全新 `run_id`，不要对待保全的失败现场重跑 seed。seed 记录 `scenario=order_idempotency`、`phase=E2E-0`、`harness_required=true`、`marks_goal_done=false`；它既不创建 Goal，也不启动 Temporal 或 Runner。不得使用 `--apply-reference-fix` 跑正式场景。

### 固定版本记录

本次**只读观察**：Cairn HEAD `3f8617514d7c90b791fcb58a692a7ddf87d51767`；Ring HEAD `f61df68049c75ec5bd6bd279f51b196e89722031`。Ring 工作树存在大量在途改动，HEAD 不能代表全部当前字节。固定输入文件 SHA-256 为 `e27f9474c4aaa13f7b4d8319ec00db60652031b5d3bd2d4d9901ecd3f3fbd81e`；seed 脚本为 `21da616feb4441c347f9796b178e9d1da15b03341d1936d88f8c717262419e49`；业务运行脚本为 `8bd8da21ea48c03643a6c1ea00cfae841d0794b23d41d9da407be3c93c367c70`。这些 SHA 是源文件字节摘要，不是一次 E2E 工件。

正式运行在**运行开始前**记录两仓完整 commit、`git status --porcelain`、涉及文件的 SHA-256、Ring/Cairn 构建工件或镜像 digest、依赖锁摘要、迁移版本、Harness pin/构建摘要、Broker 与验证器镜像 digest、模型 provider/model/profile/probe 时间、预算、随机种子、Temporal target/namespace/task queue、隔离 PG/S3 标识。若有未提交代码，先固化可复核补丁及 SHA，或换干净 checkout；只记 HEAD 不满足冻结要求。seed 后记录输出的 `initial_commit` 和 `input_digest`，再用 `git -C <executor-worktree> rev-parse HEAD` 对照。`input_digest` 是脚本对排序、紧凑 canonical JSON 的摘要；它不等于原文件字节 SHA。所有时间用 UTC ISO 8601，显示时可另附 Asia/Shanghai。

## 2. 目标、合同和配置前提

1. 在独立的 Ring project/Goal、隔离 PostgreSQL、对象仓、Temporal namespace 与工作区中运行。预登记 seed 产出的 `source.git`/`initial_commit` 为允许的仓库来源；`GoalCreate.base_commit` 与之相同。核对 `project_id`、Goal contract、`state_revision`、`contract_revision`、`contract_digest`、`plan_revision`。`POST /api/v1/goals` 只建 DRAFT，`start` 的 HTTP 202 只是命令受理。
2. Goal 的 required criterion 至少覆盖：同 key 顺序去重、并发去重、不同 key 仍能各建一单、库存分别为 8/6、补丁来源与路径合法、独立最终验证。合同里的预算、重试策略、`policy_id`、`model_profile_id`、`skill_set_id`、`base_commit` 和 `required_file:order_service/store.py` 等约束必须在运行前冻结。TaskContract 的 required acceptance、coverage、capability、预算和路径须与 Goal/Policy 一致。实际 `GoalCreate`/`PlanCreate` 以运行时固定 Ring 版本的协议为准，不能把本段当作已经注册的合同。
3. Policy 仅允许此隔离仓库所需的读、写、检查、seal 工具；Task 的读路径逐一列出 `order_service/store.py`、所需公开检查、`FIXED_INPUT.json` 与依赖文件，写操作另由 Broker 限制到 `order_service/store.py`。受保护检查、参考修复、`.git`、凭据与产物目录禁止 Executor 写入或读取。Cairn B2 桥对 Task `allowed_paths` 要求规范相对**精确文件**，不能把 Ring 旧测试中的 `order_service/**` 直接作为 Cairn PlanCreate；Policy 的允许规则、Task 精确路径及 Broker 实际读写限制三处须现场核对。网络默认禁止外发；模型如使用远端，需部署云策略和 ModelProfile 双重许可，并记录探针。不要把模型凭据写入工件。
4. 配置应有可读取的 project scoped Policy、active SkillSet/skill versions、ModelProfile、TASK VerificationProfile 和 GOAL/GLOBAL VerificationProfile，以及独立验证器的固定镜像、fixture 摘要、阈值和受保护检查。Manager/Reason 只能提出计划数据；Executor 只能执行已发布 Task；Auditor 使用独立身份和工作区；Kernel/PG 独占 Goal DONE 裁决。检查角色、凭据、lease/fencing、队列和实际进程，不能从配置默认值推断它们在线。
5. Cairn 产品模式需要 `CAIRN_PRODUCT_MODE=ring`、安全的 `CAIRN_RING_BASE_URL`/`CAIRN_PUBLIC_ORIGIN`、同源代理下的 Ring `ring_session`/CSRF、Cairn owner ACL 与 Ring project scope。新建 Cairn project 时 `bootstrap_enabled=false`，确认旧 Cairn dispatcher/CLI 进程已停，再绑定已存在且同 project 的 Ring Goal。绑定不创建/启动 Goal。浏览器不持有 Ring service key；状态失联显示 `UNKNOWN`。Cairn 自动零工具 Reason、从 `CANDIDATE` 到受控 `PUBLISHED` 的采纳、证据回流 Fact、完成提议目前仍需接通并经契约探针确认；缺任一项，正式融合 E2E 停在前置门禁，不能绕过来宣布通过。

## 3. 当前 Cairn 图 API 与纵向流程

当前源码路由无 `/api` 前缀。`POST /projects` 接受 `title/origin/goal/bootstrap_enabled/hints`，写入 `origin` 与 `goal` Fact；`GET /projects/{id}` 返回项目、Facts、Intents、Hints。`POST /projects/{id}/ring-binding` 以 Ring project/Goal UUID 建唯一绑定；`GET /projects/{id}/ring-status` 即时读 Ring Goal/snapshot，只有 Ring Goal `DONE` 且匹配、有效的 ReleaseManifest 可读时才显示 DONE。`POST /projects/{id}/intents` 在绑定项目仅接受未 claim 的提议（`worker=null`）。`GET /projects/{id}/plan-context` 返回图摘要、Goal 合同版本/摘要、计划版本、标准和预算。`POST /projects/{id}/intents/{intent_id}/plan-candidate` 要求 operator、完整 `PlanCreate`、来源 Fact、图/合同版本、路径/能力/预算/coverage；回 `CANDIDATE` 仍未发布。`GET /projects/{id}/plan-candidates` 与同 Intent 的 `/reconcile` 用于观察和原键核对。绑定项目现有 `complete`、`reopen`、status、worker claim/conclude 路径被拒绝。

验收流程固定为：浏览器建图并绑定 Goal → 图中产生**至少两个不同**的修复方向 Intent → operator 选择一个、提交完整候选 → 可信 PLAN 路径发布一个 Task → Temporal 交付 Activity，持久 Runner/Executor 经 Broker 修复隔离 worktree → Broker 的效果回执和 EvidenceLedger 封存 CandidateManifest → 独立 Auditor 执行公开及受保护检查 → Ring 集成/最终屏障/ReleaseManifest → Cairn 按来源与版本回写 Fact 并再做一轮 Reason → 浏览器回读 Ring DONE。未接通的步骤不能以人工写 SQLite、直接调 Broker、使用参考修复、或在页面手填“完成”替代；人工完整 PlanCreate 候选可作为现阶段 B2 观察，但不计自动融合通过。

## 4. 最终断言矩阵（全部为待运行）

每行保存原始请求/响应或命令输出、权威记录 ID、时间、断言结果和对应工件路径。矩阵中的 `passed` 仅在该行证据齐全时填写。

| 层 | 正向断言与权威证据 | 失败/恢复断言 |
|---|---|---|
| 浏览器与身份 | 真实浏览器操作建图、至少两个 Intent、选中一个；截图/trace 与 `GET /projects/{id}` 对上图 ID。绑定后只展示权威 Ring 状态。 | 跨用户/跨 project 404 或 403；失联标 `UNKNOWN`；无有效 Release 时不显示 DONE；绑定项目的 Cairn 完成/重开旁路被拒。 |
| Cairn 图/API | `ring-binding` 唯一；`plan-context` 的图/合同摘要与请求一致；一个 Intent 原键只见一个候选，另一个保持提议。 | 图或合同变更返回冲突；候选回包丢失后同一 operator 用同一请求/键对账，不换键重投。 |
| Ring Control/PG | Goal、Plan `CANDIDATE→PUBLISHED`、Task、Activity、命令的 ID/状态/版本与授权 API、隔离 PG 同向；最终 Goal DONE 有匹配 ReleaseManifest。 | 202、CANDIDATE、Task DONE、模型文本均不能单独触发 Goal DONE；版本冲突和未决义务须阻断。 |
| Temporal | 固定 namespace/task queue 的 workflow/history 显示 PLAN、EXECUTE、AUDIT、FINALIZE 活动与对应 PG ID；重放不重复外部操作。 | worker 停启和超时后的 history/重试可追溯；只见进程启动不算 poller 在线。 |
| Runner/三权 | 精确持久进程、近期 task queue poll、lease/heartbeat/fencing、Manager/Executor/Auditor 身份与工作区隔离；实际工具流经 Broker。 | 失租后旧 owner 不能继续提交；Runner 死亡后新 owner 可接管而不扩张权限。 |
| Broker/Effect Gateway | 修复写入、检查、seal 各有固定 `effect_id`、operation identity、可信 receipt；实际文件 diff 仅在允许路径，执行次数与 receipt 对上。 | 回包丢失/迟到回执保持原键查询；`UNKNOWN` 未对账前不可重发写入或跨最终屏障；禁止路径与网络动作被拒。 |
| Ledger/Candidate | EvidenceEnvelope、内容摘要、对象仓字节、CandidateManifest、基线与修复 commit/diff 互相对应；公开与受保护检查输出来源分离。 | 工件不可读/摘要不符、受保护内容进入 Executor、仅有模型总结时均不能标可信 Fact。 |
| 独立 Auditor | 独立身份调用固定验证器，顺序同 key 为一单库存 8、并发同 key 为一单库存 8、不同 key 为两单库存 6；审计及 GOAL/GLOBAL 判定可回查。 | 审计失败、缺测、基础设施错误均阻断完成；不得用 Executor 自测替代独立审计。 |
| 回流与 Reason | Cairn Fact 具有 Ring 来源 ID、digest、信任状态、版本和去重键；再一轮 Reason 引用新 Fact，UI 可点到授权证据。 | 乱序/重复 SSE 不倒退 revision/seq、不重复 Fact；断流或 410 后 snapshot 重建；权限撤销隐藏证据。 |
| 最终屏障/恢复 | PG 中工程 effect 排空、`unknown_effects=0`、标准满足、独立审计通过、barrier `RELEASED`、ReleaseManifest 有效；Cairn `ring-status` 与之同向。 | 审计失败、UNKNOWN、屏障竞争、进程重启、回包丢失、重复投递各注入一次；无假 DONE、无重复副作用，不能确定时保持 BLOCKED/UNKNOWN。 |

以上需要隔离环境的真实注入和权威对账。一次成功修复不证明长跑稳定性；长跑、对照收益、生产可用性分别给独立结果。

## 5. 原始输出与 SHA-256 工件契约

正式 runner 每次创建全新 `artifacts/fusion/<run_id>/`；失败时保留目录、隔离库和工作区，只按明确保留策略清理。建议结构如下；这里没有生成任何运行工件：

```text
artifacts/fusion/<run_id>/
  manifest.json                 # 固定输入/版本/配置摘要/环境边界/UTC 时间/ID 映射
  commands.txt                  # 按执行顺序的完整命令和退出码；密钥仅写变量名
  results.json                  # 每行层级及负例的 passed/failed/skipped/not-run
  raw/browser/{trace.zip,screenshots/,console.txt,network.har}
  raw/api/{cairn.ndjson,ring.ndjson}
  raw/pg/{queries.sql,rows.ndjson,migration.txt}
  raw/temporal/{history.json,poller.txt}
  raw/runner/{process.txt,logs.txt,leases.ndjson}
  raw/broker/{effects.ndjson,receipts.ndjson,workspace.diff}
  raw/ledger/{evidence.ndjson,candidate.json,object-digests.txt}
  raw/auditor/{verification.json,stdout.txt,stderr.txt}
  raw/recovery/{faults.ndjson,before.json,after.json}
  SHA256SUMS                     # 除自身外，对每个实际存在文件逐一作字节 SHA-256
```

`manifest.json` 至少含 `schema_version`、`scenario`、`run_id`、`started_at_utc`、`ended_at_utc`、`cairn_commit`、`ring_commit`、`dirty_patch_sha256`、固定输入/seed/业务运行脚本三份源文件的字节 SHA、seed 的 `initial_commit/input_digest`、Harness/镜像/锁/迁移/模型配置摘要、隔离资源 ID、Cairn project/Fact/Intent ID、Ring project/Goal/Plan/Task/Activity/effect/receipt/evidence/candidate/audit/release ID、命令退出码和相对原始路径。`results.json` 每条包含 `layer`、`assertion_id`、`status`、`reason`、`evidence_paths`、`source_of_truth`。原始输出保留 HTTP 状态与 request ID、PG revision/seq、Temporal workflow/run ID、Broker 操作次数与审计阈值；逐项脱敏 cookie、Bearer、API key、密钥、受保护测试正文和模型隐藏推理。脱敏规则及脱敏前后摘要须由有权限的保管侧记录，公开工件不能泄密。SHA-256 只证明字节完整，不证明来源可信或业务正确。

## 6. 完整复跑顺序与命令草案

**以下命令未在本批执行。`scripts/run_fusion_e2e.sh` 只是拟议接口，当前不存在，不能复制后声称可运行。** 正式 runner 需实现第 3–7 步、固定资源配置、非零退出与工件归档；实施后再按真实 CLI 参数修订本节。

```bash
# 1. 在干净、隔离 checkout 固定两仓与环境；不打印 .env 或凭据。
CAIRN_ROOT=/absolute/path/to/cairn
RING_ROOT=/absolute/path/to/ringharness
RUN_ID=fusion-order-<UTC-timestamp>-<unique-suffix>
git -C "$CAIRN_ROOT" rev-parse HEAD
git -C "$RING_ROOT" rev-parse HEAD
git -C "$CAIRN_ROOT" status --porcelain
git -C "$RING_ROOT" status --porcelain
shasum -a 256 "$RING_ROOT/tests/fixtures/business_e2e/order_service/FIXED_INPUT.json" \
  "$RING_ROOT/scripts/seed_business_e2e.py" "$RING_ROOT/scripts/run_business_e2e.sh"

# 2. 固定源仓；记录 JSON 输出的 initial_commit/input_digest，不用 --apply-reference-fix。
cd "$RING_ROOT"
uv run python scripts/seed_business_e2e.py --run-id "$RUN_ID"
git -C ".runtime/e2e/$RUN_ID/executor-worktree" rev-parse HEAD

# 3. 待实现：预检隔离 PG/S3、迁移、Temporal poller、三权身份、Broker、验证器、模型。
# 4. 待实现：浏览器建图/绑定，产生两个 Intent，原键提交一个候选并受控发布。
# 5. 待实现：真实 Temporal/Runner/Broker/独立 Auditor/最终屏障；回流 Fact 再 Reason。
# 6. 待实现：逐项注入权限、断流、失租、UNKNOWN、审计失败，按权威来源对账。
# 7. 待实现：导出上述 raw、manifest/results、SHA256SUMS；任一 required skip 即非通过。
# 拟议入口（不存在）：
# bash scripts/run_fusion_e2e.sh --run-id "$RUN_ID" --seed-dir ".runtime/e2e/$RUN_ID" \
#   --cairn-url "$CAIRN_URL" --ring-url "$RING_URL" --artifacts "artifacts/fusion/$RUN_ID" \
#   --require-live --require-no-skip

# Ring 自身业务链参考命令；它不覆盖 Cairn 浏览器/图/回流/融合恢复，另存结果。
# RING_HARNESS_CHECKOUT=<pinned-built-checkout> bash scripts/run_business_e2e.sh live 1
```

现有 `run_business_e2e.sh` 的默认模式跑 `tests/e2e/` 全目录；`live` 模式只指定 `test_e2e4_run_activation_live_diagnose_seal_then_goal_done`。它从 `.runtime/*.env` 取环境、默认找 PG/S3/Temporal，并检查 `RING_HARNESS_CHECKOUT` 的 AgentLoop `lib`；每轮原始日志写 `/tmp/rbe-<i>.log`，同编号后跑可能覆盖。当前脚本按 pytest 退出码计 `pass`，退出码 0 仍可能含 skip；未生成融合 manifest/SHA256SUMS。正式融合 runner 必须复制原始日志到本次 run 目录、解析逐项 passed/failed/skipped、拒绝零通过/部分跳过/非零退出，并保留脚本自身退出码。不得把 Ring 单项 live 结果合并成融合通过。

## 7. 判定口径与当前结果

- `passed`：固定版本与输入下真实执行该断言，原始输出、权威 ID、SHA-256 均可重放核查；全场景通过还要求矩阵所有必需正向与负例断言均 passed、无 required skip，且独立复核。
- `failed`：已运行但断言不成立、命令非零、证据矛盾、摘要错误，或应失败关闭却放行。保留现场；不能用重试结果覆盖第一次失败。
- `skipped`：测试工具显式跳过并给出条件、原因和范围。前置服务缺失、模型未许可、验证器不可用属于阻断，不得算 passed；正式 `--require-no-skip` 门禁下 required skip 使本次整体失败。
- `not-run`：本次未启动该层或场景；设计、静态源码核对、seed、启动进程和旧 Ring 成绩都不改变这个状态。

**本批结果：浏览器、Cairn-Control、PG、Temporal、Runner、Broker、Ledger、Auditor、恢复和最终融合均为 `not-run`。** 只读检查确认 fixture、两个 Ring 脚本和当前 Cairn API 的源码形状；未验证部署配置、运行时合同、脚本可运行性、真实副作用或 Goal DONE。EverOS 本地健康检查返回 `ok`，但 briefing 搜索返回 HTTP 503，原因为本地 `bge-m3` 模型缺失；本文件结论据当前源码而非 EverOS 记忆。

## 源码定位

- Ring：`tests/fixtures/business_e2e/order_service/{FIXED_INPUT.json,README.md,order_service/store.py,tests/,tests_hidden/,reference_fix/}`；`scripts/seed_business_e2e.py`；`scripts/run_business_e2e.sh`；`packages/control_kernel/src/control_kernel/protocols/{goals,plans,policies,verification}.py`；`apps/control/src/control_api/routes/{claims,read_model,goals}.py`。
- Cairn：`cairn/src/cairn/server/{app.py,models.py,routers/projects.py,routers/intents.py,integration/bindings.py,integration/identity.py,integration/intent_bridge.py,integration/ring_client.py,integration/README.md}`；`docs/integration/ringharness-development-plan.md`。
