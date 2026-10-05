"""OIDC login / session / logout；缺配置失败关闭。"""

import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import jwt
from control_api.app import create_app
from control_api.session_cookie import COOKIE_NAME, mint_session
from control_api.settings import Settings
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from oidc_stub import OidcStub


def test_auth_login_fails_closed_without_oidc(api):
    client, _token = api
    response = client.get(
        "/api/v1/auth/login",
        params={"return_to": "http://127.0.0.1:58102/"},
        follow_redirects=False,
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


def test_oidc_login_callback_session_logout():
    url = os.environ.get("RING_TEST_DATABASE_URL")
    if not url:
        import pytest

        pytest.skip("RING_TEST_DATABASE_URL required for isolated PG tests")

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    redirect_uri = "http://testserver/api/v1/auth/callback"
    stub = OidcStub(
        client_id="ring-web",
        client_secret="test-oidc-secret",
        redirect_uri=redirect_uri,
    )
    issuer = stub.start()
    try:
        settings = Settings(
            database_url=url,
            jwt_public_key=public,
            jwt_issuer="ring-test",
            jwt_audience="ring-api",
            repository_refs=["fixture"],
            cursor_secret="test-only-cursor-secret-at-least-32-bytes",
            session_secret="test-only-session-secret-at-least-32b",
            oidc_issuer=issuer,
            oidc_client_id="ring-web",
            oidc_client_secret="test-oidc-secret",
            oidc_redirect_uri=redirect_uri,
            auth_return_to_origins=["http://127.0.0.1:58102"],
            cookie_secure=False,
        )
        with TestClient(create_app(settings)) as client:
            login = client.get(
                "/api/v1/auth/login",
                params={"return_to": "http://127.0.0.1:58102/app"},
                follow_redirects=False,
            )
            assert login.status_code == 302, login.text
            assert login.headers["location"].startswith(issuer + "/authorize")
            authorize = httpx.get(
                login.headers["location"], follow_redirects=False, trust_env=False
            )
            assert authorize.status_code == 302
            callback_url = authorize.headers["location"]
            assert "/api/v1/auth/callback" in callback_url
            parsed = urlparse(callback_url)
            path = parsed.path + ("?" + parsed.query if parsed.query else "")
            callback = client.get(path, follow_redirects=False)
            assert callback.status_code == 302, callback.text
            assert callback.headers["location"] == "http://127.0.0.1:58102/app"
            assert COOKIE_NAME in client.cookies

            session = client.get("/api/v1/auth/session")
            assert session.status_code == 200, session.text
            data = session.json()["data"]
            assert data["user_id"] == "oidc-user-1"
            assert "operator" in data["roles"]
            assert data["csrf_token"]
            csrf = data["csrf_token"]

            denied = client.post("/api/v1/auth/logout", json={})
            assert denied.status_code == 403
            logged_out = client.post(
                "/api/v1/auth/logout",
                json={},
                headers={
                    "X-CSRF-Token": csrf,
                    "Origin": "http://127.0.0.1:58102",
                },
            )
            assert logged_out.status_code == 200, logged_out.text
            assert logged_out.json()["data"]["logged_out"] is True
            assert client.get("/api/v1/auth/session").status_code == 401
    finally:
        stub.stop()


def test_session_cookie_can_authorize_reads():
    url = os.environ.get("RING_TEST_DATABASE_URL")
    if not url:
        import pytest

        pytest.skip("RING_TEST_DATABASE_URL required for isolated PG tests")
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    secret = b"test-only-session-secret-at-least-32b"
    cookie, csrf, _exp = mint_session(
        secret,
        user_id="cookie-user",
        roles=["viewer"],
        project_ids=[],
    )
    settings = Settings(
        database_url=url,
        jwt_public_key=public,
        jwt_issuer="ring-test",
        jwt_audience="ring-api",
        repository_refs=["fixture"],
        cursor_secret="test-only-cursor-secret-at-least-32-bytes",
        session_secret=secret.decode(),
        auth_return_to_origins=["http://127.0.0.1:58102"],
    )
    with TestClient(create_app(settings)) as client:
        client.cookies.set(COOKIE_NAME, cookie)
        listed = client.get("/api/v1/projects")
        assert listed.status_code == 200, listed.text
        created = client.post(
            "/api/v1/projects",
            json={"name": "x", "repository_ref": "fixture"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert created.status_code == 403
        now = datetime.now(UTC)
        bearer = jwt.encode(
            {
                "sub": str(uuid4()),
                "roles": ["admin"],
                "project_ids": [],
                "iss": "ring-test",
                "aud": "ring-api",
                "iat": now,
                "exp": now + timedelta(minutes=5),
            },
            private,
            algorithm="RS256",
        )
        ok = client.post(
            "/api/v1/projects",
            json={"name": "bearer-project", "repository_ref": "fixture"},
            headers={
                "Authorization": "Bearer " + bearer,
                "Idempotency-Key": str(uuid4()),
            },
        )
        assert ok.status_code == 201, ok.text
        assert csrf
