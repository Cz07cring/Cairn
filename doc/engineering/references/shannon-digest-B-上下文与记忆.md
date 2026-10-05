# Shannon 上下文工程、记忆、Prompt 缓存与分层模型研究

## 范围、方法与结论口径

- 研究对象：`/tmp/shannon-study` 的指定文档、Python LLM service、Go orchestrator context/memory 实现、Qdrant migrations。
- 对照基线：Ringharness `AGENTS.md`、`packages/context_compiler/src/context_compiler/compile.py`、`doc/engineering/开发流程增强v2-全书吸收.md` §2 的 #4/#15。
- 状态词：**已验证**表示在本次精读代码/文档中找到直接证据；**未验证**表示本次指定切片不足以证明全仓不存在。
- 只读约束：未修改 Ringharness 或 Shannon 被跟踪文件；本报告是唯一输出。

## 0. Ringharness 对照基线

1. DONE 权限、状态权威与证据边界：`AGENTS.md:47-56` 明确模型成功/exit 0/Activity SUCCEEDED 不能产生 DONE，业务事实归 PostgreSQL，hash 完整性/来源可信/验收正确性三分离，并要求缺配置失败关闭。
2. 当前 ContextCompiler 仅作粗粒度清单估算：`packages/context_compiler/src/context_compiler/compile.py:18-30` 默认 `max_input_tokens=8192`，按 base 128、每 binding 256 等估算；`compile.py:42-47` 超限抛 `CompileRejected`，明确“不得静默裁剪合同或 Skill”。它尚未覆盖单个 Tool Result、单 Turn 累计 Tool Result、真实消息/tokenizer 开销。
3. 编译结果已提供引用和压缩血缘槽位：`compile.py:49-85` 对 binding 排序、拒绝重复，并输出 digest、classification、`session_generation`、`compaction_source_digest`。
4. 项目增强表把 Tool Result Envelope 定为缺失：`doc/engineering/开发流程增强v2-全书吸收.md:27-43` 的 #4 要求“单结果预算 + 单 Turn 聚合预算 + 持久外溢到 Artifact”；#15 要求“分区预算、VERIFIED 才召回、删索引可从 PG 重建”。`同文:111-117` 明确禁止用静默截断销毁信息。

---

# ① 逐问题发现（文件:行号 + 关键片段）

## 1. 上下文工程

### 1.1 消息/上下文预算如何计算与强制执行

**历史压缩估算是字符近似，不是 provider tokenizer。**

- `go/orchestrator/internal/activities/compression_utils.go:18-32`：总字符数 `/4`，再加每消息 5 tokens。
- `compression_utils.go:52-66`：按 tier 固定窗口：small=8k、medium=32k、large=128k、xlarge=200k，未知 tier 保守回退 8k。
- `go/orchestrator/internal/activities/compression_activities.go:31-42`：压缩阈值固定为模型窗口的 75%。
- `python/llm-service/llm_provider/anthropic_provider.py:837-853`：实际发 Anthropic 前再次估算 prompt，保留 256 token safety margin；无输出 headroom 时抛错，否则 `max_tokens=min(requested, model max, headroom)`。这是 provider 边界的硬限制，但仍基于估算。

**任务/会话累计预算由 Go BudgetManager 管理，可硬停也可警告/审批。**

- `go/orchestrator/internal/budget/manager.go:20-40`：预算含 task/session 已用量，策略含 hard limit、warning、require approval。
- `budget/manager.go:250-268`：预计使用超过 task/session budget 时，hard limit 才 `CanProceed=false`；非 hard limit 可只要求审批或产生 warning。
- `budget/manager.go:305-349`：调用后记录实际 usage，且 quota 使用 cache-aware token 总数。
- Python 侧预算默认关闭以避免双重权威：`python/llm-service/llm_provider/manager.py:1054-1069` 的 `LLM_DISABLE_BUDGETS` 默认 `1`，解析失败也 fail-open。此做法与“Go 编排器为预算权威”一致，但与 Ringharness“缺配置失败关闭”红线不同，不能照搬 fail-open。

### 1.2 Tool Result 超预算怎么办

