"""Cairn PlanInput HTTP（R1a）：operator 幂等登记 + Goal scoped 读取；≠ admit，≠ DONE。"""

from typing import Annotated
from uuid import UUID

from control_kernel.protocols.goals import GoalResource, PlanInputModeSelection
from control_kernel.protocols.plan_inputs import PlanInputPayload, PlanInputResource
from control_kernel.protocols.projects import Envelope, Meta
from control_kernel.protocols.runtime import InvalidGoalState, PlanRejected, StateRevisionConflict
from control_kernel.storage.plan_inputs import (
    get_plan_input,
    record_plan_input,
    select_plan_input_mode,
)
from control_kernel.storage.policies import ScopeNotFound, TrustBlocked
from control_kernel.storage.projects import ProjectConflict
from fastapi import Depends, FastAPI, Header, Request

from control_api.errors import ApiError
from control_api.identity import Principal


def register_plan_input_routes(app: FastAPI, identity, store) -> None:
    @app.put(
        "/api/v1/goals/{goal_id}/plan-input-mode",
        response_model=Envelope[GoalResource],
    )
    def put_plan_input_mode(
        goal_id: UUID,
        body: PlanInputModeSelection,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        if "operator" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要操作员权限")
        try:
            result = select_plan_input_mode(
                store(request).engine,
                goal_id,
                subject=p.sub,
                project_ids=p.project_ids,
                body=body,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except TrustBlocked as exc:
            raise ApiError(409, "INVALID_STATE", "项目信任核对中") from exc
        except InvalidGoalState as exc:
            raise ApiError(
                409, "INVALID_STATE", f"目标状态为 {exc.status}，无法选择规划输入模式"
            ) from exc
        except StateRevisionConflict as exc:
            raise ApiError(409, "STATE_REVISION_CONFLICT", "目标状态版本不匹配") from exc
        except PlanRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/api/v1/goals/{goal_id}/plan-inputs",
        status_code=201,
        response_model=Envelope[PlanInputResource],
    )
    def post_plan_input(
        goal_id: UUID,
        body: PlanInputPayload,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        """登记 Cairn PlanInput/v1 封存快照；候选以 Ring DB 重核，绝不改写其状态。

        不创建 Plan/Task、不派发 Runner/Temporal、绝不写 Goal/Task DONE。
        """
        if "operator" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要操作员权限")
        try:
            result = record_plan_input(
                store(request).engine,
                goal_id,
                subject=p.sub,
                project_ids=p.project_ids,
                key=idempotency_key,
                body=body,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except TrustBlocked as exc:
            raise ApiError(409, "INVALID_STATE", "项目信任核对中，暂不接受规划输入") from exc
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        except PlanRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", exc.message) from exc
        if result.marks_goal_done:
            raise ApiError(500, "INTERNAL", "规划输入登记不得写 Goal DONE")
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.get(
        "/api/v1/goals/{goal_id}/plan-inputs/{plan_input_id}",
        response_model=Envelope[PlanInputResource],
    )
    def read_plan_input(
        goal_id: UUID,
        plan_input_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        """Goal scoped 读取；ID 与 goal 不符统一 404；读回复算字节 digest。"""
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有规划输入读取权限")
        try:
            result = get_plan_input(
                store(request).engine,
                goal_id=goal_id,
                plan_input_id=plan_input_id,
                subject=p.sub,
                project_ids=p.project_ids,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))
