# Shannon 安全/隔离/治理机制研究：对 Ringharness 的借鉴清单

> 研究对象：`/tmp/shannon-study`（Kocoro-lab/Shannon，本地 clone）  
> 对照项目：`/Users/ring/Documents/code/ringharness`  
> 日期：2026-09-12  
> 方法：只读静态精读；未启动服务、未运行 Firecracker，也未执行跨租户集成测试。下文凡无运行证据均明确标作“静态确认”或“未验证”。

## 0. 结论先行

Shannon 最值得借鉴的不是“照搬 Rust/Firecracker/OPA”，而是三类机制：

1. **沙箱能力边界与负向测试模板**：WASI 默认无网络、显式 preopen 工作区、fuel/epoch/内存/输出限额；路径 canonicalize + symlink 检查 + quota。
2. **策略影子评估的运维面**：off/dry-run/enforce、稳定 canary 分桶、策略版本指标、拒绝原因与错误率/延迟指标。
3. **跨服务 W3C trace context**：Go/Rust/Python 传播 `traceparent`，配合低基数 Prometheus 指标。

但 Shannon 也提供了必须避免的反例：默认策略 `dry-run + fail-open`、未知模式放行、开发万能身份、共享 DB 超级权限、无 RLS、缓存命中绕过租户校验、附件无 tenant key、Firecracker HTTP 面未见认证、VM 输出无上限、jailer 默认关闭。它们都与 Ringharness 红线冲突。

---

# ① 逐问题发现（文件:行号 + 关键片段）

## 1. 沙箱与执行隔离

### 1.1 两级实现：WASI 是默认主路径，Firecracker 是可选路径

- `rust/agent-core/src/wasi_sandbox.rs:43-53`：Wasmtime 启用 fuel 消耗与 epoch interruption，设置 64MiB memory guard，并关闭并行编译。
- `rust/agent-core/src/wasi_sandbox.rs:63-74`：默认 `allow_env_access=false`，资源参数来自配置，并限制 table/instance/memory 数量。
- `deploy/compose/docker-compose.yml:97-105`：session workspace 默认启用 WASI；`PYTHON_EXECUTOR_MODE` 默认 `wasi`；注释表明 Firecracker 不可用会回退 WASI。
- `rust/firecracker-executor/src/vm_pool.rs:294-365`：每 VM 唯一目录、API socket、vsock UDS/CID，以及 COW rootfs 副本。
- `rust/firecracker-executor/src/vm_pool.rs:427-494`：通过 Firecracker API 配置 vCPU/内存、内核、独立可写 rootfs、每 session ext4 workspace、vsock；**未配置任何 network interface**，因此该实现静态上没有 guest egress 路径。

**判断**：WASI 是能力式进程内沙箱；Firecracker 是更强的 microVM 边界。但 Firecracker 仍是可选实现，且接入方式与运维安全尚不完整。

### 1.2 文件系统限制

- `protos/sandbox/sandbox.proto:6-29`：SandboxService 声明“所有路径相对 session workspace root”，RPC 覆盖 read/write/list/command/search/edit/delete。
- `rust/agent-core/src/workspace.rs:33-53`：session id 限长、限字符并拒绝 `..` 与点前缀。
- `rust/agent-core/src/workspace.rs:63-120`：先 canonicalize trusted base，再 join；创建前/后均验证 `starts_with(base)`；已有 workspace 若为 symlink 直接拒绝。
- `rust/agent-core/src/wasi_sandbox.rs:313-319`：普通 allowed paths 只读 preopen。
- `rust/agent-core/src/wasi_sandbox.rs:330-349`：session workspace canonicalize 后以 `/workspace` 和 `.` 映射，授予该目录内读写创建能力。
- `rust/agent-core/src/workspace.rs:183-224`：目录 size walk 最多 50,000 entries，使用 `symlink_metadata` 并跳过 symlink，避免逃逸/循环 DoS。
- `rust/agent-core/src/sandbox_service.rs:22-40`：默认单读 10MiB、session workspace 100MiB、user memory 10MiB、命令 30s。
- `rust/agent-core/src/sandbox_service.rs:310-330`：客户端 `max_bytes` 不能扩大服务端读上限。

### 1.3 路径穿越与 symlink

