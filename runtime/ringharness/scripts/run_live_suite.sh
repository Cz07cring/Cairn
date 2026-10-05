#!/usr/bin/env bash
# 真实模型 live 任务套件执行器 —— 收敛路线 #4 的「连跑 N 个**不同**真实任务」。
#
# 与 run_convergence_soak.sh 的分工：
#   · soak        —— 同一个真实业务链反复跑，量**稳定性/失败率**
#   · 本脚本       —— 跑**一组异构** live 任务，量**覆盖面**（不同任务各自通不通）
#   两者互补：重复同一任务会把「任务本身的缺陷」误读成「整体成功率」。
#
# 用法：
#   bash scripts/run_live_suite.sh                 # 跑一轮全部 live 任务
#   bash scripts/run_live_suite.sh --repeats 3      # 连跑 3 轮（看每任务抖动）
#   bash scripts/run_live_suite.sh --list           # 只列出发现的 live 任务
#   bash scripts/run_live_suite.sh --require-live    # CI 用：任何 SKIP 直接判**失败**
#
# 设计原则（与仓库不变量一致）：
#   1. **skip ≠ pass**：live 任务缺 chat env 时 vitest 会**静默 skip**；
#      本脚本把 skip **单列并告警**，绝不并入「通过」（防「假绿」）。
#   2. **失败关闭**：缺 chat env / node_modules 直接报错退出，不伪装成绿。
#   3. **不宣称 DONE**：产物是运营记录；Goal 是否 DONE 只由 Kernel 判定。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

REPEATS=1
LIST_ONLY=0
LABEL="live-suite"
REQUIRE_LIVE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --repeats) REPEATS="$2"; shift 2 ;;
    --list) LIST_ONLY=1; shift ;;
    --require-live) REQUIRE_LIVE=1; shift ;;   # 缺 live 前置 ⇒ 失败而非跳过（对齐 RING_CI_LIVE_REQUIRED）
    --label) LABEL="$2"; shift 2 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

case "$REPEATS" in
  ''|*[!0-9]*) echo "失败关闭：--repeats 须为正整数，实得「${REPEATS}」" >&2; exit 2 ;;
esac
[ "$REPEATS" -ge 1 ] || { echo "失败关闭：--repeats 须 ≥1" >&2; exit 2; }

# ---- 发现 live 任务 ---------------------------------------------------------
TS_TESTS=()
while IFS= read -r f; do
  [ -n "$f" ] && TS_TESTS+=("${f#"$ROOT"/}")
done < <(find apps -name '*.live.test.ts' -not -path '*/node_modules/*' 2>/dev/null | sort)

PY_TESTS=()
if [ -f tests/e2e/test_e2e4_official_loop_to_goal_done.py ]; then
  PY_TESTS+=("tests/e2e/test_e2e4_official_loop_to_goal_done.py")
fi

echo "=== 发现的 live 任务 ==="
for t in "${TS_TESTS[@]}"; do echo "  [ts] $t"; done
for t in "${PY_TESTS[@]}"; do echo "  [py] $t"; done
echo "  合计: ${#TS_TESTS[@]} 个 vitest + ${#PY_TESTS[@]} 个 pytest"
echo

if [ "$LIST_ONLY" = 1 ]; then exit 0; fi

# ---- 派生 RING_TEST_*（与 scripts/test_local.py 同源）-------------------------
# 为什么需要：pytest 侧 live 用例（唯一走完整链到 **Goal DONE** 的那个）要求
# `RING_TEST_DATABASE_URL` / `RING_TEST_S3_*`，而这四个键**不在** `.runtime/*.env` 里 ——
# 它们由 `scripts/test_local.py` 在运行时按「容器实际端口 + .env 里的口令」拼出。
# 不派生 ⇒ 该用例静默 skip ⇒ 变得像「没有这个任务」（本会话已因同类误解更正过一次）。
# 只在未显式设置时派生（显式 export 优先，与仓库口径一致）。
# Node 的 fetch（undici）**不认** http_proxy/https_proxy —— 而本机到远程模型端点
# 只经 127.0.0.1:7890 可达（实测：直连 12s 超时 HTTP=000；走代理 0.07s HTTP=401）。
# 后果：所有 Node 侧 live 用例报 `ConnectTimeoutError`（看起来像业务失败），
# 实测 `NODE_USE_ENV_PROXY=1` 后同一用例 **1727ms 通过**。
# 故在此显式打开（Node ≥24 支持；未设代理时该开关是空操作）。
export NODE_USE_ENV_PROXY="${NODE_USE_ENV_PROXY:-1}"

