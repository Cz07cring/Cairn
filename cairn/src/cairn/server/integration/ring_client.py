from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import ParseResult, urlencode, urlparse

import requests


class RingUnavailable(Exception):
    pass


class RingDenied(Exception):
    pass


class RingContractUnknown(Exception):
    pass


class RingWriteRejected(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


def _secure_origin(parsed: ParseResult) -> bool:
    return parsed.scheme == "https" or (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    )


@dataclass(frozen=True)
class RingConfig:
    base_url: str
    public_origin: str

    @classmethod
    def load(cls) -> RingConfig:
        base_url = os.environ.get("CAIRN_RING_BASE_URL", "").rstrip("/")
        public_origin = os.environ.get("CAIRN_PUBLIC_ORIGIN", "").rstrip("/")
        base = urlparse(base_url)
        if (
            not _secure_origin(base)
            or not base.netloc
            or base.path
            or base.query
            or base.fragment
            or base.username
        ):
            raise RuntimeError("CAIRN_RING_BASE_URL must use HTTPS or loopback HTTP")
        parsed = urlparse(public_origin)
        if (
            not _secure_origin(parsed)
            or not parsed.netloc
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
        ):
            raise RuntimeError("CAIRN_PUBLIC_ORIGIN must use HTTPS or loopback HTTP")
        return cls(base_url, public_origin)


def _read(config: RingConfig, path: str, session_cookie: str) -> dict[str, Any]:
    try:
        response = requests.get(
            f"{config.base_url}{path}",
            cookies={"ring_session": session_cookie},
            headers={"Accept": "application/json"},
            timeout=5,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise RingUnavailable("Ring is unavailable") from exc
    if response.status_code in {401, 403, 404}:
        raise RingDenied("Ring resource is not visible")
    if response.status_code != 200:
        raise RingUnavailable("Ring read failed")
    try:
        envelope = response.json()
    except ValueError as exc:
        raise RingContractUnknown("Ring response is not JSON") from exc
    if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), dict):
        raise RingContractUnknown("Ring response envelope is unknown")
    return envelope["data"]


def read_session(config: RingConfig, session_cookie: str) -> dict[str, Any]:
    data = _read(config, "/api/v1/auth/session", session_cookie)
    if (
        not isinstance(data.get("user_id"), str)
        or not data["user_id"]
        or not isinstance(data.get("project_ids"), list)
        or not all(isinstance(p, str) for p in data["project_ids"])
        or not isinstance(data.get("roles"), list)
        or not all(isinstance(r, str) for r in data["roles"])
        or not set(data["roles"]) & {"viewer", "operator", "approver", "admin"}
        or not isinstance(data.get("csrf_token"), str)
        or not data["csrf_token"]
    ):
        raise RingContractUnknown("Ring session fields are unknown")
    return data


def read_goal(config: RingConfig, cookie: str, goal_id: str) -> dict[str, Any]:
    return _read(config, f"/api/v1/goals/{goal_id}", cookie)


def read_snapshot(config: RingConfig, cookie: str, goal_id: str) -> dict[str, Any]:
    return _read(config, f"/api/v1/goals/{goal_id}/snapshot", cookie)


def read_release(config: RingConfig, cookie: str, goal_id: str) -> dict[str, Any]:
    return _read(config, f"/api/v1/goals/{goal_id}/release", cookie)


def read_plan_input(
    config: RingConfig, cookie: str, goal_id: str, plan_input_id: str,
) -> dict[str, Any]:
    return _read(config, f"/api/v1/goals/{goal_id}/plan-inputs/{plan_input_id}", cookie)


def read_collection(
    config: RingConfig, cookie: str, path: str, *, project_id: str | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        query = urlencode({"limit": 200, **({"project_id": project_id} if project_id else {}),
                           **({"cursor": cursor} if cursor else {})})
        try:
            response = requests.get(
                f"{config.base_url}{path}?{query}", cookies={"ring_session": cookie},
                headers={"Accept": "application/json"}, timeout=5, allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise RingUnavailable("Ring is unavailable") from exc
        if response.status_code in {401, 403, 404}:
            raise RingDenied("Ring resource is not visible")
        if response.status_code != 200:
            raise RingUnavailable("Ring read failed")
        try:
            envelope = response.json()
        except ValueError as exc:
            raise RingContractUnknown("Ring response is not JSON") from exc
        if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), list):
            raise RingContractUnknown("Ring collection is unknown")
        page = envelope["data"]
        if not all(isinstance(item, dict) for item in page):
            raise RingContractUnknown("Ring collection item is unknown")
        items.extend(page)
        meta = envelope.get("meta")
        if not isinstance(meta, dict):
            raise RingContractUnknown("Ring collection metadata is unknown")
        next_cursor = meta.get("next_cursor")
        if next_cursor is None:
            return items
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
            raise RingContractUnknown("Ring collection cursor is unknown")
        seen.add(next_cursor)
        cursor = next_cursor


def submit_plan_candidate(
    config: RingConfig, cookie: str, csrf_token: str, goal_id: str,
    idempotency_key: str, body: dict[str, Any],
) -> dict[str, Any]:
    try:
        response = requests.post(
            f"{config.base_url}/api/v1/goals/{goal_id}/plans",
            cookies={"ring_session": cookie},
            headers={"Accept": "application/json", "X-CSRF-Token": csrf_token,
                     "Idempotency-Key": idempotency_key},
            json=body, timeout=10, allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise RingUnavailable("Ring plan result is unknown") from exc
    try:
        envelope = response.json()
    except ValueError as exc:
        raise RingContractUnknown("Ring plan response is not JSON") from exc
    if response.status_code == 201:
        data = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(data, dict) or data.get("status") != "CANDIDATE" or not isinstance(data.get("id"), str):
            raise RingContractUnknown("Ring candidate response is unknown")
        return data
    if response.status_code in {401, 403, 404, 409, 422}:
        error = envelope.get("error") if isinstance(envelope, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        raise RingWriteRejected(response.status_code, message if isinstance(message, str) else "Ring rejected candidate")
    raise RingUnavailable("Ring plan result is unknown")


def submit_plan_input(
    config: RingConfig, cookie: str, csrf_token: str, goal_id: str,
    idempotency_key: str, request_bytes: bytes,
) -> dict[str, Any]:
    """用固定正文和原幂等键登记 Cairn 快照；异常一律不推断写入失败。"""
    try:
        response = requests.post(
            f"{config.base_url}/api/v1/goals/{goal_id}/plan-inputs",
            cookies={"ring_session": cookie},
            headers={"Accept": "application/json", "Content-Type": "application/json",
                     "X-CSRF-Token": csrf_token,
                     "Idempotency-Key": idempotency_key},
            data=request_bytes, timeout=10, allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise RingUnavailable("Ring PlanInput result is unknown") from exc
    try:
        envelope = response.json()
    except ValueError as exc:
        raise RingContractUnknown("Ring PlanInput response is not JSON") from exc
    if response.status_code == 201:
        data = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(data, dict):
            raise RingContractUnknown("Ring PlanInput response is unknown")
        return data
    if response.status_code in {401, 403, 404, 409, 422}:
        error = envelope.get("error") if isinstance(envelope, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        raise RingWriteRejected(
            response.status_code,
            message if isinstance(message, str) else "Ring rejected PlanInput",
        )
    raise RingUnavailable("Ring PlanInput result is unknown")
