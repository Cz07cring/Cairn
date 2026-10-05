"""ReadModel HTTP：snapshot / SSE events / system status。"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from control_kernel.protocols.projects import Envelope, Meta
from control_kernel.protocols.read_model import GoalSnapshot, SystemStatus
from control_kernel.storage.policies import ScopeNotFound
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import StreamingResponse
from read_model import EventCursorExpired, goal_events, goal_snapshot, system_status

from control_api.errors import ApiError
from control_api.identity import Principal

# 长轮询窗口：窗口内无新事件则结束流；客户端按 retry 重连（标准 EventSource）。
HOLD_SECONDS = 20.0
HEARTBEAT_SECONDS = 15.0
POLL_SECONDS = 0.5
RETRY_MS = 2000


def _sse_frame(event: dict) -> bytes:
    body = (
        f"id: {event['seq']}\n"
        f"event: {event['type']}\n"
        f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n"
        "\n"
    )
    return body.encode("utf-8")


def register_read_model_routes(app: FastAPI, identity, store) -> None:
    @app.get("/api/v1/goals/{goal_id}/snapshot", response_model=Envelope[GoalSnapshot])
    def get_snapshot(
        goal_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有目标读取权限")
        try:
            result = goal_snapshot(store(request).engine, goal_id, p.sub, p.project_ids)
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.get("/api/v1/goals/{goal_id}/events")
    async def get_events(
        goal_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        after_seq: Annotated[
            str | None, Query(pattern=r"^(0|[1-9][0-9]*)$", max_length=19)
        ] = None,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ):
        """text/event-stream：id=seq，event=type，data=Event；持有窗口内心跳，结束后重连。"""
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有目标读取权限")
        if (
            after_seq is not None
            and last_event_id is not None
            and after_seq != last_event_id
        ):
            raise ApiError(400, "INVALID_REQUEST", "after_seq 与 Last-Event-ID 冲突")
        cursor = after_seq if after_seq is not None else last_event_id
        if cursor is None:
            cursor = "0"
        if last_event_id is not None and after_seq is None and (
            len(last_event_id) > 19
            or not last_event_id.isascii()
            or not last_event_id.isdigit()
            or (len(last_event_id) > 1 and last_event_id.startswith("0"))
        ):
            raise ApiError(400, "INVALID_REQUEST", "Last-Event-ID 无效")
        start_seq = int(cursor)
        engine = store(request).engine
        subject = p.sub
        project_ids = list(p.project_ids)

        try:
            await asyncio.to_thread(
                goal_events,
                engine,
                goal_id,
                subject,
                project_ids,
                after_seq=start_seq,
                limit=1,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except EventCursorExpired as exc:
            raise ApiError(410, "EVENT_CURSOR_EXPIRED", "事件游标已失效，请重新 snapshot") from exc
        except ValueError as exc:
            raise ApiError(400, "INVALID_REQUEST", "after_seq 无效") from exc

        async def frames() -> AsyncIterator[bytes]:
            yield f"retry: {RETRY_MS}\n\n".encode()
            seq = start_seq
            # 先追赶积压
            try:
                rows = await asyncio.to_thread(
                    goal_events,
                    engine,
                    goal_id,
                    subject,
                    project_ids,
                    after_seq=seq,
                    limit=200,
                )
            except EventCursorExpired:
                yield b": cursor-expired\n\n"
                return
            for event in rows:
                yield _sse_frame(event)
                seq = int(event["seq"])
            # 有积压则本轮结束，客户端按 retry 续订；无积压则短持有等新事件
            if rows:
                return
            last_beat = asyncio.get_running_loop().time()
            hold_deadline = last_beat + HOLD_SECONDS
            while asyncio.get_running_loop().time() < hold_deadline:
                if await request.is_disconnected():
                    return
                await asyncio.sleep(POLL_SECONDS)
                try:
                    rows = await asyncio.to_thread(
                        goal_events,
                        engine,
                        goal_id,
                        subject,
                        project_ids,
                        after_seq=seq,
                        limit=200,
                    )
                except EventCursorExpired:
                    yield b": cursor-expired\n\n"
                    return
                except ScopeNotFound:
                    return
                for event in rows:
                    yield _sse_frame(event)
                    seq = int(event["seq"])
                    return
                now = asyncio.get_running_loop().time()
                if now - last_beat >= HEARTBEAT_SECONDS:
                    yield b": heartbeat\n\n"
                    last_beat = now

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/v1/system/status", response_model=Envelope[SystemStatus])
    def get_system_status(
        project_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin", "ops"}:
            raise ApiError(403, "FORBIDDEN", "没有系统状态读取权限")
        objects = request.app.state.objects
        try:
            result = system_status(
                store(request).engine,
                project_id,
                p.sub,
                p.project_ids,
                object_store_probe=objects.probe if objects is not None else None,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "项目不存在") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))
