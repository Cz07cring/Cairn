#!/usr/bin/env bash
# 批次收工自检 —— 一次跑齐 CI 的全部门禁，避免「本地绿、CI 红」。
#
# 为什么要有它：本仓 CI 有五道各自独立的门，任一道缺失都会让推送变红，
# 而每道门的失败都很像「莫名其妙」：
#   ① ruff（老实但易漏跑）
#   ② pytest tests/unit（真跑）
#   ③ 契约再生成幂等（新增/改动路由后忘记重生成 ⇒ CI 报「生成代码漂移」）
#   ④ v0.5 冻结校验（改到 doc/05 等冻结文档后未重算冻结包 ⇒ CI 报冻结失败）
#   ⑤ pnpm test && pnpm build（**vitest 不做类型检查**，只有 build 才暴露类型错）
# 另有一类**只在本地出现**的假红，本脚本一并处理：隔离 worktree 缺 `.runtime`
# （provider 静默 unset ⇒ live 用例静默 skip）或缺 `node_modules`（tsx 退出 254 ⇒ 假红）。
#
# 用法（在**任意共写 worktree** 内执行）：
#   bash scripts/check_batch.sh                    # 全套
#   bash scripts/check_batch.sh --shared /path/to/shared-repo   # 指定共享树（取 .runtime/deps）
#   bash scripts/check_batch.sh --skip-frontend    # 只跑后端（纯 Python 批次省时间）
#   bash scripts/check_batch.sh --fix              # 自动执行「重生成契约」与「重算冻结」
#   bash scripts/check_batch.sh --live             # 额外加跑真实模型 live 套件（--require-live，烧配额）
#
# 退出码：0 全绿；非零 = 有门未过（不掩盖）。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SHARED=""
SKIP_FRONTEND=0
FIX=0
LIVE=0
EXTRA_TESTS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --shared) SHARED="$2"; shift 2 ;;
    --skip-frontend) SKIP_FRONTEND=1; shift ;;
    --fix) FIX=1; shift ;;
    --live) LIVE=1; shift ;;   # 可选：加跑真实模型 live 套件（烧配额 ⇒ 不默认）
    --tests) EXTRA_TESTS+=("$2"); shift 2 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

# 自动定位共享树（含 .runtime 的那棵）
if [ -z "$SHARED" ]; then
  for cand in "$HOME/Documents/code/ringharness" "$ROOT/.."; do
    [ -d "$cand/.runtime" ] && [ -f "$cand/.runtime/postgres.env" ] && { SHARED="$cand"; break; }
  done
fi

FAILED=0
step() { printf '\n=== %s ===\n' "$1"; }
ok()   { printf '  ✅ %s\n' "$1"; }
bad()  { printf '  ❌ %s\n' "$1"; FAILED=1; }

# venv 解析顺序：本树 → 共享树 → 报错。
# **不要回退到系统 python3** —— 它缺 boto3/ruff，会把「环境不对」伪装成一堆测试失败。
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ] && [ -n "$SHARED" ] && [ -x "$SHARED/.venv/bin/python" ]; then
  PY="$SHARED/.venv/bin/python"
fi
if [ ! -x "$PY" ] || ! "$PY" -c "import boto3" >/dev/null 2>&1; then
  printf '失败关闭：未找到可用 venv（需含 boto3/ruff）。\n' >&2
  printf '  已试：%s/.venv/bin/python、%s/.venv/bin/python\n' "$ROOT" "${SHARED:-<无共享树>}" >&2
  printf '  修法：uv sync --frozen --python 3.12，或用 --shared 指向已装依赖的树。\n' >&2
  exit 2
fi
echo "python：$PY"

