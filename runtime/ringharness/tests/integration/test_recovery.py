"""FINALIZE FAIL→BLOCKED；RECOVER_FINALIZATION REWORK 开放新 PLAN。"""

import os
from uuid import uuid4

from sqlalchemy import create_engine, text
from test_claims import _drain_ready_plans
from test_replan import _publish_running


def test_rework_aborts_barrier_and_creates_plan(api, objects):
    client, token, auth, goal = _publish_running(api, objects)
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    barrier_id = uuid4()
    new_epoch = str(int(goal["write_epoch"]) + 1)
    with engine.begin() as db:
        db.execute(
            text(
                """INSERT INTO finalization_barriers(
                  id,project_id,goal_id,write_epoch,status,contract_revision,plan_revision,
                  candidate_manifest_id,in_flight_engineering,unknown_effects)
                VALUES(
                  :id,:project,:goal,:epoch,'SEALED',:crev,:prev,NULL,0,0)"""
            ),
            {
                "id": barrier_id,
                "project": goal["project_id"],
                "goal": goal["id"],
                "epoch": new_epoch,
                "crev": goal["contract_revision"],
                "prev": goal["plan_revision"],
            },
        )
        blocked = (
            db.execute(
                text(
                    """UPDATE goals SET status='BLOCKED', previous_status='VERIFYING',
                      block_reason='FINALIZATION_FAIL', write_epoch=:epoch,
                      state_revision=state_revision+1, updated_at=clock_timestamp()
                      WHERE id=:id RETURNING state_revision, write_epoch, plan_revision"""
                ),
                {"id": goal["id"], "epoch": new_epoch},
            )
            .mappings()
            .one()
        )
    engine.dispose()

    # 普通 replan 仍须拒绝。
    denied = client.post(
        f"/api/v1/goals/{goal['id']}/replan",
        json={
            "expected_state_revision": blocked["state_revision"],
            "expected_plan_revision": blocked["plan_revision"],
            "reason": "cannot bypass barrier",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert denied.status_code == 409

    key = str(uuid4())
    body = {
        "expected_state_revision": blocked["state_revision"],
        "expected_plan_revision": blocked["plan_revision"],
        "barrier_id": str(barrier_id),
        "expected_write_epoch": blocked["write_epoch"],
        "action": "REWORK",
        "reason": "repair after global fail",
    }
    recovered = client.post(
        f"/api/v1/goals/{goal['id']}/finalization-recovery",
        json=body,
        headers={**auth, "Idempotency-Key": key},
    )
    assert recovered.status_code == 202, recovered.text
    command = recovered.json()["data"]
    assert command["kind"] == "RECOVER_FINALIZATION"
    assert command["status"] == "SUCCEEDED"
    assert command["result"]["action"] == "REWORK"
    assert command["result"]["final_status"] == "PLANNING"
    assert command["result"]["write_epoch"] == str(int(new_epoch) + 1)
    assert (
        client.post(
            f"/api/v1/goals/{goal['id']}/finalization-recovery",
            json=body,
            headers={**auth, "Idempotency-Key": key},
        ).json()["data"]
        == command
    )

    got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert got["status"] == "PLANNING"
    assert got["previous_status"] == "BLOCKED"
    assert got["block_reason"] is None
    assert got["write_epoch"] == str(int(new_epoch) + 1)
    assert got["barrier"] is None or got["barrier"]["status"] == "ABORTED"
    plans = client.get(
        f"/api/v1/goals/{goal['id']}/activities",
        params={"kind": "PLAN", "status": "READY"},
        headers=auth,
    ).json()["data"]
    assert len(plans) == 1
    assert plans[0]["id"] == command["result"]["activity_id"]
    _drain_ready_plans(client, token)


def test_reverify_rejected_on_fail_reason(api, objects):
    client, token, auth, goal = _publish_running(api, objects)
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    barrier_id = uuid4()
    epoch = str(int(goal["write_epoch"]) + 1)
    with engine.begin() as db:
        db.execute(
            text(
                """INSERT INTO finalization_barriers(
                  id,project_id,goal_id,write_epoch,status,contract_revision,plan_revision,
                  candidate_manifest_id,in_flight_engineering,unknown_effects)
                VALUES(
                  :id,:project,:goal,:epoch,'SEALED',:crev,:prev,NULL,0,0)"""
            ),
            {
                "id": barrier_id,
                "project": goal["project_id"],
                "goal": goal["id"],
                "epoch": epoch,
                "crev": goal["contract_revision"],
                "prev": goal["plan_revision"],
            },
        )
        blocked = (
            db.execute(
                text(
                    """UPDATE goals SET status='BLOCKED', previous_status='VERIFYING',
                      block_reason='FINALIZATION_FAIL', write_epoch=:epoch,
                      state_revision=state_revision+1, updated_at=clock_timestamp()
                      WHERE id=:id RETURNING state_revision, write_epoch, plan_revision"""
                ),
                {"id": goal["id"], "epoch": epoch},
            )
            .mappings()
            .one()
        )
    engine.dispose()
    bad = client.post(
        f"/api/v1/goals/{goal['id']}/finalization-recovery",
        json={
            "expected_state_revision": blocked["state_revision"],
            "expected_plan_revision": blocked["plan_revision"],
            "barrier_id": str(barrier_id),
            "expected_write_epoch": blocked["write_epoch"],
            "action": "REVERIFY",
            "reason": "fail cannot reverify",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert bad.status_code == 409
    _drain_ready_plans(client, token)
