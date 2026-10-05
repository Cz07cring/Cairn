"""compose：真 Kernel Activities + 真 TS RunActivation（缺 env → PENDING_ENV）。

路径：TEMPORAL START → Relay → GoalWorkflow
→ ensure/list/admit（真 Kernel）→ ring-runner 真 RunActivation → observe。
缺 checkout/JWT 时诚实 PENDING_ENV；PLAN 库内未必终态 → 允许 OBSERVE_TIMEOUT。
断言 marks_goal_done=False、backend TEMPORAL、全局 claim 空。

不可连 7233 → skip。禁 LEGACY fallback。
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from control_kernel.storage.goals import set_goal_orchestration_backend
from control_kernel.storage.orchestration import get_binding_for_goal, get_delivery_for_command
from orchestration import OrchestrationBindingContent, RealTemporalClient, ensure_workflow
from orchestration.client import DEFAULT_TASK_QUEUE, RUNNER_TASK_QUEUE
from orchestration.kernel_activities import (
    admit_execute_action,
    admit_plan_action,
    configure_kernel_activity_ports,
    ensure_goal_delivery,
    list_runtime_actions,
    observe_activity_status,
    observe_carried_activation_activity_status,
    ping_kernel,
)
from orchestration.temporal_workflows import GoalWorkflow
from sqlalchemy import text
from temporalio.client import Client
from temporalio.worker import Worker
from test_claims import _drain_ready_plans, _register_worker
from test_orchestration_backend import _start_temporal_goal

_ROOT = Path(__file__).resolve().parents[2]


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


def test_compose_real_kernel_and_ts_run_activation(api, objects, monkeypatch):
    """真 Kernel + 真 TS：PENDING_ENV / OBSERVE_TIMEOUT，Goal≠DONE。"""
    target = _resolve_target()
    if not target:
        pytest.skip("本机 Temporal（RING_TEMPORAL_TARGET 或 127.0.0.1:7233）不可连")

    host, _, port_s = target.partition(":")
    if not _temporal_reachable(host, int(port_s or "7233")):
        pytest.skip(f"Temporal target 不可达: {target}")

    runner_script = _ROOT / "apps/runner/src/temporal/composeRunnerWorker.ts"
    if not runner_script.is_file():
        pytest.skip(f"缺少 {runner_script}")

    http, token, _auth, goal, plan, command, engine = _start_temporal_goal(api, objects)
    subject = str(uuid4())
    _register_worker(subject)
    monkeypatch.setenv("RING_WORKFLOW_WORKER_SUBJECT", subject)
    monkeypatch.setenv("RING_TEMPORAL_TARGET", target)
    monkeypatch.setenv("RING_DATABASE_URL", os.environ["RING_TEST_DATABASE_URL"])

    ts_env = {
        **os.environ,
        "RING_TEMPORAL_TARGET": target,
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
        env=ts_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    real = RealTemporalClient(target=target)
    configure_kernel_activity_ports(engine=engine, temporal_client=real)
    control_activities = [
        ensure_goal_delivery,
        list_runtime_actions,
        admit_plan_action,
        admit_execute_action,
        observe_activity_status,
        observe_carried_activation_activity_status,
        ping_kernel,
    ]
    plan_id = plan["id"]

    try:
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

        async def _run() -> dict:
            temporal = await Client.connect(target)
            with ThreadPoolExecutor(max_workers=8) as pool:
                async with Worker(
                    temporal,
                    task_queue=DEFAULT_TASK_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=control_activities,
                    activity_executor=pool,
                ):
                    with engine.connect() as db:
                        row = get_binding_for_goal(db, UUID(goal["id"]))
                    assert row is not None
                    binding = OrchestrationBindingContent(
                        project_id=row["project_id"],
                        goal_id=row["goal_id"],
                        budget_scope_id=row["budget_scope_id"],
                        backend=row["backend"],
                        owner_epoch=str(row["owner_epoch"]),
                        namespace=row["namespace"],
                        workflow_id=row["workflow_id"],
                        active_run_id=row["active_run_id"],
                        worker_build_id=row["worker_build_id"],
                        contract_digest=row["contract_digest"],
                    )
                    # 观察轮次小：TS PENDING_ENV 不写库终态 → 预期 OBSERVE_TIMEOUT
                    receipt = await asyncio.to_thread(
                        ensure_workflow,
                        engine,
                        binding,
                        UUID(command["id"]),
                        real,
                        observe_max_ticks=2,
                    )
                    assert receipt.delivery_status == "ACKNOWLEDGED"
                    handle = temporal.get_workflow_handle(binding.workflow_id)
                    return await asyncio.wait_for(handle.result(), timeout=120)

        result = asyncio.run(_run())
        assert result["ok"] is True
        assert result["marks_goal_done"] is False
        assert result.get("pending_harness") is True
        ras = result.get("run_activation_results") or []
        assert ras, "应收到真 TS RunActivation 结果"
        assert ras[0].get("status") == "PENDING_ENV"
        assert ras[0].get("pending_harness") is True
        assert ras[0].get("kind") == "PLAN"
        terminals = result.get("plan_terminal_statuses") or []
        assert terminals
        assert terminals[0]["activity_id"] == plan_id
        assert terminals[0]["status"] in ("OBSERVE_TIMEOUT", "SUCCEEDED", "FAILED")
        # 诚实路径：未接线 Harness 时不应靠 stub 写成 SUCCEEDED
        assert terminals[0]["status"] == "OBSERVE_TIMEOUT"

        with engine.connect() as db:
            g = (
                db.execute(
                    text("SELECT status, orchestration_backend FROM goals WHERE id=:id"),
                    {"id": goal["id"]},
                )
                .mappings()
                .one()
            )
            delivery = get_delivery_for_command(db, UUID(command["id"]), "ENSURE_WORKFLOW")
        assert g["orchestration_backend"] == "TEMPORAL"
        assert g["status"] != "DONE"
        assert delivery is not None
        assert delivery["delivery_status"] == "ACKNOWLEDGED"
        assert RUNNER_TASK_QUEUE == "ring-runner"

        claimed = http.post(
            "/internal/v1/claims",
            json={"kinds": ["PLAN"], "capabilities": []},
            headers={
                "Authorization": "Bearer " + token(subject, ["worker"]),
                "Idempotency-Key": str(uuid4()),
            },
        )
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["data"]["lease"] is None
    finally:
        configure_kernel_activity_ports(engine=None, temporal_client=None)
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        _drain_ready_plans(http, token)
        set_goal_orchestration_backend(engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True)
        engine.dispose()
