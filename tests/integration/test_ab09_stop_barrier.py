"""AB09：Stop 未 CONFIRMED / 资源 QUARANTINED 时，新 ENGINEERING 写入失败关闭。

合成缝：对账后 effect 可为 SUCCEEDED，但最终屏障仍不得 SEAL（须 DRAINING）。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from control_kernel.storage.artifacts import Artifacts
from control_kernel.storage.finalization import open_finalization_barrier
from control_kernel.storage.stops import goal_has_unconfirmed_stop_barrier
from evidence_ledger.content import encode
from sqlalchemy import create_engine, text
from test_claims import _register_worker
from test_effects import _publish_and_claim_execute


def test_ab09_unconfirmed_stop_blocks_new_engineering_prepare(api, objects):
    """失租后 Stop=REQUESTED 且资源 QUARANTINED：重领后仍禁止新 create_step（≠DONE）。"""
    store, _, _ = objects
    client, token, auth, goal, _worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    attempt_id = exec_lease["lease"]["attempt_id"]
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])

    with engine.begin() as db:
        db.execute(
            text("""UPDATE activity_attempts
              SET lease_expires_at=:past, updated_at=clock_timestamp()
              WHERE id=:id"""),
            {"id": attempt_id, "past": datetime.now(UTC) - timedelta(seconds=5)},
        )

    # 触发 expire_stale_leases
    probe = str(uuid4())
    _register_worker(probe, kinds=("PLAN", "EXECUTE"))
    client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={
            "Authorization": "Bearer " + token(probe, ["worker"]),
            "Idempotency-Key": str(uuid4()),
        },
    )

    with engine.begin() as db:
        stop = (
            db.execute(
                text(
                    """SELECT id, status FROM stops
                    WHERE attempt_id=:id AND reason='LEASE_EXPIRED'"""
                ),
                {"id": attempt_id},
            )
            .mappings()
            .one()
        )
        q = db.execute(
            text("SELECT status FROM resource_reservations WHERE attempt_id=:id"),
            {"id": attempt_id},
        ).scalar()
    assert stop["status"] == "REQUESTED"
    assert q == "QUARANTINED"

    # 重领同一 EXECUTE（新 fencing）；Stop 仍未 CONFIRMED
    other = str(uuid4())
    _register_worker(other, kinds=("EXECUTE",))
    reclaimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={
            "Authorization": "Bearer " + token(other, ["worker"]),
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert reclaimed.status_code == 200, reclaimed.text
    new_lease = reclaimed.json()["data"]
    assert new_lease["lease"] is not None
    assert new_lease["activity"]["id"] == activity_id
    assert int(new_lease["lease"]["fencing_epoch"]) > int(
        exec_lease["lease"]["fencing_epoch"]
    )

    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": new_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "ab09-new-write",
            "tool_ref": "read_file",
        },
        headers={
            "Authorization": "Bearer " + token(other, ["worker"]),
        },
    )
    # 第136批：create_step 与 prepare 新写入共用 assert_goal_allows
    assert step.status_code == 422, step.text
    err = step.json()["error"]
    assert err["code"] == "STOP_UNCONFIRMED" or "STOP_UNCONFIRMED" in str(err)

    still_stop = client.get(f"/internal/v1/stops/{stop['id']}", headers=auth).json()[
        "data"
    ]
    assert still_stop["status"] == "REQUESTED"
    assert client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"][
        "status"
    ] != "DONE"
    # 最终屏障同源闸：未确认 Stop 时 goal_has_unconfirmed_stop_barrier 为真
    # （open_finalization_barrier 据此保持 DRAINING、不 SEALED）
    with engine.connect() as db:
        assert goal_has_unconfirmed_stop_barrier(db, UUID(goal["id"])) is True
    engine.dispose()


def _ensure_global_criterion(client, auth, engine, project_id: str, goal_id: str) -> UUID:
    """为 Goal 合同挂上 GOAL/GLOBAL 标准，使 open_finalization_barrier 可开屏障。"""
    example = json.loads(
        (Path(__file__).parents[2] / "doc/contracts/verifier-fixtures-v2.json").read_text()
    )["examples"][3]
    definition = {**example["definition"], "project_id": project_id}
    digest = (
        "sha256:"
        + hashlib.sha256(
            encode(
                json.dumps(
                    {
                        "schema_version": 3,
                        "object_type": "VerifierDefinition",
                        "content": definition,
                        "reference_bindings": [],
                    }
                )
            )
        ).hexdigest()
    )
    with engine.begin() as db:
        db.execute(
            text("""INSERT INTO verifier_definitions(project_id,ref,content_digest,content,approval_record_digest)
            VALUES(:project,:ref,:digest,CAST(:content AS jsonb),:approval)
            ON CONFLICT DO NOTHING"""),
            {
                "project": project_id,
                "ref": "approved.ab09-global",
                "digest": digest,
                "content": json.dumps(definition),
                "approval": "sha256:" + "b" * 64,
            },
        )
    created = client.post(
        "/api/v1/verification-profiles",
        json={
            "project_id": project_id,
            "name": f"ab09-global-{uuid4().hex[:8]}",
            "target_scope": "GOAL",
            "verifier_ref": "approved.ab09-global",
            "verifier_digest": digest,
            "required_layers": ["GLOBAL"],
            "thresholds": [
                {
                    "metric": "checks_passed",
                    "operator": "EQ",
                    "expected": "true",
                    "unit": "boolean",
                }
            ],
            "required_evidence_kinds": ["trusted_verifier_result"],
            "applicability_rule_ref": "all_required",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    profile_id = UUID(created.json()["data"]["id"])
    with engine.begin() as db:
        row = (
            db.execute(
                text("SELECT contract FROM goals WHERE id=:id FOR UPDATE"),
                {"id": goal_id},
            )
            .mappings()
            .one()
        )
        contract = dict(row["contract"])
        criteria = list(contract.get("success_criteria") or [])
        criteria.append(
            {
                "id": "AB09-G",
                "description": "全局验收（合成测）",
                "required": True,
                "verification_profile_id": str(profile_id),
            }
        )
        contract["success_criteria"] = criteria
        db.execute(
            text(
                """UPDATE goals SET contract=CAST(:contract AS jsonb),
                  state_revision=state_revision+1, updated_at=clock_timestamp()
                  WHERE id=:id"""
            ),
            {"id": goal_id, "contract": json.dumps(contract)},
        )
    return profile_id


def test_ab09_reconciled_succeeded_still_drains_barrier_when_stop_unconfirmed(
    api, objects
):
    """对账后 effect=SUCCEEDED（无 UNKNOWN/在途），Stop 未确认 → 屏障 DRAINING 不 SEAL（≠DONE）。"""
    store, _, _ = objects
    client, token, auth, goal, _worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    attempt_id = exec_lease["lease"]["attempt_id"]
    project_id = exec_lease["activity"]["project_id"]
    goal_id = goal["id"]
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])

    _ensure_global_criterion(client, auth, engine, project_id, goal_id)

    blob = b'{"path":"src/ab09-reconciled.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    artifact = Artifacts(engine, store).ingest_raw(
        UUID(project_id),
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity="test:ab09-barrier",
    )

    candidate_id = uuid4()
    task_id = uuid4()
    effect_id = uuid4()
    step_id = uuid4()

    with engine.begin() as db:
        # 失租 → Stop REQUESTED + 资源 QUARANTINED（真实 expire 路径）
        db.execute(
            text("""UPDATE activity_attempts
              SET lease_expires_at=:past, updated_at=clock_timestamp()
              WHERE id=:id"""),
            {"id": attempt_id, "past": datetime.now(UTC) - timedelta(seconds=5)},
        )

    probe = str(uuid4())
    _register_worker(probe, kinds=("PLAN", "EXECUTE"))
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={
            "Authorization": "Bearer " + token(probe, ["worker"]),
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert claimed.status_code == 200, claimed.text

    with engine.begin() as db:
        stop = (
            db.execute(
                text(
                    """SELECT status FROM stops
                    WHERE attempt_id=:id AND reason='LEASE_EXPIRED'"""
                ),
                {"id": attempt_id},
            )
            .mappings()
            .one()
        )
        assert stop["status"] == "REQUESTED"
        assert (
            db.execute(
                text("SELECT status FROM resource_reservations WHERE attempt_id=:id"),
                {"id": attempt_id},
            ).scalar()
            == "QUARANTINED"
        )

        # 对账成功：原工程结果可记为 SUCCEEDED（无在途 / 无 UNKNOWN）
        db.execute(
            text(
                """INSERT INTO tasks(
                  id,project_id,goal_id,contract,contract_digest,contract_revision,
                  plan_revision,state_revision,status,work_lineage_id,execution_round)
                VALUES(
                  :id,:project,:goal,CAST(:contract AS jsonb),:digest,1,1,1,'RUNNING',:lineage,1)"""
            ),
            {
                "id": task_id,
                "project": project_id,
                "goal": goal_id,
                "contract": json.dumps(
                    {
                        "id": str(task_id),
                        "goal_id": goal_id,
                        "title": "ab09",
                        "objective": "barrier",
                    }
                ),
                "digest": "sha256:" + uuid4().hex + uuid4().hex[:32],
                "lineage": task_id,
            },
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
        db.execute(
            text(
                """INSERT INTO effect_intents(
                  id,project_id,goal_id,activity_id,logical_step_id,intent_revision,
                  payload_digest,tool_ref,replay_class,scope,status,
                  input_artifact_id,producer_attempt_id)
                VALUES(
                  :id,:project,:goal,:activity,:step,1,
                  :digest,'read_file','READ_ONLY','ENGINEERING','SUCCEEDED',
                  :artifact,:attempt)"""
            ),
            {
                "id": effect_id,
                "project": project_id,
                "goal": goal_id,
                "activity": activity_id,
                "step": step_id,
                "digest": "sha256:" + uuid4().hex + uuid4().hex[:32],
                "artifact": artifact.id,
                "attempt": attempt_id,
            },
        )

        assert goal_has_unconfirmed_stop_barrier(db, UUID(goal_id)) is True
        open_finalization_barrier(db, UUID(goal_id), candidate_id)

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

        goal_row = (
            db.execute(
                text("SELECT status FROM goals WHERE id=:id"),
                {"id": goal_id},
            )
            .mappings()
            .one()
        )
        # 可进入 VERIFYING，但不得 DONE；且无 SEALED 屏障
        assert goal_row["status"] in ("VERIFYING", "RUNNING")
        assert goal_row["status"] != "DONE"

    got = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert got["status"] != "DONE"
    if got.get("barrier") is not None:
        assert got["barrier"]["status"] == "DRAINING"
    engine.dispose()


def test_ab09_stop_confirmed_promotes_draining_barrier_to_sealed(api, objects):
    """AB09 正控：对账 SUCCEEDED + DRAINING 后 Stop CONFIRMED → SEALED+FINALIZE；≠DONE。"""
    store, _, _ = objects
    client, token, auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    attempt_id = exec_lease["lease"]["attempt_id"]
    project_id = exec_lease["activity"]["project_id"]
    goal_id = goal["id"]
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])

    _ensure_global_criterion(client, auth, engine, project_id, goal_id)

    blob = b'{"path":"src/ab09-seal.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    artifact = Artifacts(engine, store).ingest_raw(
        UUID(project_id),
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity="test:ab09-seal",
    )

    candidate_id = uuid4()
    task_id = uuid4()
    effect_id = uuid4()
    step_id = uuid4()

    with engine.begin() as db:
        db.execute(
            text("""UPDATE activity_attempts
              SET lease_expires_at=:past, updated_at=clock_timestamp()
              WHERE id=:id"""),
            {"id": attempt_id, "past": datetime.now(UTC) - timedelta(seconds=5)},
        )

    probe = str(uuid4())
    _register_worker(probe, kinds=("PLAN", "EXECUTE"))
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={
            "Authorization": "Bearer " + token(probe, ["worker"]),
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert claimed.status_code == 200, claimed.text

    with engine.begin() as db:
        stop = (
            db.execute(
                text(
                    """SELECT id, status FROM stops
                    WHERE attempt_id=:id AND reason='LEASE_EXPIRED'"""
                ),
                {"id": attempt_id},
            )
            .mappings()
            .one()
        )
        assert stop["status"] == "REQUESTED"

        db.execute(
            text(
                """INSERT INTO tasks(
                  id,project_id,goal_id,contract,contract_digest,contract_revision,
                  plan_revision,state_revision,status,work_lineage_id,execution_round)
                VALUES(
                  :id,:project,:goal,CAST(:contract AS jsonb),:digest,1,1,1,'RUNNING',:lineage,1)"""
            ),
            {
                "id": task_id,
                "project": project_id,
                "goal": goal_id,
                "contract": json.dumps(
                    {
                        "id": str(task_id),
                        "goal_id": goal_id,
                        "title": "ab09-seal",
                        "objective": "promote",
                    }
                ),
                "digest": "sha256:" + uuid4().hex + uuid4().hex[:32],
                "lineage": task_id,
            },
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
        db.execute(
            text(
                """INSERT INTO effect_intents(
                  id,project_id,goal_id,activity_id,logical_step_id,intent_revision,
                  payload_digest,tool_ref,replay_class,scope,status,
                  input_artifact_id,producer_attempt_id)
                VALUES(
                  :id,:project,:goal,:activity,:step,1,
                  :digest,'read_file','READ_ONLY','ENGINEERING','SUCCEEDED',
                  :artifact,:attempt)"""
            ),
            {
                "id": effect_id,
                "project": project_id,
                "goal": goal_id,
                "activity": activity_id,
                "step": step_id,
                "digest": "sha256:" + uuid4().hex + uuid4().hex[:32],
                "artifact": artifact.id,
                "attempt": attempt_id,
            },
        )
        assert goal_has_unconfirmed_stop_barrier(db, UUID(goal_id)) is True
        open_finalization_barrier(db, UUID(goal_id), candidate_id)
        barrier = (
            db.execute(
                text("SELECT status FROM finalization_barriers WHERE goal_id=:goal"),
                {"goal": goal_id},
            )
            .mappings()
            .one()
        )
        assert barrier["status"] == "DRAINING"

    # 原 attempt 持有者提交 EXITED∧compute_released → CONFIRMED → 晋升 SEALED
    confirm = client.post(
        f"/internal/v1/stops/{stop['id']}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "stop_id": str(stop["id"]),
            "activation_id": attempt_id,
            "attempt_id": attempt_id,
            "resource_instance_id": attempt_id,
            "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "observation": "EXITED",
            "compute_released": True,
            "write_capability_revoked": True,
            "proof_artifact_ids": [],
        },
        headers=worker_auth,
    )
    assert confirm.status_code == 201, confirm.text
    assert confirm.json()["data"]["disposition"] == "APPLIED"

    with engine.connect() as db:
        stop_row = (
            db.execute(
                text("SELECT status FROM stops WHERE id=:id"),
                {"id": stop["id"]},
            )
            .mappings()
            .one()
        )
        assert stop_row["status"] == "CONFIRMED"
        assert goal_has_unconfirmed_stop_barrier(db, UUID(goal_id)) is False
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
