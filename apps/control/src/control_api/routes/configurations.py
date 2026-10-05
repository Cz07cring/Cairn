"""Shared immutable configuration HTTP behavior; typed route wrappers define actual schemas."""

import base64
import hashlib
import hmac
import json
from uuid import UUID

from control_kernel.protocols.projects import Envelope, Meta
from control_kernel.protocols.verification import VerificationProfileCreate
from control_kernel.storage.projects import ProjectConflict
from evidence_ledger.content import encode
from fastapi import Request

from control_api.errors import ApiError
from control_api.identity import Principal


class ConfigurationHTTP:
    def __init__(self, store, cursor_key, repository, object_type: str, path: str):
        self.store, self.cursor_key, self.repository = store, cursor_key, repository
        self.object_type, self.path = object_type, path

    def create(self, body, request: Request, principal: Principal, key: str):
        if "admin" not in principal.roles:
            raise ApiError(403, "FORBIDDEN", "需要管理员权限")
        references = []
        if isinstance(body, VerificationProfileCreate):
            references = [
                {
                    "object_type": "VerifierDefinition",
                    "object_id": body.verifier_ref,
                    "content_digest": body.verifier_digest,
                }
            ]
        try:
            canonical = encode(
                json.dumps(
                    {
                        "schema_version": 3,
                        "object_type": self.object_type,
                        "content": body.model_dump(mode="json"),
                        "reference_bindings": references,
                    }
                )
            )
        except ValueError as exc:
            raise ApiError(422, "VALIDATION_ERROR", "配置内容无效") from exc
        digest = "sha256:" + hashlib.sha256(canonical).hexdigest()
        try:
            result = self.repository(self.store(request).engine).create(
                principal.sub,
                principal.project_ids,
                key,
                body,
                digest,
                json.loads(canonical)["content"],
            )
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    def list(
        self,
        project_id: UUID,
        request: Request,
        principal: Principal,
        limit: int,
        cursor: str | None,
    ):
        if not set(principal.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有读取权限")
        scope = json.dumps(
            [self.path, str(project_id), principal.sub, sorted(principal.project_ids)],
            separators=(",", ":"),
        )
        secret = self.cursor_key()
        after = None
        if cursor:
            try:
                raw = base64.urlsafe_b64decode(cursor).decode()
                value, signature = raw.split(".", 1)
                expected = hmac.new(secret, (scope + value).encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(signature, expected):
                    raise ValueError("signature")
                after = UUID(value)
            except (ValueError, UnicodeError) as exc:
                raise ApiError(400, "INVALID_REQUEST", "分页标识无效") from exc
        rows = self.repository(self.store(request).engine).list(
            project_id, principal.sub, principal.project_ids, limit + 1, after
        )
        next_cursor = None
        if len(rows) > limit:
            value = str(rows[limit - 1].id)
            signature = hmac.new(secret, (scope + value).encode(), hashlib.sha256).hexdigest()
            next_cursor = base64.urlsafe_b64encode((value + "." + signature).encode()).decode()
        return Envelope(
            data=rows[:limit],
            meta=Meta(request_id=request.state.request_id, next_cursor=next_cursor),
        )