**结论：Shannon 有字符级单工具格式裁剪和整轮累计停止，但没有 Ringharness 所需的“保全原文 → Artifact → 摘要/指针”的 Envelope；存在明确红线冲突。**

1. Agent loop 的总预算：
   - `python/llm-service/llm_service/api/agent.py:1883-1899`：普通模式默认 3 轮、5 次工具、60,000 输出字符；research 模式 20 轮、25 次、300,000 字符；可由 context/env 覆盖。
   - `agent.py:2060-2061` 先把本轮已格式化结果长度累加；`agent.py:2129-2138` 再检查累计字符预算并停止。因此预算是**事后停止**，当前 turn 可以越过上限；不是 admission/reservation，也不是严格“不超预算”。
   - `agent.py:2109-2119` 在检查前已把完整 `tool_results` 追加回消息；所以“累计预算”主要限制后续迭代，不保证当前 LLM 上下文一定在预算内。
2. 单结果/格式裁剪：
   - web search 每项只留 1500 chars：`agent.py:2926-2970`。
   - 其他工具通常把整个 output JSON/string 加入 formatted result，未见统一单结果 hard cap：`agent.py:2987-3044`。
   - 供上游 persistence/interpretation 的 execution record 会把 content-rich tool 每字符串裁到 100,000、其他工具裁到 2,000：`agent.py:3080-3097`；raw record 仍在当前 Python 内存保留完整 output：`agent.py:3109-3117`，但此处未见持久化引用。
   - interpretation 聚合默认最多 50,000 chars；各 tool 分支继续按 1.5k/6k/8k/30k/40k 裁剪，达到总上限就 `break`：`agent.py:557-599,621-783`。未返回 omitted count、digest、可取回 pointer。
   - 独立 `/tools/execute` 文本格式器（明确不影响 agent loop）全局裁到 6000：`llm_service/tools/text_formatter.py:1-12,34-52`；列表只取前 20，字段值裁 500，页面 content 裁 3000：`text_formatter.py:63-82,91-158`。
3. **没有外溢/指针化**：在本次精读的 agent loop、tool formatter、context docs 中，未发现超预算 Tool Result 先持久化到对象/Artifact 并用 digest/pointer 代替的实现。`migrations/qdrant/create_collections.py:63-68` 虽创建 `tool_results` collection，但本次指定实现切片未验证 agent loop 的超限结果写入该 collection，也未见可恢复指针协议。
4. **与 Ringharness 红线冲突**：Shannon 多处直接 `[:N]`、`break` 丢弃尾部，仅偶尔加 `...`/`[TRUNCATED]`，不提供完整内容的权威取回路径。Ringharness 只能借鉴“单结果 + 累计 turn 双预算”和工具类别配额思想，不能照搬静默裁剪。

### 1.3 是否有 microcompact/macrocompact 类分层压缩

**已验证只有单层滑窗语义摘要；分层/递归摘要仍是未来项。**

- 触发前置：`go/orchestrator/internal/workflows/simple_workflow.go:163-181` 要求 version gate `context_compress_v1`、有 session、history >20，再检查估算 token。
- 阈值与防抖：`compression_activities.go:31-42` 达模型窗口 75% 才触发；`compression_activities.go:44-75` 距上次至少新增 20 条且至少 30 分钟。
- 目标：`simple_workflow.go:190-198` target 是窗口 37.5%；`simple_workflow.go:252-264` 可从 context 读取 trigger/target ratio。
- 内容形态：`simple_workflow.go:265-272` 把 summary 注入 `context_summary="Previous context summary: ..."`，历史仅保留 first 3 + last 20。切片算法在 `workflows/helpers.go:120-147`。
- summary 生成和存储：`activities/context_compress.go:41-70` 调 `/context/compress`；`context_compress.go:100-110` 先 best-effort PII redaction 再估算；`context_compress.go:127-166` 生成 embedding 后把 `session_id/tenant_id/type/timestamp/content/summary_id` 直接 upsert 到 Qdrant `summaries`。
- 恢复/召回：`activities/semantic_memory.go:164-203` 对 query 做 embedding，在相同 session/tenant 下 SearchSummaries，加入 `_source=summary`；总 memory 项最多 10 条（`:214-218`）。
- `docs/context-window-management.md:348-362` 明确“Hierarchical summaries for very long sessions”仍 under consideration。因此不能称为 macrocompact/microcompact；目前是**原历史保留 + 一次 lossy summary + 滑窗**。
- 细节风险：初次压缩状态更新要求 `Summary != "" && Stored`（`simple_workflow.go:200-228`），但随后 on-the-fly 路径只要求 `Summary != ""` 就注入（`:255-272`），存储失败时仍可使用临时摘要。Ringharness 若采用，必须把“可注入”和“已持久化可恢复”分开建模，并失败关闭关键压缩 checkpoint。

