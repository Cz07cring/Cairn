"""从封存 CandidateManifest 物化只读验证树。

字节必须来自对象仓（digest），不信任调用方自带内容。
隐藏测（holdout）不在候选内，由调用方另行挂载。≠ Goal DONE。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class MaterializeRejected(Exception):
    """物化失败：路径非法、digest 不匹配或缺字节。"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


_MAX_MATERIALIZE_FILES = 500
_MAX_MATERIALIZE_FILE_BYTES = 8 * 1024 * 1024
_MAX_MATERIALIZE_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_HOLDOUT_ENTRIES = 2000
_MAX_HOLDOUT_DEPTH = 64


@dataclass
class _HoldoutCopyBudget:
    max_files: int
    max_file_bytes: int
    max_total_bytes: int
    max_entries: int
    max_depth: int
    file_count: int = 0
    total_bytes: int = 0
    entry_count: int = 0

    def account_entry(self) -> None:
        self.entry_count += 1
        if self.entry_count > self.max_entries:
            raise MaterializeRejected(
                f"holdout 目录项超过上限 {self.max_entries}"
            )

    def require_child_depth(self, depth: int) -> None:
        if depth > self.max_depth:
            raise MaterializeRejected(
                f"holdout 目录深度超过上限 {self.max_depth}"
            )

    def begin_file(self) -> None:
        self.file_count += 1
        if self.file_count > self.max_files:
            raise MaterializeRejected(
                f"holdout 文件数超过上限 {self.max_files}"
            )

    def account_chunk(self, *, file_bytes: int, chunk_bytes: int) -> int:
        next_file_bytes = file_bytes + chunk_bytes
        if next_file_bytes > self.max_file_bytes:
            raise MaterializeRejected(
                f"holdout 单文件超过上限 {self.max_file_bytes}"
            )
        next_total = self.total_bytes + chunk_bytes
        if next_total > self.max_total_bytes:
            raise MaterializeRejected(
                f"holdout 总字节超过上限 {self.max_total_bytes}"
            )
        self.total_bytes = next_total
        return next_file_bytes


def _safe_rel_path(relative_path: str) -> Path:
    if not relative_path or relative_path.strip() != relative_path:
        raise MaterializeRejected("路径为空或含首尾空白")
    if relative_path.startswith(("/", "~")):
        raise MaterializeRejected("禁止绝对路径")
    if "\\" in relative_path:
        raise MaterializeRejected("禁止反斜杠路径")
    parts = relative_path.split("/")
    if ".." in parts:
        raise MaterializeRejected("禁止父目录逃逸")
    if any(
        part in ("", ".")
        or any(ord(char) < 32 or ord(char) == 127 for char in part)
        for part in parts
    ):
        raise MaterializeRejected("路径含空段、点段或控制字符")
    return Path(*parts)


def _open_materialize_root(dest_root: Path) -> tuple[Path, int]:
    """创建并固定真实空目录；后续对象读取不能替换写入根身份。"""
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise MaterializeRejected("当前平台不支持安全物化目录")
    root = dest_root.absolute()
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        try:
            root.mkdir(parents=True, mode=0o700)
        except OSError as exc:
            raise MaterializeRejected("无法创建物化目标") from exc
        metadata = root.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise MaterializeRejected("物化目标必须为真实目录，禁止符号链接")
    try:
        if any(root.iterdir()):
            raise MaterializeRejected("物化目标必须为空目录")
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except MaterializeRejected:
        raise
    except OSError as exc:
        raise MaterializeRejected("物化目标无法安全打开") from exc
    return root, root_fd


def _root_identity_matches(root: Path, root_fd: int) -> bool:
    try:
        path_meta = root.lstat()
        fd_meta = os.fstat(root_fd)
    except OSError:
        return False
    return (
        stat.S_ISDIR(path_meta.st_mode)
        and not stat.S_ISLNK(path_meta.st_mode)
        and path_meta.st_dev == fd_meta.st_dev
        and path_meta.st_ino == fd_meta.st_ino
    )


