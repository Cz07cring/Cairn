"""Broker 宿主：dispatch 后真实读文件、collector 上传、提交 TrustedReceipt。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from .git_diff import execute_git_diff
from .read_file import execute_read_file
from .run_tests import execute_run_tests
from .seal_candidate import execute_seal_candidate_prepare
from .write_file import execute_write_file


class BrokerProtocolError(RuntimeError):
    """Control API 未按 Broker 协议接受请求。"""


def _require_http_status(response, expected: int, operation: str):
    """显式校验 HTTP 状态；不能依赖会被 Python ``-O`` 删除的 assert。"""
    actual = getattr(response, "status_code", None)
    if actual == expected:
        return response
    detail = str(getattr(response, "text", "")).strip()[:500]
    suffix = f"：{detail}" if detail else ""
    raise BrokerProtocolError(
        f"{operation}失败：预期 HTTP {expected}，实际 HTTP {actual}{suffix}"
    )


def run_read_file_effect(
    client,
    *,
    worker_auth: dict,
    lease: dict,
    effect: dict,
    project_id: UUID,
    workspace_root: Path,
    allowed_paths: list[str],
    input_bytes: bytes,
) -> dict:
    """对已 PREPARED 的 effect：dispatch → 沙箱读 → PUT artifact → receipt。

    调用方不得手造 result 字节；内容必须来自 workspace_root 真实文件。
    """
    return _run_tool_effect(
        client,
        worker_auth=worker_auth,
        lease=lease,
        effect=effect,
        project_id=project_id,
        execute=lambda: execute_read_file(
            workspace_root, input_bytes, allowed_paths=allowed_paths
        ),
        result_content=lambda result: result.content,
        result_meta=lambda result: result,
    )


def run_write_file_effect(
    client,
    *,
    worker_auth: dict,
    lease: dict,
    effect: dict,
    project_id: UUID,
    workspace_root: Path,
    allowed_paths: list[str],
    input_bytes: bytes,
    protected_paths: list[str] | None = None,
) -> dict:
    """对已 PREPARED 的 write_file：dispatch → 沙箱写 → PUT 证据工件 → receipt。"""
    return _run_tool_effect(
        client,
        worker_auth=worker_auth,
        lease=lease,
        effect=effect,
        project_id=project_id,
        execute=lambda: execute_write_file(
            workspace_root,
            input_bytes,
            allowed_paths=allowed_paths,
            protected_paths=protected_paths,
        ),
        result_content=lambda result: result.content,
        result_meta=lambda result: result,
    )


def run_run_tests_effect(
    client,
    *,
    worker_auth: dict,
    lease: dict,
    effect: dict,
    project_id: UUID,
    workspace_root: Path,
    input_bytes: bytes,
    timeout_seconds: int = 120,
) -> dict:
    """对已 PREPARED 的 run_tests：dispatch → 批准 suite argv → PUT 证据 → receipt。"""
    return _run_tool_effect(
        client,
        worker_auth=worker_auth,
        lease=lease,
        effect=effect,
        project_id=project_id,
        execute=lambda: execute_run_tests(
            workspace_root,
            input_bytes,
            timeout_seconds=timeout_seconds,
        ),
        result_content=lambda result: result.content,
        result_meta=lambda result: result,
    )


def run_git_diff_effect(
    client,
    *,
    worker_auth: dict,
    lease: dict,
    effect: dict,
    project_id: UUID,
    workspace_root: Path,
    input_bytes: bytes,
    timeout_seconds: int = 30,
) -> dict:
    """对已 PREPARED 的 git_diff：dispatch → 固定 git argv → PUT 证据 → receipt。"""
    return _run_tool_effect(
        client,
        worker_auth=worker_auth,
        lease=lease,
        effect=effect,
        project_id=project_id,
        execute=lambda: execute_git_diff(
            workspace_root,
            input_bytes,
            timeout_seconds=timeout_seconds,
        ),
        result_content=lambda result: result.content,
        result_meta=lambda result: result,
    )


def run_seal_candidate_effect(
    client,
    *,
    worker_auth: dict,
    lease: dict,
    effect: dict,
    project_id: UUID,
    workspace_root: Path,
    allowed_paths: list[str],
    input_bytes: bytes,
) -> dict:
    """dispatch → 真实 worktree 快照 → POST /candidates/seal → receipt。≠ Goal DONE。"""
    effect_id = effect["id"]
    dispatched = client.post(
        f"/internal/v1/effects/{effect_id}/dispatch",
        json={
            "lease": lease,
            "effect_state_revision": effect["state_revision"],
        },
        headers=worker_auth,
    )
    _require_http_status(dispatched, 200, "Effect 派发")
    effect = dispatched.json()["data"]

    prepared = execute_seal_candidate_prepare(
        workspace_root, input_bytes, allowed_paths=allowed_paths
    )
    result_ids: list[str] = []
    candidate = None
    evidence_body = prepared.content
    if prepared.observed_outcome == "SUCCEEDED" and prepared.snapshot_bytes is not None:
        # 先入库每个候选文件字节（digest 寻址），再封存快照元数据
        upload_headers = {
            **worker_auth,
            "Content-Type": "application/octet-stream",
            "X-Ring-Project-Id": str(project_id),
            "X-Ring-Activity-Id": str(lease["activity_id"]),
            "X-Ring-Attempt-Id": str(lease["attempt_id"]),
            "X-Ring-Fencing-Epoch": str(lease["fencing_epoch"]),
        }
        for blob_digest, blob_body in prepared.file_blobs.items():
            uploaded_blob = client.put(
                f"/internal/v1/artifacts/{blob_digest}/content",
                content=blob_body,
                headers=upload_headers,
            )
            _require_http_status(uploaded_blob, 201, "候选文件上传")
        snap_digest = "sha256:" + hashlib.sha256(prepared.snapshot_bytes).hexdigest()
        uploaded_snap = client.put(
            f"/internal/v1/artifacts/{snap_digest}/content",
            content=prepared.snapshot_bytes,
            headers=upload_headers,
        )
        _require_http_status(uploaded_snap, 201, "候选快照上传")
        snap_art_id = uploaded_snap.json()["data"]["id"]
        sealed = client.post(
            "/internal/v1/candidates/seal",
            json={
                "lease": lease,
                "workspace_snapshot_artifact_id": snap_art_id,
                "verification_profile_ids": prepared.verification_profile_ids,
            },
            headers=worker_auth,
        )
        if sealed.status_code != 201:
            # 封存失败：仍交 FAILED receipt，不冒充成功
            failed_at = datetime.now(UTC)
            receipt = client.post(
                f"/internal/v1/effects/{effect_id}/receipts",
                json={
                    "receipt_id": str(uuid4()),
                    "effect_id": effect_id,
                    "producer_activity_id": str(lease["activity_id"]),
                    "producer_attempt_id": str(lease["attempt_id"]),
                    "fencing_epoch": lease["fencing_epoch"],
                    "started_at": prepared.started_at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                    + "Z",
                    "finished_at": failed_at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                    "exit_code": 1,
                    "timed_out": False,
                    "result_artifact_ids": [],
                    "observed_outcome": "FAILED",
                },
                headers=worker_auth,
            )
            _require_http_status(receipt, 201, "封存失败 receipt 提交")
            return {
                "effect": effect,
                "result": prepared,
                "receipt": receipt.json()["data"],
                "result_artifact_ids": [],
                "candidate": None,
                "seal_error": sealed.text,
            }
        candidate = sealed.json()["data"]
        # 证据补上 candidate_id / content_digest
        meta = json.loads(evidence_body.decode()) if evidence_body else {}
        meta["candidate_manifest_id"] = candidate["id"]
        meta["candidate_content_digest"] = candidate["content_digest"]
        evidence_body = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode()

    if evidence_body is not None:
        digest = "sha256:" + hashlib.sha256(evidence_body).hexdigest()
        uploaded = client.put(
            f"/internal/v1/artifacts/{digest}/content",
            content=evidence_body,
            headers={
                **worker_auth,
                "Content-Type": "application/octet-stream",
                "X-Ring-Project-Id": str(project_id),
                "X-Ring-Activity-Id": str(lease["activity_id"]),
                "X-Ring-Attempt-Id": str(lease["attempt_id"]),
                "X-Ring-Fencing-Epoch": str(lease["fencing_epoch"]),
            },
        )
        _require_http_status(uploaded, 201, "候选证据上传")
        result_ids.append(uploaded.json()["data"]["id"])

    receipt = client.post(
        f"/internal/v1/effects/{effect_id}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "effect_id": effect_id,
            "producer_activity_id": str(lease["activity_id"]),
            "producer_attempt_id": str(lease["attempt_id"]),
            "fencing_epoch": lease["fencing_epoch"],
            "started_at": prepared.started_at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "finished_at": prepared.finished_at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "exit_code": prepared.exit_code,
            "timed_out": prepared.timed_out,
            "result_artifact_ids": result_ids,
            "observed_outcome": prepared.observed_outcome,
        },
        headers=worker_auth,
    )
    _require_http_status(receipt, 201, "候选 receipt 提交")
    return {
        "effect": effect,
        "result": prepared,
        "receipt": receipt.json()["data"],
        "result_artifact_ids": result_ids,
        "candidate": candidate,
    }


def _run_tool_effect(
    client,
    *,
    worker_auth: dict,
    lease: dict,
    effect: dict,
    project_id: UUID,
    execute,
    result_content,
    result_meta,
) -> dict:
    effect_id = effect["id"]
    dispatched = client.post(
        f"/internal/v1/effects/{effect_id}/dispatch",
        json={
            "lease": lease,
            "effect_state_revision": effect["state_revision"],
        },
        headers=worker_auth,
    )
    _require_http_status(dispatched, 200, "Effect 派发")
    effect = dispatched.json()["data"]

    result = execute()
    result_ids: list[str] = []
    body = result_content(result)
    if body is not None:
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        uploaded = client.put(
            f"/internal/v1/artifacts/{digest}/content",
            content=body,
            headers={
                **worker_auth,
                "Content-Type": "application/octet-stream",
                "X-Ring-Project-Id": str(project_id),
                "X-Ring-Activity-Id": str(lease["activity_id"]),
                "X-Ring-Attempt-Id": str(lease["attempt_id"]),
                "X-Ring-Fencing-Epoch": str(lease["fencing_epoch"]),
            },
        )
        _require_http_status(uploaded, 201, "工具结果上传")
        result_ids.append(uploaded.json()["data"]["id"])

    meta = result_meta(result)
    receipt = client.post(
        f"/internal/v1/effects/{effect_id}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "effect_id": effect_id,
            "producer_activity_id": str(lease["activity_id"]),
            "producer_attempt_id": str(lease["attempt_id"]),
            "fencing_epoch": lease["fencing_epoch"],
            "started_at": meta.started_at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "finished_at": meta.finished_at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "exit_code": meta.exit_code,
            "timed_out": meta.timed_out,
            "result_artifact_ids": result_ids,
            "observed_outcome": meta.observed_outcome,
        },
        headers=worker_auth,
    )
    _require_http_status(receipt, 201, "工具 receipt 提交")
    return {
        "effect": effect,
        "result": result,
        "receipt": receipt.json()["data"],
        "result_artifact_ids": result_ids,
    }
