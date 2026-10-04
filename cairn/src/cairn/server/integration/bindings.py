from __future__ import annotations

import sqlite3
import re
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException

from cairn.server.integration.ring_client import (
    RingConfig,
    RingContractUnknown,
    RingDenied,
    RingUnavailable,
    read_goal,
    read_release,
    read_snapshot,
)


def binding_for(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM ring_bindings WHERE cairn_project_id = ?", (project_id,)
    ).fetchone()


def project_role(conn: sqlite3.Connection, project_id: str, user_id: str) -> str | None:
    row = conn.execute(
        "SELECT role FROM project_acl WHERE project_id = ? AND user_id = ?",
        (project_id, user_id),
    ).fetchone()
    return row["role"] if row else None


def require_project_access(
    conn: sqlite3.Connection, project_id: str, user_id: str, *, owner: bool = False
) -> None:
    role = project_role(conn, project_id, user_id)
    if role is None:
        raise HTTPException(404, "Project not found")
    if owner and role != "owner":
        raise HTTPException(403, "Project owner required")


def require_unbound(conn: sqlite3.Connection, project_id: str) -> None:
    if binding_for(conn, project_id):
        raise HTTPException(409, "Ring-bound project cannot use Cairn worker or state writes")


def visible_goal(
    config: RingConfig, cookie: str, ring_project_id: str, ring_goal_id: str
) -> dict:
    try:
        goal = read_goal(config, cookie, ring_goal_id)
    except RingDenied as exc:
        raise HTTPException(404, "Ring Goal not found") from exc
    except RingUnavailable as exc:
        raise HTTPException(503, "Ring is unavailable") from exc
    except RingContractUnknown as exc:
        raise HTTPException(503, "Ring contract is unknown") from exc
    if (
        goal.get("id") != ring_goal_id
        or goal.get("project_id") != ring_project_id
        or type(goal.get("state_revision")) is not int
        or goal["state_revision"] < 1
    ):
        raise HTTPException(503, "Ring Goal version is unknown")
    return goal


def verified_goal(
    config: RingConfig, cookie: str, ring_project_id: str, ring_goal_id: str
) -> tuple[dict, dict]:
    goal = visible_goal(config, cookie, ring_project_id, ring_goal_id)
    try:
        snapshot = read_snapshot(config, cookie, ring_goal_id)
    except RingDenied as exc:
        raise HTTPException(404, "Ring Goal not found") from exc
    except RingUnavailable as exc:
        raise HTTPException(503, "Ring is unavailable") from exc
    except RingContractUnknown as exc:
        raise HTTPException(503, "Ring contract is unknown") from exc
    if (
        not isinstance(snapshot.get("goal"), dict)
        or snapshot["goal"].get("id") != ring_goal_id
        or snapshot["goal"].get("project_id") != ring_project_id
        or snapshot["goal"].get("state_revision") != goal["state_revision"]
        or not isinstance(snapshot.get("latest_seq"), str)
        or re.fullmatch(r"(0|[1-9][0-9]{0,18})", snapshot["latest_seq"]) is None
    ):
        raise HTTPException(503, "Ring Goal version is unknown")
    return goal, snapshot


def ring_status(config: RingConfig, cookie: str, binding: sqlite3.Row) -> dict:
    try:
        goal, snapshot = verified_goal(
            config, cookie, binding["ring_project_id"], binding["ring_goal_id"]
        )
    except HTTPException as exc:
        if exc.status_code == 404:
            raise
        return {"state": "UNKNOWN", "reason": exc.detail, "observed_at": None}
    status = goal.get("status")
    if status not in {
        "DRAFT", "PLANNING", "RUNNING", "VERIFYING", "PAUSING", "PAUSED",
        "CANCELLING", "CANCELLED", "BLOCKED", "FAILED", "DONE",
    }:
        return {"state": "UNKNOWN", "reason": "Ring status is unknown", "observed_at": None}
    if status == "DONE":
        try:
            release = read_release(config, cookie, binding["ring_goal_id"])
        except RingDenied:
            return {"state": "UNKNOWN", "reason": "Ring release is not visible", "observed_at": None}
        except (RingUnavailable, RingContractUnknown):
            return {"state": "UNKNOWN", "reason": "Ring release is unavailable", "observed_at": None}
        manifest = release.get("manifest")
        validity = release.get("validity")
        if (
            not isinstance(manifest, dict)
            or manifest.get("goal_id") != binding["ring_goal_id"]
            or manifest.get("project_id") != binding["ring_project_id"]
            or not isinstance(validity, dict)
            or validity.get("status") != "VALID"
            or goal.get("release_manifest_id") != manifest.get("id")
        ):
            return {"state": "UNKNOWN", "reason": "Ring release version is unknown", "observed_at": None}
    return {
        "state": status,
        "state_revision": goal["state_revision"],
        "latest_seq": snapshot["latest_seq"],
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "ring_updated_at": goal.get("updated_at"),
    }


def canonical_uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise HTTPException(422, "Ring IDs must be UUIDs") from exc
