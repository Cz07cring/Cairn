# Ring product mode (B1)

`CAIRN_PRODUCT_MODE=ring` enables the Ring product entry. The default is
`standalone`, which retains Cairn's original local protocol. Any other mode
value fails startup. A database with Ring bindings cannot start in standalone
mode.

Ring mode requires:

- `CAIRN_RING_BASE_URL`: internal Ring Control API origin, for server-side reads.
- `CAIRN_PUBLIC_ORIGIN`: browser-facing Cairn origin, for strict Origin checks.
- Both origins must use HTTPS, or HTTP on loopback for local development. The
  server forwards the user's Ring session cookie to `CAIRN_RING_BASE_URL`.
- A reverse proxy that serves Cairn at `/` and Ring OIDC endpoints at
  `/api/v1/auth/*` on that same origin. Ring must allow the Cairn origin as an
  OIDC return origin and set the `ring_session` cookie for `/`.

The browser obtains a Ring OIDC session through Ring's login endpoint. Cairn
forwards only that user's `ring_session` cookie to Ring
`GET /api/v1/auth/session`, `GET /api/v1/goals/{id}`,
`GET /api/v1/goals/{id}/snapshot`, and, for `DONE`,
`GET /api/v1/goals/{id}/release`. Cairn never stores or sends a Ring service key to
the browser. All Cairn writes in Ring mode require the Ring session's CSRF
token and an exact `Origin` match.

`POST /projects/{id}/ring-binding` accepts existing Ring project and Goal UUIDs.
It checks the Ring user project scope, Goal and snapshot versions, then writes
one SQLite binding. It does not create or start a Ring Goal. An identical
binding request is idempotent; a different or duplicate Goal binding conflicts.
`GET /projects/{id}/ring-status` reads Ring on demand. A `DONE` label requires
the matching, valid ReleaseManifest. Unknown version or read failure yields
`UNKNOWN` or a fail-closed HTTP error.

Every business request first validates the current Ring `/auth/session` response.
If identity is unavailable or its contract is unknown, reads fail with 503.
Project lists and local graph reads then use the Cairn ACL and the session's
current `project_ids`; they remain available while Ring Goal or snapshot reads
are temporarily unavailable. A revoked ACL or Ring project scope hides the
project. The Ring status endpoint reports `UNKNOWN` when Goal or snapshot
versions cannot be read, and does not turn that state into `DONE`. Binding
creation and bound-project writes still require fresh Goal and snapshot checks.

The project creator owns the Cairn ACL. The owner can add or remove a viewer
with `PUT` and `DELETE /projects/{id}/members/{user_id}`; `GET` lists members.
Every bound project request checks the current user's Ring project scope and
the Cairn ACL. Writes also check fresh Goal visibility and version. Existing
standalone projects have no ACL owner and remain inaccessible in Ring mode
until an explicit migration is designed.

Binding requires an active, idle Cairn project with no unfinished Intent,
including an unclaimed Intent. Before binding, operators must also confirm
that its dispatcher has stopped and no Cairn CLI container or process is still
running. An expired SQLite worker lease alone does not prove a host process has
stopped; B1 does not provide an atomic cross-system process fence.

Ring-bound projects reject Cairn reason/worker claims, Intent worker writes,
`complete`, `reopen`, status changes and deletion. The dispatcher also skips
Ring-bound projects. B1 does not publish Intent candidates, run Ring tasks, or
reconcile Ring effects; those remain later batches of the integration plan.

## B2: unclaimed Intent and plan candidate

The project owner can add multiple unclaimed Intent proposals to a bound graph.
The server replaces the submitted `creator` with the authenticated Ring user
and requires `worker=null`. Bound projects still reject the old claim,
heartbeat, release, conclude, complete, Reason CLI, and Explore CLI paths.
Automatic zero-tool Ring Reason is not connected; the current bridge accepts a
human-authored complete `PlanCreate` JSON for one selected Intent.

