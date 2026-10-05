"""有界采集子进程双流；完整输出只做增量摘要。"""

from __future__ import annotations

import hashlib
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from subprocess import DEVNULL, PIPE, Popen, TimeoutExpired
from threading import Event, Thread
from time import monotonic

_PIPE_EOF_GRACE_SECONDS = 0.5
_DRAIN_STOP_SECONDS = 1.0


@dataclass(frozen=True)
class BoundedProcessResult:
    returncode: int
    stdout_prefix: bytes
    stderr_prefix: bytes
    stdout_digest: str
    stderr_digest: str
    stdout_bytes: int
    stderr_bytes: int

    @property
    def stdout_truncated(self) -> bool:
        return self.stdout_bytes > len(self.stdout_prefix)

    @property
    def stderr_truncated(self) -> bool:
        return self.stderr_bytes > len(self.stderr_prefix)


def _drain_stream(
    stream,
    *,
    limit: int,
    state: dict[str, object],
    stop: Event,
) -> None:
    """持续排空一个 pipe；仅保留前缀，避免另一 pipe 写满造成死锁。"""
    prefix = bytearray()
    digest = hashlib.sha256()
    size = 0
    try:
        fd = stream.fileno()
        os.set_blocking(fd, False)
        while not stop.is_set():
            try:
                chunk = os.read(fd, 64 * 1024)
            except BlockingIOError:
                stop.wait(0.01)
                continue
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
            remaining = limit - len(prefix)
            if remaining > 0:
                prefix.extend(chunk[:remaining])
    except (OSError, ValueError) as exc:  # 管道异常须回传主线程，不能静默生成不完整证据
        state["error"] = exc
    finally:
        stream.close()
        state["prefix"] = bytes(prefix)
        state["digest"] = "sha256:" + digest.hexdigest()
        state["size"] = size


def _finish_drains(threads: tuple[Thread, Thread], stop: Event) -> bool:
    """等待正常 EOF；遗留写端不关闭时停止 drain，返回输出是否不完整。"""
    deadline = monotonic() + _PIPE_EOF_GRACE_SECONDS
    for thread in threads:
        thread.join(max(0.0, deadline - monotonic()))
    incomplete = any(thread.is_alive() for thread in threads)
    if incomplete:
        stop.set()
        deadline = monotonic() + _DRAIN_STOP_SECONDS
        for thread in threads:
            thread.join(max(0.0, deadline - monotonic()))
    if any(thread.is_alive() for thread in threads):
        raise OSError("无法停止子进程输出采集线程")
    return incomplete


def _kill_process_group(process: Popen[bytes]) -> None:
    """超时后终止测试进程及其后代，避免孤儿继续占用工作区。"""
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


def run_bounded_process(
    argv: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    env: dict[str, str] | None = None,
) -> BoundedProcessResult:
    """执行固定 argv，同时有界采集 stdout/stderr 并摘要完整字节流。"""
    if max_stdout_bytes < 0 or max_stderr_bytes < 0:
        raise ValueError("子进程输出保留上限不能为负数")

    process = Popen(
        argv,
        cwd=cwd,
        stdin=DEVNULL,
        stdout=PIPE,
        stderr=PIPE,
        env=env,
        start_new_session=os.name == "posix",
    )
    if process.stdout is None or process.stderr is None:
        _kill_process_group(process)
        process.wait()
        raise OSError("无法创建子进程输出管道")

    stdout_state: dict[str, object] = {}
    stderr_state: dict[str, object] = {}
    stop = Event()
    threads = (
        Thread(
            target=_drain_stream,
            kwargs={
                "stream": process.stdout,
                "limit": max_stdout_bytes,
                "state": stdout_state,
                "stop": stop,
            },
            name="broker-stdout-drain",
            daemon=True,
        ),
        Thread(
            target=_drain_stream,
            kwargs={
                "stream": process.stderr,
                "limit": max_stderr_bytes,
                "state": stderr_state,
                "stop": stop,
            },
            name="broker-stderr-drain",
            daemon=True,
        ),
    )
    for thread in threads:
        thread.start()

    try:
        returncode = process.wait(timeout=timeout_seconds)
    except TimeoutExpired:
        _kill_process_group(process)
        process.wait()
        _finish_drains(threads, stop)
        raise

    incomplete = _finish_drains(threads, stop)
    for state in (stdout_state, stderr_state):
        error = state.get("error")
        if isinstance(error, (OSError, ValueError)):
            raise OSError("读取子进程输出失败") from error
    if incomplete:
        raise OSError("子进程退出后输出管道未关闭，拒绝生成不完整证据")

    return BoundedProcessResult(
        returncode=int(returncode),
        stdout_prefix=bytes(stdout_state["prefix"]),
        stderr_prefix=bytes(stderr_state["prefix"]),
        stdout_digest=str(stdout_state["digest"]),
        stderr_digest=str(stderr_state["digest"]),
        stdout_bytes=int(stdout_state["size"]),
        stderr_bytes=int(stderr_state["size"]),
    )
