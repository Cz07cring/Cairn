#!/usr/bin/env bash
# 无人值守业务路径「连续实跑 + 指标汇总」—— 收敛路线 #4/#5/#6 的执行器。
#
# 用途：连跑 N 个真实开发任务，每轮记录可验证的事实，跑完自动汇总四项指标。
#   · #4 连续跑 10–20 个不同真实任务
#   · #5 统计模型一次成功率 / 重试成功率 / Auditor 拦截率 / UNKNOWN 恢复率
#   · #6 8h / 24h / 100h 长跑（本脚本是其底座：用 --hours 或 --until 驱动）
#
# 与既有脚本的分工：
#   scripts/run_business_e2e.sh      —— 单次（或 N 次）业务链验收，判「通不通」
#   scripts/report_business_metrics.py —— 只读指标采集，判「好不好」
#   本脚本                            —— 组合两者 + 落盘可追溯的逐轮记录
#
# 用法：
#   bash scripts/run_convergence_soak.sh --runs 10             # 连跑 10 轮 live
#   bash scripts/run_convergence_soak.sh --runs 20 --out .runtime/soak-20260913
#   bash scripts/run_convergence_soak.sh --runs 100 --label overnight
#   bash scripts/run_convergence_soak.sh --hours 8             # 真按时长跑到截止（Codex P0-2）
#   bash scripts/run_convergence_soak.sh --until 2026-09-14T04:00:00Z
#   bash scripts/run_convergence_soak.sh --hours 24 --runs 500 # 时长优先，--runs 作安全上限
#   bash scripts/run_convergence_soak.sh --runs 20 --isolated-db  # **推荐**：真建独立库，结果可归因
#   bash scripts/run_convergence_soak.sh --self-test   # 自测失败分类器 + 时长参数解析
#
# 设计原则（与仓库不变量一致）：
#   1. **失败关闭**：缺 Harness/模型/库时直接非零退出，不静默降级成「跳过的绿」；
#   2. **不伪造结论**：每轮只记录**可验证事实**（退出码 + pytest 摘要 + 该轮 Goal id）；
#      「成功率」等比率一律由 report_business_metrics.py 从 PG 读取，**不在本脚本里估算**；
#   3. **不宣称 DONE**：本脚本产出的是运营记录，Goal 是否 DONE 只由 Kernel 判定。
#   4. **清单不落凭据**（Codex P1-1）：MANIFEST 只写 host/port/db 名与 URL digest，禁止密码。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

RUNS=""
HOURS=""
UNTIL_UTC=""
# 保留进程已 export 的 OUT（发布候选证据包会注入）；仅未设时默认空，再由下方 ${OUT:-…} 填路径
: "${OUT:=}"
LABEL="soak"
MODE="live"
TARGET_FLAG=""
SELF_TEST=0
ISOLATED_DB_FLAG=0
KEEP_DB=0
# 安全上限：纯 --hours/--until 时防止失控；可用 --runs 覆盖
MAX_RUNS_SAFETY=10000

while [ $# -gt 0 ]; do
  case "$1" in
    --runs) RUNS="$2"; shift 2 ;;
    --hours) HOURS="$2"; shift 2 ;;
    --until) UNTIL_UTC="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    --fsm) MODE="fsm"; shift ;;
    --self-test) SELF_TEST=1; shift ;;
    --isolated-db) ISOLATED_DB_FLAG=1; shift ;;   # 真为本轮建独立库（成功即删/失败保留）
    --keep-db) KEEP_DB=1; shift ;;                # 调试：跑完保留独立库
    --target) TARGET_FLAG="$2"; shift 2 ;;
    -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

# 默认仍为 --runs 10（短跑）；有 --hours/--until 时改为时长驱动
if [ -z "$HOURS" ] && [ -z "$UNTIL_UTC" ] && [ -z "$RUNS" ]; then
  RUNS=10
fi
if [ -n "$HOURS" ] && ! [[ "$HOURS" =~ ^[1-9][0-9]*$ ]]; then
  echo "失败关闭：--hours 须为正整数，实得「${HOURS}」" >&2
  exit 2
fi
if [ -n "$RUNS" ] && ! [[ "$RUNS" =~ ^[1-9][0-9]*$ ]]; then
  echo "失败关闭：--runs 须为正整数，实得「${RUNS}」" >&2
  exit 2
fi

