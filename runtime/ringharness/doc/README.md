# ringharness 文档导航

**完整V1开发规格v0.5 · 2026-09-11。** 本轮已将架构审计A01—A08、细节审计B01—B08、MEA图示对照C01—C04与满足性复审D01—D04同步落实到10份编号文档与本导航；设计修正不等于代码已实现或测试已通过。

## 1. 当前事实与需求来源

首次检查时工作区是空目录，没有应用源码、Git元数据、依赖/迁移或可运行服务。当前交付为开发和验收规范。来源是[构建无人值守系统](chatgpt-conversation://6aa2d397-b744-83e8-9988-7a19cb4403e4)，已读全部11轮对话与4张图；截图的成功率/性能不能当本项目成绩。

用户要求第一版完整交付，覆盖早期缩减MVP建议；本地Qwen优先、MEA隔离、Skill、持久恢复、预算、证据、审计、Web和100h全部保留。准确模型ID/服务/硬件性能仍需现场probe。

## 2. 十份文档

| 文件 | 内容 |
|---|---|
| [01 开发文档](01-开发文档.md) | 五module、三权分立、Task/Activity/Effect、事务/恢复/屏障 |
| [02 测试文档](02-测试文档.md) | T01—T24、AT01—AT08、100h门槛与静态检查记录 |
| [03 收敛文档](03-收敛文档.md) | 固定验证、覆盖关系、分计数、集成与最终完成 |
| [04 前端文档](04-前端文档.md) | 页面、实时状态、命令/效果、证据来源和屏障 |
| [05 API接口文档](05-API接口文档.md) | 唯一字段/状态/路由、内部Activity与effect/receipt协议 |
| [06 E2E开发调试文档](06-E2E开发调试文档.md) | 启动契约、E01—E10、诊断、复现证据与live验证 |
| [07 UX交互文档](07-UX交互文档.md) | 用户流程、授权/未知结果/封存状态、证据三态 |
| [08 工程化结构文档](08-工程化结构文档.md) | 收拢后的部署/目录、DB约束、CI、运维与备份 |
| [09 Harness上游核验](09-Harness上游核验.md) | 固定SHA事实与自有v0.5协议映射，区分原生/自建能力 |
| [10 架构审计闭环](10-架构审计与改进建议.md) | A01—A08整改落点、测试映射、原始报告归档 |

## 3. 核心约定

| 决策 | v0.5约定 |
|---|---|
| 角色 | Manager规划、Executor提交候选、Auditor独立验证，Kernel按规则决定DONE |
| 核心module | ControlKernel、ExecutionBroker、EvidenceLedger、ContextCompiler、ReadModel |
| 工作模型 | Task=交付，Activity=可恢复工作，ActivityAttempt=持租尝试，EffectIntent=逻辑行动 |
| 状态权威 | PG状态/journal/outbox；向量、Session、上游local job不能接管业务事实 |
| 幂等 | HTTP key去重请求；固定effect_id跨Worker接管；未知结果先对账 |
| 版本 | state_revision与lease续租分开；合同/计划/write_epoch分别检查 |
| 证据 | hash完整性、可信来源、验收正确性分开；候选/发布清单不可变 |
| 完成 | 关闭工程准入→排空→封存→独立最终验收→事务判定DONE |
| 技术 | Python控制面+TS Runner/Harness+React Web；Broker可信隔离 |
| 范围 | 完整R01—R15；开发可有顺序，验收不裁剪 |

01定义领域不变量，05定义精确JSON与路由，02/03定义验收，04/07定义呈现。修改必须同步相关文档、后续schema/迁移/生成客户端/测试，不以文档更新冒充运行验证。

## 4. 冻结版本与下一步

当前完整V1开发规格冻结为 **v0.5 / FROZEN_SPEC**（2026-09-11）。MEA逐项复审、DEV01—DEV06及RD01—RD06及FG01/FG02设计缺口已关闭，主规格以01/05及[Content Schema](contracts/content-v3.schema.json)为准；[冻结与审计记录](10-架构审计与改进建议.md)说明范围、差异及变更规则。

[冻结清单](releases/v0.5/manifest.json)、[完整快照](releases/v0.5/snapshot.zip)、[校验报告](releases/v0.5/check-report.json)保留本版内容。可运行 `python3 doc/tools/check_spec.py --verify-freeze` 检查后续是否漂移。冻结包不可覆盖；变更另升版本。现目录没有Git元数据，因此未创建或宣称Git tag。

[冻结前v0.2](archive/v0.2-before-freeze.zip)与[原始v0.1](archive/v0.1-before-audit-fixes.zip)只用于追溯。应用源码、OpenAPI生成客户端、数据库迁移、真实模型与浏览器E2E、100h验收仍未实现/执行；Content schema与文档工具已交付不能替代这些工作。实际模型ID、provider与部署参数须在冻结安全/资源边界内做能力探测，不能静默降低V1标准。

## 5. v0.5升级范围

本版关闭[开发就绪复审](reviews/v0.3-development-readiness-review.md)的5项P1和1项P2：Skill专用审计、审计去重/重审、Schema边界、事件联合、验证器合同及审批文案。新证据采用Content v3（29类），事件采用event-v1，版本域互相独立；原v1证据仅历史核验。

[原v0.3快照](releases/v0.3/snapshot.zip)与其manifest/receipt保留不变。v0.5是开发规格冻结，不是应用发布；T/AT/E、真实模型和100h仍须实施验收。

## 6. v0.5冻结范围

新增可信VerificationAssessment、验证义务排空和项目信任失效屏障，关闭[FG01/FG02](reviews/v0.4-freeze-gate-review.md)。新写入Content v3；v1/v2历史字节与摘要保留，事件独立为v1。[原v0.4快照](releases/v0.4/snapshot.zip)不覆盖。v0.5仍是开发规格，应用代码/数据库/真实模型/E2E/100h均待实施。
