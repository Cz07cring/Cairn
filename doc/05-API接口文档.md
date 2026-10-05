# API 接口文档

> V1 开发规格 v0.5；所有路由尚未实现。这是 ringharness 自有协议，不是上游 Harness 的原生 API。取代 v0.1 的任务 attempt/混合 operation 协议，旧版只在 [归档](archive/v0.1-before-audit-fixes.zip) 中保留。

## 1. 通用类型、版本与身份

公共前缀 `/api/v1`，内部 `/internal/v1`；JSON UTF-8、snake_case 字段、kebab-case 路径。UUID 使用字符串；时间 RFC3339 UTC；Digest=`sha256:`+64 位小写十六进制；epoch 与金额为十进制字符串。字符串/数组长度必须受 schema 限制；拒绝未知写字段。

所有 Resource 的 id、created_at、updated_at 由服务端生成。本文未重复写类型的字段，名称以_id结尾为uuid、以_ids结尾为uuid[]、以_digest结尾为Digest、以_at结尾为Timestamp、revision为正int；其余必须按显式模型定义，不得推测任意JSON。PlanCreate.tasks[].id 是例外：新节点由提交方分配UUID供同一候选图引用，仅在发布事务成功后成为正式Task ID；必须未占用，不能绑定其他Goal或冒充已有Task。保留节点使用其原ID且合同内容完全一致。EffectResource.id就是effect_id，ActivityResource.id就是activity_id，CommandOperation.id就是command_id，CandidateManifest.id就是candidate_manifest_id，不存在两份独立可写ID。可变业务资源有 state_revision:int（从1递增）；不可变配置有 version:int+content_digest。合同用 contract_revision，计划用 plan_revision。写入 body 的 expected_state_revision 只匹配 URL 所指业务资源；计划发布另带 expected_plan_revision。heartbeat、不可变对象上传、原始回执入库不依赖业务 revision。

浏览器同源 HttpOnly/Secure/SameSite=Lax session cookie；所有写请求校验 CSRF+Origin；CLI 短期 Bearer。角色 V=viewer、O=operator、A=approver、M=admin，必须有项目 scope。内部 workload 身份固定 worker_id、角色和允许 ActivityKind，不能靠 body 自报身份。跨项目资源用 404；未登录 401；已登录但动作不允许 403。

仅 `/health/live` 公开返回 alive；`/health/ready`、`/metrics` 只允许运维身份。OIDC login/callback 是公开身份入口，不返回项目数据。身份源 issuer/audience 固定 allowlist；具体 provider 配置由部署确定。

## 2. 三种标识不能混用

| 标识 | 作用域与规则 |
|---|---|
| Idempotency-Key | HTTP 变更请求重传；project+principal+method+规范化 path。无 project 则 principal+method+path。同 key 同规范化 body 返回首次结果；不同 body 409。内部协议也按请求去重，但不把它当业务效果键 |
| effect_id | Kernel 为已登记 logical_step_id+intent_revision 分配；跨 worker、attempt、传输请求不变。已绑定 payload_digest 不可修改；相同参数的新意图需要新 step/effect |
| receipt_id | 可信 Broker 单次结果消息标识；UNIQUE(effect_id,receipt_id)。同 ID 不同内容拒绝并告警；迟到回执不直接推进失租 attempt |

请求超时保留原 key，用命令查询或同 key 原 body 重发。恢复 Runner 必须读取已有 step/effect；新 principal 不得创建该 step 的第二份效果。活动 Goal 的请求键不删除，终态后最低30天；effect 去重记录只要其所属意图仍可被恢复/查询就不能回收。

所有同步创建返回201；查询/同步写200；异步命令202 CommandOperation。命令成功只说明控制操作完成，不代表 Goal DONE 或远端 effect 成功。

```json
{"data":{"id":"11111111-1111-4111-8111-111111111111","status":"DRAFT","state_revision":1},"error":null,"meta":{"request_id":"22222222-2222-4222-8222-222222222222"}}
```

分页 data 为数组，meta.next_cursor:string|null；cursor 不透明，limit 默认50、1—200，created_at+id 稳定排序。错误 data=null，error={code,message,details,retryable}；details 只含安全字段/current_state_revision/command_id/effect_id，不暴露堆栈、宿主路径或密钥。

| HTTP | code | 行为 |
|---|---|---|
| 400/422 | INVALID_REQUEST/VALIDATION_ERROR/DAG_CYCLE/COVERAGE_MISSING | 修正字段，拒绝整次无效写入 |
| 401/403/404 | UNAUTHENTICATED/FORBIDDEN/NOT_FOUND | 按身份和项目权限处理 |
| 409 | STATE_REVISION_CONFLICT/PLAN_REVISION_CONFLICT/IDEMPOTENCY_CONFLICT/EFFECT_PAYLOAD_CONFLICT | 刷新并解释；不能自动修改请求重发 |
| 409 | FENCING_REJECTED/WRITE_BARRIER_ACTIVE/INVALID_STATE | 停止新行动并核对现状 |
| 410 | EVENT_CURSOR_EXPIRED | 重新 snapshot |
| 413 | PAYLOAD_TOO_LARGE | 缩小请求/使用工件流 |
| 422 | EVIDENCE_INVALID/UNTRUSTED_PRODUCER/VERIFICATION_PROFILE_MISMATCH | 保留失败原因，不计作通过 |
| 429/503 | RATE_LIMITED/DEPENDENCY_UNAVAILABLE | 有限退避，尊重 Retry-After，未知数据不当零 |

默认 JSON 上限1MiB，objective 1—10000字符，criterion 1—200条，Goal.success_criteria与Task.acceptance均须1—200条且至少一条required=true；单计划最多1000节点/10000边；超限返回422 PLAN_LIMIT_EXCEEDED；V1当前计划的完整覆盖图必须在该上限内，不能以分批上传或裁剪节点绕过完整性校验。

## 3. 写入模型（未标 ? 均必填）

### 3.1 合同与验收

| 模型 | 字段/约束 |
|---|---|
| Budget | wall_clock_seconds:int>0,max_tokens:int≥0,max_cost_usd:string≥0,max_tool_calls:int≥0,max_network_calls:int≥0,max_disk_bytes:int≥0,max_gpu_seconds:DecimalString或null；null表示无GPU硬预算，仍记录可得用量 |
| TaskRetryPolicy | max_execution_rounds:int≥1,max_audit_attempts_per_candidate:int≥1,max_activity_retries:int≥0；默认4/3/3；上限不得超Goal或lineage批准额度 |
| RetryPolicy | max_execution_rounds:int≥1,max_audit_attempts_per_candidate:int≥1,max_activity_retries:int≥0,max_plan_revisions:int≥1；默认4/3/3/10 |
| Resources | cpu_millicores:int>0,memory_bytes:int>0,disk_bytes:int>0,model_slots:int≥0,browser_slots:int≥0,exclusive_labels:string[] |
| Criterion | id:string,description:string,required:bool,verification_profile_id:uuid；id 在所属合同稳定唯一，修改内容必须升合同版本 |
| GoalCreate | project_id:uuid,objective:string,success_criteria:Criterion[],constraints:string[],budget:Budget,retry_policy:RetryPolicy,policy_id:uuid,model_profile_id:uuid,skill_set_id:uuid,base_commit:string |
| TaskContract | objective:string,depends_on:uuid[],input_artifact_ids:uuid[],deliverables:{kind:string,required:bool}[],acceptance:Criterion[],covers_goal_criterion_ids:string[],allowed_paths:string[],protected_paths:string[],required_capabilities:string[],budget:Budget,retry_policy:TaskRetryPolicy,resources:Resources,risk:low/medium/high |
| CoverageEntry | goal_criterion_id:string,task_id:uuid,task_acceptance_id:string,verification_profile_id:uuid；verification_profile_id匹配Task acceptance批准配置；Goal criterion另有独立最终验证配置，不要求同一个profile |
| PlanCreate | expected_plan_revision:int或null,reason:string,tasks:{id:uuid,contract:TaskContract,replaces_task_id?:uuid}[],coverage:CoverageEntry[]；完整候选图，运行合同不能原地改写 |
| GoalContractUpdate | expected_state_revision:int,contract:GoalCreate,reason:string；仅DRAFT/PAUSED，project_id不可改 |
| VerificationProfileCreate | project_id:uuid,name:string,target_scope:TASK/GOAL/SKILL,verifier_ref:string,verifier_digest:Digest,required_layers:AuditLayer[],thresholds:Threshold[],required_evidence_kinds:string[],applicability_rule_ref:string；ref为批准registry引用，不接受任意shell |
| Threshold | metric:string,operator:EQ/LE/GE,expected:string,unit:string；验证器声明其解析类型；禁止模型自由解释阈值 |
| ControlRequest | expected_state_revision:int,reason:string（1—2000字符） |
| ReplanRequest | expected_state_revision:int,expected_plan_revision:int或null,reason:string |

AuditLayer=`MECHANICAL,SEMANTIC,ADVERSARIAL,GLOBAL`。前三层用于target_scope=TASK或SKILL，GLOBAL仅用于target_scope=GOAL；VerificationProfile 在执行前确定适用层与阈值，必要层失败后不能“有理由跳过”。非适用规则在合同批准前固定，不运行的层不记PASS。Goal coverage 中 required=true 的标准必须至少有一个精确承担关系；覆盖不等于已验证。