# ---------- 把导入绑定到**本树**（否则可能测到另一个树）----------
# 为什么必须：`.venv` 是主开发树 `uv sync` 出来的，其 site-packages 里有
# `_editable_impl_ring_*.pth`，把各 package 的 src 指回**主开发树**。
# 于是隔离 worktree 里 `import control_api`（以及契约生成）会读到**另一个树**的代码：
#   · 单测假红：`EXPECTED_DB_HEAD` 取到主树的值、alembic head 取到本树的值 ⇒ 必然不符；
#   · 契约假漂移：openapi 由主树的 app 生成 ⇒ 报 4 个文件「漂移」。
# 实测（2026-09-13，worktree）：无 PYTHONPATH 时该用例 FAILED 且报 4 文件漂移；
# 显式指向本树后 2 passed、零漂移。`inspect.getfile(control_api.app)` 证实两条路径不同。
# 故显式把本树包路径置于最前 —— 保证「测的就是这棵树」，同时消除假红与**假绿**。
_TREE_PYPATH="$(ls -d "$ROOT"/packages/*/src 2>/dev/null | tr '\n' ':')$ROOT/apps/control/src:$ROOT"
export PYTHONPATH="$_TREE_PYPATH${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONNOUSERSITE=1
# ---------- 0) 环境：隔离树补 .runtime / node_modules（否则假红或静默 skip）----------
if [ -n "$SHARED" ] && [ "$SHARED" != "$ROOT" ]; then
  step "0) 环境等价（链接共享 $SHARED 的 .runtime 与依赖）"
  for d in .runtime node_modules apps/web/node_modules apps/runner/node_modules packages/api-client/node_modules; do
    if [ -e "$SHARED/$d" ] && [ ! -e "$ROOT/$d" ]; then
      mkdir -p "$(dirname "$ROOT/$d")"; ln -sfn "$SHARED/$d" "$ROOT/$d"; echo "  链接 $d"
    fi
  done
  # 提示：node_modules 符号链接不被 .gitignore 的 `node_modules/` 规则匹配，
  # 常驻树应改用 `pnpm install --frozen-lockfile`（本脚本只对临时树做链接）。
  ok "环境已就绪"
fi

# 环境变量：优先共享树 .runtime（进程显式 export 优先级更高，故先 set -a 再覆盖）
if [ -n "$SHARED" ] && [ -d "$SHARED/.runtime" ]; then
  for f in postgres.env minio.env chat.env; do
    [ -f "$SHARED/.runtime/$f" ] && { set -a; . "$SHARED/.runtime/$f"; set +a; }
  done
  PG_PORT="$(docker port ringharness-development-pg 5432 2>/dev/null | sed 's/.*://' || true)"
  PW="${POSTGRES_PASSWORD:-ring}"; MK="${MINIO_ROOT_PASSWORD:-ring-test}"
  export RING_DATABASE_URL="${RING_DATABASE_URL:-postgresql+psycopg://ring:${PW}@127.0.0.1:${PG_PORT:-15432}/ring_test}"
  export RING_TEST_DATABASE_URL="${RING_TEST_DATABASE_URL:-$RING_DATABASE_URL}"
  export RING_TEST_S3_ENDPOINT="${RING_TEST_S3_ENDPOINT:-http://127.0.0.1:51110}"
  export RING_TEST_S3_ACCESS_KEY="${RING_TEST_S3_ACCESS_KEY:-ring-test}"
  export RING_TEST_S3_SECRET_KEY="${RING_TEST_S3_SECRET_KEY:-$MK}"
  export RING_TEMPORAL_TARGET="${RING_TEMPORAL_TARGET:-127.0.0.1:7233}"
fi

echo "仓库：$ROOT"
echo "共享树：${SHARED:-<未找到>}"
echo "HEAD：$(git log --oneline -1 | cat)"

# ---------- 1) ruff ----------
step "1) ruff（CI 同命令）"
if $PY -m ruff check apps/control packages tests scripts migrations 2>&1 | tail -3; then
  ok "ruff"
else
  bad "ruff"
fi

# ---------- 2) 单元测试 ----------
step "2) pytest tests/unit"
UNIT_OUT="$($PY -m pytest tests/unit -q 2>&1 | tail -3)"
echo "$UNIT_OUT"
echo "$UNIT_OUT" | grep -qE "[0-9]+ passed" && ! echo "$UNIT_OUT" | grep -qE "[1-9][0-9]* failed" \
  && ok "单元测试" || bad "单元测试"

