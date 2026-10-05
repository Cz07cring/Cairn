# Ringharness 源码导入记录

截至 2026-10-05（Asia/Shanghai），Cairn fork 的 `runtime/ringharness/`
由 Ringharness 隔离开发分支 `codex/cairn-ring-r1` 的提交
`66d02aa9f48ace59e18a7be066124d5b1003b6dd` 通过 `git subtree add --squash`
导入。Cairn 导入提交为 `d23c6ab`；前一提交 `ca42ebf` 保留
`git-subtree-dir` 与 `git-subtree-split` 来源信息。

这一提交将源码放进同一 Git 仓。它还不是同仓可运行部署：Cairn 的旧
`docker-compose.yaml` 仍只启动 Cairn server/dispatcher；Ring Control、
PostgreSQL、Temporal、Runner、Broker、对象仓和独立审计链尚未由 Cairn
仓统一启动。R2/R3 代码、同仓部署配置及浏览器/恢复端到端验收均未完成。
不得把源码存在解释成三权运行链已接通。

后续更新以固定提交做 subtree pull，先检查导入前后 `git subtree split`
和 Ring 源提交，再同步依赖锁、迁移和生成契约。部署须保留独立进程与
权限：Cairn 只保存图和本地请求记录；Ring PostgreSQL/Kernel 管业务状态
与 DONE；Temporal 管持久活动；Runner 与 Broker 管受控执行；Auditor 独立
验收。Cairn 不直写 Ring 数据库，不复制 Broker 或服务密钥到浏览器。

Ringharness 源仓当前没有根 `LICENSE` 文件。发布前必须核对源仓全部代码、
上游 Harness/前端依赖及第三方许可证，不在这里推断可再授权范围。