# 为什么需要：只报「失败 1 次」会把人推向「flaky」这一错误结论。
# 本会话实测：同一个 live 用例 6/7 通过、1/7 失败，**失败时机制完全相同**
# （模型反复发非法 tool 参数 → 守卫硬停 → 关全部工具准入 → 激活必然失败）。
# 因此每条失败都必须落到一个**具名机制**上；落不下去的显式报为「未分类」并打印原文尾部，
# 让分类器本身的缺口可见，而不是含糊过去。
classify_log() {
  local log="$1" rc="${2:-1}"
  # pytest 自身退出码是**权威**的非错误结果，优先于文本猜测。
  # 教训：首版把「no tests ran」误判成「环境/连接」，因为 `httpx` 签名命中了
  # 每个日志都有的弃用警告行（StarletteDeprecationWarning: Using httpx ...）。
  # 错误分类比「未分类」更糟：它把排查者指向错误的组件。
  case "$rc" in
    5) echo "无测试被选中(选择器/路径错，非业务失败)"; return 0 ;;
    4) echo "pytest 用法错误(参数/路径)"; return 0 ;;
    2) echo "收集失败(导入/语法/夹具)"; return 0 ;;
  esac
  # 硬停真签名（**第 4 次「签名过宽」教训**）：实测原文是
  #   Error: validation_loop:write_file|sha256:…|VALIDATION_REJECTED；允许零工具总结，禁止再调工具 summary_artifact=…
  # 里面**没有** TOOL_ADMISSION_CLOSED / NO_PROGRESS_FORCE_STOP，因此旧签名漏判，
  # 又被外层 `E2E_RA_BAD_STATUS` wrapper 吞进「覆盖缺口」分支 —— 把 30% 的失败
  # 误标成「模型未收口」。故：① 保留原签名；② 补 validation_loop…VALIDATION_REJECTED；
  # ③ 把 COVER 分支的 wrapper 签名（裸 E2E_RA_BAD_STATUS）删掉 —— 它出现在**所有**失败里。
  if grep -qE "TOOL_ADMISSION_CLOSED.*NO_PROGRESS_FORCE_STOP" "$log"; then
    echo "准入被关(守卫硬停:validation_loop 同内容重复|repeat_banned)"
  elif grep -qE "validation_loop:[^|]*\|sha256:[0-9a-f]+\|VALIDATION_REJECTED" "$log"; then
    # 第三百六十三批：validation_loop 改为软停（禁同参、不关全闸）；仍用真签名分类
    echo "同参软拒绝(validation_loop 空参/校验)"
  elif grep -q "NO_PROGRESS_REPEAT_BLOCKED" "$log"; then
    echo "同参软拒绝(换工具仍可写)"
  elif grep -q "NO_PROGRESS_FORCE_STOP" "$log"; then
    echo "守卫强制停止(其它原因)"
  elif grep -qE "OFFICIAL_LOOP_NO_MODEL_ROUNDS|MODEL_INVOCATION_LEDGER_" "$log"; then
    echo "模型账本/零轮次(Profile或chat未接通)"
  elif grep -qE "CHAT_CATALOG_UNREACHABLE|SSL: UNEXPECTED_EOF|UNEXPECTED_EOF_WHILE_READING" "$log"; then
    # rc-ds10：末两轮 /v1/models SSL EOF 被误吞进错配；目录不可达 ≠ BASE/MODEL 错配
    echo "环境/连接(chat目录瞬时不可达)"
  elif grep -q "CHAT_MODEL_MISMATCH" "$log"; then
    # 真签名来自 scripts/chat_model_gate.py / e2e _assert_live_chat_model_aligned
    echo "chat模型错配(BASE与MODEL未对齐)"
  elif grep -q "OFFICIAL_LOOP_CHAT_DRIVEN_SHORT" "$log"; then
    echo "模型工具数不足(期望≥2 实得<2)"
  elif grep -qE "Test timed out|timed out in [0-9]+ms" "$log"; then
    echo "超时(余量不足或负载)"
  elif grep -qE "LEASE_HEARTBEAT_FAILED|attempt 已非 ACTIVE" "$log"; then
    # 签名取自**真实日志原文**（apps/runner/src/harness/leaseHeartbeat.ts:81 →
    # Kernel LeaseRejected("INVALID_STATE","attempt 已非 ACTIVE")）：
    #   LEASE_HEARTBEAT_FAILED: HTTP 409 {"error":{"code":"INVALID_STATE","message":"attempt 已非 ACTIVE"…
    # 旧签名 `lease|ACTIVE|409` 太宽（实测某日志 `lease` 出现 17 次全是无关回显）；
    # 英文意译「no longer ACTIVE」**从未在真实日志出现**，故不入签名。
    # 收紧：旧签名 `lease|ACTIVE|409` **误命中**（实测某次日志里 `lease` 出现 17 次
    # 全是无关回显）⇒ 把「模型未收口」误判成「租约失效」。要求真签名。
    echo "租约/资源失效(心跳被拒:attempt 非 ACTIVE)"
  elif grep -qE "(E2E_RA_COVER:|OFFICIAL_LOOP_CHAT_DIAG_COVER:)" "$log"; then
    # 覆盖缺口：模型跑完但未满足 coach 要求的工具覆盖（≠ 副作用被拒）。
    # **两个变体分开命名** —— 实测两种都出现过，成因不同：
    #   · OFFICIAL_LOOP_CHAT_DIAG_COVER + missing=seal_candidate → 写了但**未封存**
    #   · E2E_RA_COVER + missing=write_file                     → **连写都没写**
    # 且**不再**把裸 `missing=` 当签名（过宽：任何含该词的日志都会被吞进来）。
    local _miss; _miss="$(grep -oE 'missing=[a-z_,]+' "$log" | head -1 | cut -d= -f2)"
    echo "覆盖缺口(模型未收口;missing=${_miss:-未知})"
  elif grep -qE "(httpx\.[A-Z][A-Za-z]*Error|OSError:|Connection refused|docker: (unexpected|Error|failed))" "$log"; then
    echo "环境/连接(非业务)"
  elif grep -qE "AssertionError" "$log"; then
    echo "断言未达(需读原文细分)"
  else
    echo "未分类(分类器缺口)"
  fi
}

