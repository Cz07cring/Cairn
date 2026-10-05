"""证明 RING_LEASE_TTL_SECONDS → claim 租约到期真实生效（非恒默认 90）。

≠ Goal DONE。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from control_api.app import create_app
from control_api.settings import Settings
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from test_claims import _register_worker, _start_goal


@pytest.fixture
def api_lease_180():
    """独立 app：lease_ttl=180，证明配置旋钮进入 claim 咽喉。"""
    url = os.environ.get("RING_TEST_DATABASE_URL")
    if not url:
        pytest.skip("RING_TEST_DATABASE_URL required for isolated PG tests")
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    settings = Settings(
        database_url=url,
        jwt_public_key=public,
        jwt_issuer="ring-test",
        jwt_audience="ring-api",
        repository_refs=["fixture"],
        cursor_secret="test-only-cursor-secret-at-least-32-bytes",
        lease_ttl_seconds=180,
        heartbeat_seconds=20,
        cloud_mode="DENY",
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
                "exp": now + timedelta(minutes=5),
            },
            private,
            algorithm="RS256",
        )

    with TestClient(create_app(settings)) as client:
        yield client, token


def test_claim_lease_expires_honors_configured_ttl(api_lease_180, objects):
    before = datetime.now(UTC)
    client, token, _auth, _goal, _plan = _start_goal(api_lease_180, objects)
    subject = str(uuid4())
    _register_worker(subject)
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={
            "Authorization": "Bearer " + token(subject, ["worker"]),
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert claimed.status_code == 200, claimed.text
    expires_raw = claimed.json()["data"]["attempt"]["lease_expires_at"]
    # PG/JSON 常见带 Z 或偏移；统一到 aware UTC
    expires = datetime.fromisoformat(expires_raw.removesuffix("Z") + "+00:00" if expires_raw.endswith("Z") else expires_raw)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    after = datetime.now(UTC)
    # 180s TTL：允许 claim 耗时与时钟抖动，落在 [170, 190] 相对 before/after 窗口
    delta_lo = (expires - after).total_seconds()
    delta_hi = (expires - before).total_seconds()
    assert delta_lo >= 170, f"TTL 过短: {delta_lo}s（应为 ~180，非默认 90）"
    assert delta_hi <= 190, f"TTL 过长: {delta_hi}s"
    # 明确排除恒默认 90：上界已卡 190；再断言不低于 120（与默认 90 区分）
    assert delta_hi >= 120
