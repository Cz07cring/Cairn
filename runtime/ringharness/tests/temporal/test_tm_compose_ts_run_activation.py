"""compose Temporal：Python GoalWorkflow ↔ 真 TS RunActivation Worker。

拉起 `composeRunnerWorker.ts` 子进程注册 ring-runner；控制面用 stub Kernel Activities。
缺 env 时 RunActivation 诚实 PENDING_ENV（证明 TS Activity 已执行，≠ Cordis live）。
不可连 7233 → skip。禁 LEGACY fallback；marks_goal_done=False。
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest
from orchestration import RealTemporalClient
from orchestration.client import DEFAULT_TASK_QUEUE, RUNNER_TASK_QUEUE
from orchestration.temporal_workflows import GoalWorkflow
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

_ROOT = Path(__file__).resolve().parents[2]
_FIXED_ACTIVITY_ID = "act-ts-run-activation-1"


def _temporal_reachable(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _resolve_target() -> str | None:
    env_target = (os.environ.get("RING_TEMPORAL_TARGET") or "").strip()
    if env_target:
        return env_target
    if _temporal_reachable("127.0.0.1", 7233):
        return "127.0.0.1:7233"
    return None


@activity.defn(name="ensure_goal_delivery")
async def _stub_ensure(command_id: str, goal_id: str) -> dict[str, str | None]:
    return {
        "command_id": command_id,
        "workflow_id": f"goal-{goal_id}",
        "run_id": "run-ts-compose",
        "delivery_status": "ACKNOWLEDGED",
    }


@activity.defn(name="list_runtime_actions")
async def _stub_list(goal_id: str, owner_epoch: str) -> dict:
    return {
        "actions": [
            {
                "activity_id": _FIXED_ACTIVITY_ID,
                "action_id": _FIXED_ACTIVITY_ID,
                "goal_id": goal_id,
                "project_id": str(uuid4()),
                "owner_epoch": owner_epoch,
                "kind": "PLAN",
            }
        ],
        "wait_hint": None,
    }


@activity.defn(name="admit_plan_action")
async def _stub_admit(activity_id: str, idempotency_key: str) -> dict[str, str | None]:
    assert idempotency_key == f"goal-wf-admit:{activity_id}"
    return {
        "activity_id": activity_id,
        "attempt_id": str(uuid4()),
        "fencing_epoch": "1",
    }


@activity.defn(name="observe_activity_status")
async def _stub_observe(activity_id: str) -> dict[str, str]:
    # 本用例重点在 TS RunActivation 回执；观察直接终态以免拖长
    return {"activity_id": activity_id, "status": "SUCCEEDED", "kind": "PLAN"}


def test_compose_python_workflow_calls_ts_run_activation():
    """GoalWorkflow 派发 ring-runner → 真 TS RunActivation → PENDING_ENV。"""
    target = _resolve_target()
    if not target:
        pytest.skip("本机 Temporal（RING_TEMPORAL_TARGET 或 127.0.0.1:7233）不可连")

    host, _, port_s = target.partition(":")
    if not _temporal_reachable(host, int(port_s or "7233")):
        pytest.skip(f"Temporal target 不可达: {target}")

    runner_script = _ROOT / "apps/runner/src/temporal/composeRunnerWorker.ts"
    if not runner_script.is_file():
        pytest.skip(f"缺少 {runner_script}")

    env = {
        **os.environ,
        "RING_TEMPORAL_TARGET": target,
        # 故意不设 checkout / JWT：RunActivation 应诚实 PENDING_ENV
        "RING_HARNESS_CHECKOUT": "",
        "RING_CONTROL_URL": "",
        "RING_WORKER_JWT": "",
        "RING_RUNNER_LIVE_DISPATCH": "0",
    }
    proc = subprocess.Popen(
        [
            "pnpm",
            "--filter",
            "@ring/runner",
            "exec",
            "tsx",
            str(runner_script),
        ],
        cwd=str(_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # 等 worker ready 日志
        ready = False
        deadline = time.time() + 45
        buf = ""
        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                pytest.fail(f"TS runner worker 提前退出 code={proc.returncode}: {out}")
            assert proc.stdout is not None
            line = proc.stdout.readline()
            if line:
                buf += line
                if "compose-runner-worker-ready" in line:
                    ready = True
                    break
            else:
                time.sleep(0.05)
        if not ready:
            proc.send_signal(signal.SIGTERM)
            pytest.fail(f"TS runner worker 未就绪，日志片段: {buf[-2000:]}")

        goal_id = str(uuid4())
        command_id = str(uuid4())
        workflow_id = f"goal-{goal_id}-ts"

        async def _run() -> dict:
            temporal = await Client.connect(target)
            control_activities = [
                _stub_ensure,
                _stub_list,
                _stub_admit,
                _stub_observe,
            ]
            async with Worker(
                temporal,
                task_queue=DEFAULT_TASK_QUEUE,
                workflows=[GoalWorkflow],
                activities=control_activities,
            ):
                real = RealTemporalClient(target=target)
                run_id = await asyncio.to_thread(
                    real.ensure_started,
                    workflow_id,
                    command_id=command_id,
                    goal_id=goal_id,
                    owner_epoch="1",
                    observe_max_ticks=3,
                )
                assert run_id
                handle = temporal.get_workflow_handle(workflow_id)
                return await asyncio.wait_for(handle.result(), timeout=90)

        result = asyncio.run(_run())
        assert result["ok"] is True
        assert result["marks_goal_done"] is False
        assert result.get("pending_harness") is True
        ras = result.get("run_activation_results") or []
        assert ras, "应收到 TS RunActivation 结果"
        first = ras[0]
        assert first.get("status") == "PENDING_ENV"
        assert first.get("pending_harness") is True
        assert first.get("kind") == "PLAN"
        assert result["plan_terminal_statuses"][0]["status"] == "SUCCEEDED"
        assert RUNNER_TASK_QUEUE == "ring-runner"
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