_derive_test_env() {
  local pg_port s3_port
  pg_port="$(docker port ringharness-development-pg 5432 2>/dev/null | sed 's/.*://' | head -1)"
  s3_port="$(docker port ringharness-development-s3 9000 2>/dev/null | sed 's/.*://' | head -1)"
  [ -n "$pg_port" ] && [ -n "$s3_port" ] || return 0   # 容器没起就不派生（下面会 skip 并如实标注）

  # 读 .env 的值（不打印）
  local pg_user pg_pw pg_db mu mp
  pg_user="$(sed -n 's/^POSTGRES_USER=//p' .runtime/postgres.env 2>/dev/null | head -1)"
  pg_pw="$(sed -n 's/^POSTGRES_PASSWORD=//p' .runtime/postgres.env 2>/dev/null | head -1)"
  pg_db="$(sed -n 's/^POSTGRES_DB=//p' .runtime/postgres.env 2>/dev/null | head -1)"
  mu="$(sed -n 's/^MINIO_ROOT_USER=//p' .runtime/minio.env 2>/dev/null | head -1)"
  mp="$(sed -n 's/^MINIO_ROOT_PASSWORD=//p' .runtime/minio.env 2>/dev/null | head -1)"
  [ -n "$pg_user" ] && [ -n "$pg_pw" ] || return 0

  local url="postgresql+psycopg://${pg_user}:${pg_pw}@127.0.0.1:${pg_port}/${pg_db}"
  export RING_DATABASE_URL="${RING_DATABASE_URL:-$url}"
  export RING_TEST_DATABASE_URL="${RING_TEST_DATABASE_URL:-$url}"
  export RING_TEST_S3_ENDPOINT="${RING_TEST_S3_ENDPOINT:-http://127.0.0.1:${s3_port}}"
  [ -n "$mu" ] && export RING_TEST_S3_ACCESS_KEY="${RING_TEST_S3_ACCESS_KEY:-$mu}"
  [ -n "$mp" ] && export RING_TEST_S3_SECRET_KEY="${RING_TEST_S3_SECRET_KEY:-$mp}"
  # 与 soak 一致：本机凭据不得被 dotenv 静默覆盖
  export no_proxy="${no_proxy:-*}" NO_PROXY="${NO_PROXY:-*}"
}
_derive_test_env

# ---- 前置检查（失败关闭）----------------------------------------------------
if [ "${#TS_TESTS[@]}" -eq 0 ]; then
  echo "失败关闭：未发现任何 *.live.test.ts —— 路径或检出不对" >&2; exit 2
fi
[ -x node_modules/.bin/vitest ] || [ -d node_modules ] || {
  echo "失败关闭：node_modules 缺失（vitest 不可用）。隔离树请先软链共享树的 node_modules。" >&2; exit 2; }

# chat 环境取自 .runtime/chat.env（与 vitest live 用例同一来源）
CHAT_ENV=".runtime/chat.env"
[ -f "$CHAT_ENV" ] || CHAT_ENV=".runtime/qwen.env"
if [ ! -f "$CHAT_ENV" ]; then
  echo "失败关闭：缺 .runtime/chat.env 与 qwen.env —— live 用例会**静默 skip**，那不是证据。" >&2; exit 2
fi

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$ROOT/.runtime/live-suite-$LABEL-$TS"
mkdir -p "$OUT"
SUMMARY="$OUT/summary.tsv"
printf 'round\ttask\tkind\texit\tpassed\tfailed\tskipped\tseconds\tnote\n' > "$SUMMARY"

START_TS="$(date +%s)"
echo "========================= 执行 ========================="
echo "轮次=${REPEATS}  输出=${OUT#$ROOT/}"

