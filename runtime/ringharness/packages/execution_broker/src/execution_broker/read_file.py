"""read_file 工具：在沙箱工作区内真实读取文件字节。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .paths import PathRejected, open_workspace_regular, resolve_workspace_path


@dataclass(frozen=True)
class ReadFileResult:
    observed_outcome: str  # SUCCEEDED / FAILED
    exit_code: int
    timed_out: bool
    started_at: datetime
    finished_at: datetime
    content: bytes | None
    path: str | None
    error: str | None


def parse_read_file_input(raw: bytes) -> str:
    """解析 input artifact：ToolPayload.parameters.path 或遗留 {"path":"..."}。"""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("input artifact 不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("input 必须为 JSON 对象")  # noqa: TRY004
    # ToolPayload：path 在 parameters 内
    if "parameters" in data or "tool_ref" in data or "tool_schema_digest" in data:
        params = data.get("parameters")
        if not isinstance(params, dict):
            raise ValueError("ToolPayload.parameters 须为对象")
        path = params.get("path")
    else:
        if "path" not in data:
            raise ValueError("input 缺少 path 字段")
        path = data["path"]
    if not isinstance(path, str) or not path:
        raise ValueError("path 必须为非空字符串")
    return path


def execute_read_file(
    workspace_root: Path,
    input_bytes: bytes,
    *,
    allowed_paths: list[str],
    max_bytes: int = 8 * 1024 * 1024,
) -> ReadFileResult:
    """真实 open/read；失败以 FAILED + 非零 exit 返回，不抛给调用方冒充成功。"""
    started = datetime.now(UTC)
    try:
        rel = parse_read_file_input(input_bytes)
        resolve_workspace_path(workspace_root, rel, allowed_paths=allowed_paths)
        try:
            fd, metadata = open_workspace_regular(
                workspace_root,
                rel,
                os.O_RDONLY,
            )
        except FileNotFoundError:
            finished = datetime.now(UTC)
            return ReadFileResult(
                observed_outcome="FAILED",
                exit_code=2,
                timed_out=False,
                started_at=started,
                finished_at=finished,
                content=None,
                path=rel,
                error="文件不存在",
            )
        with os.fdopen(fd, "rb") as source:
            if metadata.st_size > max_bytes:
                finished = datetime.now(UTC)
                return ReadFileResult(
                    observed_outcome="FAILED",
                    exit_code=3,
                    timed_out=False,
                    started_at=started,
                    finished_at=finished,
                    content=None,
                    path=rel,
                    error="文件超过读取上限",
                )
            content = source.read(max_bytes + 1)
            if len(content) > max_bytes:
                raise OSError("读取期间文件超过上限")
        finished = datetime.now(UTC)
        return ReadFileResult(
            observed_outcome="SUCCEEDED",
            exit_code=0,
            timed_out=False,
            started_at=started,
            finished_at=finished,
            content=content,
            path=rel,
            error=None,
        )
    except (PathRejected, ValueError, OSError) as exc:
        finished = datetime.now(UTC)
        message = getattr(exc, "message", None) or str(exc)
        return ReadFileResult(
            observed_outcome="FAILED",
            exit_code=1,
            timed_out=False,
            started_at=started,
            finished_at=finished,
            content=None,
            path=None,
            error=message,
        )