# ---- 分类器自测（--self-test）：让分类逻辑本身可被任何人复验 ----
# 合成日志 → 期望类别；任一不符即非零退出。含一条**回归**用例：
# 纯弃用警告行（每个 pytest 日志都有）不得被判为「环境/连接」。
if [ "$SELF_TEST" = 1 ]; then
  _st_fail=0
  # 注意：classify_log 接的是**日志文件路径**，不是内容 ——
  # 首版直接把内容当参数传，grep 报「No such file or directory」。
  _st_run() {
    local desc="$1" content="$2" rc="$3" want="$4"
    local f; f="$(mktemp)"
    printf '%s\n' "$content" > "$f"
    local got; got="$(classify_log "$f" "$rc")"
    rm -f "$f"
    case "$got" in
      "$want"*) printf '  ✅ %s → %s\n' "$desc" "$got" ;;
      *) printf '  ❌ %s → 实得「%s」，期望「%s」\n' "$desc" "$got" "$want"; _st_fail=$((_st_fail + 1)) ;;
    esac
  }
  echo "=== 失败分类器自测 ==="
  _st_run "未选到测试(rc=5)"          "3 deselected, 2 warnings in 0.02s" 5 "无测试被选中"
  _st_run "pytest 用法错(rc=4)"       "INTERNALERROR> usage: pytest" 4 "pytest 用法错误"
  _st_run "收集失败(rc=2)"            "ERROR tests/x.py - ImportError" 2 "收集失败"
  _st_run "准入被关(守卫硬停)"        "TOOL_ADMISSION_CLOSED:NO_PROGRESS_FORCE_STOP:validation_loop:x" 1 "准入被关"
  # 真原文 validation_loop：**软停**（第三百六十三批）；不得再标成「准入被关」
  _st_run "validation_loop软停(真原文)" "E2E_RA_BAD_STATUS:FAILED:EXECUTE:Error: validation_loop:write_file|sha256:7b10e0b7ceaa15a9f07a6e24ac34d44381cf39329e4167b93e5dda5c31cb45dd|VALIDATION_REJECTED；允许零工具总结，禁止再调工具 summary_artifact=eca1621e" 1 "同参软拒绝(validation_loop"
  # 回归：只有 wrapper、没有 COVER 标记时**不得**判成「覆盖缺口」（wrapper 出现在所有失败里）。
  _st_run "回归:裸 BAD_STATUS 不得算覆盖缺口" "Error: E2E_RA_BAD_STATUS:FAILED:EXECUTE:HTTP_500" 1 "未分类"
  _st_run "同参软拒绝"                "NO_PROGRESS_REPEAT_BLOCKED" 1 "同参软拒绝"
  _st_run "守卫强停(其它)"            "NO_PROGRESS_FORCE_STOP:already_force_stopped" 1 "守卫强制停止"
  _st_run "模型工具数不足"            "OFFICIAL_LOOP_CHAT_DRIVEN_SHORT: 期望 ≥2 工具，实际 0" 1 "模型工具数不足"
  _st_run "账本零轮次"                "OFFICIAL_LOOP_NO_MODEL_ROUNDS: loopError=(none)" 1 "模型账本/零轮次"
  _st_run "chat模型错配"              "Failed: CHAT_MODEL_MISMATCH: RING_LOCAL_QWEN_MODEL=deepseek-flash 不在 http://127.0.0.1:8001/v1/models" 1 "chat模型错配"
  _st_run "chat目录瞬时不可达"        "CHAT_CATALOG_UNREACHABLE: 列举模型失败（已重试 3 次）: <urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING]" 1 "环境/连接"
  _st_run "超时"                      "Error: Test timed out in 5000ms." 1 "超时"
  # 夹具必须用**真实日志原文**：此前的英文意译（"attempt is no longer ACTIVE (409)"）
  # 在真实日志里**从未出现**，会让签名与现实脱节（改完签名自测即红，才发现是夹具假）。
  _st_run "租约/资源失效(真原文)"     "Error: LEASE_HEARTBEAT_FAILED: HTTP 409 {\"error\":{\"code\":\"INVALID_STATE\",\"message\":\"attempt 已非 ACTIVE\",\"retryable\":false}}" 1 "租约/资源失效"
  _st_run "覆盖缺口(未封存)"          "Error: E2E_RA_BAD_STATUS:FAILED:EXECUTE:OFFICIAL_LOOP_CHAT_DIAG_COVER: missing=seal_candidate modelRounds=26" 1 "覆盖缺口(模型未收口;missing=seal_candidate)"
  _st_run "覆盖缺口(连写都没写)"      "Error: E2E_RA_COVER: missing=write_file got=git_diff,run_tests,read_file,read_file" 1 "覆盖缺口(模型未收口;missing=write_file)"
  _st_run "回归:裸 missing= 不得单独成类" "step: cache missing=0 items loaded" 1 "未分类"
  _st_run "回归:无关 lease 回显不得算租约" "config: lease_ttl=600 ACTIVE pool_size=5 lease lease" 1 "未分类"
  _st_run "环境/连接(真异常)"         "httpx.ConnectError: all connection attempts failed" 1 "环境/连接"
  _st_run "断言未达"                  "AssertionError: assert 'FAILED' == 'DONE'" 1 "断言未达"
  _st_run "未分类(缺口可见)"          "something entirely new" 1 "未分类"
  _st_run "回归:弃用警告不得算环境"   "StarletteDeprecationWarning: Using \`httpx\` with starlette_testclient" 0 "未分类"
  echo "-------------------------------------------------------"
  # Codex P0-2：时长参数必须真实可解析（不得只写在注释里）
  echo "=== 时长参数解析自测 ==="
  _deadline_epoch() {
    python3 -c "
from datetime import datetime, timezone, timedelta
mode, a = '$1', '$2'
now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
if mode == 'hours':
    d = now + timedelta(hours=int(a))
elif mode == 'until':
    d = datetime.strptime(a, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
else:
    raise SystemExit('bad mode')
print(int(d.timestamp()))
"
  }
  got_h="$(_deadline_epoch hours 8)"
  want_h=1789329600  # 2026-09-13T20:00:00Z
  if [ "$got_h" = "$want_h" ]; then
    echo "  ✅ --hours 8 截止 epoch=$got_h"
  else
    echo "  ❌ --hours 解析失败：实得 $got_h 期望 $want_h"; _st_fail=$((_st_fail + 1))
  fi
  got_u="$(_deadline_epoch until 2026-09-14T04:00:00Z)"
  want_u=1789358400
  if [ "$got_u" = "$want_u" ]; then
    echo "  ✅ --until 截止 epoch=$got_u"
  else
    echo "  ❌ --until 解析失败：实得 $got_u 期望 $want_u"; _st_fail=$((_st_fail + 1))
  fi
  echo "-------------------------------------------------------"
  # Codex P0-1：ModelInvocation 账本闸门（纯函数，不连库；须用项目 venv 以加载 sqlalchemy）
  echo "=== ModelInvocation 账本闸门自测 ==="
  _ST_PY="$ROOT/.venv/bin/python"
  [ -x "$_ST_PY" ] || _ST_PY="uv run python"
  _ledger_st="$($_ST_PY - <<'PY'
import importlib.util, sys
from pathlib import Path
root = Path(".").resolve()
spec = importlib.util.spec_from_file_location(
    "report_business_metrics", root / "scripts" / "report_business_metrics.py"
)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
sys.modules["report_business_metrics"] = m  # dataclass 解析依赖 sys.modules
spec.loader.exec_module(m)
empty = {"metrics": {"model_first_attempt_success": {"numerator": 0, "denominator": 0}}}
ok = {"metrics": {"model_first_attempt_success": {"numerator": 1, "denominator": 2}}}
try:
    m.require_model_invocation_ledger(empty)
    print("EMPTY_SHOULD_RAISE")
    sys.exit(1)
except RuntimeError as e:
    if "MODEL_INVOCATION_LEDGER_EMPTY" not in str(e):
        print(f"WRONG_ERROR:{e}")
        sys.exit(1)
assert m.require_model_invocation_ledger(ok) == 2
print("OK")
PY
)" || true
  if [ "$_ledger_st" = "OK" ]; then
    echo "  ✅ require_model_invocation_ledger 空账本失败关闭 / 有分母通过"
  else
    echo "  ❌ ModelInvocation 闸门自测失败：$_ledger_st"; _st_fail=$((_st_fail + 1))
  fi
  echo "-------------------------------------------------------"
  # Codex P0-3：同候选锁版闸门
  echo "=== 发布候选锁版自测 ==="
  if $_ST_PY scripts/assert_release_candidate_lock.py --self-test; then
    echo "  ✅ assert_release_candidate_lock 同 SHA / 缺 SHA / 不一致 / expect / dirty"
  else
    echo "  ❌ 发布候选锁版自测失败"; _st_fail=$((_st_fail + 1))
  fi
  echo "-------------------------------------------------------"
  if [ "$_st_fail" = 0 ]; then echo "分类器自测通过"; exit 0; fi
  echo "分类器自测失败 ${_st_fail} 项"; exit 1
fi


STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${OUT:-$ROOT/.runtime/soak-${LABEL}-${STAMP}}"
mkdir -p "$OUT"
SUMMARY="$OUT/summary.tsv"

# ---- 环境（与 scripts/run_business_e2e.sh 同口径）----
# ⚠ 合并顺序必须是 **进程显式 export > chat.env > qwen.env**（AGENTS §9 明订）。
# 但 `set -a; . file` 会**无条件覆盖**已存在的值 —— 于是 chat.env 里的
# RING_LOCAL_QWEN_BASE=…deepseek… 会静默吃掉调用方的显式 export。
# 实测（2026-09-13）：显式指向本机 :8001 + 本机 model 名后，脚本仍打印
# `模型=deepseek-flash base=https://api.deepseek.com`。
# 危害不是「没生效」，而是**假实验结论** —— 做模型对照的人会看到「换了模型结果一样」，
# 而两轮其实是同一个模型。属 AGENTS §7.1 规则 17 同族：旋钮每层都可能静默丢弃。
_SAVE_BASE="${RING_LOCAL_QWEN_BASE-}"
_SAVE_MODEL="${RING_LOCAL_QWEN_MODEL-}"
_SAVE_KEY="${RING_LOCAL_QWEN_API_KEY-}"
_SAVE_CLOUD="${RING_CLOUD_MODE-}"
_SAVE_TIMEOUT="${RING_LOCAL_QWEN_TIMEOUT-}"
# 另两个 provider 授权旋钮同样必须护住 —— 否则「换 provider_ref 做实验」会被静默覆盖，
# 得出假的「改了也没用」结论（本会话已因这类静默覆盖差点误判过模型对照）。
_SAVE_MPR="${RING_MODEL_PROVIDER_REF-}"
_SAVE_CCPR="${RING_CHAT_CLOUD_PROVIDER_REF-}"
# 云合同开关同样要护住：它是「夹具 Profile 是否跟随部署云模式」的唯一开关。
_SAVE_ALLOW_CLOUD="${RING_TEST_ALLOW_CLOUD-}"
for f in .runtime/postgres.env .runtime/minio.env .runtime/chat.env; do
  [ -f "$f" ] && { set -a; . "$f"; set +a; }
