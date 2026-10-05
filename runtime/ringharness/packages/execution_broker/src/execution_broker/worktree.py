"""Attempt 工作区隔离：每 attempt 独立目录，禁止跨 attempt 共享写入根。"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from subprocess import TimeoutExpired
from uuid import UUID, uuid4

from .bounded_process import BoundedProcessResult, run_bounded_process
from .safe_git import safe_git_argv, safe_git_env


class WorkspaceUnavailable(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def workspace_base(env: dict[str, str] | None = None) -> Path | None:
    raw = (env or os.environ).get("RING_WORKSPACE_ROOT", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def attempt_workspace_path(base: Path, project_id: UUID, attempt_id: UUID) -> Path:
    """派生词法路径；候选目录不能用 resolve() 跟随到另一 attempt。"""
    root = base.resolve()
    candidate = root / "projects" / str(project_id) / "attempts" / str(attempt_id)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise WorkspaceUnavailable("工作区路径逃出 RING_WORKSPACE_ROOT") from exc
    return candidate


def _open_attempt_directory(root: Path, project_id: UUID, attempt_id: UUID) -> int:
    """从 workspace 根 fd 逐级创建并打开 attempt，拒绝任一 symlink 组件。"""
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise WorkspaceUnavailable("当前平台不支持安全打开工作区目录")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        current_fd = os.open(root, flags)
    except OSError as exc:
        raise WorkspaceUnavailable("workspace 根无法安全打开") from exc

    parts = ("projects", str(project_id), "attempts", str(attempt_id))
    try:
        for part in parts:
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    # 并发创建后仍必须重新经 O_NOFOLLOW 打开。
                    pass
                try:
                    next_fd = os.open(part, flags, dir_fd=current_fd)
                except OSError as exc:
                    raise WorkspaceUnavailable(
                        "工作区目录含符号链接或无法安全打开"
                    ) from exc
            except OSError as exc:
                raise WorkspaceUnavailable(
                    "工作区目录含符号链接或无法安全打开"
                ) from exc
            previous_fd = current_fd
            current_fd = next_fd
            os.close(previous_fd)
        return current_fd
    except (OSError, WorkspaceUnavailable):
        os.close(current_fd)
        raise


def _ensure_attempt_marker(
    attempt_fd: int,
    project_id: UUID,
    attempt_id: UUID,
) -> None:
    """原子创建或校验 attempt 身份，禁止链接 marker 与身份复用。"""
    expected = f"{project_id}:{attempt_id}\n".encode()
    marker_flags = os.O_NOFOLLOW
    try:
        marker_fd = os.open(
            ".ring-attempt",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | marker_flags,
            0o600,
            dir_fd=attempt_fd,
        )
    except FileExistsError:
        try:
            marker_fd = os.open(
                ".ring-attempt",
                os.O_RDONLY | marker_flags,
                dir_fd=attempt_fd,
            )
        except OSError as exc:
            raise WorkspaceUnavailable("attempt marker 无法安全打开") from exc
        try:
            metadata = os.fstat(marker_fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise WorkspaceUnavailable("attempt marker 必须为单链接普通文件")
            with os.fdopen(marker_fd, "rb") as marker:
                marker_fd = -1
                actual = marker.read(len(expected) + 1)
            if actual != expected:
                raise WorkspaceUnavailable("attempt marker 身份不匹配")
        finally:
            if marker_fd >= 0:
                os.close(marker_fd)
        return
    except OSError as exc:
        raise WorkspaceUnavailable("attempt marker 无法安全创建") from exc

    try:
        with os.fdopen(marker_fd, "wb") as marker:
            marker.write(expected)
            marker.flush()
            os.fsync(marker.fileno())
    except OSError as exc:
        raise WorkspaceUnavailable("attempt marker 写入失败") from exc


def _create_checkout_marker(workspace: Path, content: str) -> None:
    """检出完成后独占创建保留 marker，拒绝仓库内容占用或重定向该名称。"""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    marker_fd: int | None = None
    try:
        directory_fd = os.open(workspace, flags)
    except OSError as exc:
        raise WorkspaceUnavailable("检出工作区无法安全打开") from exc
    try:
        try:
            marker_fd = os.open(
                ".ring-attempt",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
        except FileExistsError as exc:
            raise WorkspaceUnavailable("检出内容占用 attempt marker 保留名") from exc
        except OSError as exc:
            raise WorkspaceUnavailable("attempt marker 无法安全创建") from exc
        metadata = os.fstat(marker_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise WorkspaceUnavailable("attempt marker 必须为单链接普通文件")
        with os.fdopen(marker_fd, "wb") as marker:
            marker_fd = None
            marker.write(content.encode())
            marker.flush()
            os.fsync(marker.fileno())
    finally:
        if marker_fd is not None:
            os.close(marker_fd)
        os.close(directory_fd)


def _replace_checkout_marker(workspace: Path, content: str) -> None:
    """用同目录临时普通文件替换 marker；不会跟随检出内容提供的 symlink。"""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    temporary = f".ring-attempt-quarantine-{uuid4().hex}"
    temporary_exists = False
    temp_fd: int | None = None
    try:
        directory_fd = os.open(workspace, flags)
    except OSError as exc:
        raise WorkspaceUnavailable("失败工作区无法安全打开") from exc
    try:
        try:
            temp_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            temporary_exists = True
        except OSError as exc:
            raise WorkspaceUnavailable("隔离 marker 临时文件无法安全创建") from exc
        metadata = os.fstat(temp_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise WorkspaceUnavailable("隔离 marker 必须为单链接普通文件")
        with os.fdopen(temp_fd, "wb") as marker:
            temp_fd = None
            marker.write(content.encode())
            marker.flush()
            os.fsync(marker.fileno())
        try:
            os.replace(
                temporary,
                ".ring-attempt",
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        except OSError as exc:
            raise WorkspaceUnavailable("隔离 marker 无法安全替换") from exc
        temporary_exists = False
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if temporary_exists:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


def ensure_attempt_workspace(
    project_id: UUID,
    attempt_id: UUID,
    *,
    env: dict[str, str] | None = None,
    required: bool = False,
) -> Path | None:
    """创建 attempt 独占目录。required=True 且未配置根时拒绝。"""
    base = workspace_base(env)
    if base is None:
        if required:
            raise WorkspaceUnavailable("未配置 RING_WORKSPACE_ROOT")
        return None
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = attempt_workspace_path(base, project_id, attempt_id)
    attempt_fd = _open_attempt_directory(base, project_id, attempt_id)
    try:
        _ensure_attempt_marker(attempt_fd, project_id, attempt_id)
    finally:
        os.close(attempt_fd)
    return path


def _run_git_command(
    argv: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    purpose: str,
) -> BoundedProcessResult:
    try:
        result = run_bounded_process(
            argv,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=4096,
            max_stderr_bytes=8192,
            env=safe_git_env(),
        )
    except TimeoutExpired as exc:
        raise WorkspaceUnavailable(f"{purpose}超时") from exc
    except OSError as exc:
        raise WorkspaceUnavailable(f"{purpose}启动失败") from exc
    if result.returncode != 0:
        detail = (result.stderr_prefix or result.stdout_prefix).decode(
            "utf-8", errors="replace"
        )[:500]
        raise WorkspaceUnavailable(f"{purpose}失败: {detail}")
    return result


def _git_sha(result: BoundedProcessResult, *, purpose: str) -> str:
    sha = result.stdout_prefix.decode("utf-8", errors="replace").strip().lower()
    if len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha):
        raise WorkspaceUnavailable(f"{purpose}未返回合法 SHA")
    return sha


def _reject_checkout_filters(
    repository_path: Path,
    *,
    timeout_seconds: int,
) -> None:
    """拒绝会在 checkout 中执行命令的仓库本地 filter 配置。"""
    try:
        result = run_bounded_process(
            safe_git_argv(
                "-C",
                str(repository_path),
                "config",
                "--local",
                "--name-only",
                "--get-regexp",
                r"^filter\..*\.(clean|smudge|process|required)$",
            ),
            cwd=repository_path,
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=4096,
            max_stderr_bytes=4096,
            env=safe_git_env(),
        )
    except TimeoutExpired as exc:
        raise WorkspaceUnavailable("检查 checkout filter 超时") from exc
    except OSError as exc:
        raise WorkspaceUnavailable("检查 checkout filter 失败") from exc
    if result.returncode == 1 and result.stdout_bytes == 0:
        return
    if result.returncode != 0:
        detail = (result.stderr_prefix or result.stdout_prefix).decode(
            "utf-8", errors="replace"
        )[:500]
        raise WorkspaceUnavailable(f"检查 checkout filter 失败: {detail}")
    configured = result.stdout_prefix.decode("utf-8", errors="replace").strip()
    raise WorkspaceUnavailable(
        f"仓库配置了不受支持的 checkout filter: {configured[:500]}"
    )


def _restore_or_quarantine_workspace_marker(
    workspace: Path,
    marker_text: str | None,
) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    non_marker_children = [
        child for child in workspace.iterdir() if child.name != ".ring-attempt"
    ]
    content = "QUARANTINED\n" if non_marker_children else marker_text
    if content:
        _replace_checkout_marker(workspace, content)


def seed_workspace_at_commit(
    workspace: Path,
    repository_path: Path,
    base_commit: str,
    *,
    git_timeout_seconds: int = 30,
) -> str:
    """用 git worktree 在 workspace 检出固定 commit；已检出则核验 HEAD。

    返回实际 HEAD SHA。失败不假装成功。
    """
    if not repository_path.is_dir():
        raise WorkspaceUnavailable(f"仓库路径不存在: {repository_path}")
    commit = base_commit.strip().lower()
    if len(commit) < 7 or any(c not in "0123456789abcdef" for c in commit):
        raise WorkspaceUnavailable("base_commit 非法")
    _reject_checkout_filters(
        repository_path,
        timeout_seconds=git_timeout_seconds,
    )

    git_dir = workspace / ".git"
    if git_dir.exists() or (workspace / ".git").is_file():
        expected = _git_sha(
            _run_git_command(
                safe_git_argv("-C", str(repository_path), "rev-parse", commit),
                cwd=repository_path,
                timeout_seconds=git_timeout_seconds,
                purpose="解析 base_commit",
            ),
            purpose="base_commit",
        )
        head = _git_sha(
            _run_git_command(
                safe_git_argv("-C", str(workspace), "rev-parse", "HEAD"),
                cwd=workspace,
                timeout_seconds=git_timeout_seconds,
                purpose="核验工作区 HEAD",
            ),
            purpose="工作区 HEAD",
        )
        if head != expected:
            raise WorkspaceUnavailable(f"工作区 HEAD 与 base_commit 不匹配: {head}")
        return head

    # worktree add 要求目标目录为空或不存在；清掉仅含 marker 的占位。
    marker = workspace / ".ring-attempt"
    marker_text = marker.read_text(encoding="utf-8") if marker.exists() else None
    for child in workspace.iterdir():
        if child.name == ".ring-attempt":
            child.unlink()
        else:
            raise WorkspaceUnavailable("工作区非空，拒绝覆盖检出")
    # git worktree add 需要目录不存在
    workspace.rmdir()
    try:
        _run_git_command(
            safe_git_argv(
                "-C",
                str(repository_path),
                "worktree",
                "add",
                "--detach",
                str(workspace),
                commit,
            ),
            cwd=repository_path,
            timeout_seconds=git_timeout_seconds,
            purpose="git worktree 检出",
        )
    except WorkspaceUnavailable:
        _restore_or_quarantine_workspace_marker(workspace, marker_text)
        raise

    marker_content = marker_text or "seeded\n"
    try:
        head = _git_sha(
            _run_git_command(
                safe_git_argv("-C", str(workspace), "rev-parse", "HEAD"),
                cwd=workspace,
                timeout_seconds=git_timeout_seconds,
                purpose="核验新工作区 HEAD",
            ),
            purpose="新工作区 HEAD",
        )
        _create_checkout_marker(workspace, marker_content)
    except WorkspaceUnavailable:
        _restore_or_quarantine_workspace_marker(workspace, marker_text)
        raise
    return head