def _create_materialized_file(root_fd: int, rel: Path) -> int:
    """从固定根 fd 逐级创建文件，拒绝任一链接或非目录组件。"""
    directory_fd = os.dup(root_fd)
    file_fd: int | None = None
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        for part in rel.parts[:-1]:
            try:
                next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
                try:
                    next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                except OSError as exc:
                    raise MaterializeRejected(
                        "候选父路径含符号链接或无法安全创建"
                    ) from exc
            except OSError as exc:
                raise MaterializeRejected(
                    "候选父路径含符号链接或无法安全创建"
                ) from exc
            os.close(directory_fd)
            directory_fd = next_fd

        try:
            file_fd = os.open(
                rel.parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise MaterializeRejected("候选文件无法安全创建") from exc
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise MaterializeRejected("候选目标必须为单链接普通文件")
        return file_fd
    except BaseException:
        if file_fd is not None:
            os.close(file_fd)
        raise
    finally:
        os.close(directory_fd)


def materialize_candidate_worktree(
    dest_root: Path,
    files: list[dict],
    *,
    read_bytes: Callable[[str], bytes],
    read_only: bool = True,
    max_files: int = _MAX_MATERIALIZE_FILES,
    max_file_bytes: int = _MAX_MATERIALIZE_FILE_BYTES,
    max_total_bytes: int = _MAX_MATERIALIZE_TOTAL_BYTES,
) -> int:
    """按 manifest.files 写入 dest；read_bytes(digest) 取对象仓字节。

    返回写入文件数。dest 须为空或不存在。
    """
    if not files:
        raise MaterializeRejected("候选文件列表为空")
    if max_files <= 0 or max_file_bytes <= 0 or max_total_bytes <= 0:
        raise MaterializeRejected("候选物化限制必须为正数")
    if len(files) > max_files:
        raise MaterializeRejected(f"候选文件数超过上限 {max_files}")

    seen: set[str] = set()
    written = 0
    total_bytes = 0
    root, root_fd = _open_materialize_root(dest_root)
    try:
        for entry in files:
            if not isinstance(entry, dict):
                raise MaterializeRejected("文件条目须为对象")
            path = entry.get("path")
            digest = entry.get("digest")
            mode = entry.get("mode") or "100644"
            if not isinstance(path, str) or not isinstance(digest, str):
                raise MaterializeRejected("path/digest 必须为字符串")
            if path in seen:
                raise MaterializeRejected(f"重复路径: {path}")
            seen.add(path)
            if (
                not digest.startswith("sha256:")
                or len(digest) != 71
                or any(char not in "0123456789abcdef" for char in digest[7:])
            ):
                raise MaterializeRejected(f"digest 非法: {path}")
            if mode not in {"100644", "100755"}:
                raise MaterializeRejected(f"mode 非法: {path}")
            rel = _safe_rel_path(path)
            try:
                body = read_bytes(digest)
            except FileNotFoundError as exc:
                raise MaterializeRejected(f"候选文件字节未入库: {path}") from exc
            if not isinstance(body, bytes):
                raise MaterializeRejected(f"候选文件字节类型非法: {path}")
            if len(body) > max_file_bytes:
                raise MaterializeRejected(
                    f"候选单文件超过上限 {max_file_bytes}: {path}"
                )
            total_bytes += len(body)
            if total_bytes > max_total_bytes:
                raise MaterializeRejected(
                    f"候选总字节超过上限 {max_total_bytes}"
                )
            actual = "sha256:" + hashlib.sha256(body).hexdigest()
            if actual != digest:
                raise MaterializeRejected(f"字节与 digest 不一致: {path}")
            if not _root_identity_matches(root, root_fd):
                raise MaterializeRejected("物化目标在对象读取期间被替换")
            fd = _create_materialized_file(root_fd, rel)
            with os.fdopen(fd, "wb") as target:
                target.write(body)
                target.flush()
                os.fsync(target.fileno())
                os.fchmod(target.fileno(), 0o755 if mode == "100755" else 0o644)
            written += 1

        if not _root_identity_matches(root, root_fd):
            raise MaterializeRejected("物化目标身份发生变化")
        if read_only:
            make_tree_read_only(root)
        return written
    except BaseException:
        if _root_identity_matches(root, root_fd):
            _remove_partial_tree(root)
        raise
    finally:
        os.close(root_fd)


def make_tree_read_only(root: Path) -> None:
    """去掉写权限；目录保留执行位以便遍历。"""
    for path in sorted(root.rglob("*"), reverse=True):
        mode = path.stat().st_mode
        path.chmod(mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)
    root_mode = root.stat().st_mode
    root.chmod(root_mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)


def _open_holdout_root(root: Path) -> int:
    """固定 holdout 源根；后续遍历不再从路径重新定位。"""
    try:
        root_meta = root.lstat()
    except FileNotFoundError as exc:
        raise MaterializeRejected("holdout 源不存在") from exc
    if stat.S_ISLNK(root_meta.st_mode) or not stat.S_ISDIR(root_meta.st_mode):
        raise MaterializeRejected("holdout 源必须为真实目录，禁止符号链接")
    root_fd: int | None = None
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened = os.fstat(root_fd)
    except OSError as exc:
        if root_fd is not None:
            os.close(root_fd)
        raise MaterializeRejected("holdout 源无法安全打开") from exc
    if opened.st_dev != root_meta.st_dev or opened.st_ino != root_meta.st_ino:
        os.close(root_fd)
        raise MaterializeRejected("holdout 源根在打开期间发生变化")
    return root_fd


def _open_attach_root(root: Path) -> tuple[Path, int, int]:
    """固定已物化的目标根，返回路径、fd 与原始模式。"""
    absolute = root.absolute()
    try:
        before = absolute.lstat()
    except FileNotFoundError as exc:
        raise MaterializeRejected("物化目标不存在") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise MaterializeRejected("物化目标必须为真实目录")
    root_fd: int | None = None
    try:
        root_fd = os.open(
            absolute,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        opened = os.fstat(root_fd)
    except OSError as exc:
        if root_fd is not None:
            os.close(root_fd)
        raise MaterializeRejected("物化目标无法安全打开") from exc
    if not _same_entry(before, opened) or not stat.S_ISDIR(opened.st_mode):
        os.close(root_fd)
        raise MaterializeRejected("物化目标在打开期间发生变化")
    return absolute, root_fd, stat.S_IMODE(opened.st_mode)


def _same_entry(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and stat.S_IFMT(before.st_mode) == stat.S_IFMT(after.st_mode)
        and before.st_nlink == after.st_nlink
    )


def _copy_holdout_regular(
    source_fd: int,
    target_fd: int,
    name: str,
    before: os.stat_result,
    budget: _HoldoutCopyBudget,
) -> None:
    source_file: int | None = None
    target_file: int | None = None
    try:
        try:
            source_file = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=source_fd,
            )
        except OSError as exc:
            raise MaterializeRejected(
                "holdout 源文件变成符号链接或无法安全打开"
            ) from exc
        opened = os.fstat(source_file)
        if (
            not _same_entry(before, opened)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
        ):
            raise MaterializeRejected("holdout 源文件在打开期间发生变化")
        budget.begin_file()
        try:
            target_file = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=target_fd,
            )
        except OSError as exc:
            raise MaterializeRejected("holdout 目标文件无法安全创建") from exc
        with os.fdopen(source_file, "rb") as source, os.fdopen(
            target_file, "wb"
        ) as target:
            source_file = None
            target_file = None
            file_bytes = 0
            while chunk := source.read(1024 * 1024):
                file_bytes = budget.account_chunk(
                    file_bytes=file_bytes,
                    chunk_bytes=len(chunk),
                )
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
            target_mode = stat.S_IMODE(opened.st_mode) & ~(
                stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
            )
            os.fchmod(target.fileno(), target_mode)
    finally:
        if source_file is not None:
            os.close(source_file)
        if target_file is not None:
            os.close(target_file)


