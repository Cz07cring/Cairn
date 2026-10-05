"""Keep the original Cairn PlanInput request identity until Ring acknowledges it."""

from __future__ import annotations

import sqlite3
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from cairn.server.integration.intent_bridge import digest
from cairn.server.integration.plan_snapshot import decode_snapshot
from cairn.server.services import utcnow


def _canonical_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def registration_view(row: sqlite3.Row) -> dict[str, Any]:
    """Return an observed local write state, never a Ring Goal/Task outcome."""
    return {
        "snapshot_digest": row["snapshot_digest"],
        "state": row["state"],
        "ring_plan_input_id": row["ring_plan_input_id"],
        "ring_content_digest": row["ring_content_digest"],
        "ring_status_observed": row["ring_status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def prepare_registration(
    conn: sqlite3.Connection, *, project_id: str, actor: str,
    ring_project_id: str, ring_goal_id: str, snapshot_digest: str,
) -> tuple[sqlite3.Row, dict[str, Any]]:
    """Persist the exact body and key before any network write."""
    sealed = conn.execute(
        """SELECT project_id,ring_project_id,ring_goal_id,created_by,canonical_json
           FROM ring_plan_snapshots WHERE project_id=? AND digest=?""",
        (project_id, snapshot_digest),
    ).fetchone()
    if sealed is None:
        raise HTTPException(404, "Sealed snapshot not found")
    scope = (project_id, ring_project_id, ring_goal_id, actor)
    if tuple(sealed[key] for key in ("project_id", "ring_project_id", "ring_goal_id", "created_by")) != scope:
        raise HTTPException(409, "Sealed snapshot does not match the current actor and binding")
    raw = bytes(sealed["canonical_json"])
    snapshot = decode_snapshot(raw, snapshot_digest)
    if (
        snapshot.get("schema") != "PlanInput/v1"
        or tuple(snapshot.get(key) for key in ("cairn_project_id", "ring_project_id", "ring_goal_id", "created_by")) != scope
        or not _canonical_uuid(snapshot.get("candidate_plan_id"))
        or not isinstance(snapshot.get("candidate_content_digest"), str)
    ):
        raise HTTPException(503, "Stored snapshot scope is inconsistent")
    # The same actor and snapshot always use the same Ring idempotency identity.
    key = "cairn-plan-input-" + digest({
        "schema": "CairnPlanInputRegistration/v1", "project_id": project_id,
        "ring_project_id": ring_project_id, "ring_goal_id": ring_goal_id,
        "actor": actor, "snapshot_digest": snapshot_digest,
    }).split(":", 1)[1]
    row = conn.execute(
        """SELECT * FROM ring_plan_input_registrations
           WHERE project_id=? AND snapshot_digest=? AND actor=?""",
        (project_id, snapshot_digest, actor),
    ).fetchone()
    if row is None:
        now = utcnow()
        conn.execute(
            """INSERT INTO ring_plan_input_registrations
               (project_id,snapshot_digest,ring_project_id,ring_goal_id,actor,
                idempotency_key,request_json,state,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,'UNKNOWN',?,?)""",
            (project_id, snapshot_digest, ring_project_id, ring_goal_id, actor,
             key, raw, now, now),
        )
        row = conn.execute(
            """SELECT * FROM ring_plan_input_registrations
               WHERE project_id=? AND snapshot_digest=? AND actor=?""",
            (project_id, snapshot_digest, actor),
        ).fetchone()
    elif (
        tuple(row[name] for name in ("ring_project_id", "ring_goal_id", "actor", "idempotency_key"))
        != (ring_project_id, ring_goal_id, actor, key)
        or bytes(row["request_json"]) != raw
    ):
        raise HTTPException(503, "Stored PlanInput registration is inconsistent")
    if row is None:
        raise HTTPException(503, "Stored PlanInput registration is unavailable")
    return row, snapshot


def accept_registration(
    conn: sqlite3.Connection, *, row: sqlite3.Row, snapshot: dict[str, Any],
    ring_result: dict[str, Any],
) -> sqlite3.Row:
    """Accept only an exact Ring acknowledgment for the saved snapshot."""
    if (
        not _canonical_uuid(ring_result.get("id"))
        or ring_result.get("project_id") != row["ring_project_id"]
        or ring_result.get("goal_id") != row["ring_goal_id"]
        or ring_result.get("created_by") != row["actor"]
        or ring_result.get("status") != "STAGED"
        or ring_result.get("content_digest") != row["snapshot_digest"]
        or ring_result.get("candidate_plan_id") != snapshot["candidate_plan_id"]
        or ring_result.get("candidate_content_digest") != snapshot["candidate_content_digest"]
        or ring_result.get("payload") != snapshot
        or ring_result.get("marks_goal_done") is not False
    ):
        raise HTTPException(503, "Ring PlanInput acknowledgment is inconsistent; result UNKNOWN")
    current = conn.execute(
        """SELECT * FROM ring_plan_input_registrations
           WHERE project_id=? AND snapshot_digest=? AND actor=?""",
        (row["project_id"], row["snapshot_digest"], row["actor"]),
    ).fetchone()
    if current is None or bytes(current["request_json"]) != bytes(row["request_json"]):
        raise HTTPException(503, "Saved PlanInput request changed; result UNKNOWN")
    if current["state"] == "ACKED":
        if (
            current["ring_plan_input_id"] != ring_result["id"]
            or current["ring_content_digest"] != ring_result["content_digest"]
        ):
            raise HTTPException(503, "Conflicting Ring PlanInput acknowledgment; result UNKNOWN")
        return current
    conn.execute(
        """UPDATE ring_plan_input_registrations SET state='ACKED',ring_plan_input_id=?,
           ring_content_digest=?,ring_status=?,updated_at=?
           WHERE project_id=? AND snapshot_digest=? AND actor=? AND state='UNKNOWN'""",
        (ring_result["id"], ring_result["content_digest"], ring_result["status"], utcnow(),
         row["project_id"], row["snapshot_digest"], row["actor"]),
    )
    result = conn.execute(
        """SELECT * FROM ring_plan_input_registrations
           WHERE project_id=? AND snapshot_digest=? AND actor=?""",
        (row["project_id"], row["snapshot_digest"], row["actor"]),
    ).fetchone()
    if result is None:
        raise HTTPException(503, "Stored PlanInput acknowledgment is unavailable")
    return result
