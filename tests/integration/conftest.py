"""Real HTTP/auth/PG behavior; isolated database required, never production."""

import os
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from control_api.app import create_app
from control_api.settings import Settings
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient


@pytest.fixture
def api():
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
        local_qwen_base=os.environ.get("RING_LOCAL_QWEN_BASE", "http://127.0.0.1:8001"),
        local_qwen_api_key=os.environ.get("RING_LOCAL_QWEN_API_KEY"),
        # 默认 DENY；仅显式 RING_TEST_ALLOW_CLOUD=1 时跟随部署云模式
        cloud_mode=(
            os.environ.get("RING_CLOUD_MODE", "DENY")
            if os.environ.get("RING_TEST_ALLOW_CLOUD") == "1"
            else "DENY"
        ),
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