## 2. 记忆系统

### 2.1 写入、召回与隔离

**Qdrant 会直接保存模型 query/answer 或分块。**

- 写入入口 `go/orchestrator/internal/activities/record_query.go:71-95`：embedding/vector service 不可用时返回 Stored=false 但不抛业务错误；短于 50 或包含若干 error 词则跳过。
- 去重 `record_query.go:97-121`：query embedding 在同 session/tenant 相似度 >0.95 就认为 near-duplicate。它并不比较事实内容或 answer 冲突。
- 分块写入 `record_query.go:124-206`：answer 分块后批量 embedding，payload 含 query、chunk text、qa_id/index/count、session/user/tenant/model/timestamp 及任意 metadata。
- 非分块写入 `record_query.go:210-242`：payload 直接包含完整 query/answer，并 upsert Qdrant。
- agent 维度：`activities/agent_memory.go:52-87` 写入 metadata `agent_id/role/source=agent`；召回 `agent_memory.go:22-49` 按 session+agent+tenant。
- session 维度：`vectordb/search.go:51-80` 按 session 必选、tenant 非空时附加过滤；`:113-136` 按 timestamp 倒序后取 topK。
- migration 索引：`migrations/qdrant/create_collections.py:118-156` 给 task_embeddings 建 session/tenant/user/agent/qa_id/is_chunked/timestamp 索引；summaries 有 session/tenant/user/timestamp（`:194-214`）。decomposition_patterns 有 session/user/strategy/success_rate/timestamp（`create_decomposition_patterns.py:70-113`）。

**隔离结论：**

- 已有 tenant + session 主隔离；agent memory 额外按 agent_id。
- role 只是 payload metadata，未见作为通用召回强制过滤。
- 未见 Ringharness 的 project_id 隔离维度；对项目型软件工厂不可直接用 session 代替 project。
- 文档明确 cross-session retrieval 未实现、session 严格隔离：`docs/memory-system-architecture.md:205-219`。
- 另有 user 文件记忆：`activities/memory_extract.go:80-119` 由 LLM 从 query/result（先截到 1000/4000 chars）抽取；`:184-229` 写到 `/tmp/shannon-users/{user}/memory` 并更新索引；`:232-275` triple 只记录 h/r/t、源文件和时间。这是 user 隔离，但仍没有 project/session/事实状态。

### 2.2 如何避免“语义相似 = 事实”

**结论：没有 VERIFIED admission；相似度、success rate、`_source` 只是排序/来源类型，不能证明事实。与 Ringharness 红线冲突。**

- 召回 `semantic_memory_chunked.go:49-70` 仅使用 threshold（负值默认 0.75）、topK 和 session/tenant filter。
- 结果附 `_similarity_score`，并可 MMR 重排：`semantic_memory_chunked.go:85-97,117-159,249-275`；没有 verified/trust/source digest 过滤。
- hierarchical merge 仅标 `_source=recent|semantic|summary`：`semantic_memory.go:58-81,122-159,164-203`。这是检索渠道，不是来源可信度或验收裁决。
- user memory 写入由 LLM 的 `worth_remembering` 决定，只做长度质量门：`memory_extract.go:149-175`；文件/triples 直接落地（`:188-218,232-275`），没有 PROPOSED→VERIFIED。
- supervisor 会把 execution state `COMPLETED` 聚合为 success rate：`supervisor_memory.go:336-374`；`RecommendWorkflowStrategy` 再按 success、探索项、时延与 token penalty 评分并返回 confidence/source（`:486-575`）。这对策略建议可用，但 **COMPLETED 不能在 Ringharness 等价为 DONE/正确**。
- decomposition advisor 会在文本相似和历史 success_rate 超阈值时复用 subtasks/strategy：`supervisor_memory.go:620-637`。Ringharness 若直接照搬会把“相似执行曾完成”当成可复用正确经验，违反 DONE 只归 Kernel及证据三分离。

