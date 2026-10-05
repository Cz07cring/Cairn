#!/usr/bin/env bash
# Codex P0-1 / P0-3 / P1-4 运营闸门自测入口（无 live 配额、不连模型）。
# CI foundation 与本机收工均可调用；≠ Goal DONE，也不替代 100h。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="uv run python"

echo "=== 发布候选运营闸门（自测）==="
bash scripts/run_convergence_soak.sh --self-test
"$PY" scripts/assert_release_candidate_lock.py --self-test
# Issue #68 空参链：必填 schema 与静默填空一致性（无 live）
"$PY" scripts/check_tool_param_required.py
# 纯函数闸门：空账本 / 故障样本不足须失败关闭
"$PY" - <<'PY'
import importlib.util
import sys
from pathlib import Path

root = Path(".").resolve()
spec = importlib.util.spec_from_file_location(
    "report_business_metrics", root / "scripts" / "report_business_metrics.py"
)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
sys.modules["report_business_metrics"] = m
spec.loader.exec_module(m)

empty = {"metrics": {"model_first_attempt_success": {"numerator": 0, "denominator": 0}}}
try:
    m.require_model_invocation_ledger(empty)
    raise SystemExit("P0-1: empty ledger should raise")
except RuntimeError as e:
    assert "MODEL_INVOCATION_LEDGER_EMPTY" in str(e)

ok = {"metrics": {"model_first_attempt_success": {"numerator": 1, "denominator": 2}}}
assert m.require_model_invocation_ledger(ok) == 2

no_fault = {
    "metrics": {
        "unknown_recovery": {"recovery": {"numerator": 0, "denominator": 0}},
        "retry_success": {"numerator": 0, "denominator": 0},
    }
}
try:
    m.require_fault_metric_samples(no_fault)
    raise SystemExit("P1-4: empty fault samples should raise")
except RuntimeError as e:
    assert "FAULT_METRIC_UNKNOWN_EMPTY" in str(e)

print("OK metrics gates (P0-1 / P1-4 pure)")
PY

echo "发布候选运营闸门自测全部通过（≠ Goal DONE / ≠ 100h）"