- `rust/agent-core/src/sandbox_service.rs:203-230`：已有路径 canonicalize 后必须位于 workspace；写不存在路径时 canonicalize parent。
- `rust/agent-core/src/sandbox_service.rs:453-544`：创建目录前遍历每个 path component；遇到 symlink 时解析目标并验证仍在 workspace；ParentDir 直接拒绝。
- `rust/agent-core/src/sandbox_service.rs:563-568`：创建后再次 canonicalize 做 defense-in-depth。
- `rust/agent-core/tests/test_sandbox_service.rs:255-315`：有 `../session-a` 与 `/etc/passwd` 负向测试。
- `rust/agent-core/tests/test_sandbox_service.rs:319-345`：有 workspace quota 超限测试。

**缺口/风险**：上述是“检查后再普通文件 API”的路径，未见 `openat2(RESOLVE_BENEATH|NO_SYMLINKS)` 或目录 fd 相对操作；检查与写入之间仍理论上存在 TOCTOU 窗口。Firecracker sync 使用 `rsync -a`（`rust/firecracker-executor/src/workspace_sync.rs:137-145,205-213`），会保留 symlink；未见 sync 前后对 symlink、device、hardlink 的拒绝，不能把 Agent Core 单点校验等价为完整链路安全。

### 1.4 网络 egress 与 metadata

- `rust/agent-core/src/wasi_sandbox.rs:254-258`：WASI preview1 默认无网络 capability，TCP/UDP/HTTP 操作应 ENOSYS。
- Firecracker `configure_vm` 只配置 machine/boot/drives/vsock（`vm_pool.rs:427-494`），未注册 NIC；静态上 guest 无一般网络，也因此无法访问 `169.254.169.254`。

**未验证**：没有实跑 egress/IPv6/DNS/metadata 负向矩阵；也未见宿主代理或未来 NIC 模式的 metadata deny 规则。因此只能说“当前实现没有 NIC”，不能说“已有可审计 egress policy”。

### 1.5 CPU/内存/磁盘/超时/输出

**WASI：**
- 内存/对象数：`wasi_sandbox.rs:404-435` 创建 StoreLimits 并挂 limiter。
- CPU：`wasi_sandbox.rs:437-440` 设置 fuel。
- 时间：`wasi_sandbox.rs:449-451` 设置 epoch deadline；ticker 在 `222-236` 每 100ms increment epoch。
- 输出：`wasi_sandbox.rs:387-399` stdout/stderr 各 1MiB memory pipe。
- 磁盘：`sandbox_service.rs:22-40` + quota 检查；但并发写下 quota check 与 write 是否原子，未验证。

**Firecracker：**
- vCPU/内存：`config.rs:43-50` 默认 2 vCPU/1024MiB；`vm_pool.rs:427-433` 下发到 machine config。
- 磁盘：`vm_pool.rs:463-477` 创建 1GiB sparse ext4 并作为可写 secondary drive。
- 超时：请求上限被 executor cap（`vm_runner.rs:203-220`）；guest 超时 kill + wait（`guest-agent/src/main.rs:122-154`）。
- 输出：guest 用线程 `read_to_string` 读到 EOF（`guest-agent/src/main.rs:101-120`），**没有字节上限**；恶意程序可造成 guest/host 内存压力。
- 未见磁盘 rate limiter、IOPS limiter、cgroup 或 Firecracker balloon；microVM 内隔离不等于宿主总量治理。

### 1.6 VM 生命周期、残留与接口边界

- `vm_pool.rs:250-275`：session idle 后 VM 可回 warm pool；只清 `workspace_dir` 引用，VM 内状态是否 scrub 未见证据。
- `vm_pool.rs:305-308,566-575`：每 VM rootfs COW 副本，terminate 时删除临时目录。
- `workspace_sync.rs:8-76`：workspace ext4 有 dirty/clean marker，异常后 e2fsck；这是崩溃一致性而非安全清除证明。
- `main.rs:395-417,430-470`：cleanup 从校验后的 session id 派生路径，并删除 ext4、目录、state 文件。
- `config.rs:29-32`：jailer 默认关闭。
- `vm_pool.rs:371-394`：即便启用 jailer，示例以 `uid=0/gid=0` 启动。
- `main.rs:521-538`：executor 默认监听 `0.0.0.0:9001`，路由直接挂 `/execute`、download/list/cleanup；该文件未见 auth middleware。若网络边界未额外封闭，这是高风险控制面。

### 1.7 沙箱凭证下发与回收

- WASI 默认不继承环境（`wasi_sandbox.rs:260-263`）；只有显式允许才加入 env。
- sandbox proto 的 CommandRequest 只有 `session_id/command/timeout/user_id`（`sandbox.proto:138-152`），没有 credential capability、租约或回收 token。
- Firecracker GuestRequest 只有 `code/stdin/timeout`（`guest-agent/src/main.rs:20-34`），未见凭据字段。