done
[ -n "$_SAVE_BASE" ] && export RING_LOCAL_QWEN_BASE="$_SAVE_BASE"
[ -n "$_SAVE_MODEL" ] && export RING_LOCAL_QWEN_MODEL="$_SAVE_MODEL"
[ -n "$_SAVE_KEY" ] && export RING_LOCAL_QWEN_API_KEY="$_SAVE_KEY"
[ -n "$_SAVE_CLOUD" ] && export RING_CLOUD_MODE="$_SAVE_CLOUD"
[ -n "$_SAVE_TIMEOUT" ] && export RING_LOCAL_QWEN_TIMEOUT="$_SAVE_TIMEOUT"
[ -n "$_SAVE_MPR" ] && export RING_MODEL_PROVIDER_REF="$_SAVE_MPR"
[ -n "$_SAVE_CCPR" ] && export RING_CHAT_CLOUD_PROVIDER_REF="$_SAVE_CCPR"
[ -n "$_SAVE_ALLOW_CLOUD" ] && export RING_TEST_ALLOW_CLOUD="$_SAVE_ALLOW_CLOUD"

PG_PORT="$(docker port ringharness-development-pg 5432 2>/dev/null | sed 's/.*://' || true)"
PG_PASS="${POSTGRES_PASSWORD:-ring}"
MK_PASS="${MINIO_ROOT_PASSWORD:-ring-test}"
export RING_DATABASE_URL="${RING_DATABASE_URL:-postgresql+psycopg://ring:${PG_PASS}@127.0.0.1:${PG_PORT:-15432}/ring_test}"
export RING_TEST_DATABASE_URL="${RING_TEST_DATABASE_URL:-$RING_DATABASE_URL}"
export RING_TEST_S3_ENDPOINT="${RING_TEST_S3_ENDPOINT:-http://127.0.0.1:51110}"
export RING_TEST_S3_ACCESS_KEY="${RING_TEST_S3_ACCESS_KEY:-ring-test}"
export RING_TEST_S3_SECRET_KEY="${RING_TEST_S3_SECRET_KEY:-$MK_PASS}"
export RING_TEMPORAL_TARGET="${RING_TEMPORAL_TARGET:-127.0.0.1:7233}"

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="uv run python"


# ---- 前置检查：失败关闭 ----
# 注意：`--self-test` 只验证失败分类器，**不需要**任何环境 ⇒ 跳过前置检查，
# 否则自测会因为「没配环境」而失败关闭（首版即如此）。
fail() { echo "失败关闭：$*" >&2; exit 2; }
[ -n "${RING_HARNESS_CHECKOUT:-}" ] || fail "RING_HARNESS_CHECKOUT 未设置（上游 Harness 缺失会让业务链用例静默 skip）"
[ -d "${RING_HARNESS_CHECKOUT}/packages/core/agent-loop/lib" ] || fail "Harness AgentLoop 未构建: $RING_HARNESS_CHECKOUT"
command -v docker >/dev/null || fail "缺 docker（无法探测开发容器端口）"
docker port ringharness-development-pg 5432 >/dev/null 2>&1 || fail "开发 PG 容器未运行（ringharness-development-pg）"

# 位置说明：必须放在**前置检查之后**。首版放在 PY 解析后（预检查之前），
# 于是任何 fail-closed（如缺 RING_HARNESS_CHECKOUT）都会在**建库之后**退出，
# 留下孤儿库（实测：一次 exit 2 遗留 ring_soak_…）。
# 自己遵守刚写下的规矩：**先校验，再产生副作用**。

# ---- 独立库（--isolated-db）：让失败率**可归因** ----
# 为什么必须：全仓（serve_local / test_local / check_batch）共用同一个 `ring_test`；
# 且 e2e 用例自带 `UPDATE activities SET status='CANCELLED' … WHERE goal_id<>:goal`
# （取消**其他** Goal 的活动）⇒ 多方互损。共享库上的「失败率」无法归因到产品
# （AGENTS §7.1 规则 16 的成因）。实测：共享库 10 轮 8/10；独立库 20 轮 18/20，
# 但两者**机制不同** —— 独立库仍暴露真实缺陷（覆盖缺口），共享库额外注入干扰。
#
# 与 `RING_SOAK_ISOLATED_DB=1`（声明式）的分工：**声明可能为假**，
# 本开关是**实际创建**并把 DB 名写进 MANIFEST，使「可归因」是被证实而非被声称。
ISO_DB_NAME=""
if [ "$ISOLATED_DB_FLAG" = 1 ]; then
  ADMIN_URL="${RING_DATABASE_URL%%/ring_test}"
  ISO_DB_NAME="ring_soak_$(date -u +%Y%m%d%H%M%S)_$$"
  echo "== 独立库模式：创建 $ISO_DB_NAME =="
  "$PY" - "$ADMIN_URL" "$ISO_DB_NAME" <<'PYEOF' || fail "创建独立库失败（--isolated-db）"
import sys, psycopg
base, name = sys.argv[1], sys.argv[2]
url = base.replace("postgresql+psycopg://", "postgresql://") + "/postgres"
with psycopg.connect(url, autocommit=True) as conn:
    conn.execute(f'CREATE DATABASE "{name}"')
print("  created", name)
PYEOF
  export RING_DATABASE_URL="${ADMIN_URL}/${ISO_DB_NAME}"
  export RING_TEST_DATABASE_URL="$RING_DATABASE_URL"
  echo "  RING_DATABASE_URL → …/${ISO_DB_NAME}"
  "$PY" -m alembic upgrade head >"$ROOT/.runtime/alembic-iso.log" 2>&1 \
    || fail "独立库 alembic upgrade 失败（见 .runtime/alembic-iso.log）"
  echo "  alembic upgrade head ✓"
fi

