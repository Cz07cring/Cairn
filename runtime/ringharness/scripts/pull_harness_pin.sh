#!/usr/bin/env bash
# 从 GitHub 拉取固定 SHA 到旁路目录 .runtime/harness-pins/<sha>/，可选 build:lib:host。
# 热切换：设置 RING_HARNESS_CHECKOUT / RING_HARNESS_PIN 后由 Runner 调 hotSwapHarnessRuntime。
#
# 用法（仓库根）：
#   bash scripts/pull_harness_pin.sh <40-char-sha> [--build]
#   export RING_HARNESS_CHECKOUT=... RING_HARNESS_PIN=...
#   bash scripts/build_harness_agent_loop.sh   # 若未传 --build
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SHA="${1:-}"
BUILD=0
if [[ "${2:-}" == "--build" ]] || [[ "${1:-}" == "--build" ]]; then
  BUILD=1
fi
if [[ "$SHA" == "--build" ]]; then
  echo "用法: bash scripts/pull_harness_pin.sh <40-char-sha> [--build]" >&2
  exit 1
fi
if [[ ! "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "HARNESS_PIN_INVALID: 需要 40 位小写 hex SHA，got '${SHA:-<empty>}'" >&2
  exit 1
fi

PINS_ROOT="${RING_HARNESS_PINS_ROOT:-$ROOT/.runtime/harness-pins}"
DEST="$PINS_ROOT/$SHA"
REPO_URL="${RING_HARNESS_REPO_URL:-https://github.com/deepseek-ai/deepseek-harness}"
ARCHIVE_URL="${REPO_URL%/}/archive/${SHA}.tar.gz"
PIN_FILE=".ringharness-pinned-commit"

mkdir -p "$PINS_ROOT"
if [[ -f "$DEST/$PIN_FILE" ]] && [[ "$(tr -d '[:space:]' <"$DEST/$PIN_FILE")" == "$SHA" ]]; then
  echo "reused: $DEST"
else
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/ring-harness-pull.XXXXXX")"
  cleanup() { rm -rf "$WORK"; }
  trap cleanup EXIT
  echo "fetch: $ARCHIVE_URL"
  curl -fsSL -o "$WORK/archive.tar.gz" "$ARCHIVE_URL"
  mkdir -p "$WORK/staging"
  tar -xzf "$WORK/archive.tar.gz" -C "$WORK/staging"
  TOP="$(find "$WORK/staging" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  if [[ -z "$TOP" ]]; then
    echo "HARNESS_PULL_LAYOUT: 无顶层目录" >&2
    exit 1
  fi
  rm -rf "$DEST"
  mkdir -p "$PINS_ROOT"
  cp -R "$TOP" "$DEST"
  printf '%s\n' "$SHA" >"$DEST/$PIN_FILE"
  echo "pulled: $DEST"
fi

export RING_HARNESS_CHECKOUT="$DEST"
export RING_HARNESS_PIN="$SHA"
if [[ "$BUILD" -eq 1 ]]; then
  bash "$ROOT/scripts/build_harness_agent_loop.sh"
fi

echo "OK: 设置环境后即可热切换："
echo "  export RING_HARNESS_CHECKOUT=$DEST"
echo "  export RING_HARNESS_PIN=$SHA"
echo "  # Runner: hotSwapHarnessRuntime({ checkoutDir, pin })"
