#!/usr/bin/env bash
# Codex P0-3：同 SHA 发布候选证据包（自测必跑；live 可选）。
#
# 用法：
#   bash scripts/package_release_candidate_evidence.sh
#   bash scripts/package_release_candidate_evidence.sh --live-runs 5 --label rc364
#   bash scripts/package_release_candidate_evidence.sh --live-runs 10 --label rc-qwen --chat-profile qwen
#
# --chat-profile：
#   deepseek（默认）清进程 RING_LOCAL_QWEN_*，让 .runtime/chat.env 成套
#   qwen               加载 .runtime/qwen.env 并保留进程覆盖（压过 chat.env）
#   process            保留调用方已 export 的 RING_LOCAL_QWEN_*
#
# 失败关闭：
#   - 运营闸门自测失败
#   - 有**已跟踪**未提交改动（禁止脏树拼候选；未跟踪噪声除外）
#   - 运行期间 HEAD 漂移（结束 SHA ≠ 启动锁定 SHA）
#   - --live-runs>0 时 soak 非零，或 MANIFEST 与启动锁定 SHA / 干净树不符
#
# ≠ Goal DONE；≠ 100h；不替代仓库 secrets 的 live CI。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LIVE_RUNS=0
LABEL="rc-pack"
PACKAGE_OUT=""
KEEP_ON_FAIL=0
# chat 来源：deepseek=清进程侧让 chat.env 成套；qwen=用 .runtime/qwen.env 覆盖进程并保留；
# process=保留调用方已 export 的 RING_LOCAL_QWEN_*（对照实验）
CHAT_PROFILE="deepseek"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --live-runs) LIVE_RUNS="${2:-0}"; shift 2 ;;
    --label) LABEL="${2:-rc-pack}"; shift 2 ;;
    --out) PACKAGE_OUT="${2:-}"; shift 2 ;;
    --keep-on-fail) KEEP_ON_FAIL=1; shift ;;
    --chat-profile)
      CHAT_PROFILE="${2:-deepseek}"
      shift 2
      ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

case "$CHAT_PROFILE" in
  deepseek|qwen|process) ;;
  *)
    echo "失败关闭：--chat-profile 须为 deepseek|qwen|process（实得 ${CHAT_PROFILE}）" >&2
    exit 2
    ;;
esac

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PACKAGE_OUT="${PACKAGE_OUT:-$ROOT/.runtime/release-candidate-${LABEL}-${STAMP}}"
mkdir -p "$PACKAGE_OUT"

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="uv run python"

# 启动时锁定 SHA：结束时不得用「当时 HEAD」重解析冒充同候选（rc372 假绿根因）
LOCKED_SHA="$(git rev-parse HEAD)"
LOCKED_SHORT="$(git rev-parse --short HEAD)"
# 仅已跟踪改动算脏；未跟踪（lefthook 草稿等）不阻断启动，但 soak 侧 REQUIRE_CLEAN 用 tracked-only
TRACKED_DIRTY="$(git status --porcelain --untracked-files=no | wc -l | tr -d ' ')"
UNTRACKED="$(git status --porcelain -u | grep -c '^??' || true)"

{
  echo "label=$LABEL"
  echo "created_utc=$STAMP"
  echo "git_sha=$LOCKED_SHA"
  echo "git_sha_short=$LOCKED_SHORT"
  echo "locked_sha=$LOCKED_SHA"
  echo "tracked_dirty=$TRACKED_DIRTY"
  echo "untracked_count=$UNTRACKED"
  echo "live_runs=$LIVE_RUNS"
  echo "chat_profile=$CHAT_PROFILE"
  echo "marks_goal_done=false"
  echo "note=Codex P0-3 同 SHA 证据包；启动锁版；≠ Goal DONE；≠ 100h"
} >"$PACKAGE_OUT/INDEX.txt"

if [ "$TRACKED_DIRTY" != 0 ]; then
  echo "失败关闭：存在已跟踪未提交改动（Codex P0-3 禁止脏树拼发布候选）" >&2
  git status --porcelain --untracked-files=no | head -20 >&2
  exit 2
fi

# 全角括号不得紧贴 $VAR（bash 会把 PACKAGE_OUT（ 当成变量名；set -u 下假崩）
echo "=== 1/3 运营闸门自测 → ${PACKAGE_OUT} (locked=${LOCKED_SHORT}) ==="
bash scripts/check_release_candidate_gates.sh | tee "$PACKAGE_OUT/gates.log"

if [ "$LIVE_RUNS" -le 0 ]; then
  echo "跳过 live（未传 --live-runs）；INDEX=$PACKAGE_OUT/INDEX.txt"
  echo "package_ok=1" >>"$PACKAGE_OUT/INDEX.txt"
  exit 0
fi

[ -n "${RING_HARNESS_CHECKOUT:-}" ] || {
  echo "失败关闭：live 需要 RING_HARNESS_CHECKOUT" >&2
  exit 2
}