if [ "$MODE" = "live" ]; then
  [ -n "${RING_LOCAL_QWEN_BASE:-}" ] && [ -n "${RING_LOCAL_QWEN_MODEL:-}" ] \
    || fail "live 模式缺模型配置（RING_LOCAL_QWEN_BASE/MODEL；检查 .runtime/chat.env）"
  export RING_E2E4_LIVE_CHAT=1
  # Codex P0-1：账本 create 须命中冻结 Profile；无此开关时夹具固定 DENY+本机 Qwen，
  # 而 chat.env 常为 DeepSeek → ModelInvocation 拒登 → modelRounds=0 假 COVER。
  export RING_TEST_ALLOW_CLOUD=1
  # 第三百六十二批：显式 export 优先于 chat.env 时，常出现「:8001 + deepseek-*」错配。
  # 在烧轮次前用 /v1/models 失败关闭（exit 2），禁止带着 model not found 空转。
  echo "live 前置：核对 RING_LOCAL_QWEN_MODEL ∈ ${RING_LOCAL_QWEN_BASE}/v1/models …"
  "$PY" - <<'PY' || fail "live chat 模型错配（CHAT_MODEL_MISMATCH；清进程 RING_LOCAL_QWEN_* 或对齐 chat.env）"
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path("scripts").resolve()))
from chat_model_gate import ChatModelMismatch, assert_chat_model_configured
try:
    r = assert_chat_model_configured(dict(os.environ), timeout=15.0)
except ChatModelMismatch as e:
    print(e.message, file=sys.stderr)
    raise SystemExit(1)
print(f"  ✅ model={r.model} base={r.base}（目录 {len(r.model_ids)} 项）")
PY
  TARGET="tests/e2e/test_e2e4_official_loop_to_goal_done.py::test_e2e4_run_activation_live_diagnose_seal_then_goal_done"
  echo "注意：live 模式按 .runtime/chat.env（或进程显式 export）决定**实际**模型端点；"
  echo "      本脚本不在运行中切换 provider —— 要换模型请改 chat.env 或显式 export 后重跑。"
else
  TARGET="tests/e2e/"
fi
[ -n "$TARGET_FLAG" ] && TARGET="$TARGET_FLAG"

# ---- 时长驱动（Codex P0-2）：--hours / --until 优先于纯轮次 ----
START_TS="$(date -u +%s)"
DRIVER="runs"
DEADLINE_TS=""
STATE_FILE="$OUT/soak_state.env"

if [ -n "$UNTIL_UTC" ]; then
  DRIVER="until"
  DEADLINE_TS="$(
    $PY -c "
from datetime import datetime, timezone
raw = '''$UNTIL_UTC'''
d = datetime.strptime(raw, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
print(int(d.timestamp()))
"
  )" || fail "--until 须为 UTC，形如 2026-09-14T04:00:00Z"
elif [ -n "$HOURS" ]; then
  DRIVER="hours"
  # 续跑：同 --out 目录若已有 started_utc，则从原起点计时（Codex：重启后续计时）
  if [ -f "$STATE_FILE" ]; then
    # shellcheck disable=SC1090
    . "$STATE_FILE"
    START_TS="${SOAK_STARTED_TS:-$START_TS}"
    DEADLINE_TS="${SOAK_DEADLINE_TS:-}"
  fi
  if [ -z "${DEADLINE_TS:-}" ]; then
    DEADLINE_TS=$((START_TS + HOURS * 3600))
  fi
fi

MAX_RUNS="${RUNS:-$MAX_RUNS_SAFETY}"
if [ "$DRIVER" = "runs" ]; then
  MAX_RUNS="$RUNS"
fi

DEADLINE_UTC="n/a"
if [ -n "$DEADLINE_TS" ]; then
  DEADLINE_UTC="$($PY -c "from datetime import datetime, timezone; print(datetime.fromtimestamp(int('$DEADLINE_TS'), tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))")"
  {
    echo "SOAK_STARTED_TS=$START_TS"
    echo "SOAK_DEADLINE_TS=$DEADLINE_TS"
    echo "SOAK_DRIVER=$DRIVER"
  } > "$STATE_FILE"
fi

echo "==================================================================="
echo "业务路径连续实跑  模式=${MODE}  驱动=${DRIVER}  输出=${OUT}"
if [ "$DRIVER" = "runs" ]; then
  echo "轮次上限=${MAX_RUNS}"
else
  echo "截止 UTC=${DEADLINE_UTC}  轮次安全上限=${MAX_RUNS}"
fi
echo "Temporal=${RING_TEMPORAL_TARGET}"
# ---- 配置一致性检查：历史坑的记录 + 失败时的排查线索 --------------------------
# 历史（2026-09-13，Issue #70）：`.runtime/chat.env` 指向云端时，测试夹具曾以
# DENY + 本机 ref 建 ModelProfile，与部署层不一致 ⇒ 官方账本 422 ⇒ 该错被吞 ⇒
# 外部只看到「模型零轮」（实测 0 轮/2s，而配置一致时为 1 passed/41s）。
#
# **该缺陷已由 Cursor 第三百五十八批从源头修掉**（夹具 Profile 跟随部署云模式）。
# 实测（2026-09-13，产品码 9e361b2，独立库 10 轮）：**不设** RING_TEST_ALLOW_CLOUD
# 亦正常跑通（43–81s/轮，而非 3s 快速失败）⇒ 该变量**不再是必需品**。
#
# 故此处**不再断言会失败** —— 断言一个已被修掉的前提，会把读者引向错误方向
# （正是本会话反复出现的「过时断言」毛病）。只在真出现「模型零轮」时给排查线索；
# 是否设置仍如实记入 MANIFEST。
case "${RING_LOCAL_QWEN_BASE:-}" in
  *127.0.0.1*|*localhost*|'') ;;
  *)
    if [ "${RING_TEST_ALLOW_CLOUD:-}" != "1" ]; then
      echo "ℹ️  chat base 指向远程且未设 RING_TEST_ALLOW_CLOUD=1。"
      echo "     历史上这会导致账本 422（被吞成「模型零轮」，见 Issue #70）；"
      echo "     该缺陷已由第三百五十八批从源头修复，实测不设亦可跑通。"
      echo "     若本次出现「模型零轮」，可先试：RING_TEST_ALLOW_CLOUD=1"
    fi
    ;;
esac
echo "模型=${RING_LOCAL_QWEN_MODEL:-<未设>}  base=${RING_LOCAL_QWEN_BASE:-<未设>}  allow_cloud=${RING_TEST_ALLOW_CLOUD:-<未设>}"
echo "开始（UTC）：$($PY -c "from datetime import datetime, timezone; print(datetime.fromtimestamp(int('$START_TS'), tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))")"
echo "==================================================================="

# Codex P0-3：运行期锁版 —— SHA/脏树在**启动时**钉死，收尾写 MANIFEST 不得重读当时 HEAD
# （rc372：INDEX=cea0678 而 MANIFEST/锁版=91187f5 + git_dirty=1，因结束时 --expect-sha HEAD 假绿）
HEAD_NOW="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unset)"
if [ -n "${RING_SOAK_LOCK_SHA:-}" ]; then
  LOCKED_GIT_SHA="${RING_SOAK_LOCK_SHA}"
  if [ "$HEAD_NOW" != "$LOCKED_GIT_SHA" ] && [ "$LOCKED_GIT_SHA" != "unset" ]; then
    echo "失败关闭：RING_SOAK_LOCK_SHA=${LOCKED_GIT_SHA:0:12}… ≠ HEAD=${HEAD_NOW:0:12}…（启动时已漂移）" >&2
    exit 3
  fi
