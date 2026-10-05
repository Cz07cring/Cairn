"""OIDC Authorization Code + PKCE；缺配置时失败关闭，无万能登录旁路。"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient


def pkce_pair() -> tuple[str, str]:
    import base64

    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def build_authorize_url(
    *,
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_challenge: str,
    scopes: str = "openid profile",
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{authorization_endpoint}?{query}"


def discover(issuer: str, timeout: float = 5.0) -> dict[str, Any]:
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.json()


def exchange_code(
    *,
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
    timeout: float = 10.0,
) -> dict[str, Any]:
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        response = client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": code_verifier,
            },
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        return response.json()


def verify_id_token(
    *,
    id_token: str,
    jwks_uri: str,
    issuer: str,
    audience: str,
    nonce: str,
) -> dict[str, Any]:
    jwks = PyJWKClient(jwks_uri, cache_keys=True)
    key = jwks.get_signing_key_from_jwt(id_token)
    claims = jwt.decode(
        id_token,
        key.key,
        algorithms=["RS256"],
        audience=audience,
        issuer=issuer,
        options={"require": ["exp", "iat", "sub", "iss", "aud", "nonce"]},
    )
    if claims.get("nonce") != nonce:
        raise ValueError("nonce mismatch")
    return claims
