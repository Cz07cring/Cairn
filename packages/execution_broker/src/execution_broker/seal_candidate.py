"""seal_candidate：从真实 worktree 采集 WorkspaceSnapshot 并调用 Kernel 封存。

禁止模型自填 files hash；证据仅为摘要。≠ Goal DONE。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from subprocess import TimeoutExpired
from uuid import UUID

from .bounded_process import run_bounded_process
from .paths import PathRejected, open_workspace_regular, resolve_workspace_path
from .safe_git import safe_git_argv, safe_git_env

_MAX_CANDIDATE_FILE_BYTES = 8 * 1024 * 1024
_MAX_CANDIDATE_TOTAL_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class SealCandidateResult:
    observed_outcome: str
    exit_code: int
    timed_out: bool
    started_at: datetime
    finished_at: datetime
    content: bytes | None
    snapshot_bytes: bytes | None
    # digest → 真实文件字节；封存前须入库，供 Auditor 物化
    file_blobs: dict[str, bytes]
    verification_profile_ids: list[str]
    git_commit: str | None
    file_count: int
    error: str | None


def parse_seal_candidate_input(raw: bytes) -> list[str]:
    """解析 verification_profile_ids；拒绝 command / 自填 files。"""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("input artifact 不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("input 必须为 JSON 对象")  # noqa: TRY004
    if "command" in data or "files" in data:
        raise ValueError("禁止 command 或自填 files")
    if "parameters" in data or "tool_ref" in data or "tool_schema_digest" in data:
        params = data.get("parameters")
        if not isinstance(params, dict):
            raise ValueError("ToolPayload.parameters 须为对象")
        if "command" in params or "files" in params:
            raise ValueError("禁止 command 或自填 files")
        ids = params.get("verification_profile_ids")
    else:
        ids = data.get("verification_profile_ids")
    if not isinstance(ids, list) or not ids:
        raise ValueError("verification_profile_ids 必须为非空数组")
    out: list[str] = []
    for item in ids:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("verification_profile_ids 项必须为非空字符串")
        try:
            UUID(item)
        except ValueError as exc:
            raise ValueError("verification_profile_ids 须为 UUID") from exc
        out.append(item)
    return out


def _git_head(root: Path, *, timeout_seconds: int) -> str | None:
    try:
        completed = run_bounded_process(
            safe_git_argv("rev-parse", "HEAD"),
            cwd=root,
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=4096,
            max_stderr_bytes=4096,
            env=safe_git_env(),
        )
    except TimeoutExpired:
        return None
    if completed.returncode != 0:
        return None
    head = completed.stdout_prefix.decode(errors="replace").strip()
    if len(head) == 40 and all(c in "0123456789abcdef" for c in head):
        return head
    return None


def build_workspace_snapshot(
    workspace_root: Path,
    *,
    allowed_paths: list[str],
    max_files: int = 500,
    max_file_bytes: int = _MAX_CANDIDATE_FILE_BYTES,
    max_total_bytes: int = _MAX_CANDIDATE_TOTAL_BYTES,
    git_timeout_seconds: int = 30,
) -> tuple[dict, bytes, dict[str, bytes]]:
    """遍历 allowlist 内普通文件，按真实字节算 digest；不信调用方自报 hash。

    第三返回值为 digest→字节，供封存前入库（Auditor 物化依赖）。
    """
    root = workspace_root.resolve()
    if not root.is_dir():
        raise ValueError("工作区根不存在")
    if max_files <= 0 or max_file_bytes <= 0 or max_total_bytes <= 0:
        raise ValueError("候选快照限制必须为正数")
    entries: list[dict] = []
    blobs: dict[str, bytes] = {}
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".git/") or rel == ".git":
            continue
        try:
            resolve_workspace_path(root, rel, allowed_paths=allowed_paths)
        except PathRejected:
            continue
        fd, metadata = open_workspace_regular(root, rel, os.O_RDONLY)
        with os.fdopen(fd, "rb") as source:
            if metadata.st_size > max_file_bytes:
                raise ValueError(f"候选文件超过单文件上限 {max_file_bytes}")
            data = source.read(max_file_bytes + 1)
        if len(data) > max_file_bytes:
            raise ValueError(f"候选文件超过单文件上限 {max_file_bytes}")
        total_bytes += len(data)
        if total_bytes > max_total_bytes:
            raise ValueError(f"候选快照超过总字节上限 {max_total_bytes}")
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        # CandidateFileEntry.mode 用 git 风格
        mode = "100755" if metadata.st_mode & stat.S_IXUSR else "100644"
        entries.append({"path": rel, "digest": digest, "mode": mode})
        blobs[digest] = data
        if len(entries) > max_files:
            raise ValueError(f"快照文件数超过上限 {max_files}")
    if not entries:
        raise ValueError("候选快照不能为空")
    snapshot = {
        "files": entries,
        "git_commit": _git_head(root, timeout_seconds=git_timeout_seconds),
        "dependency_lock_digests": [],
        "submodules": [],
        "lfs_objects": [],
        "image_digests": [],
    }
    raw = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return snapshot, raw, blobs


def execute_seal_candidate_prepare(
    workspace_root: Path,
    input_bytes: bytes,
    *,
    allowed_paths: list[str],
    git_timeout_seconds: int = 30,
) -> SealCandidateResult:
    """采集快照；实际 POST seal 由 host 在持有 lease 时完成。"""
    started = datetime.now(UTC)
    try:
        profile_ids = parse_seal_candidate_input(input_bytes)
        snapshot, raw, blobs = build_workspace_snapshot(
            workspace_root,
            allowed_paths=allowed_paths,
            git_timeout_seconds=git_timeout_seconds,
        )
        finished = datetime.now(UTC)
        evidence = {
            "git_commit": snapshot["git_commit"],
            "file_count": len(snapshot["files"]),
            "snapshot_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "verification_profile_ids": profile_ids,
            "paths_preview": [f["path"] for f in snapshot["files"][:8]],
            "unique_blob_count": len(blobs),
        }
        content = json.dumps(evidence, ensure_ascii=False, separators=(",", ":")).encode()
        if len(content) > 900:
            evidence["paths_preview"] = evidence["paths_preview"][:3]
            content = json.dumps(evidence, ensure_ascii=False, separators=(",", ":")).encode()
        return SealCandidateResult(
            observed_outcome="SUCCEEDED",
            exit_code=0,
            timed_out=False,
            started_at=started,
            finished_at=finished,
            content=content,
            snapshot_bytes=raw,
            file_blobs=blobs,
            verification_profile_ids=profile_ids,
            git_commit=snapshot["git_commit"],
            file_count=len(snapshot["files"]),
            error=None,
        )
    except (ValueError, PathRejected, OSError) as exc:
        finished = datetime.now(UTC)
        return SealCandidateResult(
            observed_outcome="FAILED",
            exit_code=1,
            timed_out=False,
            started_at=started,
            finished_at=finished,
            content=None,
            snapshot_bytes=None,
            file_blobs={},
            verification_profile_ids=[],
            git_commit=None,
            file_count=0,
            error=str(exc),
        )