else
  LOCKED_GIT_SHA="$HEAD_NOW"
fi
LOCKED_GIT_SHA_SHORT="$(git -C "$ROOT" rev-parse --short "$LOCKED_GIT_SHA" 2>/dev/null || echo unset)"
# 与证据包一致：只认已跟踪脏树（未跟踪噪声不阻断；REQUIRE_CLEAN 时拒绝 tracked dirty）
if git -C "$ROOT" status --porcelain --untracked-files=no 2>/dev/null | grep -q .; then
  LOCKED_GIT_DIRTY=1
else
  LOCKED_GIT_DIRTY=0
fi
if [ "${RING_SOAK_REQUIRE_CLEAN:-0}" = "1" ] && [ "$LOCKED_GIT_DIRTY" = "1" ]; then
  echo "失败关闭：RING_SOAK_REQUIRE_CLEAN=1 但启动时已有已跟踪脏树" >&2
  git -C "$ROOT" status --porcelain --untracked-files=no | head -20 >&2
  exit 3
fi
echo "锁版 git_sha=${LOCKED_GIT_SHA_SHORT} dirty=${LOCKED_GIT_DIRTY} require_clean=${RING_SOAK_REQUIRE_CLEAN:-0}"

PASS=0; FAIL=0; COMPLETED=0
# 续跑：同 OUT 已有 summary 则保留并接续序号
if [ -f "$SUMMARY" ] && [ "$(wc -l < "$SUMMARY" | tr -d ' ')" -gt 1 ]; then
  COMPLETED="$(awk -F'\t' 'NR>1 {c=$1} END{print c+0}' "$SUMMARY")"
  PASS="$(awk -F'\t' 'NR>1 && $2==0 {n++} END{print n+0}' "$SUMMARY")"
  FAIL="$(awk -F'\t' 'NR>1 && $2!=0 {n++} END{print n+0}' "$SUMMARY")"
  echo "续跑：已有 ${COMPLETED} 轮记录，从第 $((COMPLETED + 1)) 轮继续"
else
  printf 'run\texit\tresult_line\tseconds\n' > "$SUMMARY"
fi

i="$COMPLETED"
while true; do
  NOW="$(date -u +%s)"
  if [ -n "$DEADLINE_TS" ] && [ "$NOW" -ge "$DEADLINE_TS" ]; then
    echo "到达截止时间 ${DEADLINE_UTC}，停止新轮次（已完成 ${i} 轮）"
    break
  fi
  if [ "$i" -ge "$MAX_RUNS" ]; then
    echo "到达轮次上限 ${MAX_RUNS}，停止"
    break
  fi
  i=$((i + 1))
  LOG="$OUT/run-$(printf '%03d' "$i").log"
  T0="$(date -u +%s)"
  # shellcheck disable=SC2086
  $PY -m pytest $TARGET -q > "$LOG" 2>&1
  RC=$?
  T1="$(date -u +%s)"
  SECS=$((T1 - T0))
  LINE="$(grep -E '[0-9]+ (passed|failed)' "$LOG" | tail -1 | sed 's/^[[:space:]]*//')"
  [ -n "$LINE" ] || LINE="（无 pytest 摘要，见日志）"
  if [ "$RC" -eq 0 ]; then PASS=$((PASS + 1)); else FAIL=$((FAIL + 1)); fi
  printf '%s\t%s\t%s\t%s\n' "$i" "$RC" "$LINE" "$SECS" >> "$SUMMARY"
  if [ -n "$DEADLINE_TS" ]; then
    echo "[$i/? deadline=${DEADLINE_UTC}] exit=${RC} ${SECS}s | ${LINE}"
  else
    echo "[$i/$MAX_RUNS] exit=${RC} ${SECS}s | ${LINE}"
  fi
done
COMPLETED="$i"
RUNS_DONE="$COMPLETED"

ELAPSED=$(( $(date -u +%s) - START_TS ))

echo
echo "===================== 逐轮结果汇总 ====================="
echo "轮次  exit  用时      结果"
awk -F'\t' 'NR>1 {printf "%-5s %-5s %-9s %s\n", $1, $2, $4"s", $3}' "$SUMMARY"
echo "-------------------------------------------------------"
echo "通过 ${PASS} / 失败 ${FAIL} / 共 ${RUNS_DONE}；总用时 ${ELAPSED}s（驱动=${DRIVER}）"
echo "逐轮日志：$OUT/run-NNN.log"

# ---- 失败分类（把「1 failed」变成「1 failed：<机制>」）----

