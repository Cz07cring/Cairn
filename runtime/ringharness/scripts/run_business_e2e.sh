#!/usr/bin/env bash
# 真实业务路径验收运行器（收敛仪器）
#
# 用途：把「目标 → 计划 → 执行(官方 AgentLoop) → 封存候选 → 审计 → 集成 →
#       最终屏障 → Kernel 判 DONE」这条**全业务链**反复跑起来，用于：
#         · 单次验收（是否真的能跑通）
#         · 稳定性复现（连跑 N 次，判定「一次侥幸」还是「稳定」）
#         · 后续 10–20 任务 / 8h / 24h / 100h 长跑的底座
#
# 两种模式：
#   FSM/scripted（不需要模型，CI 亦跑）：
#       bash scripts/run_business_e2e.sh                 # 跑 tests/e2e 全部
#   真实模型自主（需 .runtime/chat.env 提供模型凭据）：
#       RING_E2E4_LIVE_CHAT=1 bash scripts/run_business_e2e.sh live 3
#
# 环境要求：隔离 PG/S3（.runtime/*.env）、Temporal（RING_TEMPORAL_TARGET 或 127.0.0.1:7233）、
#           上游 Harness checkout（RING_HARNESS_CHECKOUT；可由 scripts/pull_harness_pin.sh 取得）。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MODE="${1:-all}"      # all | live
REPEAT="${2:-1}"

# ---- 环境：优先 .runtime/*.env，其次本机默认端口 ----
if [ -f .runtime/postgres.env ]; then
  set -a; . .runtime/postgres.env; set +a
fi
if [ -f .runtime/minio.env ]; then
  set -a; . .runtime/minio.env; set +a
fi
if [ -f .runtime/chat.env ]; then
  set -a; . .runtime/chat.env; set +a
fi

PG_PORT="$(docker port ringharness-development-pg 5432 2>/dev/null | sed 's/.*://' || true)"
PG_PASS="${POSTGRES_PASSWORD:-ring}"
MK_PASS="${MINIO_ROOT_PASSWORD:-ring-test}"
export RING_DATABASE_URL="${RING_DATABASE_URL:-postgresql+psycopg://ring:${PG_PASS}@127.0.0.1:${PG_PORT:-15432}/ring_test}"
export RING_TEST_DATABASE_URL="${RING_TEST_DATABASE_URL:-$RING_DATABASE_URL}"
export RING_TEST_S3_ENDPOINT="${RING_TEST_S3_ENDPOINT:-http://127.0.0.1:51110}"
export RING_TEST_S3_ACCESS_KEY="${RING_TEST_S3_ACCESS_KEY:-ring-test}"
export RING_TEST_S3_SECRET_KEY="${RING_TEST_S3_SECRET_KEY:-$MK_PASS}"
export RING_TEMPORAL_TARGET="${RING_TEMPORAL_TARGET:-127.0.0.1:7233}"

PY="${ROOT}/.venv/bin/python"
[ -x "$PY" ] || PY="uv run python"

if [ "$MODE" = "live" ]; then
  export RING_E2E4_LIVE_CHAT=1
  # 与 soak 同口径：live 合同须允许云 Profile（见 RING_LOCAL_QWEN_* / chat.env）
  export RING_TEST_ALLOW_CLOUD=1
  TARGET="tests/e2e/test_e2e4_official_loop_to_goal_done.py::test_e2e4_run_activation_live_diagnose_seal_then_goal_done"
else
  TARGET="tests/e2e/"
fi

# 前置检查（失败关闭，不静默降级）
[ -n "${RING_HARNESS_CHECKOUT:-}" ] || { echo "RING_HARNESS_CHECKOUT 未设置：上游 Harness 缺失会让业务全链用例静默 skip" >&2; exit 2; }
[ -d "${RING_HARNESS_CHECKOUT}/packages/core/agent-loop/lib" ] || { echo "Harness AgentLoop 未构建（缺 lib/）: $RING_HARNESS_CHECKOUT" >&2; exit 2; }

echo "模式=${MODE} 重复=${REPEAT} 目标=${TARGET}"
echo "Temporal=${RING_TEMPORAL_TARGET} 模型=${RING_LOCAL_QWEN_MODEL:-<未设>} base=${RING_LOCAL_QWEN_BASE:-<未设>}"

pass=0; fail=0
for i in $(seq 1 "$REPEAT"); do
  echo "===== 第 ${i}/${REPEAT} 次 ====="
  # shellcheck disable=SC2086
  $PY -m pytest $TARGET -q > "/tmp/rbe-${i}.log" 2>&1
  rc=$?
  tail -1 "/tmp/rbe-${i}.log"
  if [ "$rc" -eq 0 ]; then pass=$((pass+1)); else fail=$((fail+1)); fi
done

echo "===== 汇总：pass=${pass} fail=${fail} ====="
[ "$fail" -eq 0 ]