**结论**：未发现完善的短期凭据 broker/下发/撤销实现。当前更接近“不向沙箱下发凭据”；这是好默认，但无法满足需要外部 API 的受控执行。也未见 per-execution secret tmpfs、TTL、single-use exchange、执行后 revoke 的证据。

---

## 2. 策略治理（OPA）

### 2.1 组织与加载

- 策略均使用 `package shannon.task`，按 base/security/vector/team 文件组织；例如 `config/opa/policies/base.rego:1-18`、`security.rego:1-18`、`teams/*/policy.rego:1-35`。
- 引擎递归读取 Rego、统一编译查询 `data.shannon.task.decision`（`internal/policy/engine.go:117-179`）。
- base 是 default deny（`base.rego:9-13`）且声明 deny precedence（`16-27`）；但 dev 环境广泛 allow（`29-37`）。
- team 策略按 `input.context.team` 提供模型、token、tool obligations（如 customer-support `policy.rego:19-43`）。

**版本化成熟度**：
- `engine.go:718-746` 对排序后的文件名+内容做 MD5，只留前 8 hex。
- `metrics.go:107-114,199-201` 把 `policy_path/version_hash/load_timestamp` 作为 Gauge labels。
- 未见不可变策略仓、签名 bundle、策略版本 DB 行、回滚引用或供应链 provenance。MD5 截短只适合部署提示，不适合安全证据。

### 2.2 决策点

- `activities/agent.go:599-677` 在 agent Activity 构造 PolicyInput 后调用全局 engine；这是**编排 Activity 内的执行前检查**，不是独立边缘 PDP，也不是 Ringharness 式 Kernel 不可绕过提交咽喉。
- `engine.go:244-280` 评估、应用 mode、记 metrics/cache。

**风险**：只对接入 `evaluateAgentPolicy` 的调用点生效；未找到“所有 effect/工具提交均必须携带已验证 decision receipt”的统一机制。故不能视为全系统策略内核。

### 2.3 失败语义：默认 fail-open，与 Ringharness 红线冲突

- `config/shannon.yaml:251-261`：默认启用但 `mode=dry-run`、`fail_closed=false`。
- `engine.go:104-110`：启动编译失败时，fail-closed 才报错；否则警告后禁用引擎。
- `engine.go:201-216`：默认 decision 的 Allow 是 `!FailClosed`。
- `engine.go:238-255`：输入转换/评估错误，只有 FailClosed 才拒绝。
- `engine.go:555-577`：dry-run 无条件把最终 Allow 改为 true。
- `engine.go:585-592`：未知 effective mode 也放行；注释称 safe behavior，但实际 `Allow=true`。

**Ringharness 折中**：不照搬 fail-open。策略缺失、不可解析、编译失败、未知模式、版本不可信、依赖不可用均返回 503/阻塞；“影子”只能计算 `would_decision`，不能覆盖权威决策。紧急开关应停止新派发/进入 BLOCKED，而不是切 dry-run 后放行。

### 2.4 影子/canary 与热重载

- `policy/config.go:21-41`：canary 百分比、用户/agent allowlist 与 dry-run override；带 SLO thresholds。
- `engine.go:596-606`：按 `userID|agentID` 做稳定 hash 分桶。
- `engine.go:535-575`：记录 configured/effective mode 与 would/actual。
- `config/manager.go:595-614`：`.rego` 变化触发所有 reload handler，失败只记录日志。
- `engine.go:163-179`：新策略先在局部 `compiled` 编译成功，再赋给 `e.compiled`，单次 LoadPolicies 看起来保留旧 prepared query；但未见 reload 与并发 Evaluate 的锁/atomic pointer，**并发数据竞争和真正原子切换未验证**。

### 2.5 可重放审计记录

- Decision 类型有 `PolicyVersion` 和 AuditTags（`engine.go:70-83`），但 parseResults 只填 session/agent/mode tags（`311-358`）。
- 版本 hash 只写 Prometheus deployment info（`metrics.go:199-201`），未证实写回每次 Decision。
- 日志/metrics 记录 allow/reason/mode/session/agent 与策略整体版本，但未找到持久化的：`decision_id`、canonical input digest、policy bundle digest、policy data digest、decision output digest、调用点/subject revision。

**结论**：Shannon 有可观测决策，但没有足够证据构成可重放授权收据。Ringharness 已有 `ExecutionBinding.policy_digest`（如 `packages/control_kernel/storage/probe.py:185-204`）与 immutable policy row，应在此基础上补 input/decision digest，不应退化成 Prometheus label。

---

## 3. 多租户隔离

