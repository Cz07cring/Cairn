"""HttpOnly session cookie：HMAC 签名载荷，不含宿主私钥。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from control_api.identity import Principal

COOKIE_NAME = "ring_session"
OIDC_COOKIE = "ring_oidc"
CSRF_HEADER = "X-CSRF-Token"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(raw: str) -> bytes:
    pad = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + pad)


def sign_payload(secret: bytes, payload: dict[str, Any]) -> str:
    body = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def verify_payload(secret: bytes, token: str) -> dict[str, Any]:
    body, mac = token.split(".", 1)
    expected = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        raise ValueError("session mac")
    return json.loads(_b64url_decode(body))


def mint_session(
    secret: bytes,
    *,
    user_id: str,
    roles: list[str],
    project_ids: list[str],
    ttl_seconds: int = 3600,
) -> tuple[str, str, datetime]:
    """返回 (cookie_value, csrf_token, expires_at)。"""
    expires = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    csrf = secrets.token_urlsafe(24)
    value = sign_payload(
        secret,
        {
            "sub": user_id,
            "roles": roles,
            "project_ids": project_ids,
            "csrf": csrf,
            "exp": int(expires.timestamp()),
        },
    )
    return value, csrf, expires


def principal_from_session(secret: bytes, cookie: str) -> tuple[Principal, str, datetime]:
    data = verify_payload(secret, cookie)
    exp = datetime.fromtimestamp(int(data["exp"]), tz=UTC)
    if exp <= datetime.now(UTC):
        raise ValueError("session expired")
    principal = Principal.model_validate(
        {
            "sub": data["sub"],
            "roles": data["roles"],
            "project_ids": data["project_ids"],
        }
    )
    return principal, str(data["csrf"]), exp


def mint_oidc_pending(
    secret: bytes,
    *,
    state: str,
    nonce: str,
    code_verifier: str,
    return_to: str,
) -> str:
    return sign_payload(
        secret,
        {
            "state": state,
            "nonce": nonce,
            "code_verifier": code_verifier,
            "return_to": return_to,
            "exp": int((datetime.now(UTC) + timedelta(minutes=10)).timestamp()),
        },
    )


def load_oidc_pending(secret: bytes, cookie: str) -> dict[str, Any]:
    data = verify_payload(secret, cookie)
    if datetime.fromtimestamp(int(data["exp"]), tz=UTC) <= datetime.now(UTC):
        raise ValueError("oidc pending expired")
    return data
