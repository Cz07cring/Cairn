from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import ParseResult, urlparse

import requests


class RingUnavailable(Exception):
    pass


class RingDenied(Exception):
    pass


class RingContractUnknown(Exception):
    pass


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
