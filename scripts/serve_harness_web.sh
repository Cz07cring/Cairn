#!/usr/bin/env bash
# 启动固定 pin 的 DeepSeek Harness Web（执行面；对话流由其托管）。
# Ringharness 驾驶舱只深链打开，不维护上游 UI。
#
# 用法：
#   export RING_HARNESS_CHECKOUT="$PWD/.runtime/harness-pins/<sha>"
#   bash scripts/serve_harness_web.sh
#   bash scripts/serve_harness_web.sh --port 3080
#
# 打开终端打印的**带 token** URL；裸 :3080 会 401。
# ≠ Goal DONE；工具副作用仍须经 Runner→Broker→Kernel。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT=3080
while [ "$#" -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:?}"; shift 2 ;;
    -h|--help)
      sed -n '2,14p' "$0"
      exit 0
      ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

PIN="${RING_HARNESS_PIN:-c291e7961a515f6d7af9304e7fd1d257929aef26}"
CHECKOUT="${RING_HARNESS_CHECKOUT:-$ROOT/.runtime/harness-pins/$PIN}"
[ -d "$CHECKOUT" ] || {
  echo "失败关闭：缺少 Harness checkout：$CHECKOUT" >&2
  echo "  先跑：bash scripts/pull_harness_pin.sh $PIN --build" >&2
  exit 2
}

echo "Harness Web 执行面"
echo "  pin=$PIN"
echo "  checkout=$CHECKOUT"
echo "  port=$PORT"
echo "  驾驶舱深链：VITE_RING_HARNESS_WEB_URL=<dsh 打印的带 token URL>"
echo "  口径：对话完成 ≠ Goal DONE；不维护上游 UI"
echo

cd "$CHECKOUT"
if [ -x "$CHECKOUT/node_modules/.bin/dsh" ]; then
  exec "$CHECKOUT/node_modules/.bin/dsh" --profile web --port "$PORT" --no-open
fi
if command -v pnpm >/dev/null 2>&1 && [ -f "$CHECKOUT/package.json" ]; then
  # 上游源码 checkout 通过根 package.json 的 `dsh` script 暴露 CLI，
  # 并不保证 node_modules/.bin 下存在已发布包才有的 dsh shim。
  exec pnpm dsh --profile web --port "$PORT" --no-open
fi
exec npx --yes "@deepseek-ai/dsh" --profile web --port "$PORT" --no-open