TRIAGE="$OUT/triage.tsv"
printf 'run\tclass\tevidence\n' > "$TRIAGE"
if [ "$FAIL" -gt 0 ]; then
  echo
  echo "===================== 失败机制分类 ====================="
  UNCLASSIFIED=0
  for i in $(seq 1 "$RUNS_DONE"); do
    [[ "$(awk -F'\t' -v n="$i" 'NR>1 && $1==n {print $2}' "$SUMMARY")" == "0" ]] && continue
    LOG="$OUT/run-$(printf '%03d' "$i").log"
    RC_I="$(awk -F'\t' -v n="$i" 'NR>1 && $1==n {print $2}' "$SUMMARY")"
    CLS="$(classify_log "$LOG" "$RC_I")"
    # EV 必须先初始化：`set -u` 下若 case 全不匹配，`[ -z "$EV" ]` 会以
    # `EV: unbound variable` **中断整个收尾**（实测：分类表/MANIFEST/指标全丢）。
    EV=""
    # 证据抽取：**按类别取真签名**，最后才兜底到通用错误行。
    # 教训（两次同类）：单行宽 grep 会抓到无关行 —— 曾抓 `live_chat 仅支持 via_run_activation`
    # （与本次失败无关），把排查引向错误方向。
    case "$CLS" in
      租约/资源失效*)
        EV="$(grep -oE 'LEASE_HEARTBEAT_FAILED[^"]{0,70}' "$LOG" | head -1)"
        [ -z "$EV" ] && EV="$(grep -oE '(attempt 已非 ACTIVE|no longer ACTIVE|HTTP 409)' "$LOG" | head -1)"
        ;;
      覆盖缺口*)
        EV="$(grep -oE 'OFFICIAL_LOOP_CHAT_DIAG_COVER[^"]{0,90}' "$LOG" | head -1)"
        [ -z "$EV" ] && EV="$(grep -oE 'E2E_RA_COVER[^"]{0,90}' "$LOG" | head -1)"
        [ -z "$EV" ] && EV="$(grep -oE 'missing=[a-z_,]+' "$LOG" | head -1)"
        ;;
      准入被关*|同参软拒绝*|守卫强制停止*)
        # 先取**真签名**（validation_loop…VALIDATION_REJECTED 自带 sha256 与效应编号，
        # 是定位「模型在同一份内容上打转」的关键）；旧签名在真原文里不存在，
        # 导致 EV 为空并掉进通用兜底（抓到无关断言行）。
        EV="$(grep -oE 'validation_loop:[^"]{0,120}' "$LOG" | head -1)"
        [ -z "$EV" ] && EV="$(grep -oE '(TOOL_ADMISSION_CLOSED[^;"]{0,60}|NO_PROGRESS_[A-Z_]+)' "$LOG" | head -1)"
        ;;
      模型工具数不足*)
        EV="$(grep -oE 'OFFICIAL_LOOP_CHAT_DRIVEN_SHORT[^;"]{0,40}' "$LOG" | head -1)"
        ;;
      超时*)
        EV="$(grep -oE 'Test timed out in [0-9]+ms' "$LOG" | head -1)"
        ;;
    esac
    # 通用兜底：**优先取 runner 的 `stderr=Error:` 段** —— 那才是真实首错所在的载荷，
    # 而裸 `AssertionError|Error` 会先命中 pytest 打印的**测试源码行**
    # （实测抓到 `live_chat 仅支持 via_run_activation`，与失败无关）。
    if [ -z "$EV" ]; then
      EV="$(grep -oE 'stderr=Error:[^"\\]{0,140}' "$LOG" | head -1)"
    fi
    [ -z "$EV" ] && EV="$(grep -oE '^E\s+(AssertionError|RuntimeError)[^\n]{0,110}' "$LOG" | head -1)"
    [ -z "$EV" ] && EV="$(grep -m1 -E 'AssertionError|Error' "$LOG" | cut -c1-80)"
    printf '%s\t%s\t%s\n' "$i" "$CLS" "$EV" >> "$TRIAGE"
    echo "  第 $i 轮: $CLS"
    [ -n "$EV" ] && echo "            证据: $EV"
    case "$CLS" in 未分类*) UNCLASSIFIED=$((UNCLASSIFIED + 1));; esac
  done
  echo "-------------------------------------------------------"
  echo "失败 ${FAIL} 次，已分类 $((FAIL - UNCLASSIFIED)) 次"
  if [ "$UNCLASSIFIED" -gt 0 ]; then
    echo "⚠ 有 ${UNCLASSIFIED} 次**未分类** —— 这是分类器的缺口，不是「原因不明」。"
    echo "  请在 classify_log() 中补一条签名；原文尾部："
    for i in $(seq 1 "$RUNS_DONE"); do
      CLS="$(awk -F'\t' -v n="$i" 'NR>1 && $1==n {print $2}' "$TRIAGE")"
      case "$CLS" in 未分类*) echo "  --- 第 $i 轮 ---"; tail -8 "$OUT/run-$(printf '%03d' "$i").log" | sed 's/^/      /';; esac
    done
  fi
  echo "分类表：$TRIAGE"
fi

# ---- 指标采集（真值来自 PG，不在本脚本估算）----
echo
echo "===================== 指标（只读采集）====================="
METRICS_JSON="$OUT/metrics.json"
METRICS_HOURS=6
if [ -n "$HOURS" ] && [ "$HOURS" -gt 6 ]; then METRICS_HOURS="$HOURS"; fi
BY_GOAL_LIMIT="$RUNS_DONE"
[ "$BY_GOAL_LIMIT" -lt 1 ] && BY_GOAL_LIMIT=1
RC_METRICS=0
RC_LEDGER=0
if $PY scripts/report_business_metrics.py --hours "$METRICS_HOURS" --by-goal "${BY_GOAL_LIMIT}" --json > "$METRICS_JSON" 2>"$OUT/metrics.err"; then
  $PY scripts/report_business_metrics.py --hours "$METRICS_HOURS" --by-goal "${BY_GOAL_LIMIT}" 2>/dev/null | sed -n '1,40p'
  echo
  echo "指标 JSON：$METRICS_JSON"
  # Codex P0-1：live 且本批有 PASS 时，ModelInvocation 分母必须 >0（失败关闭 exit 3）
  if [ "$MODE" = "live" ] && [ "$PASS" -gt 0 ]; then
    if $PY scripts/report_business_metrics.py --hours "$METRICS_HOURS" --by-goal "${BY_GOAL_LIMIT}" --require-model-invocations >/dev/null; then
      echo "ModelInvocation 账本闸门：通过（Codex P0-1）"
    else
      echo "失败关闭：live PASS>0 但 ModelInvocation 账本分母为 0（Codex P0-1）" >&2
      RC_LEDGER=3
    fi
  fi
  # Codex P1-4：长跑可显式要求故障注入样本（短 soak 默认不开，避免共享库误杀）
  if [ "${RING_SOAK_REQUIRE_FAULT_SAMPLES:-0}" = "1" ] && [ "$PASS" -gt 0 ]; then
    if $PY scripts/report_business_metrics.py --hours "$METRICS_HOURS" --by-goal "${BY_GOAL_LIMIT}" --require-fault-samples >/dev/null; then
      echo "故障注入样本闸门：通过（Codex P1-4）"
    else
      echo "失败关闭：要求故障样本但 unknown_recovery/retry_success 分母不足（Codex P1-4）" >&2
      RC_LEDGER=3
    fi
  fi
else
  echo "⚠ 指标采集失败（见 $OUT/metrics.err）——**不**以退出码 0 掩盖" >&2
  RC_METRICS=1
fi

