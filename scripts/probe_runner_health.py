"""只读验证本机持久 Runner 进程与 Temporal ``ring-runner`` poller。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client
from temporalio.service import RPCError


@dataclass(frozen=True)
class ProcessView:
    pid: int
    ppid: int
    command: str


def _process_view(pid: int) -> ProcessView | None:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "ppid=,command="],
        check=False,
        capture_output=True,
        text=True,
    )
    line = result.stdout.strip()
    if result.returncode != 0 or not line:
        return None
    raw_ppid, _, command = line.partition(" ")
    return ProcessView(pid=pid, ppid=int(raw_ppid), command=command.strip())


def _is_descendant(pid: int, ancestor_pid: int) -> bool:
    seen: set[int] = set()
    current = pid
    while current > 1 and current not in seen:
        if current == ancestor_pid:
            return True
        seen.add(current)
        view = _process_view(current)
        if view is None:
            return False
        current = view.ppid
    return current == ancestor_pid


def _pid_from_identity(identity: str) -> int | None:
    raw_pid, separator, _host = identity.partition("@")
    if not separator or not raw_pid.isdecimal():
        return None
    return int(raw_pid)


async def _probe(args: argparse.Namespace) -> tuple[int, dict]:
    try:
        runner_pid = args.pid
        if runner_pid is None:
            runner_pid = int(args.pid_file.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError) as exc:
        return 1, {
            "status": "UNAVAILABLE",
            "reason": "RUNNER_PID_UNAVAILABLE",
            "detail": type(exc).__name__,
        }

    runner = _process_view(runner_pid)
    if runner is None:
        return 1, {
            "status": "UNAVAILABLE",
            "reason": "RUNNER_PROCESS_MISSING",
            "runner_pid": runner_pid,
        }
    if "@ring/runner" not in runner.command or "temporal-worker" not in runner.command:
        return 1, {
            "status": "UNAVAILABLE",
            "reason": "RUNNER_PROCESS_IDENTITY_MISMATCH",
            "runner": asdict(runner),
        }

    try:
        client = await Client.connect(args.target, namespace=args.namespace)
        response = await client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(
                namespace=args.namespace,
                task_queue=TaskQueue(name=args.task_queue),
                task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
            )
        )
    except (OSError, RPCError, RuntimeError, ValueError) as exc:
        return 1, {
            "status": "UNAVAILABLE",
            "reason": "TEMPORAL_DESCRIBE_FAILED",
            "detail": type(exc).__name__,
            "runner": asdict(runner),
        }

    now = datetime.now(UTC)
    pollers = []
    linked_recent = []
    for poller in response.pollers:
        last_access = poller.last_access_time.ToDatetime(tzinfo=UTC)
        age_seconds = max(0.0, (now - last_access).total_seconds())
        poller_pid = _pid_from_identity(poller.identity)
        linked = poller_pid is not None and _is_descendant(poller_pid, runner_pid)
        recent = age_seconds <= args.max_poller_age_seconds
        view = {
            "identity": poller.identity,
            "last_access_at": last_access.isoformat().replace("+00:00", "Z"),
            "age_seconds": round(age_seconds, 3),
            "linked_to_runner": linked,
            "recent": recent,
        }
        pollers.append(view)
        if linked and recent:
            linked_recent.append(view)

    if not linked_recent:
        return 1, {
            "status": "UNAVAILABLE",
            "reason": "RUNNER_POLLER_MISSING_OR_STALE",
            "runner": asdict(runner),
            "task_queue": args.task_queue,
            "pollers": pollers,
        }
    return 0, {
        "status": "HEALTHY",
        "reason": None,
        "runner": asdict(runner),
        "temporal_target": args.target,
        "namespace": args.namespace,
        "task_queue": args.task_queue,
        "pollers": pollers,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="验证持久 Runner 进程与 Temporal Activity poller"
    )
    parser.add_argument(
        "--pid-file",
        type=Path,
        default=Path(".runtime/joint/runner.pid"),
    )
    parser.add_argument("--pid", type=int)
    parser.add_argument(
        "--target",
        default=os.environ.get("RING_TEMPORAL_TARGET", "127.0.0.1:7233"),
    )
    parser.add_argument(
        "--namespace",
        default=os.environ.get("RING_TEMPORAL_NAMESPACE", "default"),
    )
    parser.add_argument("--task-queue", default="ring-runner")
    parser.add_argument("--max-poller-age-seconds", type=float, default=90.0)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.max_poller_age_seconds <= 0:
        raise SystemExit("--max-poller-age-seconds 必须大于 0")
    exit_code, result = asyncio.run(_probe(args))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
