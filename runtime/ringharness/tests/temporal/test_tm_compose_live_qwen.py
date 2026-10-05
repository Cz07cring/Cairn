"""compose：真 Kernel + 真 TS RunActivation + Cordis + live 本地 Qwen。

独立 uvicorn（Settings 内嵌测试桶），避免与 TestClient 共用 lifespan 清掉 objects。
路径：TEMPORAL START → Relay → GoalWorkflow → admit → RunActivation
→ Cordis 零工具 → Control dispatch(Qwen) → PlanCreate → observe SUCCEEDED。
Goal≠DONE。缺 checkout/Cordis/Qwen/Temporal → skip。
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
import uvicorn
from control_api.app import create_app
from control_api.settings import Settings
from control_kernel.storage.goals import set_goal_orchestration_backend
from control_kernel.storage.orchestration import get_binding_for_goal, get_delivery_for_command
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
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
from pydantic import SecretStr
from sqlalchemy import text
from temporalio.client import Client
from temporalio.worker import Worker
from test_claims import _drain_ready_plans, _register_worker
from test_orchestration_backend import _start_temporal_goal

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CHECKOUT = _ROOT / ".runtime" / "deepseek-harness"


def _temporal_reachable(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def _terminate_stale_goal_workflows(target: str) -> int:
    """测前清掉残留 GoalWorkflow，避免其 admit 触发全局 expire 清掉本测租约。"""
    temporal = await Client.connect(target)
    n = 0
    async for w in temporal.list_workflows('WorkflowType="GoalWorkflow" AND ExecutionStatus="Running"'):
        handle = temporal.get_workflow_handle(w.id, run_id=w.run_id)
        await handle.terminate(reason="live-qwen test preempt stale workflow")
        n += 1
    return n


def _resolve_target() -> str | None:
    env_target = (os.environ.get("RING_TEMPORAL_TARGET") or "").strip()
    if env_target:
        return env_target
    if _temporal_reachable("127.0.0.1", 7233):
        return "127.0.0.1:7233"
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _qwen_ready() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8001), timeout=1.0):
            return True
    except OSError:
        return False


class _AppClient:
    """httpx + app 句柄，供 _ready_project 写 client.app.state。"""

    def __init__(self, client: httpx.Client, app):
        self._client = client
        self.app = app

    def post(self, *args, **kwargs):
        return self._client.post(*args, **kwargs)

    def get(self, *args, **kwargs):
        return self._client.get(*args, **kwargs)


def test_compose_live_qwen_run_activation(objects, monkeypatch):
    """真链路 live Qwen PLAN；Goal≠DONE。"""
    target = _resolve_target()
    if not target:
        pytest.skip("本机 Temporal 不可连")
    host, _, port_s = target.partition(":")
    if not _temporal_reachable(host, int(port_s or "7233")):
        pytest.skip(f"Temporal target 不可达: {target}")

    checkout = (os.environ.get("RING_HARNESS_CHECKOUT") or "").strip() or str(
        _DEFAULT_CHECKOUT
    )
    cordis = Path(checkout) / "vendor" / "cordis" / "lib" / "index.js"
    if not cordis.is_file():
        pytest.skip(f"Cordis 未构建: {cordis}")

    # qwen.env + chat.env（DeepSeek 覆盖时不要求本机 :8001）
    import sys as _sys

    _sys.path.insert(0, str(_ROOT / "scripts"))
    from runtime_env import apply_model_runtime_env, chat_provider_label

    _merged = {**os.environ}
    apply_model_runtime_env(_ROOT, _merged)
    for k, v in _merged.items():
        if k.startswith(("RING_LOCAL_QWEN_", "RING_CHAT_")):
            monkeypatch.setenv(k, v)
    provider = chat_provider_label(_merged)
    base = (_merged.get("RING_LOCAL_QWEN_BASE") or "").rstrip("/")
    local_qwen = "127.0.0.1:8001" in base or "localhost:8001" in base
    if local_qwen and not _qwen_ready():
        pytest.skip("本地 Qwen :8001 不可连")
    if not (_merged.get("RING_LOCAL_QWEN_API_KEY") or "").strip():
        pytest.skip("缺少 RING_LOCAL_QWEN_API_KEY（qwen.env 或 chat.env）")
    # Control 进程内 dispatch 读此值
    monkeypatch.setenv(
        "RING_LOCAL_QWEN_TIMEOUT",
        _merged.get("RING_LOCAL_QWEN_TIMEOUT") or ("180" if local_qwen else "120"),
    )
    print(f"live chat provider={provider} base={base}")
    if provider != "local-qwen" and not local_qwen:
        # live 云连接器须显式打开测试云合同，避免普通夹具被宿主 chat.env 污染
        monkeypatch.setenv("RING_TEST_ALLOW_CLOUD", "1")
        monkeypatch.setenv("RING_CLOUD_MODE", "PREAUTHORIZED")

    stale = asyncio.run(_terminate_stale_goal_workflows(target))
    if stale:
        print(f"preempted {stale} stale GoalWorkflow(s)")

    runner_script = _ROOT / "apps/runner/src/temporal/composeRunnerWorker.ts"
    if not runner_script.is_file():
        pytest.skip(f"缺少 {runner_script}")

    _store, s3_client, bucket = objects
    # fixture store 默认 max_bytes=1024，context-compile 会撑爆；联调改用同桶大上限
    from evidence_ledger.objects import S3Objects

    large_store = S3Objects(s3_client, bucket, max_bytes=16 * 1024 * 1024)
    objects_for_setup = (large_store, s3_client, bucket)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )

    def token(subject, roles):
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "sub": subject,
                "roles": roles,
                "project_ids": [],
                "iss": "ring-test",
                "aud": "ring-api",
                "iat": now,
                "exp": now + timedelta(minutes=30),
            },
            private,
            algorithm="RS256",
        )

    # 禁止 lifespan 误读宿主机 RING_S3_*；用 fixture 桶
    monkeypatch.delenv("RING_S3_ENDPOINT", raising=False)
    monkeypatch.delenv("RING_S3_BUCKET", raising=False)
    monkeypatch.delenv("RING_S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("RING_S3_SECRET_KEY", raising=False)

    settings = Settings(
        database_url=os.environ["RING_TEST_DATABASE_URL"],
        jwt_public_key=public,
        jwt_issuer="ring-test",
        jwt_audience="ring-api",
        repository_refs=["fixture"],
        cursor_secret="test-only-cursor-secret-at-least-32-bytes",
        local_qwen_base=os.environ.get("RING_LOCAL_QWEN_BASE", "http://127.0.0.1:8001"),
        local_qwen_api_key=os.environ.get("RING_LOCAL_QWEN_API_KEY"),
        cloud_mode=(
            os.environ.get("RING_CLOUD_MODE", "DENY")
            if os.environ.get("RING_TEST_ALLOW_CLOUD") == "1"
            else "DENY"
        ),
        s3_endpoint=os.environ["RING_TEST_S3_ENDPOINT"],
        s3_bucket=bucket,
        s3_access_key=SecretStr(os.environ["RING_TEST_S3_ACCESS_KEY"]),
        s3_secret_key=SecretStr(os.environ["RING_TEST_S3_SECRET_KEY"]),
        artifact_max_bytes=16 * 1024 * 1024,
    )
    app = create_app(settings)
    control_port = _free_port()
    control_url = f"http://127.0.0.1:{control_port}"
    config = uvicorn.Config(app, host="127.0.0.1", port=control_port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    httpx_client = httpx.Client(
        base_url=control_url, timeout=300.0, trust_env=False
    )
    http = _AppClient(httpx_client, app)
    engine = None
    goal = None
    proc = None
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                r = httpx_client.get("/health/live")
                if r.status_code == 200:
                    break
            except (httpx.HTTPError, OSError):
                time.sleep(0.05)
        else:
            pytest.fail("Control uvicorn 未就绪")

        # lifespan 可能已建仓；统一钉 large_store，且让 _ready_project 也装它（勿用 1KiB fixture）
        app.state.objects = large_store
        # 每次请求强制挂回（防 lifespan/竞态把 objects 置 None → context-compile 503）
        app.state._force_objects = large_store

        api = (http, token)
        _http_client, _tok, _auth, goal, plan, command, engine = _start_temporal_goal(
            api, objects_for_setup
        )
        app.state.objects = large_store
        app.state._force_objects = large_store
        assert app.state.objects is not None
        subject = str(uuid4())
        _register_worker(subject)
        worker_jwt = token(subject, ["worker"])

        monkeypatch.setenv("RING_WORKFLOW_WORKER_SUBJECT", subject)
        monkeypatch.setenv("RING_TEMPORAL_TARGET", target)
        monkeypatch.setenv("RING_DATABASE_URL", os.environ["RING_TEST_DATABASE_URL"])

        ts_env = {
            **os.environ,
            "RING_TEMPORAL_TARGET": target,
            "RING_HARNESS_CHECKOUT": checkout,
            "RING_CONTROL_URL": control_url,
            "RING_WORKER_JWT": worker_jwt,
            "RING_RUNNER_LIVE_DISPATCH": "1",
            # 本机 Qwen 首 token 可能很慢；默认 180，可用环境覆盖
            "RING_LOCAL_QWEN_TIMEOUT": os.environ.get("RING_LOCAL_QWEN_TIMEOUT")
            or "180",
            "RING_LOCAL_QWEN_MODEL": (
                os.environ.get("RING_LOCAL_QWEN_MODEL")
                or "Qwen3.8-Flash-Next-Uncensored-Mixed-omlx"
            ),
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

        ready = False
        buf = ""
        dl = time.time() + 45
        while time.time() < dl:
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
            pytest.fail(f"TS runner worker 未就绪: {buf[-2000:]}")

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
                    receipt = await asyncio.to_thread(
                        ensure_workflow,
                        engine,
                        binding,
                        UUID(command["id"]),
                        real,
                        observe_max_ticks=30,
                    )
                    assert receipt.delivery_status == "ACKNOWLEDGED"
                    handle = temporal.get_workflow_handle(binding.workflow_id)
                    try:
                        return await asyncio.wait_for(handle.result(), timeout=600)
                    except TimeoutError:
                        desc = await handle.describe()
                        fail = ""
                        async for e in handle.fetch_history_events():
                            if e.HasField("activity_task_failed_event_attributes"):
                                f = e.activity_task_failed_event_attributes.failure
                                fail = (f.message or "")[:500]
                        raise TimeoutError(
                            f"GoalWorkflow 超时 status={desc.status} last_activity_fail={fail!r}"
                        ) from None

        result = asyncio.run(_run())
        assert result["ok"] is True
        assert result["marks_goal_done"] is False
        ras = result.get("run_activation_results") or []
        assert ras, "应收到 TS RunActivation"
        assert ras[0].get("status") == "ACTIVATION_SUBMITTED", ras[0]
        assert ras[0].get("pending_harness") is False
        terminals = result.get("plan_terminal_statuses") or []
        assert terminals
        assert terminals[0]["activity_id"] == plan_id
        assert terminals[0]["status"] == "SUCCEEDED"

        with engine.connect() as db:
            g = (
                db.execute(
                    text(
                        "SELECT status, orchestration_backend FROM goals WHERE id=:id"
                    ),
                    {"id": goal["id"]},
                )
                .mappings()
                .one()
            )
            delivery = get_delivery_for_command(
                db, UUID(command["id"]), "ENSURE_WORKFLOW"
            )
        assert g["orchestration_backend"] == "TEMPORAL"
        assert g["status"] != "DONE"
        assert g["status"] == "RUNNING"
        assert delivery is not None
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
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        server.should_exit = True
        thread.join(timeout=5)
        httpx_client.close()
        if engine is not None and goal is not None:
            _drain_ready_plans(http, token)
            set_goal_orchestration_backend(
                engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True
            )
            engine.dispose()
        # 关闭 lifespan 建的引擎（若仍在）
        eng = getattr(app.state, "engine", None)
        if eng is not None:
            eng.dispose()
