#!/usr/bin/env bash
# 在钉扎 DeepSeek Harness checkout 内构建官方 AgentLoop peers（host lib）。
# 用法：从 ringharness 根目录：
#   export RING_HARNESS_CHECKOUT="$PWD/.runtime/deepseek-harness"
#   bash scripts/build_harness_agent_loop.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHECKOUT="${RING_HARNESS_CHECKOUT:-$ROOT/.runtime/deepseek-harness}"
PIN_FILE="$CHECKOUT/.ringharness-pinned-commit"
EXPECTED_PIN="${RING_HARNESS_PIN:-c291e7961a515f6d7af9304e7fd1d257929aef26}"

if [[ ! -d "$CHECKOUT" ]]; then
  echo "HARNESS_CHECKOUT_MISSING: $CHECKOUT" >&2
  exit 1
fi
if [[ -f "$PIN_FILE" ]]; then
  got="$(tr -d '[:space:]' <"$PIN_FILE")"
  if [[ "$got" != "$EXPECTED_PIN" ]]; then
    echo "HARNESS_PIN_MISMATCH: want $EXPECTED_PIN got $got" >&2
    exit 1
  fi
fi

cd "$CHECKOUT"
pnpm install --frozen-lockfile
pnpm run build:lib:host
test -f packages/core/agent-loop/lib/index.js
echo "OK: AgentLoop lib ready under $CHECKOUT"