### 3.1 租户维度

- `migrations/postgres/003_authentication.sql:11-24`：tenant 有 plan、token/rate quota、active、metadata。
- `003_authentication.sql:26-74`：user/API key/refresh token 绑定 tenant；API key 只存 hash，并有 scopes/rate/expiry。
- `auth/jwt.go:35-42,74-91`：JWT claims 携带 tenant/role/scopes；`94-142` 验 HMAC method、token validity、issuer，再解析 user/tenant UUID。

### 3.2 API 层

- 正常路径缺认证会 401/Unauthenticated（`auth/middleware.go:63-93,136-173`）。
- 但 `skipAuth` 会注入 owner 级 dev identity（`middleware.go:39-52`）；gRPC 更允许来路 metadata 自选 user/tenant（`105-133`）。
- compose 默认 `GATEWAY_SKIP_AUTH=1` 与开发 JWT 默认值（`deploy/compose/docker-compose.yml:388-392`；release 也有同类默认，`docker-compose.release.yml:354-363`）。

**与 Ringharness 冲突**：明确禁止开发万能身份；缺 JWT 必须失败关闭。这些 Shannon 默认只能作为反例。

### 3.3 DB 层

- tenant_id 广泛存在并建索引（`003_authentication.sql:88-104`）。
- 未找到 `ENABLE ROW LEVEL SECURITY`/`CREATE POLICY`；迁移反而把 auth schema 所有表/sequence 的 ALL 权限给单一 `shannon` 角色（`003_authentication.sql:150-152`）。
- 因而隔离主要靠应用查询条件，不是 DB 强制。
- 某些表 tenant_id 还可空（如 `003_authentication.sql:88-90` 为旧表新增 nullable 字段；scheduled_tasks migration 也为 nullable）。

### 3.4 Redis/cache/附件

- Session 本地缓存 key 只是 sessionID（`session/manager.go:27-29,186-201`）。
- **静态发现的严重缺口**：Redis miss 后才做 tenant check（`manager.go:226-231`），但本地 cache hit 在 `188-201` 已直接返回，绕过该检查。
- Redis session key 同样未包含 tenant（`manager.go:267-270` 间接可见；具体 key builder为 session id 维度）。
- Attachment key 为 `shannon:att:<id>`，对象仅存 session id，不存 tenant（`attachments/store.go:19-27,40-59`）；Get 的 session check 是可选参数，内部调用可省略（`65-95`）。

**结论**：不能借鉴其共享缓存结构；Ringharness 应使用 `(project_id, subject_id)` 或 `(tenant/project, object id)` 复合 key，并把 scope check 放在 cache hit 和 miss 的共同出口。

### 3.5 向量库、对象层、队列

- Qdrant FindSimilarQueries 仅在 auth context 存在且 tenant 非零 UUID 时才加 filter（`vectordb/search.go:21-30`），否则无 tenant filter。
- GetSessionContext 只有调用方传入非空 tenant 才加 filter（`search.go:51-80`）。属于可选过滤，非失败关闭。
- 附件是 Redis blob，不是独立对象仓；tenant 隔离见上，较弱。
- Temporal 使用单 namespace/default 与单 task queue（`config/shannon.yaml:263-272`）；tenant 出现在 workflow memo 并在多个 API 中比较（例如 `server/service.go:1978-1985`），但没有 tenant queue/worker ACL 或 namespace 隔离证据。

### 3.6 跨租户测试

- 找到 daemon hub 对 tenant:user 分桶的单元测试（`internal/daemon/hub_test.go:59-80`）。
- Sandbox 有 session A/B 路径穿越测试（`rust/agent-core/tests/test_sandbox_service.rs:255-315`），但 session 不等于 tenant。
- 未找到覆盖 API→DB→Redis local cache→Qdrant→attachment→Temporal memo 的端到端跨租户矩阵；尤其未见针对上述 cache-hit bypass 的负向测试。

---

## 4. 可观测性

### 4.1 traces/logs/metrics 关联

- Go：`internal/tracing/tracing.go:84-103` 从 active span 生成并注入 W3C `traceparent`；`125-130` 有 parser。
- Rust：`agent-core/src/tracing.rs:96-164` 支持 HTTP header extract/inject current context；`166-180` 可取 current trace id。
- Python：`python/llm-service/main.py:52-57,134-136` 可启用 OTLP，并 instrument FastAPI/httpx。
- Gateway CORS 放行 `traceparent/tracestate`（`cmd/gateway/main.go:988-992`），OpenAI proxy 转发 `traceparent`（`cmd/gateway/internal/openai/handler.go:506-510`）。
- Rust basic logging 可选 JSON（`agent-core/src/tracing.rs:73-88`）；Firecracker executor 固定 JSON（`firecracker-executor/src/main.rs:499-505`）。