ROUND=0
while [ "$ROUND" -lt "$REPEATS" ]; do
  ROUND=$((ROUND + 1))
  echo "------------------------- 第 ${ROUND}/${REPEATS} 轮 -------------------------"

  for t in "${TS_TESTS[@]}"; do
    REL="${t#apps/runner/}"
    LOG="$OUT/r${ROUND}-$(echo "$t" | tr '/' '_').log"
    T0="$(date +%s)"
    ( cd "$ROOT" && pnpm --filter @ring/runner exec vitest run "$REL" ) > "$LOG" 2>&1
    RC=$?
    T1="$(date +%s)"
    P="$(grep -oE 'Tests +[0-9]+ passed' "$LOG" | grep -oE '[0-9]+' | head -1)"; P="${P:-0}"
    F="$(grep -oE '[0-9]+ failed' "$LOG" | grep -oE '^[0-9]+' | head -1)"; F="${F:-0}"
    S="$(grep -oE '[0-9]+ skipped' "$LOG" | grep -oE '^[0-9]+' | head -1)"; S="${S:-0}"
    NOTE=""
    # skip 检测：live 用例靠 describe.skipIf(!chat)，缺 env 时会 skip 而非红
    if [ "$P" -eq 0 ] && [ "$S" -gt 0 ]; then
      NOTE="⚠ SKIP（缺 chat env 或模型）—— 不计入通过"
      # CI 模式：跳过不是证据，直接判失败（对齐 Python 侧 RING_CI_LIVE_REQUIRED）
      if [ "$REQUIRE_LIVE" = 1 ]; then
        F=$((F + 1)); RC=1
        NOTE="❌ SKIP 但 --require-live：缺 live 前置必须失败（不得 skip 冒充绿）"
      fi
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$ROUND" "$t" ts "$RC" "$P" "$F" "$S" "$((T1 - T0))" "$NOTE" >> "$SUMMARY"
    printf '  [ts] %-70s exit=%s pass=%s fail=%s skip=%s %ss %s\n' \
      "$t" "$RC" "$P" "$F" "$S" "$((T1 - T0))" "$NOTE"
  done

  for t in "${PY_TESTS[@]}"; do
    LOG="$OUT/r${ROUND}-$(echo "$t" | tr '/' '_').log"
    T0="$(date +%s)"
    # pytest 侧 live 由 RING_E2E4_LIVE_CHAT=1 开启；缺 PG/S3 会 skip（单列）
    ( cd "$ROOT" && RING_E2E4_LIVE_CHAT="${RING_E2E4_LIVE_CHAT:-1}" \
        uv run pytest "$t" -q ) > "$LOG" 2>&1
    RC=$?
    T1="$(date +%s)"
    P="$(grep -oE '[0-9]+ passed' "$LOG" | grep -oE '^[0-9]+' | head -1)"; P="${P:-0}"
    F="$(grep -oE '[0-9]+ failed' "$LOG" | grep -oE '^[0-9]+' | head -1)"; F="${F:-0}"
    S="$(grep -oE '[0-9]+ skipped' "$LOG" | grep -oE '^[0-9]+' | head -1)"; S="${S:-0}"
    NOTE=""
    if [ "$P" -eq 0 ] && [ "$S" -gt 0 ]; then
      NOTE="⚠ SKIP（缺 RING_TEST_* 环境）—— 不计入通过"
      if [ "$REQUIRE_LIVE" = 1 ]; then
        F=$((F + 1)); RC=1
        NOTE="❌ SKIP 但 --require-live：缺 live 前置必须失败（不得 skip 冒充绿）"
      fi
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$ROUND" "$t" py "$RC" "$P" "$F" "$S" "$((T1 - T0))" "$NOTE" >> "$SUMMARY"
    printf '  [py] %-70s exit=%s pass=%s fail=%s skip=%s %ss %s\n' \
      "$t" "$RC" "$P" "$F" "$S" "$((T1 - T0))" "$NOTE"
  done
done
ELAPSED="$(( $(date +%s) - START_TS ))"

