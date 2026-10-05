"""E2E-0：可重复订单服务目标仓库（已知幂等缺陷）。

不启动 Temporal / Harness；只验证 fixture 种子、公开测失败、参考修复后通过、
两次初始化 commit/digest 一致、cleanup 仅删指定 run_id。
≠ Goal DONE；≠ E2E-1+。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import seed_business_e2e as seed


def _run_pytest(cwd: Path, *paths: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *paths],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_seed_public_idempotency_fails_on_buggy_tree(tmp_path: Path) -> None:
    run = seed.seed_run(run_id="e2e0-public-red", runtime_root=tmp_path)
    assert run.initial_commit
    assert run.input_digest == seed.fixed_input_digest()

    public = _run_pytest(run.executor_worktree, "tests/test_idempotency.py")
    assert public.returncode != 0, public.stdout + public.stderr
    assert (run.executor_worktree / "tests_hidden").exists() is False


def test_reference_fix_passes_public_and_hidden(tmp_path: Path) -> None:
    run = seed.seed_run(run_id="e2e0-ref-green", runtime_root=tmp_path)
    seed.apply_reference_fix(run.executor_worktree)
    seed.apply_reference_fix(run.auditor_worktree)

    public = _run_pytest(run.executor_worktree, "tests/")
    assert public.returncode == 0, public.stdout + public.stderr

    hidden = _run_pytest(run.auditor_worktree, "tests_hidden/")
    assert hidden.returncode == 0, hidden.stdout + hidden.stderr
    assert (run.auditor_worktree / "tests_hidden").is_dir()


def test_two_seeds_same_commit_and_input_digest(tmp_path: Path) -> None:
    a = seed.seed_run(run_id="e2e0-a", runtime_root=tmp_path)
    b = seed.seed_run(run_id="e2e0-b", runtime_root=tmp_path)
    assert a.initial_commit == b.initial_commit
    assert a.input_digest == b.input_digest == seed.fixed_input_digest()
    payload = json.loads((a.run_dir / "artifacts" / "run-summary.json").read_text())
    assert payload["scenario"] == "order_idempotency"
    assert payload["initial_commit"] == a.initial_commit
    assert payload["input_digest"] == a.input_digest


def test_cleanup_only_deletes_specified_run_id(tmp_path: Path) -> None:
    keep = seed.seed_run(run_id="e2e0-keep", runtime_root=tmp_path)
    drop = seed.seed_run(run_id="e2e0-drop", runtime_root=tmp_path)
    seed.cleanup_run(run_id="e2e0-drop", runtime_root=tmp_path)
    assert keep.run_dir.is_dir()
    assert not drop.run_dir.exists()
    with pytest.raises(FileNotFoundError):
        seed.cleanup_run(run_id="e2e0-missing", runtime_root=tmp_path)