# ---------- 2b) Temporal 编排测试 ----------
# **为什么补这一步（真实缺口）**：本脚本原先只跑 tests/unit，而
# `tests/temporal/**`（29 个文件）**完全不在门禁内** —— 它恰恰是编排面
# （workflows/activities/演变门/恢复语义）的主测试目录。
# 实测后果：一个 `NameError: name 'Path' is not defined` 在 tests/temporal 里
# **潜伏了整整一轮无人发现**（单跑该文件才暴露），因为本地门禁从不执行它。
# 失败关闭：真实失败 ⇒ bad；全 skip ⇒ 明确标注「无证据」，不冒充通过。
step "2b) pytest tests/temporal"
TEMPORAL_OUT="$($PY -m pytest tests/temporal -q 2>&1 | tail -3)"
echo "$TEMPORAL_OUT"
if echo "$TEMPORAL_OUT" | grep -qE "[1-9][0-9]* failed"; then
  bad "Temporal 编排测试"
elif echo "$TEMPORAL_OUT" | grep -qE "[0-9]+ passed"; then
  ok "Temporal 编排测试"
elif echo "$TEMPORAL_OUT" | grep -qE "[0-9]+ skipped"; then
  # 全 skip 不是证据：明确标注，不算过（本仓「skip ≠ pass」口径）
  bad "Temporal 编排测试全部 SKIP（无证据；检查 Temporal/依赖是否就绪）"
else
  bad "Temporal 编排测试（无可解析结果）"
fi

# ---------- 3) 契约幂等 ----------
step "3) 契约再生成幂等（防止 generated.ts 漂移）"
if [ "$FIX" = 1 ]; then
  $PY scripts/generate_contracts.py >/dev/null 2>&1 && echo "  已重新生成"
else
  $PY scripts/generate_contracts.py >/dev/null 2>&1
fi
if git diff --quiet -- contracts packages/api-client/src/generated.ts; then
  ok "契约无漂移"
else
  bad "契约有漂移（用 --fix 重生成，或手动跑 scripts/generate_contracts.py）"
  git status --porcelain contracts packages/api-client/src/generated.ts | head -6
fi

# ---------- 4) 冻结校验 ----------
step "4) v0.5 冻结校验"
FREEZE="$(python3 doc/tools/check_spec.py --verify-freeze 2>&1)"
if echo "$FREEZE" | grep -q '"status": "PASS"'; then
  ok "冻结校验 PASS"
else
  bad "冻结校验未通过"
  echo "$FREEZE" | grep -E '"status"|errors|FAIL' | head -6
  if [ "$FIX" = 1 ]; then
    echo "  尝试重算冻结包…"
    $PY scripts/refreeze_harness_release.py >/dev/null 2>&1 \
      && echo "  已重算，请复核 git diff 后重跑本脚本"
  else
    echo "  修法：uv run python scripts/refreeze_harness_release.py（改到 doc/05 等冻结文档时必做）"
  fi
fi

# ---------- 4b) 发布候选运营闸门（Codex P0-1/P0-3/P1-4 自测，无 live 配额）----------
step "4b) check_release_candidate_gates"
if bash scripts/check_release_candidate_gates.sh >/tmp/ring-rc-gates.log 2>&1; then
  ok "发布候选运营闸门自测"
else
  bad "发布候选运营闸门自测"
  tail -20 /tmp/ring-rc-gates.log
fi

