"""GOAL_REVIEW 落库：诊断 ≠ criterion PASS ≠ Goal/Task DONE。"""

import hashlib
import os
from uuid import UUID, uuid4

from control_kernel.storage.audits import (
    ensure_goal_review_activity,
)
from sqlalchemy import create_engine, text
from test_claims import _drain_ready, _register_worker, _start_goal


def test_goal_review_persists_lists_and_never_marks_done(api, objects):
    client, token, auth, goal, _plan = _start_goal(api, objects)
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    activity_id, snap, _created, _rem, *_ = ensure_goal_review_activity(
        engine,
        goal_id=UUID(goal["id"]),
        trigger_key=f"timer:{goal['id']}:1",
    )
    assert snap.startswith("sha256:")
    # 同触发键幂等
    again, snap2, created2, _rem2, *_ = ensure_goal_review_activity(
        engine,
        goal_id=UUID(goal["id"]),
        trigger_key=f"timer:{goal['id']}:1",
    )
    assert again == activity_id
    assert snap2 == snap
    assert created2 is False

    with engine.connect() as db:
        n = db.execute(
            text(
                """SELECT count(*) FROM goal_review_snapshots
                WHERE content_digest=:d AND goal_id=:g"""
            ),
            {"d": snap, "g": goal["id"]},
        ).scalar_one()
        assert int(n) == 1
    engine.dispose()

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
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
    assert lease["activity"]["id"] == str(activity_id)
    assert lease["activity"]["target"]["type"] == "GOAL_REVIEW"
    assert lease["activity"]["task_id"] is None
    binding = lease["activity"]["binding"]
    assert binding["subject_digest"] == snap

    outcome = client.post(
        f"/internal/v1/activities/{activity_id}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {
                "target_type": "GOAL_REVIEW",
                "review": {
                    "goal_contract_revision": binding["goal_contract_revision"],
                    "plan_revision": binding["plan_revision"],
                    "review_snapshot_digest": snap,
                    "findings": [
                        {
                            "code": "NO_PROGRESS",
                            "severity": "WARN",
                            "evidence_ids": [],
                            "recommendation": "检查执行停滞",
                        }
                    ],
                },
            },
        },
        headers=worker_auth,
    )
    assert outcome.status_code == 200, outcome.text
    assert outcome.json()["data"]["status"] == "SUCCEEDED"

    listed = client.get(f"/api/v1/goals/{goal['id']}/audits", headers=auth)
    assert listed.status_code == 200, listed.text
    items = listed.json()["data"]
    reviews = [i for i in items if i["record_type"] == "GOAL_REVIEW"]
    assert len(reviews) == 1
    review = reviews[0]["review"]
    assert review["goal_id"] == goal["id"]
    assert review["review_snapshot_digest"] == snap
    assert review["findings"][0]["code"] == "NO_PROGRESS"
    assert review["content_digest"].startswith("sha256:")
    assert "verdict" not in review

    goal_after = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_after["status"] != "DONE"
    assert goal_after["status"] in ("PLANNING", "RUNNING", "PAUSED", "VERIFYING", "BLOCKED")


def test_goal_review_rejects_snapshot_mismatch(api, objects):
    client, token, _auth, goal, _plan = _start_goal(api, objects)
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    activity_id, snap, _created, _rem, *_ = ensure_goal_review_activity(
        engine,
        goal_id=UUID(goal["id"]),
        trigger_key=f"timer:{goal['id']}:mismatch",
    )
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
    binding = lease["activity"]["binding"]
    wrong = "sha256:" + hashlib.sha256(b"wrong-snapshot").hexdigest()
    assert wrong != snap
    outcome = client.post(
        f"/internal/v1/activities/{activity_id}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {
                "target_type": "GOAL_REVIEW",
                "review": {
                    "goal_contract_revision": binding["goal_contract_revision"],
                    "plan_revision": binding["plan_revision"],
                    "review_snapshot_digest": wrong,
                    "findings": [],
                },
            },
        },
        headers=worker_auth,
    )
    assert outcome.status_code == 422, outcome.text

    # 错绑不得落库
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.connect() as db:
        n = db.execute(
            text("SELECT count(*) FROM goal_reviews WHERE activity_id=:id"),
            {"id": activity_id},
        ).scalar_one()
        assert n == 0
        status = db.execute(
            text("SELECT status FROM activities WHERE id=:id"),
            {"id": activity_id},
        ).scalar_one()
        assert status == "RUNNING"
    engine.dispose()