### 2.3 权威存储、索引重建

- Shannon 缓存文档声称 Go orchestrator owns persistent state、semantic memory single source of truth：`docs/llm-service-caching.md:5-14`。
- memory 文档同时说 `task_executions` 是 primary source of truth（`docs/memory-system-architecture.md:222-228`），但又明确 decomposition pattern 的活跃存储只在 Qdrant、PostgreSQL writes 未来才做（`:212-220,230-234`）。
- 实际 `record_query.go:151-206,220-242` 直接构建 payload 并写 Qdrant；在本次指定切片中未找到先写 PG 的 immutable memory/provenance ledger，也未找到“清空 Qdrant 后由 PG 重建”的 job/test。
- 因而结论应保守表述为：**普通 task execution 在 PG 有事实记录；语义 Q&A、summary、decomposition memory 是否可完整由 PG 重建，未验证，且 decomposition 文档明确当前不可依赖 PG 重建。Qdrant 对这些活跃记忆事实上承担了不可替代存储角色。**
- user 文件记忆中，内容 `.md` 保留而 `MEMORY.md` 索引最多 50 条、只淘汰索引引用不删内容：`memory_extract.go:320-323,429-480`。这体现“内容与索引分离”，但未发现自动扫描内容重建 MEMORY.md 的实现，故“可重建”仍未验证。

## 3. Prompt/缓存稳定性

### 3.1 两类缓存必须区分

1. **LLM response exact-match cache**：manager 层缓存最终 response，不是 provider KV cache。文档：`docs/llm-service-caching.md:1-24`。
2. **Anthropic prompt prefix cache**：通过 `cache_control` breakpoint、稳定 system/tools/message prefix 提高 provider cache hit；实现集中在 `anthropic_provider.py`。

### 3.2 exact-match response cache

- key：`llm_provider/base.py:167-188` 对 messages、model tier/model、temperature、max_tokens、functions、seed（以及 thinking/reasoning）做 `json.dumps(sort_keys=True)` 后 SHA-256。
- 风险：`docs/llm-service-caching.md:49-60` 明确 top_p、presence_penalty、response_format 等不在默认 key；依赖这些参数时必须显式 cache_key。默认 key 因此可能发生“参数不同但误命中”，Ringharness 不应照搬不完整 key。
- tenant 隔离也不是默认 key：`docs/llm-service-caching.md:73-75` 要调用方自行把 tenant/session 加进 cache_key。对 Ringharness 这是不可接受的可选安全性，应成为强制结构化字段。
- TTL/容量：内存 cache 到期删除，容量满按最早 expiry 淘汰：`llm_provider/base.py:459-491`；Redis/内存选择与默认 3600s 在 `manager.py:503-537`。
- 安全失效：读到 strict JSON 非 object 或 finish_reason 为 length/content_filter 会删除：`manager.py:590-626`；写入时同样拒绝截断、过滤、无效 JSON、空结果：`manager.py:978-1011`。
- 未见 prompt/template/model profile 版本号作为独立 invalidation namespace；依赖 prompt bytes/functions/model 变化自然换 key。显式自定义 cache_key 可绕过这些差异，需额外治理。

### 3.3 provider prompt prefix 稳定机制