**限制**：OTel 默认关闭（`config/otel/README.md:5-15`; `config/shannon.yaml:319-322`），collector 目录只有 README 示例，没有实际 collector config。`config/features.yaml:254-257` 宣称 correlation_id，但未验证所有日志自动包含 trace/span/workflow/session/effect。

### 4.2 metrics 现状与高基数

- Go 指标覆盖 workflow、task、agent、session/cache、vector、provider 等（`internal/metrics/metrics.go:8-32,91-130,133-171,201-279`）。
- Rust 指标覆盖 task/FSM/memory/tool/gRPC/enforcement（`agent-core/src/metrics.rs:10-35,100-199`）。
- OPA 指标覆盖决策、错误、latency、cache、canary、版本和 dry-run divergence（`policy/metrics.go:11-143`）。
- 有高基数风险：`agent_id` 直接作为 label（Go metrics `116-130`），`session_id` 直接作为 label（`420-426`），policy reason 也直接进入 evaluation label（`policy/metrics.go:13-19,145-148`）。
- deny reason 另有 hash+截断（`policy/metrics.go:191-196,225-228`），但仍携带 truncated reason，且注释“top 10”未见真正 top-K limiter。
- policy version label 包含 load timestamp（`107-114`），每次 reload 生成新 series。

**可借鉴边界**：借鉴“低基数业务状态指标 + trace 中放高基数 ID”，不要照搬 session/agent/reason/timestamp labels。

### 4.3 无人值守关键告警缺口

静态搜索未找到完整的：
- Temporal task queue backlog / oldest task age；
- stale lease count/age；
- UNKNOWN effect count/oldest age；
- outbox oldest age；
- QUARANTINED resource/budget age；
- policy version mismatch、policy input replay failure；
- sandbox kill latency/output truncation/egress deny；
- credential lease age/revoke failure。

现有 Prometheus/Temporal UI/health（`config/otel/README.md:75-80`）是基础设施可见性，不等于 Ringharness 所需无人值守安全 SLO 发布门。

---

## 5. 凭据管理

### 5.1 正向机制

- API key 只存 hash、prefix、scope、expiry（`003_authentication.sql:44-60`）；refresh token 也只存 hash并可 revoke（`62-74`）。
- `.gitignore` 排除 `.env`、`.secrets/`、`secrets/`、K8s secrets（`.gitignore:161-180,322-325`）。
- WASI 默认不继承环境（`wasi_sandbox.rs:260-263`），且 sandbox RPC/Firecracker guest request 不带凭据，减少误下发。
- Playwright Dockerfile 使用 non-root user（`python/playwright-service/Dockerfile:43-45`）。

### 5.2 缺口与反例

- `.env.example:1-6` 说明 unset 回退到服务默认；同一文件包含开发 DB/JWT/skip-auth 默认（具体值不在本报告复述，视为 `[REDACTED]`）。
- compose 中 `env_file` 后又用 `environment` 显式设置变量（`docker-compose.yml:225-245,369-390`）；按 Compose 语义 `environment` 优先于 `env_file`。这是可预测的覆盖机制，但大量 `${VAR:-default}` 会在缺值时静默回退，不适合生产密钥失败关闭。
- release compose 仍有静态开发 DB/JWT 回退与 skip-auth 默认（`docker-compose.release.yml:48-65,128-140,346-363`）。
- 未发现启动时检查 `.env`/secret 文件必须 `0600` 的实现；只有 gitignore 不是运行时保护。
- 多数服务 Dockerfile 未见 `USER`（只有 Playwright 明确 non-root）；Agent Core、orchestrator、gateway、llm-service 很可能以镜像默认用户运行，静态未验证最终 UID。
- compose 将同一 DB 用户给 orchestrator、gateway、llm-service（`docker-compose.yml:126-138,225-245,369-386`），没有最小 DB role；与 Ringharness“Broker 不得业务库写、Runner/Web 不持宿主凭据”不兼容。
- Firecracker executor API 未见认证；凭据下发、租约 TTL、single-use exchange、revoke、secret redaction audit 均未验证。

**Ringharness 已更强**：`packages/execution_broker/src/execution_broker/service.py:1-9,26-57` 明确 Broker 非调度权威，检测到业务 DB URL 即拒绝启动；这一红线应保留并扩展，而不是照搬 Shannon 的共享数据库身份。

---

