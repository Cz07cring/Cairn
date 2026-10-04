from __future__ import annotations

import hmac
import os
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from cairn.server.db import get_conn
from cairn.server.integration.bindings import binding_for, project_role, verified_goal
from cairn.server.integration.ring_client import (
    RingConfig,
    RingContractUnknown,
    RingDenied,
    RingUnavailable,
    read_session,
)

router = APIRouter(tags=["auth"])


def product_mode() -> bool:
    mode = os.environ.get("CAIRN_PRODUCT_MODE", "standalone")
    if mode not in {"standalone", "ring"}:
        raise RuntimeError("CAIRN_PRODUCT_MODE must be standalone or ring")
    return mode == "ring"


def _login_url(config: RingConfig) -> str:
    return f"{config.public_origin}/api/v1/auth/login?return_to={quote(config.public_origin + '/', safe='')}"


@router.get("/auth/session")
async def session(request: Request):
    if not product_mode():
        return {"mode": "standalone"}
    config = RingConfig.load()
    cookie = request.cookies.get("ring_session")
    if not cookie:
        return JSONResponse(
            {"detail": "Ring login required", "login_url": _login_url(config)},
            status_code=401,
        )
    try:
        principal = await run_in_threadpool(read_session, config, cookie)
    except RingDenied:
        return JSONResponse(
            {"detail": "Ring login required", "login_url": _login_url(config)},
            status_code=401,
        )
    except (RingUnavailable, RingContractUnknown):
        return JSONResponse({"detail": "Ring identity is unavailable"}, status_code=503)
    return {
        "mode": "ring",
        "user_id": principal["user_id"],
        "csrf_token": principal["csrf_token"],
        "login_url": _login_url(config),
    }


async def product_gate(request: Request, call_next):
    if not product_mode():
        return await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/") or path == "/auth/session":
        return await call_next(request)
    config = RingConfig.load()
    cookie = request.cookies.get("ring_session")
    if not cookie:
        return JSONResponse({"detail": "Ring login required"}, status_code=401)
    try:
        principal = await run_in_threadpool(read_session, config, cookie)
    except RingDenied:
        return JSONResponse({"detail": "Ring login required"}, status_code=401)
    except (RingUnavailable, RingContractUnknown):
        return JSONResponse({"detail": "Ring identity is unavailable"}, status_code=503)
    request.state.ring_principal = principal
    request.state.ring_cookie = cookie
    request.state.ring_config = config
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        csrf = request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(csrf, principal["csrf_token"]):
            return JSONResponse({"detail": "CSRF token required"}, status_code=403)
        if request.headers.get("origin") != config.public_origin:
            return JSONResponse({"detail": "Origin is not allowed"}, status_code=403)
    if path == "/settings" and request.method != "GET":
        return JSONResponse({"detail": "Global settings are read-only in Ring mode"}, status_code=403)
    parts = path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "projects":
        project_id = parts[1]
        with get_conn() as conn:
            role = project_role(conn, project_id, principal["user_id"])
            if role is None:
                return JSONResponse({"detail": "Project not found"}, status_code=404)
            binding = binding_for(conn, project_id)
            if request.method != "GET" and role != "owner" and not (
                len(parts) == 3 and parts[2] == "hints"
            ):
                return JSONResponse({"detail": "Project owner required"}, status_code=403)
            if binding:
                if binding["ring_project_id"] not in principal["project_ids"]:
                    return JSONResponse({"detail": "Project not found"}, status_code=404)
                if request.method != "GET":
                    try:
                        await run_in_threadpool(
                            verified_goal,
                            config,
                            cookie,
                            binding["ring_project_id"],
                            binding["ring_goal_id"],
                        )
                    except HTTPException as exc:
                        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
                allowed_bound_write = (
                    (request.method == "PUT" and len(parts) == 3 and parts[2] == "title")
                    or (request.method == "POST" and len(parts) == 3 and parts[2] in {"hints", "ring-binding", "intents", "plan-snapshots"})
                    or (request.method in {"PUT", "DELETE"} and len(parts) == 4 and parts[2] == "members")
                    or (request.method == "POST" and len(parts) == 5 and parts[2] == "intents" and parts[4] == "plan-candidate")
                    or (request.method == "POST" and len(parts) == 6 and parts[2] == "intents" and parts[4:] == ["plan-candidate", "reconcile"])
                )
                if request.method != "GET" and not allowed_bound_write:
                    return JSONResponse(
                        {"detail": "Ring-bound project cannot use Cairn worker or state writes"},
                        status_code=409,
                    )
    return await call_next(request)