def _copy_holdout_directory(
    source_fd: int,
    target_fd: int,
    budget: _HoldoutCopyBudget,
    *,
    depth: int = 0,
) -> None:
    """在已固定的源/目标目录 fd 之间递归复制，不跟随链接。"""
    try:
        names = sorted(os.listdir(source_fd))
    except OSError as exc:
        raise MaterializeRejected("holdout 源目录无法安全遍历") from exc

    for name in names:
        try:
            before = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        except OSError as exc:
            raise MaterializeRejected("holdout 源条目在遍历期间发生变化") from exc
        budget.account_entry()
        if stat.S_ISLNK(before.st_mode):
            raise MaterializeRejected("holdout 禁止符号链接")
        if stat.S_ISREG(before.st_mode):
            if before.st_nlink != 1:
                raise MaterializeRejected("holdout 禁止硬链接文件")
            _copy_holdout_regular(source_fd, target_fd, name, before, budget)
            continue
        if not stat.S_ISDIR(before.st_mode):
            raise MaterializeRejected("holdout 只允许普通文件与目录")
        child_depth = depth + 1
        budget.require_child_depth(child_depth)

        source_child: int | None = None
        target_child: int | None = None
        try:
            try:
                source_child = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=source_fd,
                )
            except OSError as exc:
                raise MaterializeRejected(
                    "holdout 源目录变成符号链接或无法安全打开"
                ) from exc
            opened = os.fstat(source_child)
            if not _same_entry(before, opened) or not stat.S_ISDIR(opened.st_mode):
                raise MaterializeRejected("holdout 源目录在打开期间发生变化")
            try:
                os.mkdir(name, mode=0o700, dir_fd=target_fd)
                target_child = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=target_fd,
                )
            except OSError as exc:
                raise MaterializeRejected("holdout 目标目录无法安全创建") from exc
            _copy_holdout_directory(
                source_child,
                target_child,
                budget,
                depth=child_depth,
            )
            target_mode = stat.S_IMODE(opened.st_mode) & ~(
                stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
            )
            os.fchmod(target_child, target_mode)
        finally:
            if source_child is not None:
                os.close(source_child)
            if target_child is not None:
                os.close(target_child)