- 工具 schema 固定：`anthropic_provider.py:412-417` 按 tool name set freeze schema，合法 schema 更新到进程重启才生效；`:680-735` key 使用排序后的 `(name,defer_loading)`，输出 tools 按 name 排序。这是强前缀稳定机制。
- system prompt 稳定/易变分区：`anthropic_provider.py:648-678` 以 `<!-- volatile -->` 分开，只有 stable prefix 打 cache_control。
- 日期注入没有放在稳定前缀：`llm_service/api/agent.py:1091-1124` 把 current date 和语言指令收集为 volatile parts，非模板 prompt 放在 volatile marker 后。**可借鉴原则：时间戳/日期/随机 ID 禁止进入 cached stable prefix。**
- message rolling breakpoint：agent loop 只保留当前最后 user message 的一个 breakpoint：`agent.py:2076-2107`；provider 对倒数第二消息打 marker（`anthropic_provider.py:554-560`）。曾设计 previous rolling marker，但实测让 30-turn cache hit 从 93% 降到 61%，已禁用：`anthropic_provider.py:817-835`。应借鉴“benchmark 后关闭负优化”，不应照搬未启用代码。
- byte stability：tool schemas 排序且冻结；`tests/test_byte_stability.py:25-75` 明确 string content 与 block content wire bytes 不同，客户端必须固定形态；nested tool input key 顺序应稳定 hash。
- TTL：`anthropic_provider.py:153-195` 按 cache_source 选 1h/5m，未知来源 fail-cheap 为 5m，operator 可 force off/5m/1h；`:961-1019` 在 provider 出口强制所有 breakpoint 使用同一 TTL，避免 Anthropic `tools→system→messages` 单调约束错误。测试证据 `tests/test_cache_ttl_uniform.py:1-12,68-144,177-236`。
- cache break 可观测：`anthropic_provider.py:331-396` 比较 system hash、排序 tool names、model；`:943-959` 变化即 warning，给出 added/removed/model delta。
- 自动全 prompt caching 被明确否决：`anthropic_provider.py:913-919` 因 swarm 动态 shared state 造成约 7% hit、净成本增加 17%，只保留显式稳定 breakpoint。与“缓存不是越多越好”一致。

## 4. 分层模型策略

### 4.1 是否按难度路由

- complexity endpoint 可先让 SMALL 模型分类 simple/standard/complex，并返回 complexity、capabilities、estimated agents/tokens/cost、reasoning、source/provider；模型失败或 JSON 不合法回退 heuristic：`llm_service/api/complexity.py:22-38,136-209`。
- agent API 的实际 tier 优先级为 top-level override > context tier > SMALL 默认：`llm_service/api/agent.py:1417-1445`。因此“难度自动映射模型 tier”不是此入口的必然行为；需要 orchestrator/caller 显式传 tier。
- Go simple workflow 会按显式 tier > per-agent budget > medium 推导 tier，预算 ≤8k/32k/128k 对应 small/medium/large，否则 xlarge：`simple_workflow.go:695-739`。这更接近“上下文容量路由”，不完全等于任务推理难度路由。
- model catalog 定义 small/medium/large 及 provider priority：`config/models.yaml:16-135`；manager 将同 tier provider 按 priority 排序（`llm_provider/manager.py:462-480`），运行时按首个可用项锁定 model，并记录 circuit-breaker skip：`manager.py:829-868`。
- orchestrator 确实记录 decomposition 的 complexity/mode/subtask/cognitive strategy：`go/orchestrator/internal/workflows/orchestrator_router.go:760-765`，并以复杂度阈值+subtask/tool shape 判断 simple（`:888-890`），再路由不同 workflow。这是**任务工作流分层**，但本次切片未验证 complexity 总会机械映射成 small/medium/large model。

### 4.2 可审计性与成本上限

- 可审计：complexity response 带 reasoning/source/provider，debug 可带 raw output（`complexity.py:22-38,190-203`）；router 记录 complexity/mode/subtasks/cognitive strategy（`orchestrator_router.go:760-765`）；model manager 对明确 model、fallback、breaker skip 有日志（`manager.py:794-868`）。
- 成本配置：`config/models.yaml:137-149` 声明 priority selection、fallback/retries/timeout，以及 max cost/request、max tokens/request、daily budget。
- **但执行未完全验证**：`manager._translate_unified_config` 只读取 model_catalog/provider settings/model tiers/selection/prompt_cache/rate limits（`manager.py:376-386`），未读取 `cost_controls`；因此不能仅凭 YAML 宣称 `max_cost_per_request` 已硬执行。
- Go BudgetManager 有 token budget preflight/record actual，但其策略可配置为 warning/approval而非必然 hard stop（`budget/manager.go:250-268`）。Ringharness 已有 `ModelInvocation.max_cost_usd/max_output_tokens/data_categories`，应继续由确定性控制面 admission，而非让 classifier 输出的 estimated_cost 自行授权。
- **红线冲突**：complexity endpoint 在 provider 不可用/输出错误时静默回退 heuristic（`complexity.py:143-145,179-209`），适合可用性优先的分类服务；Ringharness 对缺关键配置要求 503，不能把这种 fallback 当成授权或预算放行。

