"""义务 × UNKNOWN：未对账不得 ASSESSED；Goal DONE 前拦未决 effect / Stop。"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from control_kernel.protocols.runtime import PlanRejected
from control_kernel.storage.artifacts import Artifacts
from control_kernel.storage.obligations import (
    assert_goal_ready_for_done,
    assert_no_pending_obligations,
    reevaluate_quarantined_obligations_for_effect,
    settle_obligation_after_assessment,
)
from sqlalchemy import create_engine, text
from test_effects import _publish_and_claim_execute


def test_obligation_unknown_effect_quarantines_not_assessed(api, objects):
    """关联 UNKNOWN effect 时 settle → QUARANTINED，不得 ASSESSED。"""
    store, _, _ = objects
    client, _token, _auth, goal, _worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    activity_id = UUID(exec_lease["activity"]["id"])
    attempt_id = UUID(exec_lease["lease"]["attempt_id"])
    project_id = UUID(exec_lease["activity"]["project_id"])
    goal_id = UUID(goal["id"])

    blob = b'{"path":"src/obl-unknown.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    artifact = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity="test:obl-unknown",
    )

    effect_id = uuid4()
    obligation_id = uuid4()
    subject_id = uuid4()
    step_id = uuid4()

    with engine.begin() as db:
        profile_id = db.execute(
            text(
                """SELECT id FROM verification_profiles
                WHERE project_id=:p ORDER BY version DESC LIMIT 1"""
            ),
            {"p": project_id},
        ).scalar()
        assert profile_id is not None

        db.execute(
            text(
                """INSERT INTO effect_intents(
                  id,project_id,goal_id,activity_id,logical_step_id,intent_revision,
                  payload_digest,tool_ref,replay_class,scope,status,
                  input_artifact_id,producer_attempt_id)
                VALUES(
                  :id,:project,:goal,:activity,:step,1,
                  :digest,'read_file','READ_ONLY','ENGINEERING','UNKNOWN',
                  :artifact,:attempt)"""
            ),
            {
                "id": effect_id,
                "project": project_id,
                "goal": goal_id,
                "activity": activity_id,
                "step": step_id,
                "digest": "sha256:" + "b" * 64,
                "artifact": artifact.id,
                "attempt": attempt_id,
            },
        )
        db.execute(
            text(
                """INSERT INTO verification_obligations(
                  id,project_id,activity_id,attempt_id,subject_type,subject_id,
                  profile_id,layer,audit_round,effect_ids,status)
                VALUES(
                  :id,:project,:activity,:attempt,'CANDIDATE',:subject,
                  :profile,'MECHANICAL',1,ARRAY[:effect]::uuid[],'OPEN')"""
            ),
            {
                "id": obligation_id,
                "project": project_id,
                "activity": activity_id,
                "attempt": attempt_id,
                "subject": subject_id,
                "profile": profile_id,
                "effect": effect_id,
            },
        )
        settle_obligation_after_assessment(
            db,
            activity_id=activity_id,
            attempt_id=attempt_id,
            subject_id=subject_id,
            profile_id=profile_id,
            layer="MECHANICAL",
            audit_round=1,
            assessment_id=uuid4(),
        )
        status = db.execute(
            text("SELECT status FROM verification_obligations WHERE id=:id"),
            {"id": obligation_id},
        ).scalar_one()
    assert status == "QUARANTINED"

    with engine.begin() as db, pytest.raises(
        PlanRejected, match="GOAL_DONE_BLOCKED_UNSETTLED_EFFECTS"
    ):
        assert_goal_ready_for_done(db, goal_id)

    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE effect_intents SET status='SUCCEEDED',
                  state_revision=state_revision+1, updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": effect_id},
        )
        reevaluate_quarantined_obligations_for_effect(db, effect_id)
        old_status = db.execute(
            text("SELECT status, superseded_by_obligation_id, audit_round FROM verification_obligations WHERE id=:id"),
            {"id": obligation_id},
        ).mappings().one()
        assert old_status["status"] == "SUPERSEDED"
        assert old_status["superseded_by_obligation_id"] is not None
        assert int(old_status["audit_round"]) == 1
        succ = db.execute(
            text(
                """SELECT status, audit_round, effect_ids FROM verification_obligations
                WHERE id=:id"""
            ),
            {"id": old_status["superseded_by_obligation_id"]},
        ).mappings().one()
        assert succ["status"] == "OPEN"
        assert int(succ["audit_round"]) == 2
        # 幂等：再调用不得再插第三条
        reevaluate_quarantined_obligations_for_effect(db, effect_id)
        n = db.execute(
            text(
                """SELECT count(*) FROM verification_obligations
                WHERE activity_id=:a AND subject_id=:s AND profile_id=:p
                  AND layer='MECHANICAL'"""
            ),
            {
                "a": activity_id,
                "s": subject_id,
                "p": profile_id,
            },
        ).scalar_one()
        assert n == 2
    engine.dispose()


def test_assert_goal_ready_for_done_blocks_unconfirmed_stop(api, objects):
    _client, _token, _auth, goal, _worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    attempt_id = UUID(exec_lease["lease"]["attempt_id"])
    activity_id = UUID(exec_lease["activity"]["id"])
    project_id = UUID(exec_lease["activity"]["project_id"])

    with (
        engine.begin() as db,
        pytest.raises(PlanRejected, match="STOP_UNCONFIRMED"),
    ):
        db.execute(
            text(
                """INSERT INTO stops(
                  id,project_id,activity_id,attempt_id,activation_id,request_id,
                  fencing_epoch,reason,status,deadline_at,state_revision)
                VALUES(
                  :id,:project,:activity,:attempt,:activation,:req,
                  :epoch,'LEASE_EXPIRED','REQUESTED',:deadline,1)"""
            ),
            {
                "id": uuid4(),
                "project": project_id,
                "activity": activity_id,
                "attempt": attempt_id,
                "activation": attempt_id,
                "req": uuid4(),
                "epoch": int(exec_lease["lease"]["fencing_epoch"]),
                "deadline": datetime.now(UTC),
            },
        )
        assert_goal_ready_for_done(db, UUID(goal["id"]))
    engine.dispose()


def test_reevaluate_schedules_reverify_when_audit_succeeded(api, objects):
    """第142批：源 AUDIT 已 SUCCEEDED 时调度 READY 后继；claim 后 SUPERSEDE。"""
    import json

    from control_kernel.storage.activities import PLAN_RESOURCES
    from control_kernel.storage.obligations import (
        open_obligations_for_claim,
        reevaluate_quarantined_obligations_for_effect,
    )

    store, _, _ = objects
    client, _token, _auth, goal, _worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    project_id = UUID(exec_lease["activity"]["project_id"])
    goal_id = UUID(goal["id"])
    attempt_id = UUID(exec_lease["lease"]["attempt_id"])

    blob = b'{"path":"src/obl-reverify.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    artifact = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity="test:obl-reverify",
    )

    task_id = uuid4()
    audit_id = uuid4()
    candidate_id = uuid4()
    obligation_id = uuid4()
    effect_id = uuid4()
    step_id = uuid4()

    with engine.begin() as db:
        profile_id = db.execute(
            text(
                """SELECT id FROM verification_profiles
                WHERE project_id=:p ORDER BY version DESC LIMIT 1"""
            ),
            {"p": project_id},
        ).scalar()
        assert profile_id is not None
        worker_id = db.execute(
            text("SELECT worker_id FROM activity_attempts WHERE id=:id"),
            {"id": attempt_id},
        ).scalar_one()
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
                        "goal_id": str(goal_id),
                        "title": "reverify",
                        "objective": "reverify",
                    }
                ),
                "digest": "sha256:" + "d" * 64,
                "lineage": task_id,
            },
        )
        cand_digest = "sha256:" + uuid4().hex + uuid4().hex[:32]
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
                "activity": UUID(exec_lease["activity"]["id"]),
                "attempt": attempt_id,
                "baseline": "sha256:" + uuid4().hex + uuid4().hex[:32],
                "digest": cand_digest,
                "content": json.dumps({"files": []}),
                "snap": artifact.id,
            },
        )
        assignments = [
            {
                "verification_profile_id": str(profile_id),
                "layer": "MECHANICAL",
                "audit_round": 1,
            }
        ]
        db.execute(
            text(
                """INSERT INTO activities(
                  id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                  binding,verification_assignments,status,state_revision,depends_on_activity_ids,
                  retry_count,resources)
                VALUES(
                  :id,:project,:goal,:task,:goal,'AUDIT','CANDIDATE',:cand,
                  CAST(:binding AS jsonb),CAST(:assignments AS jsonb),'SUCCEEDED',1,'{}',0,
                  CAST(:resources AS jsonb))"""
            ),
            {
                "id": audit_id,
                "project": project_id,
                "goal": goal_id,
                "task": task_id,
                "cand": candidate_id,
                "binding": "{}",
                "assignments": json.dumps(assignments),
                "resources": PLAN_RESOURCES.model_dump_json(),
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
                "activity": UUID(exec_lease["activity"]["id"]),
                "step": step_id,
                "digest": "sha256:" + "e" * 64,
                "artifact": artifact.id,
                "attempt": attempt_id,
            },
        )
        db.execute(
            text(
                """INSERT INTO verification_obligations(
                  id,project_id,activity_id,attempt_id,subject_type,subject_id,
                  profile_id,layer,audit_round,effect_ids,status)
                VALUES(
                  :id,:project,:activity,:attempt,'CANDIDATE',:subject,
                  :profile,'MECHANICAL',1,ARRAY[:effect]::uuid[],'QUARANTINED')"""
            ),
            {
                "id": obligation_id,
                "project": project_id,
                "activity": audit_id,
                "attempt": attempt_id,
                "subject": candidate_id,
                "profile": profile_id,
                "effect": effect_id,
            },
        )
        reevaluate_quarantined_obligations_for_effect(db, effect_id)
        still = db.execute(
            text("SELECT status FROM verification_obligations WHERE id=:id"),
            {"id": obligation_id},
        ).scalar_one()
        assert still == "QUARANTINED"
        # 不得在已终态源 attempt 上开新 OPEN
        open_on_dead = db.execute(
            text(
                """SELECT COUNT(*) FROM verification_obligations
                WHERE activity_id=:activity AND attempt_id=:attempt
                  AND status='OPEN' AND audit_round=2"""
            ),
            {"activity": audit_id, "attempt": attempt_id},
        ).scalar_one()
        assert int(open_on_dead) == 0
        # claim 前 QUARANTINED 仍挡 DONE 门禁
        with pytest.raises(PlanRejected, match="未结算"):
            assert_no_pending_obligations(db, subject_id=candidate_id)
        ready = (
            db.execute(
                text(
                    """SELECT * FROM activities
                    WHERE goal_id=:goal AND kind='AUDIT' AND status='READY'
                      AND target_id=:cand
                    ORDER BY created_at DESC LIMIT 1"""
                ),
                {"goal": goal_id, "cand": candidate_id},
            )
            .mappings()
            .first()
        )
        assert ready is not None
        ready_binding = ready["binding"]
        if isinstance(ready_binding, str):
            ready_binding = json.loads(ready_binding)
        assert ready_binding.get("subject_digest") == cand_digest
        assigns = ready["verification_assignments"]
        if isinstance(assigns, str):
            assigns = json.loads(assigns)
        assert int(assigns[0]["audit_round"]) == 2
        # 幂等：二次 reevaluate 不另开 READY
        reevaluate_quarantined_obligations_for_effect(db, effect_id)
        ready_count = db.execute(
            text(
                """SELECT COUNT(*) FROM activities
                WHERE goal_id=:goal AND kind='AUDIT' AND status='READY'
                  AND target_id=:cand"""
            ),
            {"goal": goal_id, "cand": candidate_id},
        ).scalar_one()
        assert int(ready_count) == 1

        from control_kernel.storage.claims import binding_digest_of

        new_attempt = uuid4()
        binding = ready["binding"]
        if isinstance(binding, str):
            binding = json.loads(binding)
        db.execute(
            text(
                """INSERT INTO activity_attempts(
                  id,project_id,activity_id,worker_id,binding_digest,fencing_epoch,status,
                  lease_expires_at,renewal_seq,skill_versions,started_at)
                VALUES(
                  :id,:project,:activity,:worker,:binding,1,'ACTIVE',
                  clock_timestamp() + interval '1 hour',0,'[]'::jsonb,clock_timestamp())"""
            ),
            {
                "id": new_attempt,
                "project": project_id,
                "activity": ready["id"],
                "worker": worker_id,
                "binding": binding_digest_of(binding or {}),
            },
        )
        opened = open_obligations_for_claim(db, ready, new_attempt)
        assert len(opened) == 1
        assert opened[0]["status"] == "OPEN"
        assert int(opened[0]["audit_round"]) == 2
        assert effect_id in list(opened[0]["effect_ids"] or [])
        old = (
            db.execute(
                text(
                    """SELECT status, superseded_by_obligation_id FROM verification_obligations
                    WHERE id=:id"""
                ),
                {"id": obligation_id},
            )
            .mappings()
            .one()
        )
        assert old["status"] == "SUPERSEDED"
        assert old["superseded_by_obligation_id"] == opened[0]["id"]
    engine.dispose()


def test_reevaluate_skips_schedule_when_audit_recovering(api, objects):
    """第143批：RECOVERING 非终态，不得误调度 READY 后继。"""
    import json

    from control_kernel.storage.activities import PLAN_RESOURCES

    store, _, _ = objects
    client, _token, _auth, goal, _worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    project_id = UUID(exec_lease["activity"]["project_id"])
    goal_id = UUID(goal["id"])
    attempt_id = UUID(exec_lease["lease"]["attempt_id"])

    blob = b'{"path":"src/obl-recovering.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    artifact = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity="test:obl-recovering",
    )

    task_id = uuid4()
    audit_id = uuid4()
    candidate_id = uuid4()
    obligation_id = uuid4()
    effect_id = uuid4()
    step_id = uuid4()

    with engine.begin() as db:
        profile_id = db.execute(
            text(
                """SELECT id FROM verification_profiles
                WHERE project_id=:p ORDER BY version DESC LIMIT 1"""
            ),
            {"p": project_id},
        ).scalar()
        assert profile_id is not None
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
                        "goal_id": str(goal_id),
                        "title": "recovering",
                        "objective": "recovering",
                    }
                ),
                "digest": "sha256:" + "c" * 64,
                "lineage": task_id,
            },
        )
        assignments = [
            {
                "verification_profile_id": str(profile_id),
                "layer": "MECHANICAL",
                "audit_round": 1,
            }
        ]
        db.execute(
            text(
                """INSERT INTO activities(
                  id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                  binding,verification_assignments,status,state_revision,depends_on_activity_ids,
                  retry_count,resources)
                VALUES(
                  :id,:project,:goal,:task,:goal,'AUDIT','CANDIDATE',:cand,
                  CAST(:binding AS jsonb),CAST(:assignments AS jsonb),'RECOVERING',1,'{}',0,
                  CAST(:resources AS jsonb))"""
            ),
            {
                "id": audit_id,
                "project": project_id,
                "goal": goal_id,
                "task": task_id,
                "cand": candidate_id,
                "binding": "{}",
                "assignments": json.dumps(assignments),
                "resources": PLAN_RESOURCES.model_dump_json(),
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
                "activity": UUID(exec_lease["activity"]["id"]),
                "step": step_id,
                "digest": "sha256:" + "f" * 64,
                "artifact": artifact.id,
                "attempt": attempt_id,
            },
        )
        db.execute(
            text(
                """INSERT INTO verification_obligations(
                  id,project_id,activity_id,attempt_id,subject_type,subject_id,
                  profile_id,layer,audit_round,effect_ids,status)
                VALUES(
                  :id,:project,:activity,:attempt,'CANDIDATE',:subject,
                  :profile,'MECHANICAL',1,ARRAY[:effect]::uuid[],'QUARANTINED')"""
            ),
            {
                "id": obligation_id,
                "project": project_id,
                "activity": audit_id,
                "attempt": attempt_id,
                "subject": candidate_id,
                "profile": profile_id,
                "effect": effect_id,
            },
        )
        reevaluate_quarantined_obligations_for_effect(db, effect_id)
        assert (
            db.execute(
                text("SELECT status FROM verification_obligations WHERE id=:id"),
                {"id": obligation_id},
            ).scalar_one()
            == "QUARANTINED"
        )
        ready_n = db.execute(
            text(
                """SELECT COUNT(*) FROM activities
                WHERE goal_id=:goal AND kind='AUDIT' AND status='READY'
                  AND target_id=:cand"""
            ),
            {"goal": goal_id, "cand": candidate_id},
        ).scalar_one()
        assert int(ready_n) == 0
    engine.dispose()


def test_public_claim_supersedes_after_scheduled_reverify(api, objects):
    """第148批：调度 READY 后经公开 claim 开立 OPEN 并 SUPERSEDE（Issue #23）。"""
    import json

    from control_kernel.storage.obligations import (
        assert_no_pending_obligations,
        reevaluate_quarantined_obligations_for_effect,
    )
    from test_claims import _register_worker

    store, _, _ = objects
    client, token, _auth, goal, _worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    project_id = UUID(exec_lease["activity"]["project_id"])
    goal_id = UUID(goal["id"])
    attempt_id = UUID(exec_lease["lease"]["attempt_id"])

    blob = b'{"path":"src/obl-claim.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    artifact = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity="test:obl-claim",
    )

    task_id = uuid4()
    audit_id = uuid4()
    candidate_id = uuid4()
    obligation_id = uuid4()
    effect_id = uuid4()
    step_id = uuid4()

    with engine.begin() as db:
        # LEGACY 全局 claim 才扫 READY
        db.execute(
            text(
                """UPDATE goals SET orchestration_backend='LEGACY',
                    updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": goal_id},
        )
        profile_id = db.execute(
            text(
                """SELECT id FROM verification_profiles
                WHERE project_id=:p ORDER BY version DESC LIMIT 1"""
            ),
            {"p": project_id},
        ).scalar()
        assert profile_id is not None
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
                        "goal_id": str(goal_id),
                        "title": "claim-reverify",
                        "objective": "claim-reverify",
                    }
                ),
                "digest": "sha256:" + "c" * 64,
                "lineage": task_id,
            },
        )
        assignments = [
            {
                "verification_profile_id": str(profile_id),
                "layer": "MECHANICAL",
                "audit_round": 1,
            }
        ]
        exec_row = (
            db.execute(
                text("SELECT binding, resources FROM activities WHERE id=:id"),
                {"id": UUID(exec_lease["activity"]["id"])},
            )
            .mappings()
            .one()
        )
        exec_binding = exec_row["binding"]
        if not isinstance(exec_binding, str):
            exec_binding = json.dumps(exec_binding)
        exec_resources = exec_row["resources"]
        if not isinstance(exec_resources, str):
            exec_resources = json.dumps(exec_resources)
        db.execute(
            text(
                """INSERT INTO activities(
                  id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                  binding,verification_assignments,status,state_revision,depends_on_activity_ids,
                  retry_count,resources)
                VALUES(
                  :id,:project,:goal,:task,:goal,'AUDIT','CANDIDATE',:cand,
                  CAST(:binding AS jsonb),CAST(:assignments AS jsonb),'SUCCEEDED',1,'{}',0,
                  CAST(:resources AS jsonb))"""
            ),
            {
                "id": audit_id,
                "project": project_id,
                "goal": goal_id,
                "task": task_id,
                "cand": candidate_id,
                "binding": exec_binding,
                "assignments": json.dumps(assignments),
                "resources": exec_resources,
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
                "activity": UUID(exec_lease["activity"]["id"]),
                "step": step_id,
                "digest": "sha256:" + "d" * 64,
                "artifact": artifact.id,
                "attempt": attempt_id,
            },
        )
        # claim 路径 _live_binding(AUDIT/CANDIDATE) 须能查到候选清单
        cand_digest = "sha256:" + uuid4().hex + uuid4().hex[:32]
        baseline_digest = "sha256:" + uuid4().hex + uuid4().hex[:32]
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
                "activity": UUID(exec_lease["activity"]["id"]),
                "attempt": attempt_id,
                "baseline": baseline_digest,
                "digest": cand_digest,
                "content": json.dumps({"files": []}),
                "snap": artifact.id,
            },
        )
        db.execute(
            text(
                """INSERT INTO verification_obligations(
                  id,project_id,activity_id,attempt_id,subject_type,subject_id,
                  profile_id,layer,audit_round,effect_ids,status)
                VALUES(
                  :id,:project,:activity,:attempt,'CANDIDATE',:subject,
                  :profile,'MECHANICAL',1,ARRAY[:effect]::uuid[],'QUARANTINED')"""
            ),
            {
                "id": obligation_id,
                "project": project_id,
                "activity": audit_id,
                "attempt": attempt_id,
                "subject": candidate_id,
                "profile": profile_id,
                "effect": effect_id,
            },
        )
        reevaluate_quarantined_obligations_for_effect(db, effect_id)
        with pytest.raises(PlanRejected, match="未结算"):
            assert_no_pending_obligations(db, subject_id=candidate_id)
        ready_id = db.execute(
            text(
                """SELECT id FROM activities
                WHERE goal_id=:goal AND kind='AUDIT' AND status='READY'
                  AND target_id=:cand
                ORDER BY created_at DESC LIMIT 1"""
            ),
            {"goal": goal_id, "cand": candidate_id},
        ).scalar_one()
        # 调度已按 live binding 盖章；仅排空其它 READY AUDIT 防抢领
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE kind='AUDIT' AND status='READY' AND id<>:id"""
            ),
            {"id": ready_id},
        )

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
    assert lease["activity"]["id"] == str(ready_id)
    assert lease["activity"]["kind"] == "AUDIT"
    assigns = lease["activity"]["verification_assignments"]
    assert int(assigns[0]["audit_round"]) == 2
    assert lease["activity"]["binding"]["subject_digest"].startswith("sha256:")

    with engine.begin() as db:
        old = (
            db.execute(
                text(
                    """SELECT status, superseded_by_obligation_id FROM verification_obligations
                    WHERE id=:id"""
                ),
                {"id": obligation_id},
            )
            .mappings()
            .one()
        )
        assert old["status"] == "SUPERSEDED"
        opened = (
            db.execute(
                text(
                    """SELECT * FROM verification_obligations
                    WHERE activity_id=:aid AND attempt_id=:att AND status='OPEN'"""
                ),
                {
                    "aid": UUID(lease["activity"]["id"]),
                    "att": UUID(lease["lease"]["attempt_id"]),
                },
            )
            .mappings()
            .one()
        )
        assert int(opened["audit_round"]) == 2
        assert effect_id in list(opened["effect_ids"] or [])
        assert old["superseded_by_obligation_id"] == opened["id"]
    engine.dispose()