# ---- 落盘运行说明（可追溯；Codex P1-1/P1-2：脱敏 + 隔离标记一致）----
DB_META="$($PY -c "
import hashlib, os
from urllib.parse import urlparse
raw = os.environ.get('RING_DATABASE_URL') or ''
# 禁止把密码写入 MANIFEST；只暴露 host/port/db + url digest
safe = raw.replace('postgresql+psycopg://', 'postgresql://', 1)
u = urlparse(safe)
digest = hashlib.sha256(raw.encode()).hexdigest()[:16] if raw else 'unset'
print(f\"db_host={u.hostname or 'unset'}\")
print(f\"db_port={u.port or 'unset'}\")
print(f\"db_name={(u.path or '/').lstrip('/') or 'unset'}\")
print(f\"db_url_sha256_16={digest}\")
")"
# ---- 隔离判定：**推导优先，声明兜底，矛盾告警** ----
# 为什么不让调用方自己宣称：声明可能为假。若有人对着共享 `ring_test` 设
# `RING_SOAK_ISOLATED_DB=1`，MANIFEST 就会记下「可归因」的**假声明** —— 属「假绿」家族
# （AGENTS §7.1 规则 16/17）。故：`--isolated-db` **实际建库**时自动判为专用；
# 仅有声明而库名仍是 `ring_test` 时，记下声明但**显式告警矛盾**。
# 复用上面 DB_META 已算好的 db_name —— **单一真相源**。
# 不再跑第二个 python（首版另起一段解析，结果为空且与 DB_META 不一致，
# 属「同一事实两处独立推导」的经典分叉）。
DB_NAME_ACTUAL="$(printf '%s\n' "$DB_META" | sed -n 's/^db_name=//p' | head -1)"
[ -n "$DB_NAME_ACTUAL" ] || DB_NAME_ACTUAL="unset"
if [ -n "$ISO_DB_NAME" ]; then
  DB_IS_SHARED_DEV=0
  ISOLATED_DB=1
  DB_ISOLATION_NOTE="--isolated-db：脚本**实际创建**专用库 $ISO_DB_NAME 并跑 alembic head（可归因）"
elif [ "${RING_SOAK_ISOLATED_DB:-0}" = "1" ] && [ "$DB_NAME_ACTUAL" != "ring_test" ]; then
  DB_IS_SHARED_DEV=0
  ISOLATED_DB=1
  DB_ISOLATION_NOTE="RING_SOAK_ISOLATED_DB=1 且库名 ${DB_NAME_ACTUAL}≠ring_test：调用方声明为专用库"
else
  DB_IS_SHARED_DEV=1
  ISOLATED_DB=0
  DB_ISOLATION_NOTE="默认开发容器/业务库视为共享库，非验收专用库（与 DB_IS_SHARED_DEV=1 一致）"
fi
if [ "${RING_SOAK_ISOLATED_DB:-0}" = "1" ] && [ "$DB_NAME_ACTUAL" = "ring_test" ]; then
  echo "⚠ 矛盾：调用方声明 RING_SOAK_ISOLATED_DB=1，但库名仍是共享的 ring_test。" >&2
  echo "  已按**共享库**记账（DB_IS_SHARED_DEV=1）—— 声明不覆盖事实。要真隔离请用 --isolated-db。" >&2
fi

STARTED_UTC="$($PY -c "from datetime import datetime, timezone; print(datetime.fromtimestamp(int('$START_TS'), tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))")"

# Codex P0-3：MANIFEST 写**启动锁**的 git_sha；收尾再验 HEAD/脏树未漂移
END_HEAD="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unset)"
if git -C "$ROOT" status --porcelain --untracked-files=no 2>/dev/null | grep -q .; then
  END_TRACKED_DIRTY=1
else
  END_TRACKED_DIRTY=0
fi
GIT_SHA="${LOCKED_GIT_SHA:-$END_HEAD}"
GIT_SHA_SHORT="${LOCKED_GIT_SHA_SHORT:-unset}"
GIT_DIRTY="${LOCKED_GIT_DIRTY:-0}"
GIT_LOCK_DRIFT=0
if [ "$END_HEAD" != "$GIT_SHA" ]; then
  GIT_LOCK_DRIFT=1
fi
if [ "${RING_SOAK_REQUIRE_CLEAN:-0}" = "1" ]; then
  if [ "$GIT_LOCK_DRIFT" = "1" ]; then
    echo "失败关闭：运行期间 HEAD 漂移（locked=${GIT_SHA_SHORT} end=$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unset)）" >&2
    exit 3
  fi
  if [ "$END_TRACKED_DIRTY" = "1" ]; then
    echo "失败关闭：运行期间出现已跟踪脏树（RING_SOAK_REQUIRE_CLEAN=1）" >&2
    git -C "$ROOT" status --porcelain --untracked-files=no | head -20 >&2
    exit 3
  fi
  # 发布候选：脏树位必须反映启动锁（应为 0）；禁止用结束态未跟踪噪声把 dirty 写成 1 后仍 package_ok
  GIT_DIRTY=0
fi

cat > "$OUT/MANIFEST.txt" <<EOF
label=${LABEL}
mode=${MODE}
driver=${DRIVER}
runs_done=${RUNS_DONE}
runs_cap=${MAX_RUNS}
hours=${HOURS:-}
until_utc=${UNTIL_UTC:-}
deadline_utc=${DEADLINE_UTC}
started_utc=${STARTED_UTC}
elapsed_seconds=${ELAPSED}
pass=${PASS}
fail=${FAIL}
target=${TARGET}
temporal=${RING_TEMPORAL_TARGET}
model=${RING_LOCAL_QWEN_MODEL:-unset}
chat_base=${RING_LOCAL_QWEN_BASE:-unset}
allow_cloud=${RING_TEST_ALLOW_CLOUD:-unset}
harness_checkout=${RING_HARNESS_CHECKOUT}
git_sha=${GIT_SHA}
git_sha_short=${GIT_SHA_SHORT}
git_dirty=${GIT_DIRTY}
git_lock_drift=${GIT_LOCK_DRIFT}
git_end_sha=${END_HEAD}
git_end_tracked_dirty=${END_TRACKED_DIRTY}
${DB_META}
DB_IS_SHARED_DEV=${DB_IS_SHARED_DEV}
isolated_db=${ISOLATED_DB}
isolated_db_name=${ISO_DB_NAME:-<none>}
db_name_actual=${DB_NAME_ACTUAL}
db_isolation_note=${DB_ISOLATION_NOTE}
caveat=运营记录，非业务裁决；Goal DONE 只由 Kernel 在固定 VerificationProfile 与最终屏障上判定
EOF

# ---- 独立库收尾 ----
# 成功 ⇒ 删除（不攒库）；**失败 ⇒ 保留**并打印查证命令。
# 丢弃失败现场会让「可归因」重新变成不可能 —— 而失败现场正是最需要的东西。
if [ -n "$ISO_DB_NAME" ]; then
  if [ "$FAIL" -eq 0 ] && [ "$KEEP_DB" = 0 ]; then
    "$PY" - "${RING_DATABASE_URL%%/$ISO_DB_NAME}" "$ISO_DB_NAME" <<'PYEOF' >/dev/null 2>&1 || true
import sys, psycopg
base, name = sys.argv[1], sys.argv[2]
url = base.replace("postgresql+psycopg://", "postgresql://") + "/postgres"
with psycopg.connect(url, autocommit=True) as conn:
    conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
PYEOF
    echo "独立库 $ISO_DB_NAME 已删除（本轮全过）"
  else
    echo "⚠ 独立库 $ISO_DB_NAME **保留**（供取证）："
    # 不复述含密码的连接串：控制台/CI 日志与 MANIFEST 同等敏感。
    # 需要连接时用上面的 db_host/db_port/db_name 自行拼；口令请从 .runtime 取。
    echo "   查证：psql -h <db_host> -p <db_port> -U ring -d ${ISO_DB_NAME}（口令见 .runtime/postgres.env）"
    echo "   删除：DROP DATABASE \"$ISO_DB_NAME\" WITH (FORCE);"
  fi
fi

echo
echo "运行记录：$OUT/MANIFEST.txt"
echo "完成（UTC）：$(date -u +%FT%TZ)"

# 退出码语义：任一轮失败 ⇒ 1；指标采集失败 ⇒ 1；账本/故障样本闸（Codex P0-1/P1-4）⇒ 3
if [ "$FAIL" -gt 0 ]; then exit 1; fi
if [ "${RC_METRICS:-0}" != 0 ]; then exit 1; fi
if [ "${RC_LEDGER:-0}" != 0 ]; then exit "$RC_LEDGER"; fi
exit 0
