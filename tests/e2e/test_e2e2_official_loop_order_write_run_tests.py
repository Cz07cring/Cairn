"""E2E-2：官方 AgentLoop scripted write→run_tests→seal × 独立 Broker 修订单仓。

诚实：scriptedOrder=true ≠ 模型自主决策；本缝到 seal SUCCEEDED 为止，≠ Goal DONE。
AUDIT/FINALIZE/DONE 见 E2E-4 业务全路径。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import jwt
import pytest
import uvicorn
from control_api.app import Settings, create_app
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from e2e3_order_helpers import ALLOW
from e2e_tool_helpers import load_reference_fix_store
from evidence_ledger.objects import S3Objects
from pydantic import SecretStr
from test_claims import _drain_ready, _register_worker
from test_goals import _ready_project
from test_plans import _plan_body

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import seed_business_e2e as seed


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _harness_checkout() -> Path | None:
    """与 E2E-1 同口径：钉扎 checkout 内 AgentLoop peers 的 lib 已 build。"""
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


class _HttpApi:
    def __init__(self, client: httpx.Client, app):
        self._client = client
        self.app = app

    def post(self, url, **kwargs):
        return self._client.post(url, **kwargs)

    def get(self, url, **kwargs):
        return self._client.get(url, **kwargs)


def _broker_env(control_url: str, worker_jwt: str, workspace: Path) -> dict[str, str]:
    env = {
        **os.environ,
        "RING_BROKER_CONTROL_URL": control_url,
        "RING_BROKER_WORKER_JWT": worker_jwt,
        "RING_BROKER_WORKSPACE_ROOT": str(workspace),
        "RING_BROKER_ALLOWED_PATHS": ",".join(ALLOW),
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


def test_e2e2_official_loop_write_run_tests_order_via_broker(objects, tmp_path: Path, monkeypatch):
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")
    if not os.environ.get("RING_TEST_S3_ENDPOINT"):
        pytest.skip("RING_TEST_S3_ENDPOINT required")
    checkout = _harness_checkout()
    if checkout is None:
        pytest.skip("RING_HARNESS_CHECKOUT AgentLoop peers 未 build:lib:host")

    store, s3, bucket = objects
    # fixture 默认 max_bytes=1024；write_file 的 ToolPayload（含 reference_fix）须更大。
    store = S3Objects(s3, bucket, max_bytes=64 * 1024)
    run = seed.seed_run(run_id="e2e2-loop-ms", runtime_root=tmp_path)
    fix_body = load_reference_fix_store()
    fix_file = run.artifacts / "reference_fix_store.py"
    fix_file.write_text(fix_body, encoding="utf-8")
    buggy = (run.executor_worktree / "order_service" / "store.py").read_text(encoding="utf-8")
    assert "_by_key" not in buggy

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
        client, tok, auth, _project, goal_body = _ready_project((http, token), objects)
        policy = client.post(
            "/api/v1/policies",
            json={
                "project_id": goal_body["project_id"],
                "name": "e2e2-loop-ms",
                "allowed_tools": [
                    "read_file",
                    "write_file",
                    "run_tests",
                    "git_diff",
                    "seal_candidate",
                ],
                "allowed_paths": list(ALLOW),
                "protected_paths": [],
                "network_allowlist": [],
                "external_actions": [],
                "secret_scope_refs": [],
            },
            headers={**auth, "Idempotency-Key": str(uuid4())},
        )
        assert policy.status_code == 201, policy.text
        goal_body = {**goal_body, "policy_id": policy.json()["data"]["id"]}
        _drain_ready(client, tok, kinds=("EXECUTE", "AUDIT", "FINALIZE", "INTEGRATE", "PLAN"))
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

        from sqlalchemy import create_engine, text

        eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
        with eng.begin() as db:
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
        eng.dispose()

        exec_subject = str(uuid4())
        _register_worker(exec_subject, kinds=("PLAN", "EXECUTE"))
        exec_auth = {"Authorization": "Bearer " + token(exec_subject, ["worker"])}
        claimed = client.post(
            "/internal/v1/claims",
            json={"kinds": ["PLAN"], "capabilities": []},
            headers={**exec_auth, "Idempotency-Key": str(uuid4())},
        )
        lease = claimed.json()["data"]
        profile_id = goal["contract"]["success_criteria"][0]["verification_profile_id"]
        plan = _plan_body(goal, profile_id)
        plan["tasks"][0]["contract"]["allowed_paths"] = list(ALLOW)
        done = client.post(
            f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
            json={
                "lease": lease["lease"],
                "expected_state_revision": lease["activity"]["state_revision"],
                "outcome": {"plan": plan},
            },
            headers=exec_auth,
        )
        assert done.status_code == 200, done.text
        eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
        with eng.begin() as db:
            db.execute(
                text(
                    """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                    WHERE status='READY' AND kind='EXECUTE' AND goal_id<>:goal"""
                ),
                {"goal": goal["id"]},
            )
        eng.dispose()
        exec_lease = client.post(
            "/internal/v1/claims",
            json={"kinds": ["EXECUTE"], "capabilities": []},
            headers={**exec_auth, "Idempotency-Key": str(uuid4())},
        ).json()["data"]
        app.state.objects = store

        worker_jwt = exec_auth["Authorization"].removeprefix("Bearer ").strip()

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
                stop_broker.wait(0.15)

        broker_thread = threading.Thread(target=_broker_poller, daemon=True)
        broker_thread.start()

        turn_script = (
            _ROOT
            / "apps"
            / "runner"
            / "src"
            / "harness"
            / "e2e2OfficialMultistepOrderTurn.ts"
        )
        ts_env = {
            **os.environ,
            "RING_HARNESS_CHECKOUT": str(checkout),
            "RING_CONTROL_URL": control_url,
            "RING_WORKER_JWT": worker_jwt,
            "RING_E2E2_PROJECT_ID": str(exec_lease["activity"]["project_id"]),
            "RING_E2E2_LEASE_JSON": json.dumps(exec_lease["lease"]),
            "RING_E2E2_WRITE_PATH": "order_service/store.py",
            "RING_E2E2_WRITE_CONTENT_FILE": str(fix_file),
            "RING_E2E2_SEAL_PROFILE_IDS": str(profile_id),
            "RING_E2E2_POLL_DELAY_MS": "150",
            "RING_E2E2_POLL_MAX_ATTEMPTS": "200",
            "RING_E2E2_IDLE_TIMEOUT_MS": "120000",
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
            timeout=180,
        )
        assert ts_proc.returncode == 0, (
            f"官方 Loop 退出码={ts_proc.returncode}\n"
            f"stdout={ts_proc.stdout[-2000:]}\nstderr={ts_proc.stderr[-2000:]}\n"
            f"broker={''.join(broker_logs)[-2000:]}"
        )
        lines = [ln for ln in ts_proc.stdout.splitlines() if ln.strip().startswith("{")]
        assert lines, f"无 JSON 证据: {ts_proc.stdout[-1000:]}"
        evidence = json.loads(lines[-1])
        assert evidence["driver"] == "official-dsh-agent-loop"
        assert evidence["scriptedOrder"] is True
        assert evidence["marksGoalDone"] is False
        assert evidence["runnerCalledDispatch"] is False
        assert evidence["modelRounds"] >= 4
        assert evidence["laterRoundsCitePriorToolResults"] is True
        assert [t["toolName"] for t in evidence["trail"]] == [
            "write_file",
            "run_tests",
            "seal_candidate",
        ]
        assert evidence.get("sealEffectId")

        on_disk = (run.executor_worktree / "order_service" / "store.py").read_text(
            encoding="utf-8"
        )
        assert on_disk == fix_body
        assert "_by_key" in on_disk

        for effect_id in (
            evidence["writeEffectId"],
            evidence["testsEffectId"],
            evidence["sealEffectId"],
        ):
            got = client.get(f"/api/v1/effects/{effect_id}", headers=auth)
            assert got.status_code == 200
            assert got.json()["data"]["status"] == "SUCCEEDED"

        goal_got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
        assert goal_got["status"] != "DONE"

        summary = {
            "phase": "E2E-2-official-loop-write-run_tests-seal",
            "run_id": run.run_id,
            "goal_id": goal["id"],
            "driver": evidence["driver"],
            "scripted_order": True,
            "write_effect_id": evidence["writeEffectId"],
            "tests_effect_id": evidence["testsEffectId"],
            "seal_effect_id": evidence["sealEffectId"],
            "store_fixed_via": "official_loop+broker_write_file",
            "marks_goal_done": False,
            "non_goals": [
                "model_autonomous_tool_choice",
                "audit_finalize_goal_done",
                "live_chat_model",
            ],
        }
        (run.artifacts / "run-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (run.artifacts / "official-loop-multistep-evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    finally:
        stop_broker.set()
        if broker_thread is not None:
            broker_thread.join(timeout=10)
        server.should_exit = True
        thread.join(timeout=10)
        httpx_client.close()