# ② 与 Ringharness 的差距

| 维度 | Ringharness 已有 | 主要差距 | Shannon 提供的价值/警示 |
|---|---|---|---|
| 沙箱 | Broker 咽喉、workspace path canonicalize/allowed_paths（`execution_broker/paths.py:17-61`）；Broker 禁业务库凭据 | 尚无可验证计算隔离；缺 CPU/内存/磁盘/输出/egress/metadata/stop 负向矩阵 | WASI/Firecracker 作为 spike 候选；借测试矩阵，不预设语言/技术 |
| 策略 | project-scoped immutable version、digest、BLOCKED 不接受新配置（`storage/policies.py:31-61,78-145`） | 缺策略编译/影子/历史 journal replay/发布门；授权收据尚需 input+decision digest | 借 dry-run/canary metrics；拒绝其 fail-open/unknown-mode allow |
| 多租户 | project ACL 与查询 scope；事实在 PG | 需系统化检查 cache/object/queue/metrics scope 与跨项目矩阵 | Shannon 的 cache-hit bypass、可选 tenant filter 是应加入负测的反例 |
| 可观测 | `packages/observability` 目前 `__init__.py` 为空 | 缺统一 trace context、低基数指标、stale/UNKNOWN/outbox/quarantine SLO 与发布门 | 借 W3C propagation 和 policy指标模型；修正高基数 label |
| 凭据 | Broker 启动时拒绝 DB 凭据；worker JWT/OIDC/session | 缺细粒度 workload identity、短期 secret lease/revoke 与文件权限机械门 | 借“不向 sandbox 继承 env”；拒绝 compose 默认 secret/共享 DB role |

---

# ③ 可直接借鉴的机制清单

## A. 可移植（不绑定 Rust/OPA/Firecracker）

1. **统一 WorkspaceGuard**：trusted root canonicalize；拒绝 absolute/`..`；每层 component 做 symlink 检查；创建后复核；size walk 跳过 symlink 且有 entry cap。
2. **沙箱资源信封**：CPU、memory、wall timeout、disk bytes、stdout/stderr bytes 全部服务端取 `min(request, policy)`，并产生结构化 limit-hit receipt。
3. **默认无环境/无网络 capability**：工具只得到声明的 capability；secret 不是普通 env，也不进入 workspace/artifact/log。
4. **W3C Trace Context 跨边界传播**：API→Temporal→Kernel Activity→Broker→Runner/tool；高基数 ID 放 span/log，不放 Prometheus label。
5. **影子策略指标**：`configured_version/effective_version/would_decision/actual_decision/divergence/error/latency`，稳定按 project/subject hash 分桶。
6. **拒绝原因规范化**：固定枚举 reason code；自由文本只入日志/trace，避免 metrics label 爆炸。
7. **沙箱×停止×租户/项目负向矩阵**：路径穿越、symlink swap、workspace collision、metadata、IPv4/IPv6/DNS egress、fork/oom/disk/output bomb、timeout kill、stop receipt、旧 fencing。

## B. 需改造

1. **WASI/Firecracker 选型**：只做 spike；用同一 BrokerSandbox interface 比较 rootless container/WASI/microVM。不得因 Shannon 用 Rust 就改变 Ringharness Python+TS 裁定。
2. **OPA 生命周期**：可借编译→shadow→canary→enforce，但权威决策应进入 ControlKernel/effect preparation 咽喉；不可只在 runner/activity 边缘检查。
3. **策略 reload**：不能 fsnotify 后原地替换；应从 immutable DB version 生成 signed/hashed bundle，编译成功后原子发布，失败保持旧 active 且阻塞新版本晋级。
4. **Firecracker pool**：若采用，warm VM 回池前必须证明进程、mount、tmp、network namespace、secret 全部 scrub；否则直接销毁。jailer/non-root/seccomp/cgroup 是硬门。
5. **短期凭据**：由 Broker 依据 effect + policy + fencing 向 credential service 换单用途短 TTL token；写入 tmpfs/FD，执行后 revoke；Runner/Web 不得持业务宿主 credential。
6. **租户 scope**：将 Shannon 的可选 tenant filters 改成 Ringharness 的 project scope 必填；cache key、object key、queue memo/search attrs 都含 project，缺 scope 直接 503。

## C. 不适用/不得照搬（原因）

