"""审计只读查询：Task 原始列表与 Goal 联合 GoalAuditItem。"""

import base64
import hashlib
import hmac
import json
from typing import Annotated
from uuid import UUID

from control_kernel.protocols.audits import (
    AuditResource,
    GoalAuditItem,
    GoalReviewEnsureRequest,
    GoalReviewEnsureResult,
)
from control_kernel.protocols.projects import Envelope, Meta
from control_kernel.protocols.runtime import PlanRejected
from control_kernel.storage.audits import (
    ensure_goal_review_activity,
    list_goal_audits,
    list_task_audits,
)
from control_kernel.storage.policies import ConfigurationVersions, ScopeNotFound
from fastapi import Depends, FastAPI, Header, Query, Request
from sqlalchemy import text

from control_api.errors import ApiError
from control_api.identity import Principal


def register_audit_routes(app: FastAPI, identity, store, cursor_key=None) -> None:
    @app.get(
        "/api/v1/tasks/{task_id}/audits",
        response_model=Envelope[list[AuditResource]],
    )
    def list_audits(
        task_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
    ):
        if not {"viewer", "operator", "admin", "worker"} & set(p.roles):
            raise ApiError(403, "FORBIDDEN", "需要查看权限")
        if cursor_key is None:
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "分页密钥未配置")
        secret = cursor_key()
        scope = json.dumps(
            [
                "/api/v1/tasks/{task_id}/audits",
                str(task_id),
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
                expected = hmac.new(
                    secret, (scope + id_value).encode(), hashlib.sha256
                ).hexdigest()
                if not hmac.compare_digest(mac, expected):
                    raise ValueError("cursor")
                after = UUID(id_value)
            except (ValueError, UnicodeError) as exc:
                raise ApiError(400, "INVALID_REQUEST", "分页标识无效") from exc
        try:
            result = list_task_audits(
                store(request).engine,
                task_id,
                p.project_ids,
                limit=limit + 1,
                after=after,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        next_cursor = None
        if len(result) > limit:
            value = str(result[limit - 1].id)
            mac = hmac.new(secret, (scope + value).encode(), hashlib.sha256).hexdigest()
            next_cursor = base64.urlsafe_b64encode((value + "." + mac).encode()).decode()
        return Envelope(
            data=result[:limit],
            meta=Meta(request_id=request.state.request_id, next_cursor=next_cursor),
        )

    @app.get(
        "/api/v1/goals/{goal_id}/audits",
        response_model=Envelope[list[GoalAuditItem]],
    )
    def list_goal_audit_items(
        goal_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
    ):
        """人机可读；ACTIVE worker 持有该 Goal 下 attempt 亦可读（INTEGRATE 消费 aggregation）。

        只读投影；绝不写 Goal DONE。
        """
        human = set(p.roles) & {"viewer", "operator", "approver", "admin"}
        if not human and "worker" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "没有审计读取权限")
        if cursor_key is None:
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "分页密钥未配置")
        secret = cursor_key()
        scope = json.dumps(
            [
                "/api/v1/goals/{goal_id}/audits",
                str(goal_id),
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
            if human:
                rows = list_goal_audits(
                    store(request).engine,
                    goal_id,
                    p.sub,
                    p.project_ids,
                    limit + 1,
                    after,
                )
            else:
                # worker：须已持 Goal 下 ACTIVE attempt；用 goal.project_id 过 scope 门
                from control_kernel.protocols.runtime import WorkerForbidden
                from control_kernel.storage.goals import Goals

                try:
                    goal = Goals(store(request).engine).get_for_worker(goal_id, p.sub)
                except WorkerForbidden as exc:
                    raise ApiError(403, "FORBIDDEN", "未登记 worker") from exc
                rows = list_goal_audits(
                    store(request).engine,
                    goal_id,
                    p.sub,
                    [str(goal.project_id)],
                    limit + 1,
                    after,
                )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        next_cursor = None
        if len(rows) > limit:
            # CANDIDATE_AUDIT 用 audit.id；GOAL_REVIEW 用 review.id（联合游标）
            last = rows[limit - 1]
            value = str(last.audit.id) if last.record_type == "CANDIDATE_AUDIT" else str(
                last.review.id
            )
            mac = hmac.new(secret, (scope + value).encode(), hashlib.sha256).hexdigest()
            next_cursor = base64.urlsafe_b64encode((value + "." + mac).encode()).decode()
        return Envelope(
            data=rows[:limit],
            meta=Meta(request_id=request.state.request_id, next_cursor=next_cursor),
        )

    @app.post(
        "/internal/v1/goals/{goal_id}/goal-reviews",
        status_code=201,
        response_model=Envelope[GoalReviewEnsureResult],
    )
    def ensure_goal_review(
        goal_id: UUID,
        body: GoalReviewEnsureRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        """按 trigger_key 去重创建 AUDIT(GOAL_REVIEW)；快照由 Kernel 钉扎（≠ DONE）。

        Idempotency-Key 仅作请求去重外壳；业务去重权威为 trigger_key。
        """
        if not {"worker", "admin", "operator"} & set(p.roles):
            raise ApiError(403, "FORBIDDEN", "需要 worker/admin/operator 创建 GOAL_REVIEW")
        _ = idempotency_key  # 外壳保留；业务幂等见 trigger_key
        engine = store(request).engine
        try:
            with engine.connect() as db:
                goal = (
                    db.execute(
                        text("SELECT project_id FROM goals WHERE id=:id"),
                        {"id": goal_id},
                    )
                    .mappings()
                    .first()
                )
                if goal is None:
                    raise ScopeNotFound()
                ConfigurationVersions.check_scope(
                    db, goal["project_id"], p.sub, p.project_ids
                )
            activity_id, snap, created, remaining, max_reviews, min_interval, stagnation = (
                ensure_goal_review_activity(
                    engine,
                    goal_id=goal_id,
                    trigger_key=body.trigger_key,
                    review_seq=body.review_seq,
                )
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except PlanRejected as exc:
            raise ApiError(
                422,
                exc.code or "VALIDATION_ERROR",
                str(exc),
            ) from exc
        return Envelope(
            data=GoalReviewEnsureResult(
                activity_id=activity_id,
                review_snapshot_digest=snap,
                created=created,
                reviews_remaining=remaining,
                max_reviews=max_reviews,
                min_interval_seconds=min_interval,
                stagnation_seconds=stagnation,
                marks_goal_done=False,
            ),
            meta=Meta(request_id=request.state.request_id),
        )
