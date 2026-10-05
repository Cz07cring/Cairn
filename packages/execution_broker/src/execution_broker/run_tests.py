"""run_tests 工具：仅执行批准的 suite→argv 模板，禁止任意 shell。

证据含 argv/cwd/exit_code/stdout·stderr digest 与预览。≠ Goal DONE。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory

from .bounded_process import run_bounded_process

# suite → 固定 argv（相对 workspace cwd）；禁止 command 字符串。
# auditor：公开 + 隐藏；隐藏目录必须存在（防 Executor 冒充 Auditor）。
APPROVED_SUITES: dict[str, tuple[str, ...]] = {
    "public": (sys.executable, "-P", "-m", "pytest", "-q", "tests/"),
    "auditor": (
        sys.executable,
        "-P",
        "-m",
        "pytest",
        "-q",
        "tests/",
        "tests_hidden/",
    ),
}

_PASSTHROUGH_TEST_ENV = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "SYSTEMROOT",
    "WINDIR",
)


@dataclass(frozen=True)
class RunTestsResult:
    observed_outcome: str  # SUCCEEDED / FAILED
    exit_code: int
    timed_out: bool
    started_at: datetime
    finished_at: datetime
    content: bytes | None
    suite: str | None
    argv: tuple[str, ...]
    stdout_text: str
    stderr_text: str
    error: str | None


def resolve_suite_argv(suite: str, *, workspace_root: Path | None = None) -> tuple[str, ...]:
    argv = APPROVED_SUITES.get(suite)
    if argv is None:
        raise ValueError(f"未知 suite：{suite}")
    if suite == "auditor":
        if workspace_root is None:
            raise ValueError("auditor suite 需要 workspace_root")
        hidden = workspace_root.resolve() / "tests_hidden"
        if not hidden.is_dir():
            raise ValueError("auditor suite 要求 tests_hidden 目录存在")
    return argv


def parse_run_tests_input(raw: bytes) -> str:
    """解析 input：仅允许 parameters.suite / 遗留 {suite}；拒绝 command。"""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("input artifact 不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("input 必须为 JSON 对象")  # noqa: TRY004
    if "command" in data or (
        isinstance(data.get("parameters"), dict) and "command" in data["parameters"]
    ):
        raise ValueError("禁止自由 command；仅允许批准 suite")
    if "parameters" in data or "tool_ref" in data or "tool_schema_digest" in data:
        params = data.get("parameters")
        if not isinstance(params, dict):
            raise ValueError("ToolPayload.parameters 须为对象")
        suite = params.get("suite")
    else:
        suite = data.get("suite")
    if not isinstance(suite, str) or not suite:
        raise ValueError("suite 必须为非空字符串")
    if suite not in APPROVED_SUITES:
        raise ValueError(f"未知 suite：{suite}")
    return suite


def _isolated_test_environment(root: Path) -> dict[str, str]:
    """构造候选测试最小环境；不得继承 Broker 的 secret 与用户配置目录。"""
    home = root / "home"
    temporary = root / "tmp"
    cache = root / "cache"
    config = root / "config"
    for directory in (home, temporary, cache, config):
        directory.mkdir(mode=0o700)
    env = {key: os.environ[key] for key in _PASSTHROUGH_TEST_ENV if key in os.environ}
    env.update(
        {
            "HOME": str(home),
            "TMPDIR": str(temporary),
            "TMP": str(temporary),
            "TEMP": str(temporary),
            "XDG_CACHE_HOME": str(cache),
            "XDG_CONFIG_HOME": str(config),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONSAFEPATH": "1",
            "PYTHONUTF8": "1",
        }
    )
    return env


def execute_run_tests(
    workspace_root: Path,
    input_bytes: bytes,
    *,
    timeout_seconds: int = 120,
    max_capture_bytes: int = 256_000,
) -> RunTestsResult:
    """在 workspace cwd 下跑批准 argv；超时/启动失败 → FAILED。"""
    started = datetime.now(UTC)
    empty_argv: tuple[str, ...] = ()
    try:
        suite = parse_run_tests_input(input_bytes)
        root = workspace_root.resolve()
        if not root.is_dir():
            raise ValueError("工作区根不存在")
        argv = resolve_suite_argv(suite, workspace_root=root)
        with TemporaryDirectory(prefix="ring-run-tests-") as isolated_root:
            completed = run_bounded_process(
                list(argv),
                cwd=root,
                timeout_seconds=timeout_seconds,
                max_stdout_bytes=max_capture_bytes,
                max_stderr_bytes=max_capture_bytes,
                env=_isolated_test_environment(Path(isolated_root)),
            )
        stdout = completed.stdout_prefix.decode("utf-8", errors="replace")
        stderr = completed.stderr_prefix.decode("utf-8", errors="replace")
        finished = datetime.now(UTC)
        # 证据 argv 归一化解释器路径，避免本机绝对路径撑破 1KiB objects 限额
        evidence_argv = ["python", *argv[1:]] if argv else []
        evidence = {
            "suite": suite,
            "argv": evidence_argv,
            "cwd": ".",
            "exit_code": completed.returncode,
            "timed_out": False,
            "stdout_digest": completed.stdout_digest,
            "stderr_digest": completed.stderr_digest,
            "stdout_bytes": completed.stdout_bytes,
            "stderr_bytes": completed.stderr_bytes,
            "stdout_truncated": completed.stdout_truncated,
            "stderr_truncated": completed.stderr_truncated,
            # 红测给更长预览，便于模型定位失败断言（仍受 objects 1KiB 限额约束）
            "stdout_preview": stdout[:400] if completed.returncode != 0 else stdout[:120],
            "stderr_preview": stderr[:160] if completed.returncode != 0 else stderr[:80],
        }
        content = json.dumps(evidence, ensure_ascii=False, separators=(",", ":")).encode()
        if len(content) > 900:
            evidence["stdout_preview"] = (
                stdout[:160] if completed.returncode != 0 else stdout[:40]
            )
            evidence["stderr_preview"] = (
                stderr[:80] if completed.returncode != 0 else stderr[:20]
            )
            content = json.dumps(evidence, ensure_ascii=False, separators=(",", ":")).encode()
        return RunTestsResult(
            observed_outcome="SUCCEEDED",
            exit_code=int(completed.returncode),
            timed_out=False,
            started_at=started,
            finished_at=finished,
            content=content,
            suite=suite,
            argv=argv,
            stdout_text=stdout,
            stderr_text=stderr,
            error=None,
        )
    except ValueError as exc:
        finished = datetime.now(UTC)
        return RunTestsResult(
            observed_outcome="FAILED",
            exit_code=1,
            timed_out=False,
            started_at=started,
            finished_at=finished,
            content=None,
            suite=None,
            argv=empty_argv,
            stdout_text="",
            stderr_text="",
            error=str(exc),
        )
    except TimeoutExpired:
        finished = datetime.now(UTC)
        return RunTestsResult(
            observed_outcome="FAILED",
            exit_code=124,
            timed_out=True,
            started_at=started,
            finished_at=finished,
            content=None,
            suite=None,
            argv=empty_argv,
            stdout_text="",
            stderr_text="",
            error="run_tests 超时",
        )
    except OSError as exc:
        finished = datetime.now(UTC)
        return RunTestsResult(
            observed_outcome="FAILED",
            exit_code=1,
            timed_out=False,
            started_at=started,
            finished_at=finished,
            content=None,
            suite=None,
            argv=empty_argv,
            stdout_text="",
            stderr_text="",
            error=str(exc),
        )
