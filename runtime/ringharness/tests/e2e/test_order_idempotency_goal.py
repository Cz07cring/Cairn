"""订单幂等 Goal · E2E-1 分缝。

已覆盖：独立 Broker 进程；官方 AgentLoop × ToolResult 第二轮。
本文件继续补 Temporal START + PLAN 零工具。≠ Goal DONE；≠ E2E-2+。
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
import uvicorn
from control_api.app import create_app
from control_api.settings import Settings
from control_kernel.storage.artifacts import Artifacts
from control_kernel.storage.goals import set_goal_orchestration_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from orchestration import FakeTemporalClient, OrchestrationBindingContent, ensure_workflow
from pydantic import SecretStr
from sqlalchemy import text
from test_claims import _drain_ready, _drain_ready_plans, _register_worker
from test_goals import _ready_project
from test_orchestration_backend import _start_temporal_goal
from test_plans import _plan_body

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import seed_business_e2e as seed

_ORDER_ALLOWED = ["order_service/**", "tests/**", "pyproject.toml", "FIXED_INPUT.json"]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _HttpApi:
    """让 httpx.Client 形状贴近 TestClient，供 claim 辅助函数复用。"""

    def __init__(self, client: httpx.Client, app) -> None:
        self._client = client
        self.app = app

    def post(self, url: str, **kwargs):
        return self._client.post(url, **kwargs)

    def get(self, url: str, **kwargs):
        return self._client.get(url, **kwargs)


def _publish_and_claim_execute_order_paths(api, objects):
    """与 _publish_and_claim_execute 相同，但 Policy/Task 允许 order_service/**。"""
    client, token, auth, _project, goal_body = _ready_project(api, objects)
    policy = client.post(
        "/api/v1/policies",
        json={
            "project_id": goal_body["project_id"],
            "name": "order-e2e",
            "allowed_tools": ["read_file"],
            "allowed_paths": _ORDER_ALLOWED,
            "protected_paths": [],
            "network_allowlist": [],
            "external_actions": [],
            "secret_scope_refs": [],
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert policy.status_code == 201, policy.text
    goal_body = {**goal_body, "policy_id": policy.json()["data"]["id"]}

    _drain_ready(client, token, kinds=("EXECUTE", "AUDIT", "FINALIZE", "INTEGRATE", "PLAN"))
    created = client.post(
        "/api/v1/goals",
        json=goal_body,
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    goal = created.json()["data"]
    started = client.post(
        f"/api/v1/goals/{goal['id']}/start",
        json={"expected_state_revision": 1, "reason": "plan"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert started.status_code == 202, started.text
    plan_activity = client.get(
        f"/api/v1/goals/{goal['id']}/activities",
        params={"kind": "PLAN"},
        headers=auth,
    ).json()["data"][0]

    import os as _os

    from sqlalchemy import create_engine, text

    engine = create_engine(_os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text("""UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
              WHERE status='READY' AND kind='PLAN' AND id<>:id"""),
            {"id": plan_activity["id"]},
        )
        db.execute(
            text(
                """UPDATE activities SET status='READY', updated_at=clock_timestamp()
                WHERE id=:id AND status IN ('CANCELLED','READY')"""
            ),
            {"id": plan_activity["id"]},
        )
    engine.dispose()

    subject = str(uuid4())
    _register_worker(subject, kinds=("PLAN", "EXECUTE"))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    profile_id = goal["contract"]["success_criteria"][0]["verification_profile_id"]
    plan = _plan_body(goal, profile_id)
    plan["tasks"][0]["contract"]["allowed_paths"] = list(_ORDER_ALLOWED)
    done = client.post(
        f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {"plan": plan},
        },
        headers=worker_auth,
    )
    assert done.status_code == 200, done.text

    engine = create_engine(_os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE status='READY' AND kind='EXECUTE' AND goal_id<>:goal"""
            ),
            {"goal": goal["id"]},
        )
    engine.dispose()
    exec_claim = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert exec_claim.status_code == 200, exec_claim.text
    exec_lease = exec_claim.json()["data"]
    assert exec_lease["activity"]["kind"] == "EXECUTE"
    assert exec_lease["activity"]["goal_id"] == goal["id"]
    return client, token, auth, goal, worker_auth, exec_lease


def test_e2e1_independent_broker_process_reads_seeded_order_service(
    objects, tmp_path: Path, monkeypatch
):
    """E2E-1 seam：seed 订单仓 → uvicorn Control → Broker 子进程 --once 读 store.py。"""
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")
    if not os.environ.get("RING_TEST_S3_ENDPOINT"):
        pytest.skip("RING_TEST_S3_ENDPOINT required")

    store, _, bucket = objects
    run = seed.seed_run(run_id="e2e1-broker-read", runtime_root=tmp_path)
    # objects fixture 默认 max_bytes=1KiB；读小文件验证独立 Broker 进程，不读大 store.py。
    target_file = run.executor_worktree / "FIXED_INPUT.json"
    assert target_file.is_file()
    file_bytes = target_file.read_bytes()
    assert len(file_bytes) < 1024

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
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
        s3_endpoint=os.environ["RING_TEST_S3_ENDPOINT"],
        s3_bucket=bucket,
        s3_access_key=SecretStr(os.environ["RING_TEST_S3_ACCESS_KEY"]),
        s3_secret_key=SecretStr(os.environ["RING_TEST_S3_SECRET_KEY"]),
        artifact_max_bytes=16 * 1024 * 1024,
    )
    app = create_app(settings)
    port = _free_port()
    control_url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    httpx_client = httpx.Client(base_url=control_url, timeout=60.0, trust_env=False)
    http = _HttpApi(httpx_client, app)
    broker_proc: subprocess.Popen[str] | None = None
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if httpx_client.get("/health/live").status_code == 200:
                    break
            except (httpx.HTTPError, OSError):
                time.sleep(0.05)
        else:
            pytest.fail("Control uvicorn 未就绪")

        app.state.objects = store
        app.state._force_objects = store

        client, _tok, auth, goal, worker_auth, exec_lease = (
            _publish_and_claim_execute_order_paths((http, token), objects)
        )
        app.state.objects = store
        activity_id = exec_lease["activity"]["id"]
        project_id = UUID(exec_lease["activity"]["project_id"])
        engine = app.state.engine

        step = client.post(
            f"/internal/v1/activities/{activity_id}/steps",
            json={
                "lease": exec_lease["lease"],
                "predecessor_step_id": None,
                "purpose": "e2e1 读订单仓 FIXED_INPUT",
                "tool_ref": "read_file",
            },
            headers=worker_auth,
        )
        assert step.status_code == 201, step.text

        input_blob = b'{"path":"FIXED_INPUT.json"}'
        input_digest = "sha256:" + hashlib.sha256(input_blob).hexdigest()
        input_art = Artifacts(engine, store).ingest_raw(
            project_id,
            input_digest,
            BytesIO(input_blob),
            mime="application/json",
            producer_identity="test:e2e1-input",
        )
        prepared = client.post(
            "/internal/v1/effects/prepare",
            json={
                "lease": exec_lease["lease"],
                "logical_step_id": step.json()["data"]["logical_step_id"],
                "intent_revision": 1,
                "tool_ref": "read_file",
                "input_artifact_id": str(input_art.id),
            },
            headers=worker_auth,
        )
        assert prepared.status_code == 201, prepared.text
        effect = prepared.json()["data"]
        assert effect["status"] == "PREPARED"

        worker_jwt = worker_auth["Authorization"].removeprefix("Bearer ").strip()
        broker_env = {
            **os.environ,
            "RING_BROKER_CONTROL_URL": control_url,
            "RING_BROKER_WORKER_JWT": worker_jwt,
            "RING_BROKER_WORKSPACE_ROOT": str(run.executor_worktree),
            "RING_BROKER_ALLOWED_PATHS": "order_service/**,FIXED_INPUT.json,tests/**,pyproject.toml",
        }
        for key in ("RING_DATABASE_URL", "RING_CONTROL_DATABASE_URL"):
            broker_env.pop(key, None)
        # 子进程 httpx 默认读代理环境；本机 SOCKS 未装 socksio 会直接炸掉。
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            broker_env.pop(key, None)

        broker_proc = subprocess.Popen(
            [sys.executable, "-m", "broker_app", "--once"],
            cwd=str(_ROOT),
            env=broker_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            out, _ = broker_proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            broker_proc.send_signal(signal.SIGTERM)
            out = broker_proc.stdout.read() if broker_proc.stdout else ""
            pytest.fail(f"Broker --once 超时: {out[-2000:]}")

        assert broker_proc.returncode == 0, f"Broker 退出码={broker_proc.returncode}: {out}"

        effect_got = client.get(f"/api/v1/effects/{effect['id']}", headers=auth)
        assert effect_got.status_code == 200, effect_got.text
        body = effect_got.json()["data"]
        assert body["status"] == "SUCCEEDED", body
        assert body.get("evidence_ids"), body

        goal_got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth)
        assert goal_got.status_code == 200
        assert goal_got.json()["data"]["status"] != "DONE"

        summary = {
            "phase": "E2E-1-broker-process",
            "run_id": run.run_id,
            "initial_commit": run.initial_commit,
            "effect_id": effect["id"],
            "effect_status": body["status"],
            "goal_id": goal["id"],
            "goal_status": goal_got.json()["data"]["status"],
            "broker_returncode": broker_proc.returncode,
            "read_path": "FIXED_INPUT.json",
            "bytes_sha256": hashlib.sha256(file_bytes).hexdigest(),
            "marks_goal_done": False,
            "non_goals": [
                "temporal_start",
                "official_agent_loop",
                "tool_result_reenter",
                "goal_done",
            ],
        }
        (run.artifacts / "run-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (run.artifacts / "effects.json").write_text(
            json.dumps(body, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    finally:
        if broker_proc is not None and broker_proc.poll() is None:
            broker_proc.send_signal(signal.SIGTERM)
            try:
                broker_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                broker_proc.kill()
        server.should_exit = True
        httpx_client.close()
        thread.join(timeout=5)


def _harness_checkout() -> Path | None:
    raw = (os.environ.get("RING_HARNESS_CHECKOUT") or "").strip()
    root = Path(raw) if raw else _ROOT / ".runtime" / "deepseek-harness"
    if not root.is_dir():
        return None
    needed = [
        root / "vendor" / "cordis" / "lib" / "index.js",
        root / "packages" / "core" / "agent-loop" / "lib" / "index.js",
        root / "packages" / "llm" / "llm" / "lib" / "index.js",
        root / "packages" / "core" / "session" / "lib" / "index.js",
        root / "packages" / "session" / "session-projection" / "lib" / "index.js",
        root / "packages" / "core" / "system-prompt" / "lib" / "index.js",
        root / "packages" / "core" / "tools" / "lib" / "index.js",
        root / "packages" / "core" / "agent" / "lib" / "index.js",
    ]
    if not all(p.is_file() for p in needed):
        return None
    return root.resolve()


def _broker_env(control_url: str, worker_jwt: str, workspace: Path) -> dict[str, str]:
    env = {
        **os.environ,
        "RING_BROKER_CONTROL_URL": control_url,
        "RING_BROKER_WORKER_JWT": worker_jwt,
        "RING_BROKER_WORKSPACE_ROOT": str(workspace),
        "RING_BROKER_ALLOWED_PATHS": "order_service/**,FIXED_INPUT.json,tests/**,pyproject.toml",
    }
    for key in ("RING_DATABASE_URL", "RING_CONTROL_DATABASE_URL"):
        env.pop(key, None)
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        env.pop(key, None)
    return env


def test_e2e1_official_agent_loop_toolresult_via_independent_broker(
    objects, tmp_path: Path, monkeypatch
):
    """E2E-1：官方 scripted AgentLoop → Gateway prepare → 独立 Broker 进程 → ToolResult 第二轮。

    未宣称 Temporal START / PLAN 零工具全路径 / Goal DONE。
    """
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")
    if not os.environ.get("RING_TEST_S3_ENDPOINT"):
        pytest.skip("RING_TEST_S3_ENDPOINT required")
    checkout = _harness_checkout()
    if checkout is None:
        pytest.skip("RING_HARNESS_CHECKOUT AgentLoop peers 未 build:lib:host")

    store, _, bucket = objects
    run = seed.seed_run(run_id="e2e1-official-loop", runtime_root=tmp_path)
    target_file = run.executor_worktree / "FIXED_INPUT.json"
    file_bytes = target_file.read_bytes()
    assert b"checkout-20260912-001" in file_bytes

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
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
        s3_endpoint=os.environ["RING_TEST_S3_ENDPOINT"],
        s3_bucket=bucket,
        s3_access_key=SecretStr(os.environ["RING_TEST_S3_ACCESS_KEY"]),
        s3_secret_key=SecretStr(os.environ["RING_TEST_S3_SECRET_KEY"]),
        artifact_max_bytes=16 * 1024 * 1024,
    )
    app = create_app(settings)
    port = _free_port()
    control_url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    httpx_client = httpx.Client(base_url=control_url, timeout=60.0, trust_env=False)
    http = _HttpApi(httpx_client, app)
    stop_broker = threading.Event()
    broker_logs: list[str] = []
    broker_thread: threading.Thread | None = None
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if httpx_client.get("/health/live").status_code == 200:
                    break
            except (httpx.HTTPError, OSError):
                time.sleep(0.05)
        else:
            pytest.fail("Control uvicorn 未就绪")

        app.state.objects = store
        app.state._force_objects = store
        client, _tok, auth, goal, worker_auth, exec_lease = (
            _publish_and_claim_execute_order_paths((http, token), objects)
        )
        app.state.objects = store
        worker_jwt = worker_auth["Authorization"].removeprefix("Bearer ").strip()
        project_id = exec_lease["activity"]["project_id"]

        def _broker_poller() -> None:
            env = _broker_env(control_url, worker_jwt, run.executor_worktree)
            while not stop_broker.is_set():
                proc = subprocess.run(
                    [sys.executable, "-m", "broker_app", "--once"],
                    cwd=str(_ROOT),
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if proc.stdout:
                    broker_logs.append(proc.stdout)
                if proc.stderr:
                    broker_logs.append(proc.stderr)
                if proc.returncode not in (0,):
                    broker_logs.append(f"broker_rc={proc.returncode}")
                stop_broker.wait(0.15)

        broker_thread = threading.Thread(target=_broker_poller, daemon=True)
        broker_thread.start()

        turn_script = (
            _ROOT / "apps" / "runner" / "src" / "harness" / "e2e1OfficialBrokerProcessTurn.ts"
        )
        ts_env = {
            **os.environ,
            "RING_HARNESS_CHECKOUT": str(checkout),
            "RING_CONTROL_URL": control_url,
            "RING_WORKER_JWT": worker_jwt,
            "RING_E2E1_PROJECT_ID": str(project_id),
            "RING_E2E1_LEASE_JSON": json.dumps(exec_lease["lease"]),
            "RING_E2E1_READ_PATH": "FIXED_INPUT.json",
            "RING_E2E1_POLL_DELAY_MS": "150",
            "RING_E2E1_POLL_MAX_ATTEMPTS": "200",
            "RING_E2E1_IDLE_TIMEOUT_MS": "90000",
            "RING_HARNESS_EXECUTE_RUNTIME": "deepseek-official-agent-loop",
        }
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            ts_env.pop(key, None)

        ts_proc = subprocess.run(
            [
                "pnpm",
                "--filter",
                "@ring/runner",
                "exec",
                "tsx",
                str(turn_script),
            ],
            cwd=str(_ROOT),
            env=ts_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert ts_proc.returncode == 0, (
            f"官方 Loop 退出码={ts_proc.returncode}\n"
            f"stdout={ts_proc.stdout[-2000:]}\nstderr={ts_proc.stderr[-2000:]}\n"
            f"broker={''.join(broker_logs)[-2000:]}"
        )
        # 最后一行 JSON
        lines = [ln for ln in ts_proc.stdout.splitlines() if ln.strip().startswith("{")]
        assert lines, f"无 JSON 证据: {ts_proc.stdout[-1000:]}"
        evidence = json.loads(lines[-1])
        assert evidence["driver"] == "official-dsh-agent-loop"
        assert evidence["modelRounds"] >= 2
        assert evidence["round2CitesToolResult"] is True
        assert evidence["effectStatus"] == "SUCCEEDED"
        assert evidence["marksGoalDone"] is False
        assert evidence["runnerCalledDispatch"] is False

        effect_got = client.get(f"/api/v1/effects/{evidence['effectId']}", headers=auth)
        assert effect_got.status_code == 200
        assert effect_got.json()["data"]["status"] == "SUCCEEDED"

        goal_got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth)
        assert goal_got.json()["data"]["status"] != "DONE"

        summary = {
            "phase": "E2E-1-official-loop-broker",
            "run_id": run.run_id,
            "initial_commit": run.initial_commit,
            "harness_checkout": str(checkout),
            "driver": evidence["driver"],
            "model_rounds": evidence["modelRounds"],
            "round2_cites_tool_result": evidence["round2CitesToolResult"],
            "effect_id": evidence["effectId"],
            "goal_id": goal["id"],
            "goal_status": goal_got.json()["data"]["status"],
            "marks_goal_done": False,
            "non_goals": ["temporal_start", "plan_zero_tools_full_path", "goal_done"],
        }
        (run.artifacts / "run-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (run.artifacts / "official-loop-evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    finally:
        stop_broker.set()
        if broker_thread is not None:
            broker_thread.join(timeout=10)
        server.should_exit = True
        httpx_client.close()
        thread.join(timeout=5)


def test_e2e1_temporal_start_plan_zero_tools(api, objects, tmp_path: Path):
    """E2E-1 条件1–2：TEMPORAL START（非 LEGACY claim）+ PLAN 零工具 / 零 Step·Effect。

    绑定订单 seed 证据目录；未宣称官方 Loop 本测内再跑、未宣称 Goal DONE。
    """
    run = seed.seed_run(run_id="e2e1-temporal-plan", runtime_root=tmp_path)
    client, token, auth, goal, plan, command, engine = _start_temporal_goal(api, objects)
    try:
        refreshed = client.get(f"/api/v1/goals/{goal['id']}", headers=auth)
        assert refreshed.status_code == 200
        goal_body = refreshed.json()["data"]
        assert goal_body["orchestration_backend"] == "TEMPORAL"
        assert goal_body["status"] != "DONE"
        assert command["status"] == "ACCEPTED"
        assert plan["kind"] == "PLAN"
        assert plan["status"] == "READY"

        with engine.connect() as db:
            binding_row = (
                db.execute(
                    text("SELECT * FROM orchestration_bindings WHERE goal_id=:goal"),
                    {"goal": goal["id"]},
                )
                .mappings()
                .one()
            )
        assert binding_row["backend"] == "TEMPORAL"
        assert binding_row["workflow_id"] == f"goal-{goal['id']}"

        subject = str(uuid4())
        _register_worker(subject, kinds=("PLAN", "EXECUTE"))
        worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}

        # 全局 claim 对 TEMPORAL Goal 不得领到 PLAN lease
        claimed = client.post(
            "/internal/v1/claims",
            json={"kinds": ["PLAN"], "capabilities": []},
            headers={**worker_auth, "Idempotency-Key": str(uuid4())},
        )
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["data"]["lease"] is None

        binding = OrchestrationBindingContent(
            project_id=binding_row["project_id"],
            goal_id=binding_row["goal_id"],
            budget_scope_id=binding_row["budget_scope_id"],
            backend=binding_row["backend"],
            owner_epoch=binding_row["owner_epoch"],
            namespace=binding_row["namespace"],
            workflow_id=binding_row["workflow_id"],
            active_run_id=binding_row["active_run_id"],
            worker_build_id=binding_row["worker_build_id"],
            contract_digest=binding_row["contract_digest"],
        )
        receipt = ensure_workflow(
            engine, binding, UUID(command["id"]), FakeTemporalClient()
        )
        assert receipt.delivery_status == "ACKNOWLEDGED"

        admitted = client.post(
            "/internal/v1/runtime/admit",
            json={"activity_id": plan["id"]},
            headers={**worker_auth, "Idempotency-Key": str(uuid4())},
        )
        assert admitted.status_code == 200, admitted.text
        lease_body = admitted.json()["data"]
        assert lease_body["activity"]["kind"] == "PLAN"
        assert lease_body["activity"]["status"] == "RUNNING"
        lease = lease_body["lease"]

        steps_before = client.get(
            f"/api/v1/activities/{plan['id']}/steps", headers=auth
        )
        assert steps_before.status_code == 200
        assert steps_before.json()["data"] == []

        effects_before = client.get(
            "/api/v1/effects",
            params={
                "project_id": goal["project_id"],
                "activity_id": plan["id"],
            },
            headers=auth,
        )
        assert effects_before.status_code == 200, effects_before.text
        assert effects_before.json()["data"] == []

        # PLAN 暴露工具集必须为空：登记工具 Step 失败关闭
        denied = client.post(
            f"/internal/v1/activities/{plan['id']}/steps",
            json={
                "lease": lease,
                "predecessor_step_id": None,
                "purpose": "e2e1 plan must not tool",
                "tool_ref": "read_file",
            },
            headers=worker_auth,
        )
        assert denied.status_code in {403, 422}, denied.text
        err = denied.json()["error"]
        assert err["code"] == "ROLE_TOOL_FORBIDDEN", err

        steps_after = client.get(
            f"/api/v1/activities/{plan['id']}/steps", headers=auth
        )
        assert steps_after.json()["data"] == []
        effects_after = client.get(
            "/api/v1/effects",
            params={
                "project_id": goal["project_id"],
                "activity_id": plan["id"],
            },
            headers=auth,
        )
        assert effects_after.json()["data"] == []

        # 再次确认仍不能经 LEGACY claim 领取
        claimed_again = client.post(
            "/internal/v1/claims",
            json={"kinds": ["PLAN"], "capabilities": []},
            headers={**worker_auth, "Idempotency-Key": str(uuid4())},
        )
        assert claimed_again.json()["data"]["lease"] is None

        final_goal = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()[
            "data"
        ]
        assert final_goal["status"] != "DONE"
        assert final_goal["orchestration_backend"] == "TEMPORAL"

        summary = {
            "phase": "E2E-1-temporal-plan-zero-tools",
            "run_id": run.run_id,
            "initial_commit": run.initial_commit,
            "goal_id": goal["id"],
            "plan_activity_id": plan["id"],
            "orchestration_backend": "TEMPORAL",
            "legacy_claim_lease": None,
            "plan_steps": 0,
            "plan_effects": 0,
            "tool_step_denied_code": err["code"],
            "marks_goal_done": False,
            "non_goals": ["goal_done", "e2e2_tools", "full_goalworkflow_live_worker"],
        }
        (run.artifacts / "run-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(
            engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True
        )
        engine.dispose()
        seed.cleanup_run(run_id=run.run_id, runtime_root=tmp_path)