---

# ② 与 Ringharness 的差距

| 领域 | Ringharness 当前 | Shannon 实际机制 | 差距/判断 |
|---|---|---|---|
| Context 预算 | 引用清单粗估，默认 8192，超限拒绝 | 历史 chars/4+overhead；provider 留 headroom；累计 budget manager | Ringharness 缺真实消息、tool schema、output reserve 分区；Shannon 估算也不精确 |
| Tool Result | #4 明确缺失，禁止静默裁剪 | 单类裁剪 + turn 字符累计，超限后停止；无持久指针 | Shannon 的双预算维度可借鉴，截断行为不可照搬 |
| 压缩 | 已有 `compaction_source_digest` 槽位，未见闭环 | 75% 触发、37.5% target、first3+summary+last20，summary Qdrant | Ringharness 缺压缩 worker/摘要 Artifact/可恢复 lineage；Shannon 也无分层压缩与可靠事实边界 |
| Memory admission | PROPOSED→VERIFIED | 模型结果/LLM 摘要可直接写 Qdrant 或 user files | Shannon 明显更弱；不可降低为“worth remembering” |
| Memory provenance | #15 部分，目标为 provenance ledger | 仅 session/user/tenant/model/timestamp/source type/similarity | 缺 authority_ref、content digest、verdict/evidence、revision、verification profile |
| Memory authority | PG 为业务事实权威 | Qdrant 对 Q&A/summary/decomposition 是活跃直接存储；重建未验证 | 与 Ringharness 红线冲突；只能把向量库当可删投影 |
| 检索 | 已有 `/memories`/INDEX_MEMORY，基准缺失 | threshold+topK+MMR+recent merge，总项 cap 10 | 可借鉴 retrieval pipeline，但必须 VERIFIED filter 前置并保留引用 |
| Prompt cache | 稳定性专项缺失 | stable/volatile 分区、tool 排序冻结、cache break detector、统一 TTL、负收益 benchmark | Shannon 最有直接价值的部分 |
| 分层模型 | ModelInvocation 有上限字段 | workflow complexity 分层 + tier/provider priority | 可借鉴可审计 routing decision；成本 hard cap 不应依赖 Shannon YAML 声明 |

---

# ③ 可直接借鉴的机制清单

## A. 可移植

1. **Stable/volatile prompt 分区**：固定 system/合同/工具 schema 放 stable prefix；日期、session 状态、当前 turn 放 `volatile` 后缀。来源：`anthropic_provider.py:648-678`、`agent.py:1091-1124`。
2. **工具 schema canonicalization**：按 name 排序、同 tool-set 冻结 schema，并对 schema/tool-set/model/system 变化记录 cache-break reason。来源：`anthropic_provider.py:331-396,680-735,943-959`。
3. **缓存出口安全门**：finish_reason=length/content_filter、invalid JSON、空输出不写 cache；读到坏项主动删除。来源：`manager.py:590-626,978-1011`。
4. **压缩触发防抖**：占窗口比例 + 距上次新增消息数 + 最短时间，避免高频重压缩。来源：`compression_activities.go:31-75`。Ringharness 应把时间换成可重放/持久化状态。
5. **检索候选池扩大后 MMR**：先取 topK×3，再兼顾 relevance/diversity，最终限制条数。来源：`semantic_memory_chunked.go:62-97,249-275`。必须叠加 VERIFIED/tenant/project filter。
6. **内容与索引分离**：MEMORY.md 淘汰只删索引引用，内容文件仍在。来源：`memory_extract.go:320-323,468-480`。Ringharness 可更强地采用 PG/Artifact 权威 + 可删向量投影。

## B. 需改造

