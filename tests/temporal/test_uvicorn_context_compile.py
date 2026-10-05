"""独立 uvicorn：admit 后 POST context-compile，暴露真实错误（非 Temporal）。"""

from __future__ import annotations

import os
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
import uvicorn
from control_api.app import create_app
from control_api.settings import Settings
from control_kernel.storage.goals import set_goal_orchestration_backend
from control_kernel.storage.orchestration import admit_runtime_attempt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from evidence_ledger.objects import S3Objects
from pydantic import SecretStr
from test_claims import _drain_ready_plans, _register_worker
from test_orchestration_backend import _start_temporal_goal


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _AppClient:
    def __init__(self, client: httpx.Client, app):
        self._client = client
        self.app = app

    def post(self, *args, **kwargs):
        return self._client.post(*args, **kwargs)

    def get(self, *args, **kwargs):
        return self._client.get(*args, **kwargs)


def test_uvicorn_context_compile_after_admit(objects, monkeypatch):
    """确认 uvicorn 路径下 context-compile 非 503；为 live Temporal 扫清前置。"""
    if not os.environ.get("RING_TEST_S3_ENDPOINT"):
        pytest.skip("需要 RING_TEST_S3_ENDPOINT")

    _store, s3_client, bucket = objects
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
                "exp": now + timedelta(minutes=10),
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
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    httpx_client = httpx.Client(
        base_url=f"http://127.0.0.1:{port}", timeout=60.0, trust_env=False
    )
    http = _AppClient(httpx_client, app)
    engine = None
    goal = None
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                if httpx_client.get("/health/live").status_code == 200:
                    break
            except (httpx.HTTPError, OSError):
                time.sleep(0.05)
        else:
            pytest.fail("uvicorn 未就绪")

        app.state.objects = large_store
        app.state._force_objects = large_store

        _c, _t, _a, goal, plan, _cmd, engine = _start_temporal_goal(
            (http, token), objects_for_setup
        )
        app.state.objects = large_store
        app.state._force_objects = large_store

        subject = str(uuid4())
        _register_worker(subject)
        lease = admit_runtime_attempt(
            engine, subject, f"compile-probe:{plan['id']}", UUID(plan["id"])
        )
        assert lease.lease is not None

        compiled = httpx_client.post(
            f"/internal/v1/activities/{plan['id']}/context-compile",
            headers={"Authorization": "Bearer " + token(subject, ["worker"])},
            json={
                "lease": {
                    "activity_id": str(lease.lease.activity_id),
                    "attempt_id": str(lease.lease.attempt_id),
                    "fencing_epoch": str(lease.lease.fencing_epoch),
                },
                "max_input_tokens": 8192,
            },
        )
        assert compiled.status_code == 201, compiled.text
        assert compiled.json()["data"]["content"]["role"] == "PLANNER"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        httpx_client.close()
        if engine is not None and goal is not None:
            _drain_ready_plans(http, token)
            set_goal_orchestration_backend(
                engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True
            )
            engine.dispose()