def test_stale_subject_digest_blocks_claim_keeps_quarantine(api, objects):
    """第149批：陈旧 subject_digest → BINDING_STALE；QUARANTINED 仍挡 DONE。"""
    import json

    from control_kernel.storage.activities import PLAN_RESOURCES
    from control_kernel.storage.obligations import (
        assert_no_pending_obligations,
        reevaluate_quarantined_obligations_for_effect,
    )
    from test_claims import _register_worker

    store, _, _ = objects
    client, token, _auth, goal, _worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    project_id = UUID(exec_lease["activity"]["project_id"])
    goal_id = UUID(goal["id"])
    attempt_id = UUID(exec_lease["lease"]["attempt_id"])

    blob = b'{"path":"src/obl-stale.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    artifact = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity="test:obl-stale",
    )

    task_id = uuid4()
    audit_id = uuid4()
    candidate_id = uuid4()
    obligation_id = uuid4()
    effect_id = uuid4()

    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE goals SET orchestration_backend='LEGACY',
                    updated_at=clock_timestamp() WHERE id=:id"""
            ),
            {"id": goal_id},
        )
        profile_id = db.execute(
            text(
                """SELECT id FROM verification_profiles
                WHERE project_id=:p ORDER BY version DESC LIMIT 1"""
            ),
            {"p": project_id},
        ).scalar()
        assert profile_id is not None
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
                        "goal_id": str(goal_id),
                        "title": "stale",
                        "objective": "stale",
                    }
                ),
                "digest": "sha256:" + uuid4().hex + uuid4().hex[:32],
                "lineage": task_id,
            },
        )
        cand_digest = "sha256:" + uuid4().hex + uuid4().hex[:32]
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
                "activity": UUID(exec_lease["activity"]["id"]),
                "attempt": attempt_id,
                "baseline": "sha256:" + uuid4().hex + uuid4().hex[:32],
                "digest": cand_digest,
                "content": json.dumps({"files": []}),
                "snap": artifact.id,
            },
        )
        assignments = [
            {
                "verification_profile_id": str(profile_id),
                "layer": "MECHANICAL",
                "audit_round": 1,
            }
        ]
        db.execute(
            text(
                """INSERT INTO activities(
                  id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                  binding,verification_assignments,status,state_revision,depends_on_activity_ids,
                  retry_count,resources)
                VALUES(
                  :id,:project,:goal,:task,:goal,'AUDIT','CANDIDATE',:cand,
                  CAST(:binding AS jsonb),CAST(:assignments AS jsonb),'SUCCEEDED',1,'{}',0,
                  CAST(:resources AS jsonb))"""
            ),
            {
                "id": audit_id,
                "project": project_id,
                "goal": goal_id,
                "task": task_id,
                "cand": candidate_id,
                "binding": "{}",
                "assignments": json.dumps(assignments),
                "resources": PLAN_RESOURCES.model_dump_json(),
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
                "activity": UUID(exec_lease["activity"]["id"]),
                "step": uuid4(),
                "digest": "sha256:" + uuid4().hex + uuid4().hex[:32],
                "artifact": artifact.id,
                "attempt": attempt_id,
            },
        )
        db.execute(
            text(
                """INSERT INTO verification_obligations(
                  id,project_id,activity_id,attempt_id,subject_type,subject_id,
                  profile_id,layer,audit_round,effect_ids,status)
                VALUES(
                  :id,:project,:activity,:attempt,'CANDIDATE',:subject,
                  :profile,'MECHANICAL',1,ARRAY[:effect]::uuid[],'QUARANTINED')"""
            ),
            {
                "id": obligation_id,
                "project": project_id,
                "activity": audit_id,
                "attempt": attempt_id,
                "subject": candidate_id,
                "profile": profile_id,
                "effect": effect_id,
            },
        )
        reevaluate_quarantined_obligations_for_effect(db, effect_id)
        ready_id = db.execute(
            text(
                """SELECT id FROM activities
                WHERE goal_id=:goal AND kind='AUDIT' AND status='READY'
                  AND target_id=:cand LIMIT 1"""
            ),
            {"goal": goal_id, "cand": candidate_id},
        ).scalar_one()
        binding = db.execute(
            text("SELECT binding FROM activities WHERE id=:id"),
            {"id": ready_id},
        ).scalar_one()
        if isinstance(binding, str):
            binding = json.loads(binding)
        else:
            binding = dict(binding)
        binding["subject_digest"] = "sha256:" + "a" * 64
        db.execute(
            text(
                """UPDATE activities SET binding=CAST(:b AS jsonb), updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": ready_id, "b": json.dumps(binding)},
        )
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE kind='AUDIT' AND status='READY' AND id<>:id"""
            ),
            {"id": ready_id},
        )

    subject = str(uuid4())
    _register_worker(subject, kinds=("AUDIT",))
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["AUDIT"], "capabilities": []},
        headers={
            "Authorization": "Bearer " + token(subject, ["worker"]),
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert claimed.status_code == 409, claimed.text
    assert claimed.json()["error"]["code"] == "BINDING_STALE"

    with engine.begin() as db:
        assert (
            db.execute(
                text("SELECT status FROM verification_obligations WHERE id=:id"),
                {"id": obligation_id},
            ).scalar_one()
            == "QUARANTINED"
        )
        with pytest.raises(PlanRejected, match="未结算"):
            assert_no_pending_obligations(db, subject_id=candidate_id)
    engine.dispose()


def test_reconcile_new_round_verification_run_assesses(api, objects):
    """第152批：UNKNOWN 期间 run 不可复用；对账后新 round 可 ASSESSED 并收敛 DONE 门禁。"""
    from control_kernel.storage.obligations import assert_attempt_obligations_assessed
    from test_audits import _seal_and_finish_execute
    from test_claims import _register_worker
    from verification_run_helpers import (
        post_verification_run,
        profile_verifier_digest,
        receipt_artifact,
    )

    store, _, _ = objects
    client, token, auth, _goal, candidate, _task_id = _seal_and_finish_execute(
        api, objects
    )
    client.app.state.objects = store
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
    engine = client.app.state.engine
    project_id = UUID(lease["activity"]["project_id"])
    activity_id = UUID(lease["activity"]["id"])
    attempt_id = UUID(lease["lease"]["attempt_id"])
    assignment = lease["activity"]["verification_assignments"][0]
    receipt_id = receipt_artifact(engine, store, project_id)
    verifier_digest = profile_verifier_digest(
        client, auth, str(project_id), assignment["verification_profile_id"]
    )

    run1_id = post_verification_run(
        client,
        worker_auth,
        lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=verifier_digest,
        receipt_id=receipt_id,
        effect_status="UNKNOWN",
    )

    eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with eng.begin() as db:
        obl1 = (
            db.execute(
                text(
                    """SELECT id, status, assessment_id, audit_round FROM verification_obligations
                    WHERE activity_id=:a AND attempt_id=:t AND audit_round=1"""
                ),
                {"a": activity_id, "t": attempt_id},
            )
            .mappings()
            .one()
        )
        assert obl1["status"] == "QUARANTINED"
        assert obl1["assessment_id"] is None
        run1 = (
            db.execute(
                text(
                    """SELECT content_digest, audit_round FROM verification_runs WHERE id=:id"""
                ),
                {"id": UUID(run1_id)},
            )
            .mappings()
            .one()
        )
        assert int(run1["audit_round"]) == 1
        run1_digest = run1["content_digest"]
        assessments_r1 = db.execute(
            text(
                """SELECT count(*) FROM verification_assessments
                WHERE run_id=:id"""
            ),
            {"id": UUID(run1_id)},
        ).scalar_one()
        assert assessments_r1 == 1

        effect_id = db.execute(
            text(
                """SELECT effect_ids[1] FROM verification_obligations WHERE id=:id"""
            ),
            {"id": obl1["id"]},
        ).scalar_one()
        db.execute(
            text(
                """UPDATE effect_intents SET status='SUCCEEDED',
                  state_revision=state_revision+1, updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": effect_id},
        )
        reevaluate_quarantined_obligations_for_effect(db, effect_id)
        obl1_after = (
            db.execute(
                text(
                    """SELECT status, superseded_by_obligation_id FROM verification_obligations
                    WHERE id=:id"""
                ),
                {"id": obl1["id"]},
            )
            .mappings()
            .one()
        )
        assert obl1_after["status"] == "SUPERSEDED"
        assert obl1_after["superseded_by_obligation_id"] is not None
        succ = (
            db.execute(
                text(
                    """SELECT status, audit_round FROM verification_obligations
                    WHERE id=:id"""
                ),
                {"id": obl1_after["superseded_by_obligation_id"]},
            )
            .mappings()
            .one()
        )
        assert succ["status"] == "OPEN"
        assert int(succ["audit_round"]) == 2
        assign_round = db.execute(
            text(
                """SELECT (verification_assignments->0->>'audit_round')::int
                FROM activities WHERE id=:id"""
            ),
            {"id": activity_id},
        ).scalar_one()
        assert int(assign_round) == 2

    # 新 round：公开 VerificationRun；不得复用 round=1 的 assessment
    run2_id = post_verification_run(
        client,
        worker_auth,
        lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=verifier_digest,
        receipt_id=receipt_id,
        link_action=False,
        audit_round=2,
    )
    assert run2_id != run1_id

    with eng.begin() as db:
        run1_again = db.execute(
            text("SELECT content_digest FROM verification_runs WHERE id=:id"),
            {"id": UUID(run1_id)},
        ).scalar_one()
        assert run1_again == run1_digest
        obl_rows = (
            db.execute(
                text(
                    """SELECT status, audit_round, assessment_id FROM verification_obligations
                    WHERE activity_id=:a AND attempt_id=:t ORDER BY audit_round"""
                ),
                {"a": activity_id, "t": attempt_id},
            )
            .mappings()
            .all()
        )
        assert [r["status"] for r in obl_rows] == ["SUPERSEDED", "ASSESSED"]
        assert int(obl_rows[1]["audit_round"]) == 2
        assert obl_rows[1]["assessment_id"] is not None
        # 旧 assessment 仍可查且未绑到新义务
        old_assessment_run = db.execute(
            text(
                """SELECT run_id FROM verification_assessments
                WHERE id=(SELECT id FROM verification_assessments
                          WHERE run_id=:r1 LIMIT 1)"""
            ),
            {"r1": UUID(run1_id)},
        ).scalar_one()
        assert str(old_assessment_run) == run1_id
        assert_no_pending_obligations(db, subject_id=UUID(candidate["id"]))
        assert_attempt_obligations_assessed(
            db, activity_id=activity_id, attempt_id=attempt_id
        )
        # 幂等：再对账不得再插义务
        reevaluate_quarantined_obligations_for_effect(db, effect_id)
        n = db.execute(
            text(
                """SELECT count(*) FROM verification_obligations
                WHERE activity_id=:a AND attempt_id=:t"""
            ),
            {"a": activity_id, "t": attempt_id},
        ).scalar_one()
        assert n == 2
    eng.dispose()
