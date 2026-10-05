"""OIDC login / session / logout（公开入口不返回项目业务数据）。"""

import secrets
from typing import Annotated
from urllib.parse import urlparse
from uuid import UUID

from control_kernel.protocols.auth import AuthSession, LogoutResult
from control_kernel.protocols.projects import Envelope, Meta
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from control_api import oidc as oidc_flow
from control_api.errors import ApiError
from control_api.identity import Principal
from control_api.session_cookie import (
    COOKIE_NAME,
    CSRF_HEADER,
    OIDC_COOKIE,
    load_oidc_pending,
    mint_oidc_pending,
    mint_session,
    principal_from_session,
)


def _origin_allowed(return_to: str, allowlist: list[str]) -> bool:
    parsed = urlparse(return_to)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    if parsed.fragment:
        return False
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return origin in allowlist


def register_auth_routes(app: FastAPI, identity, cfg) -> None:
    def session_secret() -> bytes:
        if cfg.session_secret is None or len(cfg.session_secret.get_secret_value()) < 32:
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "会话签名尚未配置")
        return cfg.session_secret.get_secret_value().encode()

    def require_oidc() -> None:
        if not (
            cfg.oidc_issuer
            and cfg.oidc_client_id
            and cfg.oidc_client_secret
            and cfg.oidc_redirect_uri
            and cfg.auth_return_to_origins
        ):
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "OIDC 身份源尚未配置")

    @app.get("/api/v1/auth/login")
    def login(
        request: Request,
        return_to: Annotated[str, Query(min_length=1, max_length=2048)],
    ):
        require_oidc()
        secret = session_secret()
        if not _origin_allowed(return_to, cfg.auth_return_to_origins):
            raise ApiError(400, "INVALID_REQUEST", "return_to 不在同源允许列表")
        try:
            discovery = oidc_flow.discover(cfg.oidc_issuer)
            authorization_endpoint = discovery["authorization_endpoint"]
        except Exception as exc:
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "OIDC 发现文档不可用") from exc
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        verifier, challenge = oidc_flow.pkce_pair()
        pending = mint_oidc_pending(
            secret,
            state=state,
            nonce=nonce,
            code_verifier=verifier,
            return_to=return_to,
        )
        url = oidc_flow.build_authorize_url(
            authorization_endpoint=authorization_endpoint,
            client_id=cfg.oidc_client_id,
            redirect_uri=cfg.oidc_redirect_uri,
            state=state,
            nonce=nonce,
            code_challenge=challenge,
        )
        response = RedirectResponse(url=url, status_code=302)
        response.set_cookie(
            OIDC_COOKIE,
            pending,
            httponly=True,
            secure=cfg.cookie_secure,
            samesite="lax",
            max_age=600,
            path="/api/v1/auth",
        )
        return response

    @app.get("/api/v1/auth/callback")
    def callback(
        request: Request,
        code: Annotated[str, Query(min_length=1, max_length=4096)],
        state: Annotated[str, Query(min_length=1, max_length=512)],
    ):
        require_oidc()
        secret = session_secret()
        pending_raw = request.cookies.get(OIDC_COOKIE)
        if not pending_raw:
            raise ApiError(401, "UNAUTHENTICATED", "缺少登录中间态")
        try:
            pending = load_oidc_pending(secret, pending_raw)
        except (ValueError, KeyError, TypeError) as exc:
            raise ApiError(401, "UNAUTHENTICATED", "登录中间态无效或已过期") from exc
        if pending["state"] != state:
            raise ApiError(401, "UNAUTHENTICATED", "state 校验失败")
        try:
            discovery = oidc_flow.discover(cfg.oidc_issuer)
            token = oidc_flow.exchange_code(
                token_endpoint=discovery["token_endpoint"],
                client_id=cfg.oidc_client_id,
                client_secret=cfg.oidc_client_secret.get_secret_value(),
                redirect_uri=cfg.oidc_redirect_uri,
                code=code,
                code_verifier=pending["code_verifier"],
            )
            claims = oidc_flow.verify_id_token(
                id_token=token["id_token"],
                jwks_uri=discovery["jwks_uri"],
                issuer=cfg.oidc_issuer.rstrip("/"),
                audience=cfg.oidc_client_id,
                nonce=pending["nonce"],
            )
        except Exception as exc:
            raise ApiError(401, "UNAUTHENTICATED", "OIDC 令牌交换或校验失败") from exc
        roles = claims.get("roles")
        project_ids = claims.get("project_ids")
        if not isinstance(roles, list) or not roles:
            raise ApiError(403, "FORBIDDEN", "身份缺少角色声明")
        if not isinstance(project_ids, list):
            raise ApiError(403, "FORBIDDEN", "身份缺少项目范围声明")
        for project in project_ids:
            try:
                UUID(str(project))
            except (ValueError, TypeError) as exc:
                raise ApiError(403, "FORBIDDEN", "项目范围声明无效") from exc
        cookie, _csrf, _exp = mint_session(
            secret,
            user_id=str(claims["sub"]),
            roles=[str(r) for r in roles],
            project_ids=[str(p) for p in project_ids],
        )
        response = RedirectResponse(url=pending["return_to"], status_code=302)
        response.delete_cookie(OIDC_COOKIE, path="/api/v1/auth")
        response.set_cookie(
            COOKIE_NAME,
            cookie,
            httponly=True,
            secure=cfg.cookie_secure,
            samesite="lax",
            max_age=3600,
            path="/",
        )
        return response

    @app.get("/api/v1/auth/session", response_model=Envelope[AuthSession])
    def session(request: Request, p: Annotated[Principal, Depends(identity)]):
        secret = session_secret()
        raw = request.cookies.get(COOKIE_NAME)
        if not raw:
            raise ApiError(401, "UNAUTHENTICATED", "需要浏览器会话")
        try:
            principal, csrf, expires_at = principal_from_session(secret, raw)
        except (ValueError, KeyError, TypeError) as exc:
            raise ApiError(401, "UNAUTHENTICATED", "会话无效或已过期") from exc
        if principal.sub != p.sub:
            raise ApiError(401, "UNAUTHENTICATED", "会话与凭据不一致")
        body = AuthSession(
            user_id=p.sub,
            roles=p.roles,
            project_ids=p.project_ids,
            csrf_token=csrf,
            expires_at=expires_at,
        )
        return Envelope(data=body, meta=Meta(request_id=request.state.request_id))

    @app.post("/api/v1/auth/logout", response_model=Envelope[LogoutResult])
    def logout(request: Request):
        raw = request.cookies.get(COOKIE_NAME)
        if not raw:
            raise ApiError(401, "UNAUTHENTICATED", "请先登录")
        try:
            secret = session_secret()
            _principal, csrf, _expires = principal_from_session(secret, raw)
        except (ValueError, KeyError, TypeError) as exc:
            raise ApiError(401, "UNAUTHENTICATED", "会话无效或已过期") from exc
        csrf_header = request.headers.get(CSRF_HEADER)
        if not csrf_header or not secrets.compare_digest(csrf_header, csrf):
            raise ApiError(403, "FORBIDDEN", "CSRF 校验失败")
        origin = request.headers.get("origin")
        if origin and cfg.auth_return_to_origins and origin not in cfg.auth_return_to_origins:
            raise ApiError(403, "FORBIDDEN", "Origin 不在允许列表")
        payload = Envelope(
            data=LogoutResult(logged_out=True),
            meta=Meta(request_id=request.state.request_id),
        )
        response = JSONResponse(content=payload.model_dump(mode="json"), status_code=200)
        response.delete_cookie(COOKIE_NAME, path="/")
        response.delete_cookie(OIDC_COOKIE, path="/api/v1/auth")
        return response