1. **Tool Result 双预算**：保留单结果预算、单 turn 总预算、工具类别配额，但预算检查须在注入前 reservation/admission；超限原文写 Artifact，ContextBundle 只放 typed pointer、digest、size、preview、omitted metadata。Shannon `agent.py:1883-1899,2060-2061,2129-2138` 仅能提供维度，不能提供安全实现。
2. **滑窗摘要**：保留 75% trigger、目标占比、primers/recents 思路，但 summary 必须是不可变 Artifact，带 source bundle digest、algorithm/prompt/model version、classification、生成状态；压缩摘要是“非可信派生数据”，不得替代合同/验收证据。
3. **hierarchical retrieval**：recent+semantic+summary 合并可用，但 admission 顺序必须为 tenant/project/role/data-category/VERIFIED 硬过滤 → semantic/MMR 排序 → token packing；所有条目保留 provenance 引用。
4. **分层路由记录**：把 complexity/mode/tier/provider/reason/source/fallback 写成结构化 `RoutingDecision`，由 deterministic policy 对允许模型、数据类别、max cost 校验；模型 classifier 只能提议。
5. **exact-match cache key**：用 canonical request envelope，强制包含 tenant/project、model profile version、prompt template digest、tools digest、temperature/top_p/penalties/response format/seed/max output/data category；禁止调用方提供裸 key 覆盖安全字段。
6. **prompt-cache TTL by source**：可以按交互渠道选 5m/1h并提供强制关闭开关，但 TTL 选择必须写 invocation audit；敏感类别默认禁用跨 invocation response cache。

## C. 不适用/不可照搬及原因

1. **直接 `[:N]` 截断 Tool Result**：违反 Ringharness“禁止静默截断销毁信息”；必须外溢 Artifact 并可取回。
2. **Qdrant 直接保存并承担模型记忆权威**：违反业务事实权威在 PG；向量库必须可删可重建。
3. **LLM `worth_remembering` 直接落用户 memory**：无 PROPOSED→VERIFIED、无可信来源/验收证据，违反模型输出非可信。
4. **相似度/COMPLETED/success_rate 作为事实或成功经验**：semantic similarity 只表示近似，Workflow/agent COMPLETED 不等于 Kernel DONE。
5. **memory/embedding 缺失静默 stateless**：`docs/memory-system-architecture.md:32-49` 的 graceful degradation 与 Ringharness 关键配置失败关闭冲突；只有明确标为可选、且 VerificationProfile 不依赖记忆时才可降级。
6. **response cache 默认遗漏关键采样/格式参数、tenant 可选入 key**：存在错误命中和跨租户风险。
7. **classifier 出错即 heuristic 放行**：可以用于无权限建议，不得用于模型授权、成本 admission 或 DONE 路径。

---

# ④ Top 5 建议（为什么 / 改哪个文件 / 验收标准）

## Top 1：实现不可丢失的 ToolResultEnvelope + Artifact 外溢

- **为什么**：这是当前 #4 明确缺口；Shannon 证明单结果与 turn 总量都必须控，但其事后检查和截断不符合红线。
- **改哪里**：`packages/context_compiler/src/context_compiler/` 新增 tool-result packing/envelope；`apps/runner/src/harness/` 在 Tool Observation 进入下一 LLM turn 前封装；对象存储/工件登记仍走 `packages/evidence_ledger` 与 Kernel API，不让 Runner 直写业务事实。
- **验收**：
  1. 小结果 inline，包含 exact byte length/token estimate/digest。
  2. 单结果超限时完整原文成为 immutable Artifact，bundle 只含 typed ref+digest+preview；按 ref 可逐字节恢复且 hash 相等。
  3. 多结果单 turn 超限时 deterministic packing；不得超过预算；每个 omitted/outflow 都有 reason 与 ref。
  4. 恶意超长/Unicode/binary/嵌套 JSON fixture 不丢信息、不越 project/classification；无对象仓时 503/拒绝，不静默裁剪。

## Top 2：落地 provenance-first MemoryLedger 与可删索引重建

- **为什么**：Shannon 的 session/tenant/MMR 很实用，但它没有 VERIFIED admission，且 Qdrant 对活跃 memory 不可替代；这正是 #15 风险。
- **改哪里**：PG migration + `packages/control_kernel` memory protocol/storage；`INDEX_MEMORY` Activity；`packages/context_compiler` retrieval binding。
- **验收**：
  1. ledger 每条含 project_id、role/scope、source artifact+digest、classification、proposal author/model invocation、verification verdict/profile/evidence、revision/status。
  2. 默认召回只接受 VERIFIED；PROPOSED/REJECTED/跨 project/跨 role/越 classification 的负向测试均为 0 hit。
  3. 清空向量库后由 PG+Artifact 全量重建，固定 query 的 record-id 集、过滤结果、digest 与 golden baseline 一致。
  4. similarity score 永不改变事实状态，retrieval response 明示 score 与 authority/provenance 是不同字段。