echo "=== 2/3 live soak --runs ${LIVE_RUNS} (isolated-db, lock=$LOCKED_SHORT, chat=${CHAT_PROFILE}) ==="
case "$CHAT_PROFILE" in
  deepseek)
    # 清进程侧 RING_LOCAL_QWEN_*，让 .runtime/chat.env 成套生效（避免 :8001+deepseek 错配）
    unset RING_LOCAL_QWEN_BASE RING_LOCAL_QWEN_MODEL RING_LOCAL_QWEN_API_KEY \
      RING_LOCAL_QWEN_TIMEOUT RING_CLOUD_MODE RING_CHAT_CLOUD_PROVIDER_REF \
      RING_MODEL_PROVIDER_REF RING_TEST_ALLOW_CLOUD || true
    ;;
  qwen)
    # 本机 Qwen：加载 qwen.env 并**保留**到 soak（soak 会先读 chat.env 再被进程 export 压回）
    [ -f "$ROOT/.runtime/qwen.env" ] || {
      echo "失败关闭：--chat-profile qwen 需要 .runtime/qwen.env" >&2
      exit 2
    }
    set -a
    # shellcheck disable=SC1091
    . "$ROOT/.runtime/qwen.env"
    set +a
    export RING_CLOUD_MODE="${RING_CLOUD_MODE:-DENY}"
    unset RING_TEST_ALLOW_CLOUD RING_CHAT_CLOUD_PROVIDER_REF RING_MODEL_PROVIDER_REF || true
    if [ -z "${RING_LOCAL_QWEN_MODEL:-}" ]; then
      echo "失败关闭：qwen.env 未设 RING_LOCAL_QWEN_MODEL（请写入本机 /v1/models 中的 id）" >&2
      exit 2
    fi
    echo "  chat-profile=qwen base=${RING_LOCAL_QWEN_BASE:-?} model=${RING_LOCAL_QWEN_MODEL}"
    ;;
  process)
    # 调用方已 export；只做非空校验
    if [ -z "${RING_LOCAL_QWEN_BASE:-}" ] || [ -z "${RING_LOCAL_QWEN_MODEL:-}" ] || [ -z "${RING_LOCAL_QWEN_API_KEY:-}" ]; then
      echo "失败关闭：--chat-profile process 须已 export RING_LOCAL_QWEN_BASE/MODEL/API_KEY" >&2
      exit 2
    fi
    echo "  chat-profile=process base=${RING_LOCAL_QWEN_BASE} model=${RING_LOCAL_QWEN_MODEL}"
    ;;
esac

SOAK_ROOT="${PACKAGE_OUT}/soak-root"
mkdir -p "${SOAK_ROOT}"
# 运行期锁：MANIFEST 必须写启动 SHA；HEAD 漂移或脏树失败关闭
export RING_SOAK_LOCK_SHA="${LOCKED_SHA}"
export RING_SOAK_REQUIRE_CLEAN=1
SOAK_RC=0
OUT="${SOAK_ROOT}" bash scripts/run_convergence_soak.sh \
  --runs "${LIVE_RUNS}" --label "${LABEL}" --isolated-db --out "${SOAK_ROOT}" \
  | tee "${PACKAGE_OUT}/soak-console.log" || SOAK_RC=$?

MANIFEST="${SOAK_ROOT}/MANIFEST.txt"
# 禁止回退到任意 .runtime/MANIFEST（会误捡他次 soak，跨 SHA 假绿）
if [ ! -f "${MANIFEST}" ]; then
  echo "失败关闭：未找到本包 soak MANIFEST.txt（须为 ${SOAK_ROOT}/MANIFEST.txt）" >&2
  exit 3
fi
cp -f "${MANIFEST}" "${PACKAGE_OUT}/MANIFEST.txt"
echo "soak_manifest=${MANIFEST}" >>"${PACKAGE_OUT}/INDEX.txt"
echo "soak_rc=${SOAK_RC}" >>"${PACKAGE_OUT}/INDEX.txt"

END_SHA="$(git rev-parse HEAD)"
END_TRACKED_DIRTY="$(git status --porcelain --untracked-files=no | wc -l | tr -d ' ')"
echo "end_git_sha=${END_SHA}" >>"${PACKAGE_OUT}/INDEX.txt"
echo "end_tracked_dirty=${END_TRACKED_DIRTY}" >>"${PACKAGE_OUT}/INDEX.txt"

if [ "${END_SHA}" != "${LOCKED_SHA}" ]; then
  echo "失败关闭：运行期间 HEAD 漂移（locked=${LOCKED_SHORT} end=$(git rev-parse --short HEAD)）" >&2
  echo "package_ok=0" >>"${PACKAGE_OUT}/INDEX.txt"
  exit 3
fi
if [ "${END_TRACKED_DIRTY}" != 0 ]; then
  echo "失败关闭：运行期间出现已跟踪脏树（Codex P0-3）" >&2
  git status --porcelain --untracked-files=no | head -20 >&2
  echo "package_ok=0" >>"${PACKAGE_OUT}/INDEX.txt"
  exit 3
fi

echo "=== 3/3 same-SHA lock (expect=启动锁定 SHA + require-clean) ==="
# 关键：不得 --expect-sha HEAD（结束时重解析会掩盖启动锁漂移）
"${PY}" scripts/assert_release_candidate_lock.py \
  --expect-sha "${LOCKED_SHA}" --require-clean \
  "${PACKAGE_OUT}/MANIFEST.txt" \
  | tee "${PACKAGE_OUT}/lock.log"

if [ "${SOAK_RC}" -ne 0 ]; then
  echo "package_ok=0" >>"${PACKAGE_OUT}/INDEX.txt"
  echo "live soak 非零（rc=${SOAK_RC}）；证据已落盘并锁版，但候选**未**放行" >&2
  if [ "${KEEP_ON_FAIL}" = 1 ]; then
    echo "（--keep-on-fail：保留证据包 ${PACKAGE_OUT}）"
    exit 1
  fi
  exit 1
fi
echo "package_ok=1" >>"${PACKAGE_OUT}/INDEX.txt"
echo "发布候选证据包完成：${PACKAGE_OUT}（locked=${LOCKED_SHORT}；≠ Goal DONE / ≠ 100h）"