`GET /projects/{id}/plan-context` returns the current graph digest, Goal
contract revision and digest, expected plan revision, criteria and budget.
`POST /projects/{id}/intents/{intent_id}/plan-candidate` requires those versions
and the full Ring `PlanCreate` body, including TaskContracts and coverage. The
server verifies source Fact IDs, current graph, owner and Ring operator access,
Goal budget, policy paths, active skill capabilities, and criterion coverage.
The server prepends a `CairnSource/v1` record to the Ring plan reason; Ring's
public API has no separate Intent provenance field. Ring validates the full
contract and stores only a `CANDIDATE`. Ring Planner/Kernel retain publication
authority; no Cairn endpoint creates a Task or invokes a Runner.

Cairn stores one immutable request body, original Ring operator, and idempotency
key per Intent before sending it. A lost or unknown response remains `UNKNOWN`
in the graph. The same Ring operator uses `POST .../plan-candidate/reconcile`
to replay that exact body with the same key. Ring scopes idempotency by operator;
a different operator cannot safely replay the request.
`GET /projects/{id}/plan-candidates` reads all pages of the Goal-scoped Ring
Plan list and shows local rejection detail and the currently visible Ring plan
status. A rejected attempt needs a new
Intent; editing a submitted Intent's request is not allowed.

## C1：不可变 PlanInput/v1 图快照

`POST /projects/{id}/plan-snapshots` 只接受本地项目 owner、当前 Ring
`operator` 和仍在 Ring project scope 内的用户。请求必须显式提供
`selected_intent_id`、`fact_ids`、`hint_ids`（可以是空数组）、
`graph_digest`、`goal_contract_revision`、`goal_contract_digest`、
`expected_plan_revision`（可为 `null`），以及必填的 `candidate_plan_id`。
请求体不接收 cookie、key、Evidence 或候选正文。Ring 会话只用于本次服务端回读。

服务端先回读 Goal 的完整分页 Plan 列表，再回读当前 Goal/snapshot。
候选必须是 B2 在同一 Intent 保存的 Plan ID，Ring 当前返回的状态必须为
`CANDIDATE`、`plan_revision=null`，且有有效 `content_digest`。
候选摘要只包含受限的 Task ID、目标、依赖及 coverage 结构；不复制 reason、
完整 TaskContract 或任何 Evidence。Ring 读取失败返回 `503`，调用方应标为
`UNKNOWN`，不得猜测成功。

SQLite `BEGIN IMMEDIATE` 事务内再次检查 ACL、绑定、项目状态、图摘要、
Intent 来源边、所选 Fact/Hint 和 Goal 版本。Fact 集必须与该 Intent 当前来源
Fact 集完全一致。Intent 必须未领取且未结束。图文字、候选摘要和规范 JSON
分别有长度上界；疑似凭据文本和超限输入直接拒绝，不裁剪。敏感文本正则
只是启发式，不能证明任意 Fact 不含秘密；录入 Fact 的人仍须避免放入凭据
或原始敏感 Evidence。规范 JSON 用
UTF-8、排序键、紧凑分隔符封存为 BLOB，并以原始字节计算 `sha256:` 摘要。
`created_at` 不参与摘要；同一用户对同一选择及版本重放会得到相同摘要。

`GET /projects/{id}/plan-snapshots/{digest}` 按当前本地 ACL 和 Ring project
scope 返回封存的**原始规范 JSON 字节**，`X-Content-Digest` 给出摘要。浏览器
在选中 Intent 的快照面板按 digest 显式回读；此 API 也供后续受控 Ring 登记和审查使用。
快照只证明 Cairn 本地图字节与来源关系，不证明 Fact 是可信 Evidence。
此批不调用 Ring PlanInput 登记接口，也不启动 PLAN、发布 Plan 或创建 Task。
Ring 侧仍须在登记与 PLAN attempt 绑定时重新核对 Goal、候选、权限和摘要。