# ---- 汇总（每任务跨轮聚合；skip 单列，绝不并入通过）--------------------------
echo
echo "======================= 逐任务汇总 ======================="
TASKS="$(awk -F'\t' 'NR>1 {print $2}' "$SUMMARY" | sort -u)"
TOTAL_PASS=0; TOTAL_FAIL=0; TOTAL_SKIP=0; TASK_ALLGREEN=0; TASK_ANYRED=0; TASK_ALLSKIP=0
while IFS= read -r t; do
  [ -n "$t" ] || continue
  P="$(awk -F'\t' -v t="$t" 'NR>1 && $2==t {p+=$5} END{print p+0}' "$SUMMARY")"
  F="$(awk -F'\t' -v t="$t" 'NR>1 && $2==t {f+=$6} END{print f+0}' "$SUMMARY")"
  S="$(awk -F'\t' -v t="$t" 'NR>1 && $2==t {s+=$7} END{print s+0}' "$SUMMARY")"
  R="$(awk -F'\t' -v t="$t" 'NR>1 && $2==t {n++} END{print n+0}' "$SUMMARY")"
  TOTAL_PASS=$((TOTAL_PASS + P)); TOTAL_FAIL=$((TOTAL_FAIL + F)); TOTAL_SKIP=$((TOTAL_SKIP + S))
  if [ "$S" -gt 0 ] && [ "$P" -eq 0 ] && [ "$F" -eq 0 ]; then
    MARK="⚠ 全 SKIP（无证据）"; TASK_ALLSKIP=$((TASK_ALLSKIP + 1))
  elif [ "$F" -eq 0 ]; then
    MARK="✅"; TASK_ALLGREEN=$((TASK_ALLGREEN + 1))
  else
    MARK="❌"; TASK_ANYRED=$((TASK_ANYRED + 1))
  fi
  printf '  %s 通过 %d / 失败 %d / 跳过 %d（%s 轮）  %s\n' "$MARK" "$P" "$F" "$S" "$R" "$t"
done <<< "$TASKS"

NTASKS="$(awk -F'\t' 'NR>1 {print $2}' "$SUMMARY" | sort -u | wc -l | tr -d ' ')"
echo "---------------------------------------------------------"
echo "任务数 ${NTASKS}  轮次 ${REPEATS}  总用时 ${ELAPSED}s"
echo "断言通过 ${TOTAL_PASS} / 失败 ${TOTAL_FAIL} / 跳过 ${TOTAL_SKIP}"
echo "任务级：全绿 ${TASK_ALLGREEN} / 有红 ${TASK_ANYRED} / 全 SKIP ${TASK_ALLSKIP}"

if [ "$TOTAL_SKIP" -gt 0 ]; then
  echo "⚠ 有 ${TOTAL_SKIP} 个用例被 SKIP —— **skip 不是通过**，不能计入成功率。"
  if [ "$REQUIRE_LIVE" = 1 ]; then
    echo "❌ --require-live 已开启：上述 SKIP 已计入失败（CI 语义）。"
  fi
fi
echo "⚠ 运营观测，非业务裁决；Goal DONE 只由 Kernel 在固定 VerificationProfile 与最终屏障上判定"

cat > "$OUT/MANIFEST.txt" <<EOF
label=${LABEL}
repeats=${REPEATS}
require_live=${REQUIRE_LIVE}
test_env_derived=$([ -n "${RING_TEST_DATABASE_URL:-}" ] && echo 1 || echo 0)
test_s3_derived=$([ -n "${RING_TEST_S3_ENDPOINT:-}" ] && echo 1 || echo 0)
tasks=${NTASKS}
pass=${TOTAL_PASS}
fail=${TOTAL_FAIL}
skip=${TOTAL_SKIP}
all_green_tasks=${TASK_ALLGREEN}
any_red_tasks=${TASK_ANYRED}
all_skip_tasks=${TASK_ALLSKIP}
elapsed_seconds=${ELAPSED}
chat_env_source=${CHAT_ENV}
model=${RING_LOCAL_QWEN_MODEL:-unset}
chat_base=${RING_LOCAL_QWEN_BASE:-unset}
harness_checkout=${RING_HARNESS_CHECKOUT:-unset}
caveat=运营记录，非业务裁决；skip 不计入通过
EOF
echo "运行记录：$OUT/MANIFEST.txt"

# 退出码：有红 ⇒ 1；全绿但有 skip ⇒ 0（已显式告警）；无任务 ⇒ 2
if [ "$TOTAL_FAIL" -gt 0 ]; then exit 1; fi
exit 0