```json
{
  "project_id":"11111111-1111-4111-8111-111111111111",
  "objective":"修复示例仓库断线重连问题并验证",
  "success_criteria":[{"id":"C1","description":"独立重连验收通过","required":true,"verification_profile_id":"66666666-6666-4666-8666-666666666666"}],
  "constraints":["仅修改隔离工作区","不访问生产服务"],
  "budget":{"wall_clock_seconds":432000,"max_tokens":10000000,"max_cost_usd":"0","max_tool_calls":10000,"max_network_calls":1000,"max_disk_bytes":10737418240,"max_gpu_seconds":null},
  "retry_policy":{"max_execution_rounds":4,"max_audit_attempts_per_candidate":3,"max_activity_retries":3,"max_plan_revisions":10},
  "policy_id":"33333333-3333-4333-8333-333333333333",
  "model_profile_id":"44444444-4444-4444-8444-444444444444",
  "skill_set_id":"55555555-5555-4555-8555-555555555555",
  "base_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

示例 ID/commit 均为占位。max_cost_usd=0 禁止付费调用，不代表硬件无成本。

### 3.2 运行模型与版本

ActivityKind=`PLAN,EXECUTE,AUDIT,INTEGRATE,RECONCILE,FINALIZE,PROBE_MODEL,VALIDATE_SKILL,INDEX_MEMORY,EXPORT_EVIDENCE`。
ActivityStatus=`PENDING,READY,RUNNING,WAITING,RECOVERING,SUCCEEDED,FAILED,CANCELLED`。
GoalStatus/TaskStatus 以 [01](01-开发文档.md) 第5节唯一枚举为准。

| 模型 | 字段（Resource另含公共id/时间） |
|---|---|
| GoalResource | project_id,status:GoalStatus,state_revision,contract_revision,plan_revision:int或null,contract:GoalCreate,previous_status:GoalStatus或null,integration_commit:string或null,criterion_summary:{verified:int,total:int},budget_usage:BudgetUsage,block_reason:string或null,write_epoch:string,barrier:BarrierResource或null,release_manifest_id:uuid或null |
| GoalWallBudgetSnapshot | budget_usage_unknown:bool,elapsed_wall_seconds:int或null,active_seconds:int或null,budget_remaining_wall_seconds:int或null,budget_exhausted:bool,wall_clock_limit_seconds:int或null,marks_goal_done:false |
| OrchestrationAbandonmentCreate | reason:string,generation:int,prior_run_id:string或null |
| OrchestrationAbandonmentResource | id,project_id,goal_id,generation,reason,prior_run_id,worker_id,created_at,marks_goal_done:false |
| TaskResource | goal_id,contract:TaskContract,contract_revision,plan_revision,state_revision,status:TaskStatus,block_reason:string或null,resume_state:TaskStatus或null,work_lineage_id:uuid,execution_round:int,latest_checkpoint_id:uuid或null,replaces_task_id:uuid或null |
| ActivityResource | project_id,goal_id:uuid或null,task_id:uuid或null,binding:ExecutionBinding,budget_scope_id:uuid,kind:ActivityKind,target:ActivityTarget,verification_assignments:VerificationAssignment[],status:ActivityStatus,state_revision,depends_on_activity_ids:uuid[],wait_reason:string或null,wake_at:string或null,wait_deadline_at:string或null,resume_state:READY或null,retry_count:int,current_attempt_id:uuid或null,resources:Resources |
| ExecutionBinding | goal_contract_revision:int或null,goal_contract_digest:Digest或null,task_contract_revision:int或null,task_contract_digest:Digest或null,plan_revision:int或null,subject_digest:Digest,policy_digest:Digest,model_profile_digest:Digest或null,skill_set_digest:Digest或null；不可变，摘要按Content协议 |
| VerificationAssignment | subject_type:CANDIDATE/SKILL_VERSION,subject_id:uuid,subject_digest:Digest,verification_profile_id:uuid,profile_digest:Digest,layer:AuditLayer,audit_round:int≥1；Kernel创建Activity时固定，AUDIT候选/FINALIZE/VALIDATE_SKILL必需，其他kind为空 |
| ActivityTarget | type:GOAL_PLAN/TASK_WORK/CANDIDATE/GOAL_REVIEW/INTEGRATION/EFFECT/FINALIZATION/MODEL_PROFILE/SKILL_VERSION/MEMORY_INDEX/RELEASE_EXPORT,id:uuid；kind与type为固定映射，不能任意组合 |
| ActivityLease | lease:LeaseIdentity,activity:ActivityResource,attempt:ActivityAttemptResource,input_artifact_ids:uuid[],policy_snapshot_id:uuid；claim返回的有租约分支 |
| LeaseIdentity | activity_id:uuid,attempt_id:uuid,fencing_epoch:string |
| ActivityAttemptResource | activity_id,worker_id,binding_digest:Digest,fencing_epoch,lease_expires_at,renewal_seq:int,status:ACTIVE/COMPLETED/FAILED/EXPIRED/CANCELLED,context_digest:Digest或null,model_snapshot:ModelSnapshot或null,skill_versions:{version_id:uuid,content_digest:Digest}[],started_at,finished_at:string或null；无业务state_revision |
| ModelSnapshot | profile_id:uuid,profile_version:int,provider_ref:string,model_id:string,config_digest:Digest |
| StepResource | activity_id,logical_step_id:uuid,predecessor_step_id:uuid或null,purpose:string,tool_ref:string,intent_revision:int,effect_id:uuid或null；V1单Activity顺序登记步骤，根与每个前驱最多一个后继 |
| CommandOperation | project_id:uuid或null,goal_id:uuid或null,kind:CommandKind,status:ACCEPTED/RUNNING/SUCCEEDED/FAILED,request_digest:Digest,result:CommandResult或null,error:{code,message}或null；命令响应未知由客户端核对，不保存成远端效果UNKNOWN |
| BarrierResource | goal_id,write_epoch,status:DRAINING/SEALED/RELEASED/ABORTED,contract_revision,plan_revision,candidate_manifest_id:uuid或null,in_flight_engineering:int,unknown_effects:int |
| BudgetUsage | consumed_tokens:int,reserved_tokens:int,consumed_cost_usd:string,reserved_cost_usd:string,cost_status:CONFIRMED/ESTIMATED/UNKNOWN,elapsed_wall_seconds:int,active_seconds:int,tool_calls:int,network_calls:int,disk_bytes:int,gpu_seconds:DecimalString或null,observed_at:string |

CommandKind 固定为 START/PAUSE/RESUME/CANCEL_GOAL/CANCEL_TASK/RETRY_TASK/REPLAN/RECOVER_FINALIZATION/PROBE_MODEL/VALIDATE_SKILL/RECONCILE_EFFECT/EXPORT_EVIDENCE。CommandResult 是按 kind 的联合：控制命令{goal_id,final_status}；取消任务{task_id,final_status}；重做{replacement_task_id,work_lineage_id}；重规划{goal_id,plan_revision}；恢复验收{goal_id,barrier_id,action:REVERIFY/REWORK,write_epoch,activity_id}；探测{profile_id,activity_id,evidence_ids,capability_status}；Skill验证{version_id,activity_id,audit_id,verdict}（audit_id指SkillValidationRecord）；对账{effect_id,observed_status}；导出{artifact_id,trust_mode}。

### 3.3 效果、审批与可信回执

| 模型 | 字段 |
|---|---|
| EffectResource | project_id,goal_id:uuid或null,activity_id,logical_step_id,intent_revision,payload_digest:Digest,tool_ref:string,replay_class:READ_ONLY/IDEMPOTENT/RECONCILABLE/NON_REPLAYABLE,scope:ENGINEERING/VERIFICATION/RECONCILIATION/READ_ONLY,write_epoch:string或null,status:PREPARED/AUTHORIZED/DISPATCHED/SUCCEEDED/FAILED/UNKNOWN/CANCELLED,state_revision,approval_id:uuid或null,reservation_id:uuid或null,external_ref:string或null,evidence_ids:uuid[] |
| ApprovalResource | project_id,goal_id:uuid或null,state_revision,subject:ApprovalSubject,payload_digest,policy_version:int,scope:ApprovalScope,max_cost_usd:DecimalString,expires_at,status:PENDING/APPROVED/DENIED/EXPIRED/REVOKED,consumed_subject_id:uuid或null,decision_reason:string或null |
| ApprovalSubject | {type:EFFECT,id:uuid}或{type:MODEL_INVOCATION,id:uuid}；由受信程序绑定，不接受模型申请改type |
| ApprovalScope | tools:string[],target_refs:string[],data_categories:string[],provider_refs:string[]；不得用任意JSON扩大范围 |
| ApprovalDecision | expected_state_revision:int,decision:APPROVE/DENY,subject:ApprovalSubject,payload_digest,reason:string；必须与原请求相同，不从body重新赋权限 |
| ReconciliationRequest | expected_state_revision:int,observed_result:SUCCEEDED/FAILED,external_ref:string,evidence_ids:uuid[],reason:string；仅UNKNOWN，Kernel验证证据后推进 |
| TrustedReceipt | receipt_id:uuid,effect_id,producer_activity_id,producer_attempt_id,fencing_epoch,started_at,finished_at,exit_code:int或null,signal:string或null,timed_out:bool,stdout_artifact_id:uuid或null,stderr_artifact_id:uuid或null,result_artifact_ids:uuid[],external_ref:string或null,observed_outcome:SUCCEEDED/FAILED/UNKNOWN；生产者身份从认证通道提取，不接受模型字段 |

effect payload_digest 是ToolPayload Content envelope的JCS/SHA256摘要（含工具schema和参数）；scope/replay_class 来自工具registry+合同，Runner不能自设。旧attempt回执可被受信Broker追加到原effect的inbox，但不能用它直接完成新Activity。对账许可只允许观察既有效果，不允许借 RECONCILE 发新工程写入。

### 3.4 证据与清单

| 模型 | 字段 |
|---|---|
| ArtifactResource | project_id,digest:Digest,size_bytes:int,mime:string,representation:RAW/REDACTED/TRUNCATED,derived_from_artifact_id:uuid或null,producer_identity:string；storage_key不暴露 |
| EvidenceEnvelope | schema_version:int,project_id,goal_id:uuid或null,task_id:uuid或null,producer_activity_id,producer_attempt_id,effect_id,receipt_id,contract_digest:Digest或null,candidate_manifest_id:uuid或null,verification_profile_id:uuid或null,command_argv:string[],input_digest:Digest,environment_digest:Digest,started_at,finished_at,exit_code:int或null,signal:string或null,timed_out:bool,artifact_ids:uuid[],producer_identity:string,content_digest:Digest；Ledger从可信receipt与登记的effect生成 |
| CandidateManifest | schema_version:int,project_id,goal_id,task_id:uuid或null,protected_baseline_digest:Digest,git_commit:string或null,files:{path:string,digest:Digest,mode:string}[],dependency_lock_digests:Digest[],submodules:{path,commit,content_digest}[],lfs_objects:{path,oid,content_digest}[],image_digests:string[],verification_profile_ids:uuid[],content_digest:Digest；路径唯一且规范化，无可变文件名引用 |
| Checkpoint | activity_id,attempt_id,kind:ActivityKind,context_digest:Digest,completed_step_ids:uuid[],next_step_id:uuid或null,candidate_manifest_id:uuid或null,workspace_manifest_digest:Digest或null,session_ref:string或null,artifact_ids:uuid[],effect_ids:uuid[],schema_version:int；按kind验证，引用必须已持久化 |
| CriterionResult | criterion_id:string,verdict:PASS/INSUFFICIENT/FAIL,evidence_ids:uuid[],reason:string |
| AuditResource | producer_activity_id,producer_attempt_id,subject_candidate_manifest_id:uuid,goal_contract_revision:int,task_contract_revision:int或null,verification_profile_id:uuid,layer:AuditLayer,audit_round:int≥1,verifier_run_ids:uuid[],verdict:PASS/INSUFFICIENT/FAIL,criterion_results:CriterionResult[],evidence_ids:uuid[],reason:string；只表示候选验收 |
| GoalReviewResource | producer_activity_id,producer_attempt_id,goal_id,goal_contract_revision:int,plan_revision:int或null,review_snapshot_digest:Digest,findings:{code:string,severity:INFO/WARN/BLOCKER,evidence_ids:uuid[],recommendation:string}[]；是周期诊断，不产生criterion PASS |
| ReleaseManifest | goal_id,barrier_id,write_epoch,goal_contract_digest:Digest,plan_digest:Digest,candidate_manifest_id,verification_profile_ids:uuid[],audit_ids:uuid[],evidence_ids:uuid[],content_digest:Digest |
| EvidenceExportRequest | release_manifest_id:uuid,trust_mode:INTERNAL_COPY/OFFLINE_VERIFIABLE |
| AttestationResource | payload_artifact_id:uuid,subject_digest:Digest,format_ref:string,signature_artifact_id:uuid,key_id:string,trust_bundle_id:uuid；OFFLINE_VERIFIABLE必须有可验证的此记录 |

hash 输入：每种对象使用显式版本化Content schema按JCS编码再SHA256；排除自身content_digest与存储元数据的字段清单由schema固定，禁止先对任意Resource整包删字段再猜测输入。引用闭包和数组顺序规则见第6.1节B07。文件直接对保存字节SHA256。每个引用解析到实际 digest 后验证，不能仅比较可被替换的显示名称。CandidateManifest 封存后不可改；Auditor 只能读取或构建验证临时副本。

离线可验证导出固定format_ref=dsse-json-envelope-1.0.2，payloadType=application/vnd.ringharness.evidence.v1+json，payload为批准证据清单的JCS字节；已配置签名provider按DSSE协议签发，并携带锁定的format_ref和trust_bundle；无配置返回503 SIGNING_UNAVAILABLE，不能悄悄降级为可信导出。INTERNAL_COPY明确标记只有内部身份记录，不保证离线来源。签名验证必须使用预配置可信根/允许producer及key策略，不能直接信任导出包自带的公钥。对应用使用的是同一份已验签payload字节；禁止验签后重新解析另一份内容。签名不等于验收通过。[DSSE Envelope](https://github.com/secure-systems-lab/dsse/blob/master/envelope.md)

### 3.4.1 MEA隔离数据契约（C01—C04）

ContextBundle={schema_version:int,project_id,goal_id:uuid或null,activity_id,role:PLANNER/EXECUTOR/AUDITOR/SYSTEM,goal_contract_revision:int或null,task_contract_revision:int或null,plan_revision:int或null,input_bindings:{artifact_id:uuid,digest:Digest,classification:CONTRACT/CANDIDATE/BASELINE/EVIDENCE/MEMORY/SKILL}[],excluded_refs:{ref:string,reason_code:string}[],session_generation:int,compaction_source_digest:Digest或null,planning_feedback_ids:uuid[],content_digest:Digest}。引用同项目且逐角色授权；系统自动选择role并绑定已登记Activity，模型不能通过字段申请其他身份。敏感排除引用仅供授权诊断，不把隐藏holdout的名称或摘要发给Executor。模型实际输入须与角色过滤后快照一致，原始快照不直接作为模型消息。

ProtectedBaseline={schema_version:int,project_id,goal_id,goal_contract_revision:int,entries:{ref:string,content_digest:Digest,kind:CONTRACT/VERIFIER/GOLDEN/HOLDOUT/POLICY/SKILL}[],content_digest:Digest}。每Goal合同版本固定一份，由受信服务创建，公开响应按权限屏蔽隐藏条目；CandidateManifest新增protected_baseline_digest:Digest，Ledger封存与审计都须匹配。BaselineCheck={project_id,goal_id,producer_activity_id,producer_attempt_id,execution_round:int或null,baseline_digest:Digest,candidate_manifest_id:uuid或null,phase:BEFORE_EXECUTION/BEFORE_SEAL/BEFORE_VERIFY/AFTER_VERIFY,verdict:UNCHANGED/TAMPERED,producer_identity:string,evidence_ids:uuid[]}由受信采集端登记，模型不能提交权威verdict；改基线409 BASELINE_TAMPERED，不准DONE。

ContextBundle与ProtectedBaseline内容进入B07引用闭包，但导出仍遵守holdout ACL；无法包含受限必需对象的导出不能声称可完整离线复验，应拒绝OFFLINE_VERIFIABLE或由调用方明确选择受限INTERNAL_COPY，不能静默删引用后签名为完整包。允许仅给独立授权验证环境提供受限对象，前端展示验证结论与受限原因。

Session复用策略以01§13为准；checkpoint保存context_digest，宿主审计日志记录generation与来源。MemoryResource的VERIFIED转换没有模型公共写端点，固定核验服务必须检查来源证据与版本；置信分数不是提升为事实的凭据。


### 3.4.2 三态反馈与会话清理协议（D01—D04）

AuditAggregation由Kernel生成，不增加第四种verdict：{subject_candidate_manifest_id,goal_contract_revision,task_contract_revision:int或null,plan_revision:int,required_set_digest:Digest,accepted_audit_ids:uuid[],accepted_assessment_ids:uuid[],pending_verification_count:int≥0,trust_revision:Epoch,verdict:PASS/INSUFFICIENT/FAIL,blocking_reason_codes:string[],missing_items:{verification_profile_id,layer,criterion_id}[]}。汇总规则见01§14 D02，存在blocking_reason_codes时不能推进完成，即使已完成业务项均PASS。模型outcome的verdict必须与可核验逐项证据一致，否则422 AUDIT_VERDICT_MISMATCH；无必要记录返回INSUFFICIENT，不能由空数组推出PASS。GLOBAL使用同一规则。

D03反馈协议：PlanningFeedback={id:uuid,goal_id,task_id:uuid或null,candidate_manifest_id:uuid,goal_contract_revision:int,task_contract_revision:int或null,plan_revision:int,aggregation_digest:Digest,verdict:PASS/INSUFFICIENT/FAIL,public_criterion_results:CriterionResult[],blocking_reason_codes:string[],created_at}，不可变、由Kernel创建，不能作为模型写模型。去重键为(goal_id,task_id,candidate_manifest_id,goal_contract_revision,plan_revision,aggregation_digest)，task_id为空必须采用NULL相等语义的唯一约束。相同汇总重发返回同反馈；不同审计进展形成新摘要。ContextBundle新增planning_feedback_ids:uuid[]；只有PLANNER可接收授权反馈，其他角色为空。宿主记录(plan_activity_id,feedback_id)消费关系，只能确认该activation实际输入过的反馈，在合法PLAN outcome事务中落盘。初次无反馈允许空数组。

基于原有GET /goals/{goal_id}/audits读取AuditResource/GoalReviewResource，候选验收项带服务端aggregation:AuditAggregation；GoalSnapshot新增planning_feedback:PlanningFeedback[]和feedback_truncated:bool，仅返回当前适用版本最新100项。超限明确truncated，Manager消费独立持久队列而非前端snapshot，不被该显示上限裁掉；前端不宣称显示全部反馈。原始审计分页仍可追溯。候选反馈与周期诊断使用不同类型，GOAL_REVIEW不伪装成三态验收反馈。

SessionCleanupRecord={id:uuid,activity_id,attempt_id,session_generation:int,status:PENDING/RETRYING/CONFIRMED,pending_since,deadline_at,attempt_count:int,confirmed_at:string或null,failure_code:string或null,scope:LOCAL_TRAJECTORY}。该记录不含推理、原始session token或provider凭据，CONFIRMED只证明配置范围内的本地清理。原始轨迹在隔离临时存储中，不能因审计留痕而复制到EvidenceEnvelope。远端provider是否清理是单独能力与证据，不能从此字段推断。

BaselineCheck补齐project_id、goal_id、producer_activity_id、producer_attempt_id、execution_round:int或null；每个适用(基线digest,attempt,phase,候选)必须有受信检查。ProtectedBaseline/ContextBundle/检查记录/反馈都是显式Content schema；各自digest引用遵守B07，不将其自己的digest加入自身输入。公开接口继续做角色ACL过滤，不能新增可写“审批三态”或“强制清理成功”端点。


### 3.5 宿主模型推理与统一审批（DEV01）

| 模型 | 固定字段 |
|---|---|
| ModelInvocationCreate | lease:LeaseIdentity,invocation_seq:int≥1,context_digest:Digest,input_digest:Digest,provider_ref:string,model_id:string,max_output_tokens:int>0,max_cost_usd:DecimalString,data_categories:string[],exposed_tools:string[]（可选，默认空；PLAN 必须空；EXECUTE 登记暴露工具名，不进 Content v3）；input_digest绑定受信宿主实际发送字节摘要 |
| ModelInvocationResource | id,project_id,goal_id:uuid或null,activity_id,producer_attempt_id,invocation_seq,payload_digest:Digest,binding_digest:Digest,context_digest:Digest,input_digest:Digest,provider_ref,model_id,max_output_tokens,max_cost_usd,data_categories:string[],exposed_tools:string[],status:PREPARED/AWAITING_APPROVAL/AUTHORIZED/DISPATCHED/SUCCEEDED/FAILED/UNKNOWN/CANCELLED,state_revision,approval_id:uuid或null,reservation_id:uuid,usage_status:CONFIRMED/UNKNOWN,result_artifact_id:uuid或null；live dispatch 响应可另附 assistant_text 与 tool_calls（不持久化） |
| ModelReceipt | receipt_id:uuid,invocation_id:uuid,producer_attempt_id:uuid,observed_result:SUCCEEDED/FAILED/UNKNOWN,usage_status:CONFIRMED/UNKNOWN,input_tokens:int或null,output_tokens:int或null,cost_usd:DecimalString或null,result_artifact_id:uuid或null,observed_at:Timestamp；身份从认证通道取 |

宿主按Kernel批准模型配置发起推理，Planner不能自主调用该API、选择任意provider或改变待发送内容。UNIQUE(activity_id,invocation_seq)跨attempt去重；新输入用新序号，原序号不同input_digest返回409 MODEL_INPUT_CONFLICT。当前attempt接管先读原调用；已经DISPATCHED/UNKNOWN的调用不再dispatch，同调用最多一次发出。新推理属于新记录且重新预算/审批，旧未知费用保留；响应丢失不能视为免费重试。

模型调用与工程EffectIntent分开：Planner steps/effects仍全部禁止。Kernel校验固定binding、context、provider、model、数据范围和输出/token/cost上界后预留预算；本地或PREAUTHORIZED按合同准入，DENY云调用拒绝，APPROVAL创建subject=MODEL_INVOCATION的ApprovalResource。审批绑定invocation_id及整个调用内容摘要，改输入/provider/model/上限使旧授权不适用。等待审批使用Activity WAITING，允许新attempt在上下文与binding完全相同时接管未发出的调用；相应生产attempt在真正dispatch时确定。

dispatch按scope→Activity→model_invocation→approval→资源→预算顺序，复查未过期lease/当前合同/权限/审批，消费、预留确认、DISPATCHED与outbox同事务提交，然后受信连接器发出。正常工具审批subject=EFFECT遵守同样消费规则；UNIQUE(approval_id)，subject永久不可改，consumed_subject_id绑定原对象。拒绝/撤销/过期均不发出；发出后撤销不能抹除可能发生的费用或数据传输。暂停/取消拒新模型dispatch，已发出先停止/核对；晚到结果可结算原账，但不推进旧attempt。

原始推理不入长期工件，result_artifact_id只指结构化结果或允许保留的说明。费用未知保留预留；若provider无法证明实际用量，按批准最大额保守结算并保留usage_status=UNKNOWN，不能伪装实测值。不能获得可执行硬输出/费用上界的provider不准用于硬预算任务。对账由固定连接器后台操作完成，不由Planner调用工具，也不借RecoveryBudget启动新推理。

### 3.6 验收恢复与停止证明（DEV02/DEV03）

| 模型 | 固定字段 |
|---|---|
| FinalizationRecoveryRequest | expected_state_revision:int,expected_plan_revision:int,barrier_id:uuid,expected_write_epoch:Epoch,action:REVERIFY/REWORK,reason:string |
| ActivationRef | activation_id:uuid,activity_id:uuid,attempt_id:uuid,resource_instance_ids:uuid[]；宿主登记的具体进程/容器代次，不复用PID作为身份 |
| CheckpointProposal | lease:LeaseIdentity,checkpoint:Checkpoint；Kernel校验后才持久采纳 |
| StopRequest | request_id:uuid,activation_id:uuid,activity_id:uuid,attempt_id:uuid,fencing_epoch:Epoch,reason:PAUSE/CANCEL/LEASE_EXPIRED/FINALIZATION_RECOVERY/SHUTDOWN/TRUST_INVALIDATION,deadline_at:Timestamp；Kernel发出 |
| StopResource | id:uuid,request:StopRequest,status:REQUESTED/CONFIRMED/UNCONFIRMED,state_revision:int,receipt_ids:uuid[]；UNCONFIRMED表示截止时仍未证明停止，不代表完成 |
| StopReceipt | receipt_id:uuid,stop_id:uuid,activation_id:uuid,attempt_id:uuid,resource_instance_id:uuid,observed_at:Timestamp,observation:EXITED/ISOLATED/RUNNING/UNKNOWN,compute_released:bool,write_capability_revoked:bool,proof_artifact_ids:uuid[]；可信宿主/Broker签发，模型字段不能成为证明 |

**恢复入口：** 普通replan始终不能越过有效屏障。只有RECOVER_FINALIZATION命令可处理Goal BLOCKED且block_reason为FINALIZATION_FAIL/FINALIZATION_INSUFFICIENT，并且指定Barrier SEALED的情形；其他状态409 INVALID_STATE。O可通过公共入口提交；controller仅可根据本次有效最终审计三态和原合同预算自动创建相同类型command，同样校验、去重和计数。自动恢复按(barrier_id,aggregation_digest,action)唯一；FAIL选择REWORK，INSUFFICIENT选择REVERIFY，安全阻断必须先由授权维护流程消除，不能自批解除。

命令受理后关闭旧FINALIZE/AUDIT继续准入，等待全部旧验证activation停止并核对effects/model_invocations；若UNKNOWN/未证实停止仍在则保持command RUNNING与Goal BLOCKED。超过固定恢复deadline则command FAILED且屏障不变，不能超时自动开放。网络等待在事务外；最终短事务复核Goal/plan/epoch/barrier CAS和预算计数：

- REVERIFY：仅证据不足且候选/合同/验证器未变，无有效业务FAIL；保留SEALED与write_epoch，原FINALIZE终态不复活，新建FINALIZE Activity，Goal→VERIFYING。计入同候选/层审计上限。
- REWORK：允许有证据的可修复失败，排空全部在途写入及未知结果后，Barrier→ABORTED、write_epoch递增、Goal→RUNNING，并同事务创建PLAN Activity、记录恢复事件。原图所有EXECUTE/INTEGRATE暂停准入，直到新plan_revision合法发布；新写入许可按新binding/epoch重新签发，旧许可永不复用。

原候选/审计保留；未通过最终验收不生成可交付ReleaseManifest。预算/重试耗尽409 BUDGET_EXHAUSTED，保持阻塞或按合同失败，不强行恢复。并发恢复只有一命令通过CAS；已受理命令重复按request key返回同command，不能增加两代epoch。

**停止证明：** request_id对指定activation与参数唯一，重传不重复stop；deadline不因重试延长。StopReceipt按receipt_id去重，参数矛盾隔离，旧activation的回执不能释放新实例。仅实际EXITED且compute_released=true能回收CPU/GPU预留；ISOLATED且write_capability_revoked=true只证明不能写受保护scope，计算资源仍quarantine直到释放。RUNNING/UNKNOWN或无proof不能确认停止。所有实例均有满足本次要求的可信证明才StopResource CONFIRMED；Goal封存还要求工程效果无DISPATCHED/UNKNOWN，不仅看Stop状态。停止远端异步请求无法确认时，保留其effect/model_invocation未知记录。

Runner seam的stop只提交StopRequest并返回StopResource；最终证明通过独立StopReceipt通道到达。会话清理与进程退出分别记录，不以session删除替代停止证明。

### 3.7 数值与内容摘要协议（DEV05）

Timestamp固定UTC RFC3339，格式YYYY-MM-DDTHH:mm:ss.SSSZ。普通int均在[-9007199254740991,9007199254740991]内，再受字段非负/上限约束；超范围拒绝，不舍入。Epoch为无前导零非负十进制字符串，持久BIGINT上限9223372036854775807。DecimalString匹配0或非零整数开头、可带1—6位小数，无指数/正号/负号/多余尾零；零写"0"，金额及GPU秒用该类型。confidence_bp为0—10000整数。所有协议JSON数字只允许安全整数，浮点、NaN、Infinity、-0、重复键、无效Unicode代理项一律拒绝；整数接收后跨语言不会失真。

内容散列不直接对Resource做字段删除。固定envelope={schema_version:3,object_type:string,content:对应字段对象,reference_bindings:ReferenceBinding[]}。ReferenceBinding={object_type:string,object_id:string,content_digest:Digest}，按(object_type,object_id)排序且键唯一。各类型精确字段与嵌套结构、数组语义以[内容Schema](contracts/content-v3.schema.json)和[规范化规则](contracts/canonicalization-v3.json)为准，不允许未知字段。hash=SHA256(JCS(envelope)的UTF-8字节)，自己的content_digest不在输入。v1/v2仅用于读取历史证据；新写入固定Content v3。升级改变envelope摘要，禁止把旧版对象标成v3或重用旧digest。Plan Content只用于已发布图，API的replaces_task_id缺省先映射null；schema_version只在envelope出现一次，业务内容不重复该字段。工具effect的payload_digest为ToolPayload Content摘要；模型审批payload_digest为ModelInvocationInput Content摘要，二者都不是仅对可变参数显示文本散列。ReferenceBinding固定直接依赖；离线导出包含传递依赖并逐对象验证，不能以临时查“最新”对象替代。无依赖用空数组。非内容字段如lease、状态、session token、可变审批消费不进入Content。

集合数组按规范化规则排序、拒重复；有序数组（命令argv、任务步骤顺序、模型消息）保留顺序。JCS对象键按UTF-16码元序，无Unicode归一化。文件hash只对保存字节；ToolPayload由固定tool_ref+tool_schema_digest指定的参数schema校验，外层envelope固定，不把任意工具参数视为通用有权限JSON。模型输入input_digest对真实发送的固定请求字节计算，原始推理轨迹不属于请求内容。

[固定测试向量](contracts/hash-vectors-v3.json)列出输入、精确规范字节和预期SHA256；Python与JavaScript分别实现检查，边界/空值/Unicode/集合/有序数组/引用均覆盖。当前仅验证规格与向量，不宣称运行Ledger已实现。

### 3.8 验证器与项目级技能验收（RD01/RD05）

[Content v3 Schema](contracts/content-v3.schema.json)是下列字段的精确机器定义。公共Resource另加id/created_at/updated_at/content_digest；业务内容不得包含自己的摘要。新Content种类VerifierDefinition、VerificationRun、SkillValidationRecord均进入签名引用闭包。

| 对象 | 固定内容与责任 |
|---|---|
| VerifierDefinition | project_id,name,layer,image_digest,entrypoint_ref,input_schema_digest,output_schema_digest,timeout_seconds,metric_definitions:{metric,value_type:INTEGER/DECIMAL/BOOLEAN/STRING,unit}[],rubric_artifact_id:uuid或null,fixture_set_digest,uncertain_rule=INSUFFICIENT,timeout_rule=INFRA_UNLESS_TRUSTED_METRIC,network_policy_digest；受信管理员registry发布，profile.verifier_ref/digest引用同一不可变定义 |
| VerificationRun | project_id,producer_activity_id,producer_attempt_id,subject_type:CANDIDATE/SKILL_VERSION,subject_id,subject_digest,verification_profile_id,verifier_digest,audit_round:int≥1,layer,input_digest,environment_digest,receipt_ids:uuid[],observations:{criterion_id,metric,value:string或null,status:OBSERVED/MISSING/INFRA_ERROR/UNCERTAIN,evidence_ids:uuid[],reason_code}[]；可信验证宿主根据真实回执生成，API在模型角色之外 |
| SkillValidationRecord | project_id,producer_activity_id,producer_attempt_id,subject_skill_version_id,subject_digest,verification_profile_id,audit_round,verifier_run_ids:uuid[],verdict:PASS/INSUFFICIENT/FAIL,criterion_results:CriterionResult[],evidence_ids:uuid[],reason；Kernel在合法VALIDATE_SKILL outcome事务生成，与Skill状态/outbox原子提交 |

VerificationProfile每份只绑定一个VerifierDefinition.layer，required_layers必须恰为该层；需要多层时使用多份profile和独立criterion覆盖，禁止一份定义隐式承担其他层。GOAL只用GLOBAL，TASK/SKILL用前三层。Skill为单profile的V1合同，activation必须检查该profile所有必要criterion，不声称覆盖未配置层；需更严验证可由其固定验证器组合已批准检查，但不能冒充额外审计层。SkillCreate.acceptance固定于版本，profile修改需要新Skill版本；profile与定义都不可变且同项目。配置由受信部署registry加载（不是模型工具），校验引用/类型后才可被POST verification-profiles引用，未知定义拒绝。

验证输入合同：{subject_type,subject_id,subject_digest,profile_digest,verifier_digest,audit_round,criteria:Criterion[],candidate_or_skill_artifact_ids:uuid[]}，全为固定对象引用；input_schema_digest指向此合同批准Schema工件，输出schema指向VerificationRun业务内容批准Schema工件。宿主装配实际字节并保存input_digest，读取候选只读副本或Skill工件。验证器镜像、入口、网络及deadline从定义取得，不运行模型提供的任意命令。GLOBAL必须针对最终候选和Goal criteria。

阈值：profile.thresholds各metric须在定义内声明且单位完全一致；每条必要criterion均必须有每个配置metric观察（不适用则执行前拆到其他profile）。INTEGER为规范有符号安全整数文本，DECIMAL为规范非负DecimalString，BOOLEAN仅true/false文本，STRING为UTF-8文本；EQ支持全部，LE/GE只支持数值。解析失败/类型错/单位错拒绝记录并阻断验收；不让模型解释expected。OBSERVED必须有实际value和可信evidence；其他状态value=null。必要metric缺失/MISSING/INFRA_ERROR/UNCERTAIN→INSUFFICIENT；存在有效不达标metric→FAIL；全部有效达标→PASS。profile.thresholds至少一项。单位由定义/profile匹配，观测不自报另一单位。

语义/对抗层必须绑定rubric_artifact_id（规则文本、版本、评分维度、证据定位及不确定条件）；V1至少输出score_bp:INTEGER（0—10000，GE阈值）和critical_violation:BOOLEAN（EQ false）。证据定位必须可被独立宿主解析，缺失或评分冲突标UNCERTAIN；任何有效critical_violation=true直接FAIL，不被均分抵消。模型评分是受检观察，不保证语义正确，固定标注fixture用于实测误判并在运行前批准可接受阈值；未达标配置不得激活。机械/GLOBAL可用BOOLEAN checks_passed等明确metric。样例见contracts/verifier-fixtures-v2.json，属于协议fixture，不能冒充模型效果评测。

基础设施超时/模型断连默认INSUFFICIENT；如果超时本身是合同metric，只有可信独立计时观察证明越过阈值时才FAIL。验证命令exit=0不自动PASS，仍需全部metric；缺失输出不能伪造成功。

Skill流程：validate事务固定Skill摘要/profile/定义/criteria，设置VALIDATING并创建无Goal的VALIDATE_SKILL Activity；多次同版本验证按3.9分配轮次。worker先登记VerificationRun，再提交validation业务内容；Kernel核对身份与实际结果生成SkillValidationRecord，合法审计即使FAIL也可Activity SUCCEEDED。PASS且无阻断可返回CANDIDATE待激活；INSUFFICIENT仍CANDIDATE待补证；FAIL→REJECTED需新版本修复。activate的audit_id是SkillValidationRecord.id，必须精确同版本、当前有效必要项全PASS；不创建虚构Goal/CandidateManifest。REVOKED不复活；并发激活/撤销锁同Skill，撤销后拒新调用。验证历史通过GET /skills/{version_id}/validations读取，holdout按ACL过滤。

### 3.9 审计提交、重审与受理集合（RD02）

AuditResource新增audit_round与verifier_run_ids。验收槽为(subject_type,subject_id,subject_digest,profile_digest,layer,criterion_id)，版本由不可变subject/profile确定；Skill使用相同规则。Kernel创建审计Activity时为(subject_type,subject_id,profile_digest,layer)分配递增audit_round，不由worker自定，跨attempt恢复不增round但计入既定审计尝试预算。分配结果以ActivityResource.verification_assignments随claim返回，subject/profile/layer/round均不接受worker另选；FINALIZE含全部必需GLOBAL profile条目，不能漏分配。

不可变提交键为(project_id,producer_activity_id,audit_round,verification_profile_id,layer)，SkillValidationRecord的layer由profile确定；一次合法outcome最多一份该键内容。网络重传以原提交digest返回原ID；同键异digest写冲突隔离记录并阻断，不能最后写入覆盖。VerificationRun提交另按(producer_activity_id,producer_attempt_id,audit_round,verification_profile_id,layer)唯一；同键同内容重返原ID，异内容隔离。一个run覆盖该层全部criteria/metrics，恢复新attempt可有新run，但已成功受理的Activity不能再提交另一Audit。

当前受理集合不是“最新记录”：任一来源有效且版本匹配的FAIL永久保留在该候选集合；无有效FAIL时每个槽有有效PASS即可满足，否则INSUFFICIENT。旧INSUFFICIENT保留历史，但新合法round的PASS可补齐，不因此产生冲突；同键异内容才是提交冲突。重审仅限补证或核对环境，预算上限不因换Activity重置；业务修复产生新subject。accepted_audit_ids包含参与汇总的全部有效审计记录，accepted_assessment_ids另含Kernel枚举的全部适用可信验证判定；两者均按ID排序，required_set_digest固定；另保存无效/隔离记录和原因，不从库删除。

来源撤销：先按3.11原子封锁项目TrustState并登记失效，再在锁外遍历包含Skill的完整引用闭包；传播完成才允许解除项目封锁，不能扫描完成后才关闭准入。受信Ledger在同scope事务追加EvidenceValidityDecision={id,project_id,evidence_id,decision:INVALID,reason_code,authority_identity,proof_artifact_ids,created_at}，移出当前有效集合并重算汇总/反馈；必须保留原FAIL与失效依据，不能因“不喜欢失败”将其改无效。已提交同键冲突不可由模型解除；授权维护经可信核对登记ConflictResolution={id,conflict_id,accepted_digest,proof_artifact_ids,authority_identity,created_at}，不改原记录，仅解除该冲突阻断。两类记录由Ledger受信维护入口写入，内部固定服务接口/审计日志，无浏览器强制PASS入口。结果仍须重新汇总。

若来源撤销发生在Goal DONE之后，不修改不可变历史ReleaseManifest或复活Goal；新增ReleaseValidityRecord={release_manifest_id,status:INVALIDATED,decision_ids,created_at}，公开GET release返回{manifest:ReleaseManifest,validity:{status:VALID/INVALIDATED,decision_ids:uuid[]}}，UI标“历史完成，证据已失效”，拒新OFFLINE_VERIFIABLE导出并告警。新目标重新验收。非终态则立刻阻止DONE；失效与最终提交竞争同scope锁。

### 3.10 领域校验边界（RD03）

Content v3落实目标/标准长度、至少一条required、Epoch BIGINT上限和profile scope/layer基础校验。API先执行严格JSON与字段Schema，再由domain检查引用同项目、唯一ID、有效版本、profile与verifier匹配、阈值解析、覆盖和预算，最后构造Content并再次校验/散列。任何写入路径包括内部、seed、恢复均通过同一domain规则；不能绕过API把非法内容入库。Schema通过只表示结构合法，不代表来源或业务验收通过。

所有profile.required_layers恰一项，required_evidence_kinds与thresholds非空；所有Goal/Task/Skill至少一项required标准，禁止全optional空转。Epoch达到上限拒绝新代次并BLOCKED EPOCH_EXHAUSTED，不能溢出、回绕或字符串重置。新增字段和v2摘要不兼容v1新写，历史v1仅在其历史版本下核验，不能用于v0.5新候选。

### 3.11 验证判定受理与信任失效屏障（FG01/FG02）

**权威验证集合。** VerificationAssessment是Kernel依据固定verifier规则对VerificationRun生成的不可变Content v3记录：{run_id,project_id,subject_type,subject_id,subject_digest,verification_profile_id,layer,audit_round,trust_revision:Epoch,evaluator_digest:Digest,criterion_results:CriterionResult[],verdict:PASS/INSUFFICIENT/FAIL}。Resource另含服务端id/时间/content_digest；UNIQUE(run_id)，evaluator_digest固定领域判定算法版本。不是模型提交类型，不接受worker自报assessment。

POST verification-runs在短事务核对已登记验证动作、assignment、来源/内容/指标后，一并保存run、assessment、引用边、汇总及outbox。结构非法/来源待核对的输入只进隔离inbox，并增加pending_verification_count，不能产生可信PASS；恢复程序按固定版本判定，无法证明可信则维持阻断并有界升级。纯判定不做网络IO。若在事务前崩溃则全部未提交，原验证动作仍未结算；事务后崩溃则assessment已存在，不依赖Audit是否提交。

Kernel从持久账本枚举该subject/profile/必要槽的全部适用assessment（含旧attempt、旧round及迟到结果），不以Audit.verifier_run_ids作为筛选全集。outcome的引用只用于解释和来源对齐，缺选旧run不能隐藏反证；任一有效assessment或Audit有FAIL则总体FAIL。来源失效只能凭受信决定移出有效集合并保留历史。没有有效FAIL时，pending>0、信任封锁、必要正式Audit缺失或任一槽未有效通过均为INSUFFICIENT；仅全部必要正式审计与metric通过、pending=0且无安全阻断才PASS。真实失败观察可以在Audit缺失时阻止通过，但不伪造一份模型Audit；Activity崩溃本身仍不产生业务FAIL。

验证动作在发出前经现有EffectIntent或ModelInvocation登记并关联assignment；可信宿主维护VerificationObligation={id,project_id,activity_id,attempt_id,subject_type,subject_id,profile_id,layer,audit_round,effect_ids:uuid[],invocation_ids:uuid[],status:OPEN/ASSESSED/QUARANTINED,assessment_id:uuid或null}。一个attempt/assignment一条，run/assessment登记后仅在所列动作均有确定回执时置ASSESSED；登记动作与obligation引用同事务，未关联动作禁止发出。超时/部分输出也须生成明确INSUFFICIENT观察或进入QUARANTINED，不能删除义务。

Task DONE和Goal最终提交均关闭相应subject新增验证准入并检查全部obligation，未结算effect/invocation、隔离输入及未完成汇总均计pending。等待在事务外；最终短事务重检TrustState、subject关闭标记、完整assessment集合、正式Audit、pending和既定版本。当前提交FINALIZE的宿主lease可存活，但其模型/工具动作必须全部结算；不能要求宿主先退出才能提交自己的outcome。重审通过Kernel原子开放该subject新round并建立obligation，遵守既有REVERIFY/REWORK和预算，不自行重开工程准入。

迟到结果只要属于原已登记obligation，就按原subject纳入核对，失租不丢证据；已结算同run异内容仍隔离冲突。若已发布后发现新的可信矛盾（如重复回执内容冲突经核对成立），走信任失效流程追加ReleaseValidityRecord，不改旧ReleaseManifest、不复活终态。

**项目信任屏障。** TrustState={project_id,trust_revision:Epoch,status:OPEN/BLOCKED,decision_ids:uuid[],propagation_job_id:uuid或null}，新项目为OPEN/revision=1。TrustPropagationJob={id,project_id,decision_ids:uuid[],status:PENDING/RUNNING/COMPLETE/BLOCKED,cursor:string或null,affected_skill_ids:uuid[],affected_goal_ids:uuid[],affected_release_ids:uuid[],deadline_at,reason_code:string或null}，由可信Ledger维护，非模型Activity，使用有界运维预算和持久扫描游标。全项目信任锁固定排在所有scope锁之前；普通准入取共享锁，失效/解锁取排他锁，不在锁内遍历大图或调用网络。

收到EvidenceValidityDecision时，在一个短事务写决定、递增trust_revision、TrustState BLOCKED、传播job及outbox，再扫描引用闭包。BLOCKED拒绝新claim、context绑定、模型/工具dispatch、Skill激活、Task/Goal DONE及可信导出；允许原回执/停止/已有效果只读对账/隔离与信任修复，不借修复执行工程或新推理。所有上述准入与决定提交同锁排序；决定先提交则零新准入，派发先提交的动作保留并核对。并发新失效合并decision_ids并升revision，旧job不能解锁新revision。

传播闭包必须覆盖artifact→run→assessment/Audit/SkillValidationRecord→SkillVersion→SkillSet/ContextBundle/Activity→candidate/release。引用边在各对象登记事务维护，同项目；扫描时工程准入已关闭，不允许新绑定受影响对象。任何已激活且验收依赖失效的SkillVersion自动REVOKED（reason=VALIDATION_EVIDENCE_INVALID），CANDIDATE/VALIDATING受影响版本也撤销；其记录不改内容digest，所有新使用及在途后续行动被拒。受影响未终态活动停止/隔离并核对效果，保留预算与checkpoint；不能假取消或盲重试。引用受影响Skill的SkillSet不允许新绑定，即使其自身内容摘要未变。恢复需要新SkillVersion及独立验收，不静默复活旧版本。

所有受影响发布清单追加INVALIDATED；非终态Goal保留信任阻断和受影响节点记录。先建立各对象持久阻断/撤销、核对受影响旧执行者并排空其写入，扫描闭包完成且无未决传播任务后，可信维护程序以expected_trust_revision CAS置项目OPEN。缺边/依赖故障/预算耗尽保留BLOCKED及原因；未受影响对象可在项目重新OPEN后继续，受影响对象仍按新版本/原恢复规则处理。UI通知/SSE不承担安全生效，消息丢失不恢复权限。

GET /system/status返回trust:TrustState；SkillVersionResource增加revocation_reason:string或null。内部可信维护接口为POST /trust/invalidations {project_id,expected_trust_revision,evidence_id,reason_code,proof_artifact_ids}→202 TrustPropagationJob、GET /trust/jobs/{job_id}→TrustPropagationJob；身份来自已登记Ledger运维主体，不接受body authority。无通用unlock API，只有传播程序完成上述证明后解锁。失效接口请求重传幂等，冲突409需重新读取，不能覆盖新决定。

## 4. 辅助配置模型

| 模型 | 必填字段 |
|---|---|
| ProjectCreate | name,repository_ref（预登记引用，非任意宿主路径） |
| PolicyCreate | project_id,name,allowed_tools:string[],allowed_paths:string[],protected_paths:string[],network_allowlist:string[],external_actions:{action,mode:DENY/ALLOW/APPROVAL}[],secret_scope_refs:string[] |
| ModelProfileCreate | project_id,name,local_provider_ref,model_id,context_window:int,output_reserve:int,inference_slots:int,cloud_provider_refs:string[],cloud_mode:DENY/PREAUTHORIZED/APPROVAL |
| SkillCreate | project_id,name,source_ref,content_artifact_id,capabilities:string[],role_scopes:string[],required_tools:string[],verification_profile_id:uuid,acceptance:Criterion[]；profile须为SKILL，全部criterion指向该profile，至少一条required |
| SkillSetCreate | project_id,name,skill_version_ids:uuid[]（全部ACTIVE） |
| MemoryCreate | project_id,activity_id,kind:fact/decision/failure/hypothesis/question,statement,source_evidence_ids:uuid[],confidence_bp:int（0—10000） |

ProjectResource=公共字段+ProjectCreate+state_revision；PolicyResource=公共字段+version/content_digest+config:PolicyCreate；ModelProfileResource=公共字段+version/content_digest+config:ModelProfileCreate+capability_status:UNVERIFIED/VERIFIED/FAILED+probe_evidence_ids；SkillVersionResource=公共字段+skill_id/version/content_digest+config:SkillCreate+status:CANDIDATE/VALIDATING/ACTIVE/REVOKED/REJECTED+audit_id:uuid或null+revocation_reason:string或null；SkillSetResource=公共字段+version/content_digest+config:SkillSetCreate；VerificationProfileResource=公共字段+version/content_digest+config:VerificationProfileCreate；MemoryResource=公共字段+MemoryCreate+status:PROPOSED/VERIFIED/SUPERSEDED+valid_from:string+supersedes_id:uuid或null。

## 5. 公共路由（路径均加 /api/v1）

列表 List<X> 使用公共分页；项目列表接口除 projects 外都必传 project_id；嵌套Goal/Task资源从URL取项目scope。所有异步写返回202 CommandOperation。

| 方法与路径 | 角色 | 请求/查询 | 返回 / 调用方 |
|---|---|---|---|
| GET /auth/login | 公开 | return_to同源allowlist | 302 OIDC |
| GET /auth/callback | 回调 | code/state，校验PKCE/nonce | 302+cookie |
| GET /auth/session | 已登录 | 无 | {user_id,roles,project_ids,csrf_token,expires_at} / AppShell |
| POST /auth/logout | 已登录 | {} | {logged_out:true} |
| GET /projects | V | cursor,limit | List<ProjectResource> |
| POST /projects | M | ProjectCreate | 201 ProjectResource |
| GET /goals | V | project_id,status?,cursor,limit | List<GoalResource> |
| POST /goals | O | GoalCreate | 201 GoalResource(DRAFT) |
| GET /goals/{goal_id} | V | 无 | GoalResource |
| GET /goals/{goal_id}/wall-budget | V/W | 无 | GoalWallBudgetSnapshot；Kernel 权威推进后投影；人类走项目 scope；ACTIVE 登记 worker 可读（admit 前亦可，不要求持有 attempt）；budget_usage_unknown 时数值为 null 且 exhausted=true；marks_goal_done 恒 false |
| GET /goals/{goal_id}/goal-review-budget | V/W | 无 | GoalReviewBudgetSnapshot；复盘预算/间隔/停滞只读投影；eligible_now 仅表示预算门，≠ claim 成功；blocking_reason_code∈GOAL_REVIEW_*；marks_goal_done 恒 false |
| PUT /goals/{goal_id}/contract | O | GoalContractUpdate | GoalResource新合同版本 |
| POST /goals/{goal_id}/start | O | ControlRequest | 命令；同事务建PLAN Activity |
| POST /goals/{goal_id}/pause | O | ControlRequest | 命令；排空后才PAUSED |
| POST /goals/{goal_id}/resume | O | ControlRequest | 命令；重检预算/权限 |
| POST /goals/{goal_id}/cancel | O | ControlRequest | 命令；UNKNOWN阻止假终态 |
| GET /goals/{goal_id}/orchestration-abandonments | V | 无 | List<OrchestrationAbandonmentResource>；编排放弃事实；marks_goal_done恒false |
| POST /goals/{goal_id}/orchestration-abandonments | W | OrchestrationAbandonmentCreate | 201 OrchestrationAbandonmentResource；ACTIVE worker 登记放弃→Goal BLOCKED；幂等(goal_id,generation)；绝不写DONE |
| POST /goals/{goal_id}/orchestration-abandonment-release | O | ControlRequest | 人工解除ORCHESTRATION_ABANDONED BLOCKED，恢复previous_status；须SHUTDOWN已确认且无RUNNING；绝不写DONE |
| POST /goals/{goal_id}/replan | O | ReplanRequest | 命令；屏障下拒绝变更 |
| POST /goals/{goal_id}/finalization-recovery | O | FinalizationRecoveryRequest | 202 CommandOperation；仅固定失败恢复动作，不能强制DONE |
| GET /goals/{goal_id}/plans | V | cursor,limit | List<PlanResource> |
| POST /goals/{goal_id}/plans | O | PlanCreate | 201 PlanResource(CANDIDATE)；只存提案，controller创建PLAN Activity校验采纳，不能直接发布 |
| GET /goals/{goal_id}/tasks | V | status?,cursor,limit | List<TaskResource> |
| GET /goals/{goal_id}/activities | V | kind?,status?,cursor,limit | List<ActivityResource>；包括无Task规划 |
| GET /tasks/{task_id} | V | 无 | TaskResource |
| POST /tasks/{task_id}/cancel | O | ControlRequest | 命令 |
| POST /tasks/{task_id}/retry | O | ControlRequest | 命令；replacement保留lineage |
| GET /tasks/{task_id}/activities | V | kind?,cursor,limit | List<ActivityResource> |
| GET /activities/{activity_id} | V | 无 | ActivityResource |
| GET /activities/{activity_id}/attempts | V | cursor,limit | List<ActivityAttemptResource> |
| GET /activities/{activity_id}/checkpoints | V | cursor,limit | List<Checkpoint> |
| GET /activities/{activity_id}/steps | V | cursor,limit | List<StepResource> |
| GET /tasks/{task_id}/evidence | V | cursor,limit | List<EvidenceEnvelope> |
| GET /tasks/{task_id}/audits | V | cursor,limit | List<AuditResource> |
| GET /artifacts/{artifact_id} | V | 无 | ArtifactResource |
| GET /artifacts/{artifact_id}/content | V | Range? | 200/206文件流；ETag=digest |
| GET /candidates/{candidate_id} | V | 无 | CandidateManifest |
| GET /goals/{goal_id}/audits | V；持有该 Goal 下 ACTIVE attempt 的 worker | cursor,limit | List<GoalAuditItem>；按record_type区分周期诊断与候选验收；只读，≠ DONE |
| GET /goals/{goal_id}/release | V | 无 | {manifest:ReleaseManifest,validity:{status:VALID/INVALIDATED,decision_ids:uuid[]}}；尚无发布清单404 |
| POST /goals/{goal_id}/evidence-exports | O | EvidenceExportRequest | 命令；签名/复制由有限系统作业生成 |
| GET /commands | V | project_id?,idempotency_key?,goal_id?,cursor,limit | List<CommandOperation>；key查询限原主体，无project查询只返自身命令 |
| GET /commands/{command_id} | V | 无 | CommandOperation |
| GET /effects | V | project_id,goal_id?,activity_id?,status?,cursor,limit | List<EffectResource> |
| GET /effects/{effect_id} | V | 无 | EffectResource |
| POST /effects/{effect_id}/reconcile | A | ReconciliationRequest | 命令；创建RECONCILE，不直接改结果 |
| GET /approvals | V | project_id,status?,cursor,limit | List<ApprovalResource> |
| GET /model-invocations | V | project_id,goal_id?,activity_id?,cursor,limit | List<ModelInvocationResource>；按项目ACL，不返回模型原始输入 |
| POST /approvals/{approval_id}/revoke | A | ControlRequest | ApprovalResource；按subject锁序撤销，已发出仍核对 |
| POST /approvals/{approval_id}/decision | A | ApprovalDecision | ApprovalResource |
| GET /verification-profiles | V | project_id,cursor,limit | List<VerificationProfileResource> |
| POST /verification-profiles | M | VerificationProfileCreate | 201不可变版本 |
| GET /policies | V | project_id,cursor,limit | List<PolicyResource> |
| POST /policies | M | PolicyCreate | 201不可变版本 |
| GET /model-profiles | V | project_id,cursor,limit | List<ModelProfileResource> |
| POST /model-profiles | M | ModelProfileCreate | 201不可变版本 |
| POST /model-profiles/{profile_id}/probe | M | {} | 命令；PROBE_MODEL Activity |
| GET /skills | V | project_id,status?,cursor,limit | List<SkillVersionResource> |
| POST /skills | M | SkillCreate | 201 CANDIDATE版本 |
| POST /skills/{version_id}/validate | M | {} | 命令；VALIDATE_SKILL Activity |
| POST /skills/{version_id}/activate | M | {audit_id,reason} | SkillVersionResource；audit_id仅指同版本有效SkillValidationRecord，必要项PASS且无阻断 |
| POST /skills/{version_id}/revoke | M | {reason} | SkillVersionResource；撤销后拒新动作 |
| GET /skills/{version_id}/validations | V | cursor,limit | List<SkillValidationRecord> |
| GET /skill-sets | V | project_id,cursor,limit | List<SkillSetResource> |
| POST /skill-sets | M | SkillSetCreate | 201不可变版本 |
| GET /memories | V | project_id,kind?,q?,cursor,limit | List<MemoryResource> |
| GET /system/status | V | project_id | SystemStatus |
| GET /verification-obligations | V | project_id,status?,goal_id?,activity_id?,cursor,limit | List[VerificationObligationResource]；status=QUARANTINED 即 quarantine inbox 公开只读面；≠结算≠DONE |
| GET /verification-obligations/{obligation_id} | V | 无 | VerificationObligationResource |
| GET /goals/{goal_id}/snapshot | V | 无 | GoalSnapshot |
| GET /goals/{goal_id}/events | V | after_seq? | text/event-stream |

PlanResource=公共字段+goal_id+plan_revision:int或null+status:CANDIDATE/PUBLISHED/REJECTED+reason+tasks+coverage；SystemStatus={trust:TrustState,components:ComponentStatus[],resources:ResourceStatus,observed_at,stale:bool}；ComponentStatus={name,state:HEALTHY/DEGRADED/UNAVAILABLE/UNKNOWN,observed_at,reason_code:string或null}；ResourceStatus={model:{used,total},tools:{used,total},browsers:{used,total},queued_activities:int,quarantined_resources:int,observed_at,stale:bool}。

GoalSnapshot={goal:GoalResource,plan:PlanResource或null,tasks:TaskResource[],activities:ActivityResource[],approvals:ApprovalResource[],effects:EffectResource[],commands:CommandOperation[],planning_feedback:PlanningFeedback[],feedback_truncated:bool,latest_seq:string}。返回当前计划的业务对象及活跃/未决运行对象，历史attempt通过分页读取；不能把已截断集合宣称完整。单计划上限固定1000节点，超大历史不进入snapshot。

工件文件流不套JSON；HTML/SVG强制下载或隔离域并nosniff，不能同源执行。没有任意状态PATCH或强制DONE端点。

## 6. 内部协议（路径均加 /internal/v1）

内部调用以 Activity 为单位。除了heartbeat/上传/回执，Activity业务写请求用 `{lease:LeaseIdentity,expected_state_revision:int,...}`，revision匹配Activity。主体权限与kind/target严格对应。Broker调用Kernel的准入/结果端口，Runner不直接写账本或伪造Receipt。

| 方法与路径 | 主体 | 请求 | 返回与约束 |
|---|---|---|---|
| POST /trust/invalidations | Ledger运维主体 | {project_id,expected_trust_revision,evidence_id,reason_code,proof_artifact_ids:uuid[]} | 202 TrustPropagationJob；先封锁再传播 |
| GET /trust/jobs/{job_id} | 授权运维主体 | 无 | TrustPropagationJob |
| POST /claims | 注册Runner/系统worker | {kinds:ActivityKind[],capabilities:string[]} | {lease:null}或{lease:LeaseIdentity,activity:ActivityResource,attempt:ActivityAttemptResource,input_artifact_ids:uuid[],policy_snapshot_id:uuid}；资源容量按已登记worker读取 |
| POST /activities/{activity_id}/heartbeat | 当前owner | {lease:LeaseIdentity,renewal_seq:int} | {lease_expires_at,renewal_seq,control:CONTINUE/CHECKPOINT/STOP,pending_stop_ids:uuid[]}；无expected_state_revision，不改业务版本；attempt 上仍有 REQUESTED Stop，或 Goal 处于 PAUSING/PAUSED/CANCELLING/CANCELLED/BLOCKED 时 control=STOP（后者 pending_stop_ids 可为空） |
| POST /activities/{activity_id}/steps | 当前owner | Activity业务字段+{predecessor_step_id:uuid或null,purpose:string,tool_ref:string} | 201 StepResource；Kernel登记下一步；相同前驱重传返回原step，purpose/tool_ref不一致409 STEP_CONFLICT，未决效果不可绕开 |
| POST /effects/prepare | Broker代理当前owner | {lease,logical_step_id,intent_revision,tool_ref,input_artifact_id:uuid} | EffectResource；原step重复返回同effect；scope/replay_class/费用从registry与合同确定 |
| POST /effects/{effect_id}/dispatch | 可信Broker | {lease,effect_state_revision:int} | EffectResource；原子检查屏障/epoch/审批消费/预留，登记DISPATCHED后才发出 |
| GET /effects/{effect_id} | 有scope的Runner/Broker | 无 | EffectResource；恢复只查原effect |
| POST /effects/{effect_id}/receipts | 可信Broker/collector | TrustedReceipt | 201 {receipt_id,disposition:APPLIED/PENDING_RECONCILIATION/DUPLICATE}；不要求当前业务revision，失租不等于丢回执 |
| POST /activities/{activity_id}/checkpoints | 当前owner | Activity业务字段+Checkpoint内容（服务字段除外） | 201 Checkpoint；已完成效果/工件引用核验 |
| POST /activities/{activity_id}/outcomes | 当前owner或受信执行服务 | Activity业务字段+{outcome:ActivityOutcome} | ActivityResource；Kernel决定Task/Goal，不接受desired_status |
| PUT /artifacts/{digest}/content | 可信collector或授权上传者 | 二进制+project_id/activity_id/attempt_id/fencing_epoch授权头 | 201 ArtifactResource；无expected_state_revision；流式限额；提交关联时重验 |
| POST /candidates/seal | 可信Ledger/Broker | {lease,workspace_snapshot_ref:string,verification_profile_ids:uuid[]} | 201 CandidateManifest；从实际冻结快照采集，不能信模型自填文件hash；Goal 工程关闭态（含 BLOCKED）拒绝新封存（GOAL_ENGINEERING_CLOSED；同 digest 幂等除外） |
| POST /verification-runs | 可信验证宿主/collector | {lease,run:VerificationRun业务内容} | 201 VerificationRun；不可变证据登记免业务revision，复核assignment/主体/profile/回执和摘要；迟到按原义务受理并更新权威判定，不复活旧attempt |
| GET /verification-runs/{run_id} | 有scope的审计宿主 | 无 | VerificationRun；读取原记录、重传去重 |
| POST /memories | 记忆服务 | MemoryCreate | 201 MemoryResource(PROPOSED)；索引通过INDEX_MEMORY |
| POST /activities/{activity_id}/context | 当前受信宿主 | {lease,binding_digest,context_bundle_id:uuid} | {context_digest,binding_digest}；首次绑定CAS，不可覆盖 |
| POST /model-invocations | 当前受信宿主 | ModelInvocationCreate | 201 ModelInvocationResource；PLAN宿主可用，模型不暴露此接口；Goal 处于 PAUSING/PAUSED/CANCELLING/CANCELLED/BLOCKED/DONE/FAILED 时拒绝新登记（GOAL_INFERENCE_CLOSED；幂等重放除外） |
| POST /model-invocations/{invocation_id}/dispatch | 受信模型连接器 | {lease,expected_state_revision:int} | ModelInvocationResource；原子准入后事务外调用；Goal 处于 PAUSING/PAUSED/CANCELLING/CANCELLED/BLOCKED/DONE/FAILED 时拒绝进入 DISPATCHED（GOAL_INFERENCE_CLOSED；已 DISPATCHED 幂等回放除外） |
| POST /model-invocations/{invocation_id}/receipts | 受信模型连接器 | ModelReceipt | 201 {disposition:APPLIED/PENDING_RECONCILIATION/DUPLICATE} |
| POST /activations/{activation_id}/stop | Kernel/controller | StopRequest | 202 StopResource；记录请求，不等于停止成功 |
| POST /stops/{stop_id}/receipts | 可信宿主/Broker | StopReceipt | 201 {disposition:APPLIED/PENDING_RECONCILIATION/DUPLICATE} |
| GET /stops/{stop_id} | 授权受信宿主/Kernel | 无 | StopResource；按原资源实例核对 |
| POST /goals/{goal_id}/goal-reviews | worker/admin/operator | {trigger_key,review_seq?} | 201 GoalReviewEnsureResult={activity_id,review_snapshot_digest,created,reviews_remaining,max_reviews,min_interval_seconds,stagnation_seconds,marks_goal_done:false}；trigger_key 业务去重；拒绝时 422 且 error.code∈{GOAL_REVIEW_BUDGET_EXHAUSTED,GOAL_REVIEW_INTERVAL_TOO_SHORT,GOAL_REVIEW_NOT_STAGNANT,GOAL_REVIEW_GOAL_TERMINAL,GOAL_REVIEW_CONTRACT_INVALID,GOAL_REVIEW_TRIGGER_INVALID}；绝不写 DONE |

PLAN 权限补充：上表“当前owner”对 PLAN 允许受信宿主的context绑定、模型调用登记、heartbeat、checkpoints、outcomes；模型连接器的dispatch/receipt属固定生命周期端口，不属于Planner工具；claims 也是宿主行为，kinds 必须受已登记身份约束。模型没有 API 凭据或可调用接口。PLAN 不允许登记 steps、prepare/dispatch effect、查询 effect 工具、上传任意内容或封存候选；Broker 不得代 PLAN 绕过限制。工具路径越权返回403，错误码 ROLE_TOOL_FORBIDDEN，不产生 StepRecord/EffectIntent 或外部请求；拒绝日志由受信系统记录。已有输入由 ContextCompiler 按授权装配。PlanCreate 必须经 schema、覆盖、权限和预算校验，计划内的命令字符串不得被当成工具指令执行。

ActivityOutcome 是严格的kind联合：PLAN={plan:PlanCreate}；EXECUTE={candidate_manifest_id,evidence_ids}；AUDIT={target_type:CANDIDATE,audit:AuditResource业务内容}或{target_type:GOAL_REVIEW,review:GoalReviewResource业务内容}；INTEGRATE={candidate_manifest_id,integration_commit,evidence_ids}；RECONCILE={effect_id,observed_status:SUCCEEDED/FAILED/UNKNOWN,evidence_ids}；FINALIZE={barrier_id,candidate_manifest_id,global_audits:AuditResource业务内容[],evidence_ids}；PROBE_MODEL={profile_id,capability_status,evidence_ids}；VALIDATE_SKILL={version_id,validation:SkillValidationRecord业务内容}；INDEX_MEMORY={record_ids,index_version,evidence_ids}；EXPORT_EVIDENCE={release_manifest_id,artifact_id,trust_mode:INTERNAL_COPY/OFFLINE_VERIFIABLE,attestation_id:uuid或null}。失败联合为{failure_class,signature,evidence_ids,retry_hint}，不接受任意kind的成功payload。

候选AUDIT/FINALIZE 必须验证独立角色、被审候选和固定验证配置；GOAL_REVIEW则验证固定运行快照和诊断身份，不要求候选；不能因URL中的当前attempt与候选生产attempt不同而混淆归属。PLAN发布、全局审计不再有绕过Activity lease的独立提交后门。周期Critic是AUDIT(target=GOAL_REVIEW)，相同触发键只生成一份Activity。

FINALIZE outcome只提交验证材料，Kernel与Ledger在最终短事务中校验屏障、保存GLOBAL审计及ReleaseManifest并决定DONE，不接受模型自报最终状态。证据导出单独使用EXPORT_EVIDENCE Activity、RELEASE_EXPORT target和项目运维budget_scope；可在Goal DONE后读取已有ReleaseManifest，不能重开工程写入或FINALIZE。

Runner seam：activate(ActivityLease,ContextBundle)→ActivationRef；checkpoint(ActivationRef)→CheckpointProposal；stop(StopRequest)→StopResource；events(ActivationRef,cursor)→AsyncIterator<RuntimeEvent>。RuntimeEvent={activation_id:uuid,seq:int,type:STARTED/MODEL_OUTPUT/TOOL_PROPOSAL/CHECKPOINT_PROPOSED/STOP_OBSERVED/FINISHED,payload_ref:uuid或null}；只表示运行输入，不能直接认定业务成功。fake和Harness adapter实现同一interface，上游session字段只留在adapter。

### 6.1 步骤、审批、检查点与恢复约束（B01—B08）

B01 步骤去重：V1一个Activity内工具步骤串行，并行工作通过不同Activity表达。根步骤UNIQUE(activity_id) WHERE predecessor_step_id IS NULL；后继UNIQUE(activity_id,predecessor_step_id) WHERE predecessor_step_id IS NOT NULL。前驱必须同Activity且其effect已处于SUCCEEDED/FAILED/CANCELLED确定终态，重复登记先查已有后继，字段相同返回原对象、不同返回409 STEP_CONFLICT；同step最多一个effect且intent_revision固定为1。参数绑定后不可修改；新的业务意图用新step。换身份/HTTP key不能制造第二后继；旧lease即使重复请求也拒绝。恢复从已登记steps读取next_step，不根据模型文本重新发明步骤。

B02 审批与发送：prepare只绑定审批请求，不消费。dispatch事务按同一scope锁，校验lease、当前策略、审批APPROVED/未过期/未撤销、绑定effect/payload及预算；消费与DISPATCHED、在途账、outbox一起提交。UNIQUE(approval_id)限制一次绑定意图，consumed_subject_id只能等于绑定subject.id；同effect的安全重传复用该消费，不新增审批授权，仍检查有效性；所有发送次数的费用累计受同一max_cost_usd约束，不能每次重传重置费用上限。审批决定/撤销/过期判定与dispatch使用相同scope→Activity（需要时）→effect/model_invocation→approval锁序；已DISPATCHED的动作不能被撤销记录“抹除”，只能停止后续行动并核对。无审批策略不生成伪审批消费。

B03 PLAN检查点：kind必须匹配Activity；PLAN的workspace_manifest_digest/candidate_manifest_id/next_step_id必须null，completed_step_ids/effect_ids必须为空。context_digest指向宿主已持久化的输入快照，artifact_ids仅允许批准输入或宿主保存的规划输出，session_ref可为空。宿主固定存储端口保存规划文本并记录来源，不向Planner暴露上传工具。其他kind在工作区确实存在时才填其digest；不能捏造空工作区hash使schema通过。

B04 审计联合：GoalAuditItem={record_type:CANDIDATE_AUDIT,audit:AuditResource,aggregation:AuditAggregation}或{record_type:GOAL_REVIEW,review:GoalReviewResource}。GOAL_REVIEW可在无候选时绑定不可变review_snapshot_digest，包含Goal合同、计划、活动与证据引用；其建议仅触发Kernel规则，不直接改状态或贡献最终PASS。FINALIZE.global_audits按每个必需Goal VerificationProfile分别提交GLOBAL记录，每条必要criterion在所属profile内恰好出现一次；缺失、重复、错profile、错候选或非PASS都拒绝DONE。Task各profile/必要层也逐项覆盖；不得用第一份PASS代表全集。诊断输出由宿主保存，不冒充Broker测试回执。

B05 计划发布：PlanResource的plan_revision在CANDIDATE/REJECTED时为null，只有发布成功才分配；候选ID用于追踪。公共POST仅存候选，Kernel持久创建PLAN Activity，受信宿主提交该候选及其绑定digest，经与模型提案相同的发布规则处理。候选提交只允PLANNING/RUNNING且无有效屏障，DRAFT用start进入规划；PAUSED只可改合同，resume按是否失效选择规划。发布事务校验expected_plan_revision、PLAN固定goal_contract_revision及合同digest、coverage和依赖。保留Task ID必须维持原合同/依赖，删除或替换节点要排空该节点及传递下游旧工作，未知effect时不发布。依赖被替换的下游也需replacement及新验收；原DONE历史不改，新图不能沿用其旧输入证明。改合同后标记旧图不适用，resume进入PLANNING而不是执行旧图。每次候选发布要么全部提交，要么原图保持不变；受影响准入可暂停等待排空，不在数据库事务中等待。

B06 等待与恢复：Activity WAITING必须填wait_reason、wake_at、wait_deadline_at和resume_state=READY；wake_at为下次条件检查时刻，deadline为该次等待上限，不在轮询时滚动延长。PAUSED到期不自动resume；记录超期、停止自动尝试并按Goal预算进入BLOCKED/FAILED。RECOVERING核对后若结果成功可采纳，否则重领使用新attempt；WAITING不恢复旧ACTIVE身份。

B07 摘要引用闭包：每种可散列内容定义独立版本化Content schema，禁止Resource通用删除字段后猜测散列输入。显式排除该对象自己的content_digest及存储ID/时间元数据；所有跨对象身份引用同时绑定不可变目标digest。签名导出的reference_bindings=[{object_type,object_id,content_digest}]覆盖传递引用且纳入签名payload；离线验证需解析完整闭包、同项目/授权生产者校验、无悬空/冲突映射，未知schema拒绝。文件数组按规范路径排序，其余集合按schema稳定键排序，执行顺序数组保持原顺序；JCS不负责数组排序。对修改内容但沿用UUID、漏对象、改数组顺序等给出一致测试向量。输出摘要不能引用其尚未生成的自身或未来ReleaseManifest，保持证据图无环。

B08 预算耗尽收尾：项目必须配置独立RecoveryBudget={max_cost_usd:string,max_network_calls:int,max_wall_seconds:int,max_concurrent:int}，均有界；只能由Kernel固定恢复路径用于STOP/隔离/既有效果的只读核对，不允许PLAN、工程写或云推理。普通Goal预算耗尽关闭新业务准入，仍保存已到达可信回执及本地账本恢复；RECONCILE可绑定项目恢复scope，资源/费用计项目恢复总账并关联原Goal，不隐藏原消费。恢复额度也耗尽则保留UNKNOWN/quarantine、BLOCKED及人工处理原因，不靠无限重试达到终态。该特例不授予Runner改变budget_scope_id的能力。涉及原Goal状态和项目恢复账时，先按ID排序取得全部相关scope锁，再取Activity/effect等锁；不可先持恢复账锁再反向取Goal锁。



### 6.2 活动角色、target与版本绑定（DEV04）

| kind | 执行身份 | target.type / id含义 | goal_id / task_id | 允许工具scope |
|---|---|---|---|---|
| PLAN | PLANNER宿主 | GOAL_PLAN / Goal | 必填 / null | 无；仅宿主受控模型推理 |
| EXECUTE | EXECUTOR | TASK_WORK / Task | 必填 / 必填 | ENGINEERING、READ_ONLY，受TaskContract限制 |
| AUDIT | AUDITOR | CANDIDATE / CandidateManifest；GOAL_REVIEW / Goal | 必填 / 候选所属Task或null；GOAL_REVIEW为null | VERIFICATION、READ_ONLY |
| INTEGRATE | SYSTEM集成worker | INTEGRATION / Goal | 必填 / null | ENGINEERING、READ_ONLY，持集成锁 |
| RECONCILE | SYSTEM对账worker | EFFECT / EffectIntent | 跟随原effect，均可null | RECONCILIATION；不能产生新工程效果 |
| FINALIZE | AUDITOR | FINALIZATION / Barrier | 必填 / null | VERIFICATION、READ_ONLY |
| PROBE_MODEL | SYSTEM模型连接器 | MODEL_PROFILE / 模型配置版本 | null / null | 无工具；受控模型调用，项目运维预算 |
| VALIDATE_SKILL | AUDITOR技能验证worker | SKILL_VERSION / Skill版本 | null / null | VERIFICATION、READ_ONLY，独立沙箱 |
| INDEX_MEMORY | SYSTEM索引worker | MEMORY_INDEX / 项目索引记录 | null / null | 固定索引写入端口，无模型工具 |
| EXPORT_EVIDENCE | SYSTEM导出worker | RELEASE_EXPORT / ReleaseManifest | 必填 / null | READ_ONLY读取源；Ledger固定导出写入端口 |

每次创建Activity在同一scope事务固定ExecutionBinding及输入对象引用；Goal无活动plan时PLAN.plan_revision可null，模型探测配置固定但未VERIFIED是允许的PROBE_MODEL输入；AUDIT允许的target是显式联合，不能任意组合。subject_digest按上表目标的不可变版本：Goal/Task取合同Content，候选取manifest，Barrier取冻结合同/计划/候选组成的FinalizationSubject，配置取其版本，索引取MemoryIndexInput，导出取ReleaseManifest。无Goal时合同/计划字段均null；项目策略仍必填。固定种类不使用模型时model_profile_digest/skill_set_digest可null；否则必须批准且固定。

计划发布时，保留Task的合同、依赖输入、策略与候选完全不变才可由Kernel在发布事务生成BindingAdoption={binding_digest,new_plan_revision,proof_digest}，允许原binding在新图继续适用；变化的节点及下游不得adopt，须排空/replacement。adoption仅是版本适用证明，不改旧binding，也不允许跨合同版本；新证据仍记录真实生产版本。

claim在原子创建attempt前复核binding与当前有效合同/计划/目标（含Kernel已登记adoption）；差异409 BINDING_STALE，不能以新值覆盖旧Activity，Kernel取消未执行旧活动并在合法阶段重建。attempt保存binding_digest。ContextCompiler在事务外读取固定输入并持久化ContextBundle；宿主POST context用lease+binding_digest原子绑定，复核相同版本和权限；context_digest只能null→固定值，不能覆盖。编译期间合同/计划变化、Skill撤销或失租则拒绝，已生成孤儿输入按GC处理，绝不调用模型。模型/工具dispatch与outcome再次复核binding。checkpoint重建可创建新attempt的新ContextBundle，但不得改变该Activity业务binding。

ContextBundle是输入清单，不携带原始推理；宿主由批准工件生成实际提示请求，保留input_digest与context_digest关联。新反馈需要新PLAN Activity绑定新的输入，而非在固定上下文中偷换。审核最终scope引用同一固定候选；INDEX_MEMORY只允许写派生索引，不改变source of truth。租约、槽和预算适用于全部10种kind。

## 7. 最终屏障与事件

ENGINEERING effect的dispatch与关闭Goal工程准入必须持同一scope锁。先登记DISPATCHED再发网络，屏障必须等待这类在途记录全部有确定结果；重新检查epoch不能替代排空。VERIFICATION仅可读封存候选/写独立临时目录/执行批准验证器；RECONCILIATION仅核对原effect；任何scope都不能借名字绕过工具权限。

SSE 使用同源cookie；每个Goal seq为十进制字符串，服务器每15s注释心跳，客户端45s无更新标stale。snapshot在一致读事务返回latest_seq，再订阅其后事件。Last-Event-ID与after_seq冲突400；游标过期410后重取snapshot。重连1/2/4/8/30s退避；event_id去重，较低entity_state_revision不覆盖较新对象；缺口重同步。

Event固定为[事件Schema](contracts/event-v1.schema.json)的13种联合，字段{schema_version:1,event_id:uuid,project_id:uuid,goal_id:uuid,seq:Epoch,type,entity_id:uuid,entity_state_revision:int或null,occurred_at:Timestamp,payload:{resource_type,change:INVALIDATE}}。不发送资源补丁或任意payload；type→resource_type固定：GOAL_STATE_CHANGED→GOAL、PLAN_PUBLISHED→PLAN、TASK_STATE_CHANGED→TASK、ACTIVITY_STATE_CHANGED→ACTIVITY、CHECKPOINT_CREATED→CHECKPOINT、EVIDENCE_CREATED→EVIDENCE、AUDIT_RECORDED→AUDIT、EFFECT_CHANGED→EFFECT、COMMAND_CHANGED→COMMAND、APPROVAL_REQUIRED→APPROVAL、BARRIER_CHANGED→BARRIER、BUDGET_CHANGED→BUDGET、SKILL_REVOKED→SKILL。具体字段由Schema拒未知值。不可变资源revision为null。entity_id只作关联，不允许客户端拼任意URL执行。

SSE id为该Goal的十进制seq（从1起），event字段为type，data为Event；Last-Event-ID和after_seq均使用seq，event_id仅去重。收到任一已知事件后合并失效通知并重取一致snapshot；历史/证据页同时失效相应分页query，读取既有API。请求进行中到达新事件须记录dirty并至少再读一次，较旧snapshot.latest_seq不能覆盖已显示快照；heartbeat不修改数据。遇未知schema_version/type或payload校验失败，停止套用事件、重取snapshot并显示客户端需升级；不伪造业务失败，旧客户端可用有界snapshot轮询。

SKILL_REVOKED属于项目变更：撤销事务写项目outbox，Kernel立即拒绝该版本的新行动；受信outbox投递按(event_source_id,goal_id)幂等向引用该Skill的非终态Goal分配本Goal seq。不为无Goal项目创建假Goal；项目技能页在进入/聚焦时及可见期间最多每10秒重新查询，安全撤销不依赖UI扇出成功。所有事件和重读均按项目ACL过滤。租约心跳不污染业务seq。

活动Goal保留全业务事件，终态最低30天；日志通过工件分页，不塞SSE。慢消费者超缓冲断开后按游标恢复。

## 8. 协议验收

Pydantic/domain schema生成OpenAPI与TS client；state枚举和联合类型不能前后端各写一套。实际路由、字段、nullable、默认值、权限、迁移与本文逐项核对；v0.1尚无运行调用方，所以不设计永久兼容旧attempt/operations路径。若实现时发现已有调用方，必须显式迁移并验证，不能并存两种事实来源。

T01—T24与AT01—AT08/E01—E10覆盖跨身份重传、无Task规划恢复、长上传续租、旧回执对账、固定验证配置、屏障并发与可信证据。当前仅完成文档契约更新，尚无生成OpenAPI或运行接口可供验证。