# ---------- 5) 指定测试（pytest 或 vitest，按扩展名分派）----------
# 为什么按扩展名分派：`--tests` 原先只会走 pytest —— 传一个 `.ts` 路径会得到
# `(no match in any of [<Dir harness>])` 并被报成 ❌。**工具报的假失败**会让使用者
# 怀疑被测代码，故这里显式分派，并给出各自的通过判据。
if [ "${#EXTRA_TESTS[@]}" -gt 0 ]; then
  step "5) 指定测试"
  for t in "${EXTRA_TESTS[@]}"; do
    case "$t" in
      *.ts|*.tsx)
        # vitest 以**包目录**为 cwd（这里 apps/runner），故仓库相对路径必须先剥掉
        # `apps/runner/` 前缀，否则报 "No test files found" 并被误判为 ❌。
        # （实测：`pnpm --filter @ring/runner exec vitest run apps/runner/src/x.test.ts`
        #   找不到文件；`... run src/x.test.ts` 才行。）
        rel="${t#apps/runner/}"
        out="$(pnpm --filter @ring/runner exec vitest run "$rel" 2>&1 | grep -E 'Test Files|Tests ' | tail -2)"
        echo "  [vitest] $t → $(echo "$out" | tr '\n' ' ')"
        echo "$out" | grep -qE "Tests +[0-9]+ passed" && ! echo "$out" | grep -qE "failed" \
          || bad "vitest: $t"
        ;;
      *)
        out="$($PY -m pytest "$t" -q 2>&1 | tail -2)"
        echo "  [pytest] $t → $out"
        echo "$out" | grep -qE "[0-9]+ passed" && ! echo "$out" | grep -qE "[1-9][0-9]* failed" \
          || bad "pytest: $t"
        ;;
    esac
  done
fi

# ---------- 6) 前端（vitest 不做类型检查，build 才暴露类型错）----------
if [ "$SKIP_FRONTEND" = 0 ]; then
  step "6) pnpm test && pnpm build && pnpm typecheck"
  FE_LOG="$(mktemp)"
  if (pnpm test && pnpm build && pnpm typecheck) > "$FE_LOG" 2>&1; then
    ok "前端三件套"
  else
    bad "前端失败"
    grep -E "Failed Tests|FAIL |Test timed out|error TS|ERR_PNPM" "$FE_LOG" | head -12
    echo "  完整日志：$FE_LOG"
  fi
  grep -E "Test Files|Tests " "$FE_LOG" | tail -4
  echo "  错误计数（error TS/ERR_PNPM/ELIFECYCLE）：$(grep -cE 'error TS|ERR_PNPM|ELIFECYCLE' "$FE_LOG")"
  [ -f "$FE_LOG" ] && grep -qE "Failed Tests" "$FE_LOG" || rm -f "$FE_LOG"
fi

# ---------- 7) （可选）真实模型 live 套件 ----------
# 为什么不默认跑：需要真实凭据并**烧模型配额**，还会与别人的 live 争用（本仓明文约定）。
# 为什么必须提供：`foundation.yml` 不设 chat env ⇒ 7 个 `*.live.test.ts` **静默 skip**，
# 于是「全绿」里它们一次都没跑 —— 本会话的 live 通路回归正是这样躲过 CI 的。
# `--require-live` 让「跳过」判为失败（对齐 Python 侧 RING_CI_LIVE_REQUIRED 语义）。
if [ "$LIVE" = 1 ]; then
  step "7) 真实模型 live 套件（--require-live）"
  # Node fetch 不认 http_proxy（见 run_live_suite.sh 内注释）；不开这个开关，
  # 所有 Node 侧 live 用例会报 ConnectTimeoutError，看起来像业务失败。
  export NODE_USE_ENV_PROXY="${NODE_USE_ENV_PROXY:-1}"
  if [ -x scripts/run_live_suite.sh ] || [ -f scripts/run_live_suite.sh ]; then
    LIVE_OUT="$(bash scripts/run_live_suite.sh --require-live --label "${LIVE_LABEL:-check-batch}" 2>&1)"
    LIVE_RC=$?
    echo "$LIVE_OUT" | grep -E "^(  [❌✅⚠]|任务数|断言通过|⚠ 有)" | head -16
    [ "$LIVE_RC" -eq 0 ] && ok "live 套件" || bad "live 套件（rc=${LIVE_RC}；skip 已计入失败）"
  else
    bad "live 套件：未找到 scripts/run_live_suite.sh"
  fi
fi

printf '\n==================================================\n'
if [ "$FAILED" = 0 ]; then
  echo "全绿 —— 可以推送"
else
  echo "存在未过的门 —— 不要推（上面的 ❌ 即修法线索）"
fi
exit "$FAILED"