## Top 3：实现可恢复的两级压缩（microcompact → macrocompact）

- **为什么**：Shannon 只有一次 lossy summary，且 docs 明确 hierarchical summary 未实现；Ringharness 已有 `compaction_source_digest`，适合建立更强闭环。
- **改哪里**：`packages/context_compiler/src/context_compiler/compile.py` 与新 `compaction.py`；Kernel artifact/provenance 协议；Temporal compaction Activity/worker。
- **验收**：
  1. 软阈值先 microcompact：去重复/结构化 tool envelope/引用化，不改合同与 VERIFIED evidence。
  2. 更高阈值才 macrocompact：生成 summary Artifact，带 source bundle digest、prompt/model/profile version；原件始终可取回。
  3. replay/重试对同 source digest 幂等；摘要写完才 checkpoint；存储失败不激活新 generation。
  4. adversarial fixture 断言 goal/task contracts、verification criteria、UNKNOWN effects、失败证据不可被摘要吞掉；最终 ContextBundle 在预算内。

## Top 4：建立 PromptPrefixManifest 与 byte-stability CI

- **为什么**：Shannon 实测表明日期、工具顺序/schema 漂移、content shape 会显著破坏命中；其 stable/volatile、tool freeze、cache-break detector 是最成熟可移植机制。
- **改哪里**：`packages/context_compiler` 生成 canonical prompt manifest；Runner connector 统一 wire content shape；`ModelInvocation` 增加 prompt/template/tools digest 与 cache decision；新增 cache stability tests。
- **验收**：
  1. 相同合同/bindings/tool manifest 重编译 byte-identical；map 插入顺序、进程重启不改 digest。
  2. 日期/trace/session volatile 字段变化不改变 stable-prefix digest；合同、tool schema、model profile 变化必须改变 digest并记录 break reason。
  3. PLAN activation 的 tools digest 对应严格空列表。
  4. cache key 强制含 tenant/project、全部生成参数及版本 digest；跨租户和 top_p/response_format 差异均 miss；截断/过滤/非法结构永不入 cache。

## Top 5：引入“模型提议、Kernel 准入”的可审计分层路由

- **为什么**：Shannon 已返回 complexity/reason/source/provider 并记录路由，但成本 YAML 的硬执行未验证、模型错误会 heuristic fallback。Ringharness 已有 ModelInvocation 上限，适合做更强 deterministic admission。
- **改哪里**：`packages/control_kernel` 增加 RoutingDecision/Policy；model profile/version storage；调用入口在创建 `ModelInvocation` 前执行；observability/read model 展示 proposal→decision→fallback。
- **验收**：
  1. fixture 将 simple/standard/complex proposal 映射到允许 tier；记录 classifier invocation、raw proposal artifact digest、规则版本、chosen model、拒绝/回退原因。
  2. `max_output_tokens/max_cost_usd/data_categories/cloud_mode` 任一不满足均在外呼前拒绝，调用计数为 0。
  3. classifier 不可用时：若 profile 允许 deterministic default 则明确 degraded decision；否则 503；两者都不能扩大权限或预算。
  4. 模型调用成功只记录 invocation，不触碰 Goal DONE；DONE 仍只由 ControlKernel VerificationProfile 判定。

---

## 总结

Shannon 最值得 Ringharness 吸收的是：**上下文预算维度、压缩防抖、recent+semantic+summary 检索编排、prompt stable/volatile 分区与工具 canonicalization、结构化路由观测**。最需要拒绝的是：**Tool Result 静默裁剪、Qdrant/LLM memory 直接承担事实、相似度或 COMPLETED 被当成正确、缺依赖 fail-open**。Ringharness 应把 Shannon 的性能机制包在更强的 PG 权威、Artifact 保全、VERIFIED admission、Kernel 准入和可机械验收之内。