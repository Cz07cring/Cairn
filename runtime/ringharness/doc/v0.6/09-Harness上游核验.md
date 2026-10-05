# Harness与Temporal上游边界核验

> v0.6 / DESIGN_REVISION · 2026-09-11。Temporal 技术栈修订，尚未实现、未冻结、未完成运行验收。本文与链接的 v0.5 基线合读：本文明确列出的替换规则优先，其余领域、安全及验收要求全部继承；不把历史“未开始”状态当作当前代码状态。

继承基线：[09-Harness上游核验.md](../09-Harness上游核验.md)。

## 已核验资料与推论边界

本轮只读官方文档，未安装Temporal、未运行重放/集成或100h。下列设计映射是Ringharness选择，不是上游原生提供的三权或三态保证。

| 官方资料 | 可依赖机制 | 项目要求 |
|---|---|---|
| [Temporal架构](https://github.com/temporalio/temporal/blob/main/docs/architecture/README.md) | 事件历史与确定性Workflow；Activity需要考虑幂等/重试 | 模型/工具调用隔离为Activity，业务副作用另设幂等和对账 |
| [Python SDK](https://github.com/temporalio/sdk-python) | Python Workflow/Activity与重放约束 | Kernel IO放控制Activity，Workflow只做确定性编排 |
| [TypeScript SDK](https://github.com/temporalio/sdk-typescript) | TS Worker与Activity适配能力 | 仅Runner承接activation，业务Workflow统一写Python |
| [Continue-As-New](https://docs.temporal.io/workflow-execution/continue-as-new) | 新run承接长期Workflow | 固定业务ID，保留预算/消费水位；run_id不是业务幂等ID |
| [Activity执行](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/activities/activity-execution.mdx) | 取消通知与执行协作 | 不把取消当杀进程/释放资源，独立StopReceipt核对 |

Harness仍按v0.5核验的SHA `c291e7961a515f6d7af9304e7fd1d257929aef26` 作为接入基线；该SHA不是“最新版本”承诺。升级另做构建、类型和权限回归，不能为了配Temporal默认拉最新版。

## 实际调用位置

Python GoalWorkflow → Temporal具名TS RunActivation Activity → Runner加载固定Harness配置 → 独立会话 → 模型/工具桥 → Kernel回执。所有名称为本项目设计命名；官方SDK精确注册签名从锁定版本类型声明获取。

Planner也使用Harness，但不注册任何模型工具或动态Skill工具。ContextCompiler在调用前装配固定规划Skill。Executor工具全部经Broker；Auditor只对授权候选运行验证，不能接入Executor可写缓存和推理历史。

当前代码已有Cordis boot及自有llm桥，不能视为完整官方agent loop；fixture buildPlan不能作为真实规划完成依据。M2必须由实际模型输出解析PlanCreate，并通过Kernel schema/覆盖/预算校验。

## 上游升级验证

固定源码归档内容清单和digest，单独的文本pin文件不能证明源码未改。保存Server/SDK/Harness/Worker构建组合；分别测试JSON跨语言、取消、异常、流事件、工具拒绝、session清理、旧history replay。不能把Temporal历史恢复当作Harness进程内存或worktree自动恢复。
