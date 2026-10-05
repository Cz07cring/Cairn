"""Broker 固定 Git 启动参数：仓库配置不能扩张为宿主回调执行。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_SAFE_GIT_CONFIG = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "submodule.recurse=false",
)


def _resolve_git_executable() -> str | None:
    """在 Broker 模块装载时固定部署环境中的 Git，拒绝后续 PATH 劫持。"""
    discovered = shutil.which("git")
    if not discovered:
        return None
    try:
        resolved = Path(discovered).resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return None
    return str(resolved)


_GIT_EXECUTABLE = _resolve_git_executable()


def safe_git_argv(*args: str) -> list[str]:
    """构造禁用 hook/fsmonitor/submodule 回调的固定 Git argv。"""
    if _GIT_EXECUTABLE is None:
        raise RuntimeError("Broker 启动环境缺少受信 Git 可执行文件")
    return [_GIT_EXECUTABLE, *_SAFE_GIT_CONFIG, *args]


def safe_git_env(source: dict[str, str] | None = None) -> dict[str, str]:
    """移除可重定向仓库、配置或 helper 的父进程 Git 环境。"""
    env = dict(os.environ if source is None else source)
    for key in tuple(env):
        if key.startswith("GIT_"):
            del env[key]
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
        }
    )
    return env
