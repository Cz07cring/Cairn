"""种子业务 E2E 目标仓库：带已知幂等缺陷的订单服务。

用法：
  uv run python scripts/seed_business_e2e.py --run-id <id>
  uv run python scripts/seed_business_e2e.py --cleanup --run-id <id>

默认输出根：仓库 `.runtime/e2e/`（gitignore）。测试可传入 runtime_root。
不写 Goal DONE；不启动 Temporal/Harness。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE = _REPO_ROOT / "tests" / "fixtures" / "business_e2e" / "order_service"
_DEFAULT_RUNTIME = _REPO_ROOT / ".runtime" / "e2e"

# 固定作者/时间，保证连续两次 seed 得到相同 initial commit。
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "ringharness-e2e",
    "GIT_AUTHOR_EMAIL": "e2e@ringharness.local",
    "GIT_COMMITTER_NAME": "ringharness-e2e",
    "GIT_COMMITTER_EMAIL": "e2e@ringharness.local",
    "GIT_AUTHOR_DATE": "2026-09-12T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-09-12T00:00:00+00:00",
}

FIXED_INPUT_PATH = _TEMPLATE / "FIXED_INPUT.json"


@dataclass(frozen=True)
class SeededRun:
    run_id: str
    run_dir: Path
    source_git: Path
    executor_worktree: Path
    auditor_worktree: Path
    artifacts: Path
    initial_commit: str
    input_digest: str


def fixed_input() -> dict[str, object]:
    return json.loads(FIXED_INPUT_PATH.read_text(encoding="utf-8"))


def fixed_input_digest() -> str:
    """对固定业务输入做 canonical JSON SHA-256。"""
    payload = json.dumps(fixed_input(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **_GIT_ENV}
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=check,
    )


def _copy_public_template(dest: Path) -> None:
    """复制公开源码与公开测试；不含 tests_hidden / reference_fix（防 Executor 泄题）。"""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        _TEMPLATE,
        dest,
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            ".pytest_cache",
            "reference_fix",
            "tests_hidden",
            "README.md",
        ),
    )


def _init_source_repo(source: Path) -> str:
    """建立 bare 源仓，便于同时挂 executor / auditor worktree。"""
    staging = source.parent / "_staging_source"
    _copy_public_template(staging)
    _git(staging, "init", "-b", "main")
    _git(staging, "add", "-A")
    _git(staging, "commit", "-m", "e2e0: buggy order service baseline")
    sha = _git(staging, "rev-parse", "HEAD").stdout.strip()
    if source.exists():
        shutil.rmtree(source)
    _git(staging, "clone", "--bare", str(staging), str(source))
    shutil.rmtree(staging)
    return sha


def _checkout_worktree(source: Path, worktree: Path, *, include_hidden_tests: bool) -> None:
    if worktree.exists():
        shutil.rmtree(worktree)
    # bare 源仓上对同一 commit 可挂多个 detached worktree（同名分支不能并行检出）。
    _git(source, "worktree", "add", "--detach", str(worktree), "main")
    hidden = worktree / "tests_hidden"
    if include_hidden_tests:
        if hidden.exists():
            shutil.rmtree(hidden)
        shutil.copytree(_TEMPLATE / "tests_hidden", hidden)
    elif hidden.exists():
        shutil.rmtree(hidden)


def seed_run(*, run_id: str, runtime_root: Path | None = None) -> SeededRun:
    if not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
        raise ValueError(f"非法 run_id: {run_id!r}")
    root = Path(runtime_root) if runtime_root is not None else _DEFAULT_RUNTIME
    run_dir = root / run_id
    if run_dir.exists():
        shutil.rmtree(run_dir)
    source = run_dir / "source.git"
    executor = run_dir / "executor-worktree"
    auditor = run_dir / "auditor-worktree"
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    initial_commit = _init_source_repo(source)
    digest = fixed_input_digest()
    _checkout_worktree(source, executor, include_hidden_tests=False)
    _checkout_worktree(source, auditor, include_hidden_tests=True)

    summary = {
        "scenario": "order_idempotency",
        "run_id": run_id,
        "initial_commit": initial_commit,
        "input_digest": digest,
        "fixed_input": fixed_input(),
        "harness_required": True,
        "phase": "E2E-0",
        "marks_goal_done": False,
    }
    (artifacts / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return SeededRun(
        run_id=run_id,
        run_dir=run_dir,
        source_git=source,
        executor_worktree=executor,
        auditor_worktree=auditor,
        artifacts=artifacts,
        initial_commit=initial_commit,
        input_digest=digest,
    )


def apply_reference_fix(worktree: Path) -> None:
    """将 reference_fix/store.py 覆盖到 worktree（仅 fixture 自检）。"""
    src = _TEMPLATE / "reference_fix" / "store.py"
    dest = worktree / "order_service" / "store.py"
    if not dest.parent.is_dir():
        raise FileNotFoundError(f"worktree 缺少 order_service: {worktree}")
    shutil.copyfile(src, dest)


def cleanup_run(*, run_id: str, runtime_root: Path | None = None) -> None:
    if not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
        raise ValueError(f"非法 run_id: {run_id!r}")
    root = Path(runtime_root) if runtime_root is not None else _DEFAULT_RUNTIME
    run_dir = root / run_id
    if not run_dir.exists():
        raise FileNotFoundError(f"run 不存在: {run_dir}")
    # 先卸 worktree，避免残留 git 锁。
    source = run_dir / "source.git"
    if source.is_dir():
        for name in ("executor-worktree", "auditor-worktree"):
            wt = run_dir / name
            if wt.exists():
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt)],
                    cwd=source,
                    capture_output=True,
                    check=False,
                )
    shutil.rmtree(run_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed business E2E order_service fixture")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runtime-root", type=Path, default=None)
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--apply-reference-fix", action="store_true")
    args = parser.parse_args(argv)
    if args.cleanup:
        cleanup_run(run_id=args.run_id, runtime_root=args.runtime_root)
        print(f"cleaned {args.run_id}")
        return 0
    run = seed_run(run_id=args.run_id, runtime_root=args.runtime_root)
    if args.apply_reference_fix:
        apply_reference_fix(run.executor_worktree)
        apply_reference_fix(run.auditor_worktree)
    print(
        json.dumps(
            {
                "run_id": run.run_id,
                "initial_commit": run.initial_commit,
                "input_digest": run.input_digest,
                "run_dir": str(run.run_dir),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
