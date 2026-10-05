"""第三百四十六批：NO_PROGRESS_STOP 挡最终屏障 SEAL；GOAL_REQUIRES_REVIEW 后可封存；≠ DONE。"""

from __future__ import annotations

import hashlib
import json
import os
from io import BytesIO
from uuid import UUID, uuid4

from control_kernel.storage.activation_terminations import (
    goal_has_unresolved_no_progress_stop,
    record_activation_termination,
)
from control_kernel.storage.artifacts import Artifacts
from control_kernel.storage.finalization import open_finalization_barrier
from sqlalchemy import create_engine, text
from test_ab09_stop_barrier import _ensure_global_criterion
from test_effects import _publish_and_claim_execute


def test_no_progress_stop_keeps_barrier_draining_until_review(api, objects):
    """NO_PROGRESS_STOP → DRAINING；GOAL_REQUIRES_REVIEW 后晋升 SEALED+FINALIZE；≠DONE。"""
    store, _, _ = objects
    client, _token, auth, goal, _worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = UUID(exec_lease["activity"]["id"])
    attempt_id = UUID(exec_lease["lease"]["attempt_id"])
    project_id = UUID(exec_lease["activity"]["project_id"])
    goal_id = UUID(goal["id"])
    task_id = UUID(exec_lease["activity"]["task_id"])
    candidate_id = uuid4()
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])

    _ensure_global_criterion(client, auth, engine, str(project_id), str(goal_id))

    with engine.connect() as db:
        subject = db.execute(
            text(
                """SELECT w.subject FROM workers w
                JOIN activity_attempts a ON a.worker_id=w.id
                WHERE a.id=:id"""
            ),
            {"id": attempt_id},
        ).scalar_one()

    stop_row = record_activation_termination(
        engine,
        subject=subject,
        project_ids=[],
        activity_id=activity_id,
        attempt_id=attempt_id,
        reason="NO_PROGRESS_STOP",
        detail="nudge_budget_exhausted",
    )
    assert stop_row["marks_goal_done"] is False
    assert stop_row["reason"] == "NO_PROGRESS_STOP"

    artifact = Artifacts(engine, store).ingest_raw(
        project_id,
        "sha256:" + hashlib.sha256(b"no-progress-seal").hexdigest(),
        BytesIO(b"no-progress-seal"),
        mime="application/octet-stream",
        producer_identity="test:no-progress-seal",
    )

    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE goals SET status='RUNNING', updated_at=clock_timestamp()
                WHERE id=:id AND status NOT IN ('DONE','FAILED','CANCELLED','BLOCKED')"""
            ),
            {"id": goal_id},
        )
        db.execute(
            text(
                """INSERT INTO candidate_manifests(
                  id,project_id,goal_id,task_id,activity_id,attempt_id,
                  protected_baseline_digest,content_digest,content,
                  workspace_snapshot_artifact_id)
                VALUES(
                  :id,:project,:goal,:task,:activity,:attempt,
                  :baseline,:digest,CAST(:content AS jsonb),:snap)"""
            ),
            {
                "id": candidate_id,
                "project": project_id,
                "goal": goal_id,
                "task": task_id,
                "activity": activity_id,
                "attempt": attempt_id,
                "baseline": "sha256:" + uuid4().hex + uuid4().hex[:32],
                "digest": "sha256:" + uuid4().hex + uuid4().hex[:32],
                "content": json.dumps({"files": []}),
                "snap": artifact.id,
            },
        )
        assert goal_has_unresolved_no_progress_stop(db, goal_id) is True
        open_finalization_barrier(db, goal_id, candidate_id)
        barrier = (
            db.execute(
                text(
                    """SELECT status, in_flight_engineering, unknown_effects
                    FROM finalization_barriers WHERE goal_id=:goal"""
                ),
                {"goal": goal_id},
            )
            .mappings()
            .one()
        )
        assert barrier["status"] == "DRAINING"
        assert int(barrier["in_flight_engineering"]) == 0
        assert int(barrier["unknown_effects"]) == 0
        finalize_n = db.execute(
            text(
                """SELECT count(*) FROM activities
                WHERE goal_id=:goal AND kind='FINALIZE'"""
            ),
            {"goal": goal_id},
        ).scalar_one()
        assert int(finalize_n) == 0
        goal_status = db.execute(
            text("SELECT status FROM goals WHERE id=:id"),
            {"id": goal_id},
        ).scalar_one()
        assert goal_status == "VERIFYING"
        assert goal_status != "DONE"

    # 同 attempt 不能换 reason；另插 SUCCEEDED attempt 登记 GOAL_REQUIRES_REVIEW
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

    review_row = record_activation_termination(
        engine,
        subject=subject,
        project_ids=[],
        activity_id=activity_id,
        attempt_id=review_attempt_id,
        reason="GOAL_REQUIRES_REVIEW",
        detail="no_progress_acknowledged",
    )
    assert review_row["marks_goal_done"] is False
    assert review_row["reason"] == "GOAL_REQUIRES_REVIEW"

    with engine.connect() as db:
        assert goal_has_unresolved_no_progress_stop(db, goal_id) is False
        barrier = (
            db.execute(
                text(
                    """SELECT status, in_flight_engineering, unknown_effects
                    FROM finalization_barriers WHERE goal_id=:goal"""
                ),
                {"goal": goal_id},
            )
            .mappings()
            .one()
        )
        assert barrier["status"] == "SEALED"
        assert int(barrier["in_flight_engineering"]) == 0
        assert int(barrier["unknown_effects"]) == 0
        finalize_n = db.execute(
            text(
                """SELECT count(*) FROM activities
                WHERE goal_id=:goal AND kind='FINALIZE' AND status='READY'"""
            ),
            {"goal": goal_id},
        ).scalar_one()
        assert int(finalize_n) == 1
        goal_status = db.execute(
            text("SELECT status FROM goals WHERE id=:id"),
            {"id": goal_id},
        ).scalar_one()
        assert goal_status == "VERIFYING"
        assert goal_status != "DONE"

    got = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert got["status"] == "VERIFYING"
    assert got["status"] != "DONE"
    assert got["barrier"]["status"] == "SEALED"
    engine.dispose()
