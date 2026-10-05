"""Goal 事件追加与读取；seq 在 Goal 内单调递增。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Connection, text


def _occurred_at_z(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return stamp


def append_goal_event(
    db: Connection,
    *,
    project_id: UUID,
    goal_id: UUID,
    event_type: str,
    entity_id: UUID,
    entity_state_revision: int | None,
    resource_type: str,
) -> dict:
    """同事务追加一条 Event；payload 仅 INVALIDATE 通知，详情走 snapshot。"""
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
        {"scope": f"goal_events:{goal_id}"},
    )
    next_seq = db.execute(
        text("SELECT COALESCE(MAX(seq),0)+1 FROM goal_events WHERE goal_id=:goal"),
        {"goal": goal_id},
    ).scalar_one()
    event_id = uuid4()
    occurred = _occurred_at_z()
    event = {
        "schema_version": 1,
        "event_id": str(event_id),
        "project_id": str(project_id),
        "goal_id": str(goal_id),
        "seq": str(next_seq),
        "type": event_type,
        "entity_id": str(entity_id),
        "entity_state_revision": entity_state_revision,
        "occurred_at": occurred,
        "payload": {"resource_type": resource_type, "change": "INVALIDATE"},
    }
    db.execute(
        text(
            """INSERT INTO goal_events(
              goal_id,seq,event_id,project_id,type,entity_id,entity_state_revision,
              occurred_at,payload)
            VALUES(
              :goal,:seq,:eid,:project,:type,:entity,:rev,
              CAST(:occurred AS timestamptz),CAST(:payload AS jsonb))"""
        ),
        {
            "goal": goal_id,
            "seq": next_seq,
            "eid": event_id,
            "project": project_id,
            "type": event_type,
            "entity": entity_id,
            "rev": entity_state_revision,
            "occurred": occurred,
            "payload": json.dumps(
                {"resource_type": resource_type, "change": "INVALIDATE"},
                separators=(",", ":"),
            ),
        },
    )
    return event


def latest_seq(db: Connection, goal_id: UUID) -> str:
    value = db.execute(
        text("SELECT COALESCE(MAX(seq),0) FROM goal_events WHERE goal_id=:goal"),
        {"goal": goal_id},
    ).scalar_one()
    return str(int(value))


def list_events_after(
    db: Connection, goal_id: UUID, after_seq: int, limit: int
) -> list[dict]:
    rows = db.execute(
        text(
            """SELECT event_id,project_id,goal_id,seq,type,entity_id,entity_state_revision,
                      occurred_at,payload
               FROM goal_events
               WHERE goal_id=:goal AND seq>:after
               ORDER BY seq ASC LIMIT :limit"""
        ),
        {"goal": goal_id, "after": after_seq, "limit": limit},
    ).mappings()
    out = []
    for row in rows:
        occurred = row["occurred_at"]
        if isinstance(occurred, datetime):
            occurred_s = occurred.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        else:
            occurred_s = str(occurred)
        out.append(
            {
                "schema_version": 1,
                "event_id": str(row["event_id"]),
                "project_id": str(row["project_id"]),
                "goal_id": str(row["goal_id"]),
                "seq": str(row["seq"]),
                "type": row["type"],
                "entity_id": str(row["entity_id"]),
                "entity_state_revision": row["entity_state_revision"],
                "occurred_at": occurred_s,
                "payload": row["payload"],
            }
        )
    return out
