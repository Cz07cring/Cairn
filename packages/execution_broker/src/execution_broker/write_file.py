"""write_file 工具：在沙箱工作区内受控写入（含前后摘要与 patch 证据）。

不开放任意 shell；路径门禁与 read_file 同形。≠ Goal DONE。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import unified_diff
from pathlib import Path

from .paths import (
    PathRejected,
    atomic_replace_workspace_regular,
    open_workspace_regular,
    resolve_workspace_path,
)


@dataclass(frozen=True)
class WriteFileResult:
    observed_outcome: str  # SUCCEEDED / FAILED
    exit_code: int
    timed_out: bool
    started_at: datetime
    finished_at: datetime
    """结果工件 JSON 字节（path/before/after/patch）；失败时可为 None。"""
    content: bytes | None
    path: str | None
    before_digest: str | None
    after_digest: str | None
    patch_text: str | None
    error: str | None


def _digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def parse_write_file_input(raw: bytes) -> tuple[str, str]:
    """解析 input：ToolPayload.parameters 或遗留 {path, content}。"""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("input artifact 不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("input 必须为 JSON 对象")  # noqa: TRY004
    if "parameters" in data or "tool_ref" in data or "tool_schema_digest" in data:
        params = data.get("parameters")
        if not isinstance(params, dict):
            raise ValueError("ToolPayload.parameters 须为对象")
        path = params.get("path")
        content = params.get("content")
    else:
        path = data.get("path")
        content = data.get("content")
    if not isinstance(path, str) or not path:
        raise ValueError("path 必须为非空字符串")
    if not isinstance(content, str):
        raise ValueError("content 必须为字符串")  # noqa: TRY004
    return path, content


def execute_write_file(
    workspace_root: Path,
    input_bytes: bytes,
    *,
    allowed_paths: list[str],
    protected_paths: list[str] | None = None,
    max_bytes: int = 1 * 1024 * 1024,
) -> WriteFileResult:
    """真实写入；失败以 FAILED 返回，不抛给调用方冒充成功。"""
    started = datetime.now(UTC)
    try:
        rel, text = parse_write_file_input(input_bytes)
        payload = text.encode("utf-8")
        if len(payload) > max_bytes:
            finished = datetime.now(UTC)
            return WriteFileResult(
                observed_outcome="FAILED",
                exit_code=1,
                timed_out=False,
                started_at=started,
                finished_at=finished,
                content=None,
                path=rel,
                before_digest=None,
                after_digest=None,
                patch_text=None,
                error="内容超过写入上限",
            )
        resolve_workspace_path(
            workspace_root,
            rel,
            allowed_paths=allowed_paths,
            protected_paths=list(protected_paths or ()),
        )
        metadata: os.stat_result | None
        try:
            fd, metadata = open_workspace_regular(
                workspace_root,
                rel,
                os.O_RDONLY,
                create_parents=True,
            )
        except FileNotFoundError:
            metadata = None
            before = b""
        else:
            with os.fdopen(fd, "rb") as source:
                if metadata.st_size > max_bytes:
                    finished = datetime.now(UTC)
                    return WriteFileResult(
                        observed_outcome="FAILED",
                        exit_code=1,
                        timed_out=False,
                        started_at=started,
                        finished_at=finished,
                        content=None,
                        path=rel,
                        before_digest=None,
                        after_digest=None,
                        patch_text=None,
                        error="现有文件超过写入上限",
                    )
                before = source.read(max_bytes + 1)
            if len(before) > max_bytes:
                finished = datetime.now(UTC)
                return WriteFileResult(
                    observed_outcome="FAILED",
                    exit_code=1,
                    timed_out=False,
                    started_at=started,
                    finished_at=finished,
                    content=None,
                    path=rel,
                    before_digest=None,
                    after_digest=None,
                    patch_text=None,
                    error="读取期间现有文件超过写入上限",
                )
        after = payload
        before_digest = _digest_bytes(before)
        after_digest = _digest_bytes(after)
        before_lines = before.decode("utf-8", errors="replace").splitlines(keepends=True)
        after_lines = after.decode("utf-8", errors="replace").splitlines(keepends=True)
        patch = "".join(
            unified_diff(
                before_lines,
                after_lines,
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
        )
        patch_bytes = patch.encode("utf-8")
        # 证据工件保持紧凑：完整 patch 过大时只留 digest + 预览，避免撑爆小对象仓限额。
        evidence: dict[str, object] = {
            "path": rel,
            "before_digest": before_digest,
            "after_digest": after_digest,
            "bytes_written": len(after),
            "patch_digest": _digest_bytes(patch_bytes),
        }
        if len(patch_bytes) <= 512:
            evidence["patch"] = patch
        else:
            evidence["patch_preview"] = patch[:400]
        evidence_content = json.dumps(
            evidence,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        # 原子替换是最后一个可能失败的外部效果；提交后不得再因证据计算失败误报 FAILED。
        atomic_replace_workspace_regular(
            workspace_root,
            rel,
            payload,
            expected=metadata,
            create_parents=True,
        )
        finished = datetime.now(UTC)
        return WriteFileResult(
            observed_outcome="SUCCEEDED",
            exit_code=0,
            timed_out=False,
            started_at=started,
            finished_at=finished,
            content=evidence_content,
            path=rel,
            before_digest=before_digest,
            after_digest=after_digest,
            patch_text=patch,
            error=None,
        )
    except (PathRejected, ValueError, OSError) as exc:
        finished = datetime.now(UTC)
        message = getattr(exc, "message", None) or str(exc)
        return WriteFileResult(
            observed_outcome="FAILED",
            exit_code=1,
            timed_out=False,
            started_at=started,
            finished_at=finished,
            content=None,
            path=None,
            before_digest=None,
            after_digest=None,
            patch_text=None,
            error=message,
        )
