"""路径安全：禁止逃逸，并按策略 allowed_paths 约束。"""

from __future__ import annotations

import fnmatch
import os
import stat
from pathlib import Path
from uuid import uuid4


class PathRejected(Exception):
    """路径非法或越权。"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _open_workspace_parent(
    workspace_root: Path,
    relative_path: str,
    *,
    create_parents: bool = False,
) -> tuple[int, str]:
    """沿目录 fd 打开目标父目录，返回父目录 fd 与最终文件名。"""
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise PathRejected("当前平台不支持安全目录打开")

    parts = Path(relative_path).parts
    if (
        not parts
        or relative_path.startswith(("/", "~"))
        or "\\" in relative_path
        or any(part in ("", "..") for part in parts)
    ):
        raise PathRejected("文件路径不是安全相对路径")

    root = workspace_root.resolve()
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        directory_fd = os.open(root, directory_flags)
    except OSError as exc:
        raise PathRejected("工作区根无法安全打开") from exc

    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            except FileNotFoundError:
                if not create_parents:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory_fd)
                except FileExistsError:
                    # 并发创建后仍走 O_NOFOLLOW 打开，不能信任刚出现的目录项。
                    pass
                try:
                    next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                except OSError as exc:
                    raise PathRejected(
                        "工作区路径含符号链接、非目录或不可访问目录组件"
                    ) from exc
            except OSError as exc:
                raise PathRejected(
                    "工作区路径含符号链接、非目录或不可访问目录组件"
                ) from exc
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd, parts[-1]
    except BaseException:
        os.close(directory_fd)
        raise


def open_workspace_regular(
    workspace_root: Path,
    relative_path: str,
    flags: int,
    *,
    create_parents: bool = False,
    mode: int = 0o600,
) -> tuple[int, os.stat_result]:
    """沿目录 fd 打开工作区文件，拒绝父级或最终文件 symlink。"""
    directory_fd, filename = _open_workspace_parent(
        workspace_root,
        relative_path,
        create_parents=create_parents,
    )

    fd: int | None = None
    try:
        try:
            fd = os.open(
                filename,
                flags | os.O_NOFOLLOW,
                mode,
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise PathRejected("禁止经符号链接访问文件") from exc

        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise PathRejected("目标不是普通文件")
        if metadata.st_nlink != 1:
            raise PathRejected("禁止访问硬链接文件")
        return fd, metadata
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    finally:
        os.close(directory_fd)


def _require_same_target(
    directory_fd: int,
    filename: str,
    expected: os.stat_result | None,
) -> None:
    """原子替换前复验目标目录项没有被并发创建或换 inode。"""
    try:
        current = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        if expected is None:
            return
        raise PathRejected("写入期间目标文件被删除") from None
    if expected is None:
        raise PathRejected("写入期间目标文件被并发创建")
    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
        raise PathRejected("写入期间目标变成链接或非普通文件")
    identity = (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
    )
    expected_identity = (
        expected.st_dev,
        expected.st_ino,
        expected.st_size,
        expected.st_mtime_ns,
        expected.st_ctime_ns,
    )
    if identity != expected_identity:
        raise PathRejected("写入期间目标文件发生变化")


def atomic_replace_workspace_regular(
    workspace_root: Path,
    relative_path: str,
    content: bytes,
    *,
    expected: os.stat_result | None,
    create_parents: bool = False,
    mode: int = 0o600,
) -> None:
    """同目录写临时普通文件并原子替换；失败时不修改原目标。"""
    directory_fd, filename = _open_workspace_parent(
        workspace_root,
        relative_path,
        create_parents=create_parents,
    )
    temporary = f".ring-write-{uuid4().hex}"
    temporary_exists = False
    temp_fd: int | None = None
    try:
        _require_same_target(directory_fd, filename, expected)
        try:
            temp_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            temporary_exists = True
        except OSError as exc:
            raise PathRejected("无法安全创建写入临时文件") from exc
        metadata = os.fstat(temp_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise PathRejected("写入临时目标不是单链接普通文件")
        target_mode = stat.S_IMODE(expected.st_mode) & 0o777 if expected else mode
        with os.fdopen(temp_fd, "wb") as output:
            temp_fd = None
            output.write(content)
            output.flush()
            os.fchmod(output.fileno(), target_mode)
            os.fsync(output.fileno())
        _require_same_target(directory_fd, filename, expected)
        os.replace(
            temporary,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
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


def resolve_workspace_path(
    workspace_root: Path,
    relative_path: str,
    *,
    allowed_paths: list[str],
    protected_paths: list[str] | None = None,
) -> Path:
    """将相对路径解析到工作区根下；拒绝绝对路径、.. 逃逸、策略外与受保护路径。"""
    if not relative_path or relative_path.strip() != relative_path:
        raise PathRejected("路径为空或含首尾空白")
    if relative_path.startswith(("/", "~")):
        raise PathRejected("禁止绝对路径")
    if "\\" in relative_path:
        raise PathRejected("禁止反斜杠路径")
    parts = Path(relative_path).parts
    if any(p in ("..", "") for p in parts):
        raise PathRejected("禁止父目录逃逸")

    root = workspace_root.resolve()
    if not root.is_dir():
        raise PathRejected("工作区根不存在")

    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PathRejected("路径逃出工作区") from exc

    # symlink：解析后仍须在根内（上面 resolve 已跟随链路）。
    if not _path_allowed(relative_path, allowed_paths):
        raise PathRejected("路径不在策略 allowed_paths 内")
    if protected_paths and _path_allowed(relative_path, list(protected_paths)):
        raise PathRejected("路径落在 protected_paths")
    return candidate


def _path_allowed(relative_path: str, allowed_paths: list[str]) -> bool:
    if not allowed_paths:
        return False
    norm = relative_path.replace("\\", "/")
    for pattern in allowed_paths:
        pat = pattern.replace("\\", "/")
        if fnmatch.fnmatch(norm, pat):
            return True
        # 目录前缀：src/** 也匹配 src/main.py
        if pat.endswith("/**") and (norm == pat[:-3] or norm.startswith(pat[:-2])):
            return True
    return False
