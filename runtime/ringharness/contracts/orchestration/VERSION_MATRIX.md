# Temporal 版本矩阵（与 deploy/temporal/VERSIONS.md 同步摘要）
#
# Python SDK: temporalio==1.32.0
# Temporal Server: temporalio/auto-setup@sha256:607d68caa111338d754771efb876c92dfcdae06d056e4530bb31cd0f37406e6a (1.28.1；本机联调已核验)
# Temporal UI: temporalio/ui@sha256:28bb3ea5a6ea3e09f16b521f32ab727c96470f7f1e420c66a6cbfb02001a8aa2
# Temporal PG: postgres@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94 → 127.0.0.1:15433
# TS SDK: @temporalio/{worker,activity,client,workflow}@1.23.0（精确 pin）
# Worker build id (Python): m0-py-1.32.0-dev → queue ring-control
# Worker build id (TS Runner): m0-ts-1.23.0-dev → queue ring-runner
# 状态：部分（Server digest 本机联调已钉；GoalWorkflow→RunActivation→observe；生产 CAN 水位；Worker 失联接续；compose roundtrip 可测；LEGACY 默认创建仍在；PLAN 缺省 PENDING_ENV；观察≠验收/Goal DONE；≠ live Qwen 100h；≠ v0.6 冻结）

权威说明见 ../../deploy/temporal/VERSIONS.md。