def _remove_partial_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def attach_holdout_tests(
    dest_root: Path,
    holdout_src: Path,
    *,
    max_files: int = _MAX_MATERIALIZE_FILES,
    max_file_bytes: int = _MAX_MATERIALIZE_FILE_BYTES,
    max_total_bytes: int = _MAX_MATERIALIZE_TOTAL_BYTES,
    max_entries: int = _MAX_HOLDOUT_ENTRIES,
    max_depth: int = _MAX_HOLDOUT_DEPTH,
) -> None:
    """将隐藏测挂到物化树（来自 ProtectedBaseline/fixture，非候选）。"""
    if (
        max_files <= 0
        or max_file_bytes <= 0
        or max_total_bytes <= 0
        or max_entries <= 0
        or max_depth <= 0
    ):
        raise MaterializeRejected("holdout 物化限制必须为正数")
    budget = _HoldoutCopyBudget(
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        max_entries=max_entries,
        max_depth=max_depth,
    )
    source_fd = _open_holdout_root(holdout_src)
    try:
        root, root_fd, root_mode = _open_attach_root(dest_root)
    except BaseException:
        os.close(source_fd)
        raise
    target = root / "tests_hidden"
    target_fd: int | None = None
    target_created = False
    try:
        try:
            os.stat("tests_hidden", dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise MaterializeRejected("候选已含 tests_hidden，拒绝覆盖")
        # 物化树可能已 read_only：仅临时放开已固定根 fd 的写权限。
        os.fchmod(root_fd, root_mode | stat.S_IWUSR | stat.S_IXUSR)
        try:
            os.mkdir("tests_hidden", mode=0o700, dir_fd=root_fd)
            target_created = True
            target_fd = os.open(
                "tests_hidden",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
        except OSError as exc:
            raise MaterializeRejected("holdout 目标根无法安全创建") from exc
        _copy_holdout_directory(source_fd, target_fd, budget)
        os.fchmod(target_fd, 0o500)
    except MaterializeRejected:
        if target_created:
            _remove_partial_tree(target)
        raise
    except OSError as exc:
        if target_created:
            _remove_partial_tree(target)
        raise MaterializeRejected("holdout 无法安全复制") from exc
    finally:
        if target_fd is not None:
            os.close(target_fd)
        os.close(source_fd)
        os.fchmod(
            root_fd,
            root_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH),
        )
        os.close(root_fd)
