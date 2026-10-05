"""第三百四十八批：未复盘 NO_PROGRESS_STOP 禁止新 ENGINEERING 写入；≠ DONE。"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

from control_kernel.protocols.runtime import PlanRejected
from control_kernel.storage.activation_terminations import (
    goal_has_unresolved_no_progress_stop,
    record_activation_termination,
)
from control_kernel.storage.stops import assert_goal_allows_new_engineering_writes
from sqlalchemy import create_engine, text
from test_effects import _publish_and_claim_execute


def test_no_progress_stop_blocks_new_engineering_writes(api, objects):
    """NO_PROGRESS_STOP 未复盘 → create_step 422；GOAL_REQUIRES_REVIEW 后放行；≠DONE。"""
    client, _token, auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    activity_id = UUID(exec_lease["activity"]["id"])
    attempt_id = UUID(exec_lease["lease"]["attempt_id"])
    goal_id = UUID(goal["id"])
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])

    with engine.connect() as db:
        subject = db.execute(
            text(
                """SELECT w.subject FROM workers w
                JOIN activity_attempts a ON a.worker_id=w.id
                WHERE a.id=:id"""
            ),
            {"id": attempt_id},
        ).scalar_one()

    record_activation_termination(
        engine,
        subject=subject,
        project_ids=[],
        activity_id=activity_id,
        attempt_id=attempt_id,
        reason="NO_PROGRESS_STOP",
        detail="block_engineering",
    )

    with engine.connect() as db:
        assert goal_has_unresolved_no_progress_stop(db, goal_id) is True
        try:
            assert_goal_allows_new_engineering_writes(db, goal_id)
            raise AssertionError("expected PlanRejected")
        except PlanRejected as exc:
            assert "NO_PROGRESS_STOP_UNRESOLVED" in str(exc)

    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "after-no-progress",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 422, step.text
    err = step.json()["error"]
    assert err["code"] == "NO_PROGRESS_STOP_UNRESOLVED"
    assert "NO_PROGRESS_STOP_UNRESOLVED" in err["message"]

    before = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert before["status"] != "DONE"

    review_attempt_id = uuid4()
    with engine.begin() as db:
        db.execute(
            text(
                """INSERT INTO activity_attempts(
                  id,activity_id,project_id,worker_id,binding_digest,fencing_epoch,
                  lease_expires_at,renewal_seq,status,skill_versions,started_at,finished_at)
                SELECT :id, activity_id, project_id, worker_id, binding_digest,
                       fencing_epoch + 1, lease_expires_at, 0, 'COMPLETED',
                       skill_versions, started_at, clock_timestamp()
                FROM activity_attempts WHERE id=:old"""
            ),
            {"id": review_attempt_id, "old": attempt_id},
        )

    record_activation_termination(
        engine,
        subject=subject,
        project_ids=[],
        activity_id=activity_id,
        attempt_id=review_attempt_id,
        reason="GOAL_REQUIRES_REVIEW",
        detail="ack_no_progress",
    )

    with engine.connect() as db:
        assert goal_has_unresolved_no_progress_stop(db, goal_id) is False
        assert_goal_allows_new_engineering_writes(db, goal_id)

    step_ok = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "after-review",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step_ok.status_code == 201, step_ok.text

    after = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert after["status"] != "DONE"
    engine.dispose()