1. **`dry-run + fail_open=false?` Shannon 实为 `fail_closed=false`**：策略故障放行，与本仓失败关闭红线冲突。
2. **未知 policy mode 放行**：配置错误不能变成授权。
3. **开发万能身份/GATEWAY_SKIP_AUTH 默认开**：本仓明确禁止。
4. **共享 DB 用户/ALL PRIVILEGES**：破坏 Broker/Runner/Web 最小权限与职责分离。
5. **session/agent/reason/timestamp 作为 metrics labels**：高基数、成本和可用性风险。
6. **Firecracker 不带 jailer即视为安全**：microVM 只是一个边界；executor API、host privileges、workspace sync、pool scrub 仍是攻击面。
7. **执行成功/Workflow COMPLETED 作为 DONE**：与 Ringharness 证据三分离和 VerificationProfile 屏障冲突。

---

# ④ Top 5 建议（为什么 / 改哪个文件 / 验收标准）

## Top 1 — 建立 `SandboxEnvelope` 与全负向矩阵（P1）

**为什么**：Shannon 证明资源/路径/网络必须同时强制；Ringharness 当前 Broker 路径安全已有雏形，但尚非计算沙箱。最大风险是“路径安全测试绿”被误认为执行隔离完成。

**建议改动位置**：
- `packages/execution_broker/src/execution_broker/service.py`
- `packages/execution_broker/src/execution_broker/paths.py`
- 新增 `packages/execution_broker/src/execution_broker/sandbox.py`（建议名）
- `tests/unit/test_execution_broker.py`
- 新增 `tests/security/test_sandbox_negative_matrix.py`、`tests/temporal/test_tm_sandbox_stop_project_matrix.py`

**验收标准**：
- 同一套 contract tests 至少跑 rootless container 与 WASI（或明确一个候选失败）；以真实命令验证。
- absolute/`..`/symlink-chain/symlink-swap/hardlink/device/跨 project 均拒绝。
- IPv4/IPv6/DNS/redirect/metadata `169.254.169.254` 全拒绝，除非 policy 显式 allowlist。
- CPU、memory、disk、process count、wall-time、stdout、stderr 均命中上限并出 receipt；无宿主 OOM。
- stop 后进程树消失、workspace/secret mount 清理；旧 fencing 回执不能解封 QUARANTINED；Goal≠DONE。

## Top 2 — 策略版本影子评估流水线，但权威路径始终 fail-closed（P2）

**为什么**：Shannon 的 dry-run/canary/metrics 很适合降低策略发布风险，但默认 fail-open 与本仓冲突。Ringharness 已有不可变 version/digest，基础比 Shannon 更强。

**建议改动位置**：
- `packages/control_kernel/src/control_kernel/storage/policies.py`
- `packages/control_kernel/src/control_kernel/storage/effects.py`
- `packages/control_kernel/src/control_kernel/protocols/policies.py`
- `packages/orchestration/src/orchestration/kernel_activities.py`
- 新 migration：policy evaluations/releases（建议）
- 新增 `tests/integration/test_policy_shadow_release.py`、Temporal replay fixture

**验收标准**：
- 每次决策持久化 `policy_version + policy_digest + canonical_input_digest + decision_digest + reason_code + subject/effect revision + trace_id`。
- 历史 journal 可对候选版本重放；产出 allow/deny divergence 与 obligation divergence。
- 只有满足 SLO 且审批后 candidate 才晋级 active；晋级原子。
- 缺版本、digest 不符、编译失败、未知 mode、存储不可用均 503/BLOCKED，绝不执行。
- shadow 只写 `would_decision`，不能把 deny 改成 allow；紧急开关停止新派发。

## Top 3 — 跨项目/租户隔离机械矩阵，覆盖 cache/object/Temporal（P1）

**为什么**：Shannon 的 session local-cache hit 在 tenant check 前直接返回，是典型“持久层有过滤但缓存层泄漏”；这是 Ringharness 应主动防的回归类型。

**建议改动位置**：
- `packages/control_kernel/src/control_kernel/storage/artifacts.py`
- `packages/control_kernel/src/control_kernel/storage/memories.py`
- `packages/control_kernel/src/control_kernel/storage/memory_index.py`
- `packages/read_model/src/read_model/snapshot.py`
- `packages/orchestration/src/orchestration/temporal_workflows.py`
- `apps/runner/src/harness/brokerToolBridge.ts`
- 新增 `tests/security/test_cross_project_matrix.py`

**验收标准**：
- 对同 UUID/外部 key/幂等 key 在 Project A/B 构造碰撞；API、PG、cache、object content、memory/vector、read model、Temporal query/signal、Broker artifact 下载全部返回 404/拒绝且不泄漏存在性。
- cache hit 与 miss 行为一致；所有 key 以 project scope 复合命名。
- workflow memo/search attributes 包含 project，但服务端仍以认证上下文复核，不能信任调用方字段。
- Broker worker JWT 只能领取授权 project/effect；跨项目 receipt 不改变状态。

