"""git_diff 工具：只读固定 git argv，禁止任意 shell/command。

证据：base_commit、status/diff digest 与短预览。≠ Goal DONE。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from subprocess import TimeoutExpired

from .bounded_process import BoundedProcessResult, run_bounded_process
from .safe_git import safe_git_argv, safe_git_env


@dataclass(frozen=True)
class GitDiffResult:
    observed_outcome: str
    exit_code: int
    timed_out: bool
    started_at: datetime
    finished_at: datetime
    content: bytes | None
    base_commit: str | None
    dirty: bool
    error: str | None


def parse_git_diff_input(raw: bytes) -> None:
    """解析 input：允许空参数；拒绝 command / 额外字段。"""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("input artifact 不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("input 必须为 JSON 对象")  # noqa: TRY004
    if "command" in data or (
        isinstance(data.get("parameters"), dict) and "command" in data["parameters"]
    ):
        raise ValueError("禁止自由 command；仅允许固定 git argv")
    if "parameters" in data or "tool_ref" in data or "tool_schema_digest" in data:
        params = data.get("parameters")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ValueError("ToolPayload.parameters 须为对象")
        if params:
            raise ValueError("git_diff 不接受额外参数")
    elif data:
        # 遗留空对象以外的顶层键
        raise ValueError("git_diff 仅接受空参数对象")


def _run_git(root: Path, argv: list[str], *, timeout: int) -> BoundedProcessResult:
    return run_bounded_process(
        argv,
        cwd=root,
        timeout_seconds=timeout,
        max_stdout_bytes=200_000,
        max_stderr_bytes=50_000,
        env=safe_git_env(),
    )


def execute_git_diff(
    workspace_root: Path,
    input_bytes: bytes,
    *,
    timeout_seconds: int = 30,
) -> GitDiffResult:
    """在 workspace 内跑固定 git rev-parse / status / diff。"""
    started = datetime.now(UTC)
    argv_plan = [
        safe_git_argv("rev-parse", "HEAD"),
        safe_git_argv("status", "--porcelain=v1"),
        safe_git_argv(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=all",
            "HEAD",
        ),
    ]
    try:
        parse_git_diff_input(input_bytes)
        root = workspace_root.resolve()
        if not root.is_dir():
            raise ValueError("工作区根不存在")

        head_result = _run_git(root, argv_plan[0], timeout=timeout_seconds)
        head_out = head_result.stdout_prefix.decode("utf-8", errors="replace")
        head_err = head_result.stderr_prefix.decode("utf-8", errors="replace")
        if head_result.returncode != 0:
            finished = datetime.now(UTC)
            return GitDiffResult(
                observed_outcome="FAILED",
                exit_code=head_result.returncode or 1,
                timed_out=False,
                started_at=started,
                finished_at=finished,
                content=None,
                base_commit=None,
                dirty=False,
                error=(head_err or head_out or "rev-parse 失败").strip()[:200],
            )
        base = head_out.strip()
        if len(base) != 40 or any(c not in "0123456789abcdef" for c in base):
            finished = datetime.now(UTC)
            return GitDiffResult(
                observed_outcome="FAILED",
                exit_code=1,
                timed_out=False,
                started_at=started,
                finished_at=finished,
                content=None,
                base_commit=None,
                dirty=False,
                error="HEAD 不是合法 commit",
            )

        status_result = _run_git(root, argv_plan[1], timeout=timeout_seconds)
        status_out = status_result.stdout_prefix.decode("utf-8", errors="replace")
        status_err = status_result.stderr_prefix.decode("utf-8", errors="replace")
        if status_result.returncode != 0:
            finished = datetime.now(UTC)
            return GitDiffResult(
                observed_outcome="FAILED",
                exit_code=status_result.returncode,
                timed_out=False,
                started_at=started,
                finished_at=finished,
                content=None,
                base_commit=base,
                dirty=False,
                error=(status_err or "status 失败").strip()[:200],
            )

        diff_result = _run_git(root, argv_plan[2], timeout=timeout_seconds)
        diff_out = diff_result.stdout_prefix.decode("utf-8", errors="replace")
        diff_err = diff_result.stderr_prefix.decode("utf-8", errors="replace")
        if diff_result.returncode != 0:
            finished = datetime.now(UTC)
            return GitDiffResult(
                observed_outcome="FAILED",
                exit_code=diff_result.returncode,
                timed_out=False,
                started_at=started,
                finished_at=finished,
                content=None,
                base_commit=base,
                dirty=False,
                error=(diff_err or "diff 失败").strip()[:200],
            )

        dirty = bool(status_out.strip() or diff_out.strip())
        finished = datetime.now(UTC)
        evidence = {
            "base_commit": base,
            "dirty": dirty,
            "argv": argv_plan,
            "cwd": ".",
            "status_digest": status_result.stdout_digest,
            "diff_digest": diff_result.stdout_digest,
            "status_bytes": status_result.stdout_bytes,
            "diff_bytes": diff_result.stdout_bytes,
            "status_truncated": status_result.stdout_truncated,
            "diff_truncated": diff_result.stdout_truncated,
            "status_preview": status_out[:160],
            "diff_preview": diff_out[:120],
        }
        content = json.dumps(evidence, ensure_ascii=False, separators=(",", ":")).encode()
        if len(content) > 900:
            evidence["status_preview"] = status_out[:40]
            evidence["diff_preview"] = diff_out[:40]
            content = json.dumps(evidence, ensure_ascii=False, separators=(",", ":")).encode()
        return GitDiffResult(
            observed_outcome="SUCCEEDED",
            exit_code=0,
            timed_out=False,
            started_at=started,
            finished_at=finished,
            content=content,
            base_commit=base,
            dirty=dirty,
            error=None,
        )
    except ValueError as exc:
        finished = datetime.now(UTC)
        return GitDiffResult(
            observed_outcome="FAILED",
            exit_code=1,
            timed_out=False,
            started_at=started,
            finished_at=finished,
            content=None,
            base_commit=None,
            dirty=False,
            error=str(exc),
        )
    except TimeoutExpired:
        finished = datetime.now(UTC)
        return GitDiffResult(
            observed_outcome="FAILED",
            exit_code=124,
            timed_out=True,
            started_at=started,
            finished_at=finished,
            content=None,
            base_commit=None,
            dirty=False,
            error="git_diff 超时",
        )
    except OSError as exc:
        finished = datetime.now(UTC)
        return GitDiffResult(
            observed_outcome="FAILED",
            exit_code=1,
            timed_out=False,
            started_at=started,
            finished_at=finished,
            content=None,
            base_commit=None,
            dirty=False,
            error=str(exc),
        )
