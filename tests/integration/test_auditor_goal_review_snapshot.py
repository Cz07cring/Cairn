"""AUDITOR×GOAL_REVIEW：ensure 钉扎快照 → context-compile 绑定 EVIDENCE。"""

from __future__ import annotations

import os
from uuid import uuid4

from sqlalchemy import create_engine, text
from test_claims import _drain_ready, _register_worker, _start_goal


def test_auditor_goal_review_compile_binds_authoritative_snapshot(api, objects):
    store, _, _ = objects
    client, token, auth, goal, _plan = _start_goal(api, objects)
    client.app.state.objects = store

    ensure = client.post(
        f"/internal/v1/goals/{goal['id']}/goal-reviews",
        json={"trigger_key": f"critic:{goal['id']}:1"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert ensure.status_code == 201, ensure.text
    ensured = ensure.json()["data"]
    activity_id = ensured["activity_id"]
    snap = ensured["review_snapshot_digest"]
    assert snap.startswith("sha256:")

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.connect() as db:
        n = db.execute(
            text(
                """SELECT count(*) FROM goal_review_snapshots
                WHERE content_digest=:d"""
            ),
            {"d": snap},
        ).scalar_one()
        assert int(n) == 1
    _drain_ready(client, token, kinds=("AUDIT",))
    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE activities SET status='READY', updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": activity_id},
        )
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE status='READY' AND kind='AUDIT' AND id<>:id"""
            ),
            {"id": activity_id},
        )
    engine.dispose()

    subject = str(uuid4())
    _register_worker(subject, kinds=("AUDIT",))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["AUDIT"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert lease["activity"]["id"] == activity_id
    assert lease["activity"]["target"]["type"] == "GOAL_REVIEW"
    assert lease["activity"]["binding"]["subject_digest"] == snap

    compiled = client.post(
        f"/internal/v1/activities/{activity_id}/context-compile",
        json={"lease": lease["lease"], "max_input_tokens": 8192},
        headers=worker_auth,
    )
    assert compiled.status_code == 201, compiled.text
    content = compiled.json()["data"]["content"]
    assert content["role"] == "AUDITOR"
    assert content["planning_feedback_ids"] == []
    evidence = [b for b in content["input_bindings"] if b["classification"] == "EVIDENCE"]
    assert any(b["digest"] == snap for b in evidence)

    goal_after = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_after["status"] != "DONE"


def test_auditor_goal_review_compile_fails_closed_without_snapshot_row(api, objects):
    """binding 指向不存在的快照 digest 时 AUDITOR compile 失败关闭。"""
    store, _, _ = objects
    client, token, auth, goal, _plan = _start_goal(api, objects)
    client.app.state.objects = store

    ensure = client.post(
        f"/internal/v1/goals/{goal['id']}/goal-reviews",
        json={"trigger_key": f"critic:{goal['id']}:missing"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert ensure.status_code == 201, ensure.text
    activity_id = ensure.json()["data"]["activity_id"]
    fake = "sha256:" + "ab" * 32

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        row = (
            db.execute(
                text("SELECT binding, verification_assignments FROM activities WHERE id=:id"),
                {"id": activity_id},
            )
            .mappings()
            .one()
        )
        binding = dict(row["binding"] or {})
        binding["subject_digest"] = fake
        assignments = list(row["verification_assignments"] or [])
        if assignments:
            assignments[0] = dict(assignments[0])
            assignments[0]["review_snapshot_digest"] = fake
        import json as _json

        db.execute(
            text(
                """UPDATE activities
                SET binding=CAST(:binding AS jsonb),
                    verification_assignments=CAST(:assignments AS jsonb),
                    status='READY', updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {
                "id": activity_id,
                "binding": _json.dumps(binding),
                "assignments": _json.dumps(assignments),
            },
        )
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE status='READY' AND kind='AUDIT' AND id<>:id"""
            ),
            {"id": activity_id},
        )
    engine.dispose()

    subject = str(uuid4())
    _register_worker(subject, kinds=("AUDIT",))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["AUDIT"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    # binding 被改后可能 BINDING_STALE；若 claim 成功则 compile 须失败
    if claimed.status_code != 200:
        assert claimed.status_code in (409, 422), claimed.text
        return
    lease = claimed.json()["data"]
    compiled = client.post(
        f"/internal/v1/activities/{activity_id}/context-compile",
        json={"lease": lease["lease"], "max_input_tokens": 8192},
        headers=worker_auth,
    )
    assert compiled.status_code != 201, compiled.text