## Top 4 — 建立无人值守安全 SLO 与发布门（P2）

**为什么**：Shannon 有通用 metrics 和 trace 传播，却没有 stale lease/UNKNOWN/outbox/quarantine oldest-age 闭环；Ringharness 恰好最需要这些“系统卡死但进程仍健康”的指标。

**建议改动位置**：
- `packages/observability/src/ring_observability/__init__.py`（目前为空，应拆分 metrics/tracing/logging）
- `packages/control_kernel/src/control_kernel/storage/reconcile.py`
- `packages/control_kernel/src/control_kernel/storage/claims.py`
- `packages/control_kernel/src/control_kernel/storage/stops.py`
- `packages/orchestration/src/orchestration/relay.py`
- 新增 dashboards/alerts 与 `tests/observability/test_slo_release_gate.py`

**验收标准**：
- 指标至少包含 queue backlog/oldest age、outbox oldest age、stale lease count/age、UNKNOWN effect count/oldest age、QUARANTINED reservation age、stop-confirm latency、budget reserved-vs-settled delta、policy errors/divergence/version mismatch、sandbox kills/limit hits。
- Prometheus labels 仅用固定枚举/低基数（service、operation、status、reason_code、policy channel）；project/session/effect/trace id 不作 label。
- API→Temporal→Kernel→Broker→Runner/tool 保持同一 trace；结构化日志含 trace/span/project/activity/effect/fencing，但敏感字段脱敏。
- CI/发布门以合成故障验证告警触发和恢复；关键 SLO 不满足禁止自动发布。

## Top 5 — 凭据最小权限与短期租约（P1）

**为什么**：Shannon “WASI 不继承 env”值得保留，但 compose 共享 DB 身份/开发默认值是反例。Ringharness 已有 Broker 禁 DB URL，应扩成全进程凭据矩阵和短期 secret 生命周期。

**建议改动位置**：
- `packages/execution_broker/src/execution_broker/service.py`
- `apps/runner/src/harness/brokerBackedHarnessTool.ts`
- 部署 manifests/compose（实际文件按仓内发现后落地）
- 新增 `packages/execution_broker/src/execution_broker/credentials.py`（建议名）
- 新增 `tests/security/test_process_credential_matrix.py`

**验收标准**：
- Control、Broker、Runner、Web 各自允许/禁止 env 清单；Broker/Runner/Web 出现业务 DB URL 或宿主云长期 key 即启动失败。
- secret 文件 owner-only、mode `0600`；目录 `0700`；CI 检查，不满足即拒绝启动/发布。
- Broker 以 effect_id + fencing_epoch + policy_digest 换 single-use、短 TTL、最小 scope token；只经 FD/tmpfs 注入，不进入 argv/env/workspace/log/artifact。
- 完成/失败/timeout/stop 都 revoke；验证 token 随后不可用并有 revoke receipt。
- 缺 JWT/credential service/key alias/object store 一律 503；不存在开发万能身份或生产默认 secret。

---

## 研究边界与未验证项

1. 本报告是静态源码研究，未实跑 Wasmtime、Firecracker、OPA、Temporal、Redis/Qdrant。
2. 未验证 Firecracker guest 是否通过内核默认设备获得任何意外网络路径；源码未配置 NIC。
3. 未验证 sandbox quota 在并发写入下原子，也未验证 symlink TOCTOU exploitability。
4. 未找到 Shannon 策略 decision 的持久化审计表；若另有仓外日志后端，本次未验证。
5. 未找到完整跨租户端到端测试；不能据局部测试断言系统隔离成立。
6. 未检查实际部署平台（Kubernetes/IAM/security groups）对 compose/source 缺口是否有外部补偿控制。

## 最终裁定

- **书中/Shannon 通用做法**：WASI capability sandbox、Firecracker microVM、OPA dry-run/canary、W3C tracing、Prometheus 指标均可作为机制来源。
- **Ringharness 更严红线**：策略与依赖一律 fail-closed；配置 immutable；证据完整性/来源/正确性三分离；Broker 不写业务库；Runner/Web 无宿主凭据；执行成功不等于 DONE。
- **折中**：技术选型以 spike 和同一 contract test 决定；OPA 可作为编译/评估实现但不能成为旁路权威；影子评估只观测不放行；Firecracker 只有在 API auth、jailer/non-root、资源上限、pool scrub、凭据租约、stop/negative matrix 全部通过后才有资格进入候选。