"""Activity / Command 查询路由；不开放 claim/lease 与工具执行。"""

import base64
import hashlib
import hmac
import json
from typing import Annotated
from uuid import UUID

from control_kernel.protocols.projects import Envelope, Meta
from control_kernel.protocols.runtime import (
    ActivityAttemptResource,
    ActivityKind,
    ActivityResource,
    ActivityStatus,
    CheckpointResource,
    CommandOperation,
)
from control_kernel.storage.activities import Activities, Commands
from control_kernel.storage.checkpoints import list_checkpoints_for_activity
from control_kernel.storage.claims import list_attempts_for_activity
from control_kernel.storage.policies import ScopeNotFound
from fastapi import Depends, FastAPI, Query, Request

from control_api.errors import ApiError
from control_api.identity import Principal


def register_activity_routes(app: FastAPI, identity, store, cursor_key) -> None:
    @app.get(
        "/api/v1/goals/{goal_id}/activities",
        response_model=Envelope[list[ActivityResource]],
    )
    def list_goal_activities(
        goal_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        kind: Annotated[ActivityKind | None, Query()] = None,
        status: Annotated[ActivityStatus | None, Query()] = None,
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有活动读取权限")
        secret = cursor_key()
        scope = json.dumps(
            [
                "/api/v1/goals/{goal_id}/activities",
                str(goal_id),
                p.sub,
                sorted(p.project_ids),
                kind,
                status,
            ],
            separators=(",", ":"),
        )
        after = None
        if cursor:
            try:
                raw = base64.urlsafe_b64decode(cursor).decode()
                id_value, mac = raw.split(".", 1)
                expected = hmac.new(secret, (scope + id_value).encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(mac, expected):
                    raise ValueError("cursor")
                after = UUID(id_value)
            except (ValueError, UnicodeError) as exc:
                raise ApiError(400, "INVALID_REQUEST", "分页标识无效") from exc
        try:
            rows = Activities(store(request).engine).list_for_goal(
                goal_id, p.sub, p.project_ids, limit + 1, after, kind, status
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        next_cursor = None
        if len(rows) > limit:
            value = str(rows[limit - 1].id)
            mac = hmac.new(secret, (scope + value).encode(), hashlib.sha256).hexdigest()
            next_cursor = base64.urlsafe_b64encode((value + "." + mac).encode()).decode()
        return Envelope(
            data=rows[:limit],
            meta=Meta(request_id=request.state.request_id, next_cursor=next_cursor),
        )

    @app.get(
        "/api/v1/tasks/{task_id}/activities",
        response_model=Envelope[list[ActivityResource]],
    )
    def list_task_activities(
        task_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        kind: Annotated[ActivityKind | None, Query()] = None,
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有活动读取权限")
        secret = cursor_key()
        scope = json.dumps(
            [
                "/api/v1/tasks/{task_id}/activities",
                str(task_id),
                p.sub,
                sorted(p.project_ids),
                kind,
            ],
            separators=(",", ":"),
        )
        after = None
        if cursor:
            try:
                raw = base64.urlsafe_b64decode(cursor).decode()
                id_value, mac = raw.split(".", 1)
                expected = hmac.new(secret, (scope + id_value).encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(mac, expected):
                    raise ValueError("cursor")
                after = UUID(id_value)
            except (ValueError, UnicodeError) as exc:
                raise ApiError(400, "INVALID_REQUEST", "分页标识无效") from exc
        try:
            rows = Activities(store(request).engine).list_for_task(
                task_id, p.sub, p.project_ids, limit + 1, after, kind
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        next_cursor = None
        if len(rows) > limit:
            value = str(rows[limit - 1].id)
            mac = hmac.new(secret, (scope + value).encode(), hashlib.sha256).hexdigest()
            next_cursor = base64.urlsafe_b64encode((value + "." + mac).encode()).decode()
        return Envelope(
            data=rows[:limit],
            meta=Meta(request_id=request.state.request_id, next_cursor=next_cursor),
        )

    @app.get("/api/v1/activities/{activity_id}", response_model=Envelope[ActivityResource])
    def get_activity(
        activity_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        human = set(p.roles) & {"viewer", "operator", "approver", "admin"}
        if not human and "worker" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "没有活动读取权限")
        try:
            if human:
                result = Activities(store(request).engine).get(
                    activity_id, p.sub, p.project_ids
                )
            else:
                from control_kernel.protocols.runtime import WorkerForbidden

                try:
                    result = Activities(store(request).engine).get_for_worker(
                        activity_id, p.sub
                    )
                except WorkerForbidden as exc:
                    raise ApiError(403, "FORBIDDEN", "未登记 worker") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.get(
        "/api/v1/activities/{activity_id}/attempts",
        response_model=Envelope[list[ActivityAttemptResource]],
    )
    def list_activity_attempts(
        activity_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有 attempt 读取权限")
        secret = cursor_key()
        scope = json.dumps(
            [
                "/api/v1/activities/{activity_id}/attempts",
                str(activity_id),
                p.sub,
                sorted(p.project_ids),
            ],
            separators=(",", ":"),
        )
        after = None
        if cursor:
            try:
                raw = base64.urlsafe_b64decode(cursor).decode()
                id_value, mac = raw.split(".", 1)
                expected = hmac.new(secret, (scope + id_value).encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(mac, expected):
                    raise ValueError("cursor")
                after = UUID(id_value)
            except (ValueError, UnicodeError) as exc:
                raise ApiError(400, "INVALID_REQUEST", "分页标识无效") from exc
        try:
            rows = list_attempts_for_activity(
                store(request).engine,
                activity_id,
                p.sub,
                p.project_ids,
                limit + 1,
                after,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        next_cursor = None
        if len(rows) > limit:
            value = str(rows[limit - 1].id)
            mac = hmac.new(secret, (scope + value).encode(), hashlib.sha256).hexdigest()
            next_cursor = base64.urlsafe_b64encode((value + "." + mac).encode()).decode()
        return Envelope(
            data=rows[:limit],
            meta=Meta(request_id=request.state.request_id, next_cursor=next_cursor),
        )

    @app.get(
        "/api/v1/activities/{activity_id}/checkpoints",
        response_model=Envelope[list[CheckpointResource]],
    )
    def list_activity_checkpoints(
        activity_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有 checkpoint 读取权限")
        secret = cursor_key()
        scope = json.dumps(
            [
                "/api/v1/activities/{activity_id}/checkpoints",
                str(activity_id),
                p.sub,
                sorted(p.project_ids),
            ],
            separators=(",", ":"),
        )
        after = None
        if cursor:
            try:
                raw = base64.urlsafe_b64decode(cursor).decode()
                id_value, mac = raw.split(".", 1)
                expected = hmac.new(secret, (scope + id_value).encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(mac, expected):
                    raise ValueError("cursor")
                after = UUID(id_value)
            except (ValueError, UnicodeError) as exc:
                raise ApiError(400, "INVALID_REQUEST", "分页标识无效") from exc
        try:
            rows = list_checkpoints_for_activity(
                store(request).engine,
                activity_id,
                p.sub,
                p.project_ids,
                limit + 1,
                after,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        next_cursor = None
        if len(rows) > limit:
            value = str(rows[limit - 1].id)
            mac = hmac.new(secret, (scope + value).encode(), hashlib.sha256).hexdigest()
            next_cursor = base64.urlsafe_b64encode((value + "." + mac).encode()).decode()
        return Envelope(
            data=rows[:limit],
            meta=Meta(request_id=request.state.request_id, next_cursor=next_cursor),
        )

    @app.get("/api/v1/commands", response_model=Envelope[list[CommandOperation]])
    def list_commands(
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        project_id: Annotated[UUID | None, Query()] = None,
        goal_id: Annotated[UUID | None, Query()] = None,
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有命令读取权限")
        secret = cursor_key()
        scope = json.dumps(
            [
                "/api/v1/commands",
                str(project_id) if project_id else None,
                str(goal_id) if goal_id else None,
                p.sub,
                sorted(p.project_ids),
            ],
            separators=(",", ":"),
        )
        after = None
        if cursor:
            try:
                raw = base64.urlsafe_b64decode(cursor).decode()
                id_value, mac = raw.split(".", 1)
                expected = hmac.new(secret, (scope + id_value).encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(mac, expected):
                    raise ValueError("cursor")
                after = UUID(id_value)
            except (ValueError, UnicodeError) as exc:
                raise ApiError(400, "INVALID_REQUEST", "分页标识无效") from exc
        try:
            rows = Commands(store(request).engine).list_commands(
                p.sub,
                p.project_ids,
                project_id=project_id,
                goal_id=goal_id,
                limit=limit + 1,
                after=after,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        next_cursor = None
        if len(rows) > limit:
            value = str(rows[limit - 1].id)
            mac = hmac.new(secret, (scope + value).encode(), hashlib.sha256).hexdigest()
            next_cursor = base64.urlsafe_b64encode((value + "." + mac).encode()).decode()
        return Envelope(
            data=rows[:limit],
            meta=Meta(request_id=request.state.request_id, next_cursor=next_cursor),
        )

    @app.get("/api/v1/commands/{command_id}", response_model=Envelope[CommandOperation])
    def get_command(
        command_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有命令读取权限")
        try:
            result = Commands(store(request).engine).get(command_id, p.sub, p.project_ids)
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))
