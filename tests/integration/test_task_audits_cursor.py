"""Task audits 列表 cursor 分页（doc/05：cursor,limit）。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from test_audits import _seal_and_finish_execute
from test_claims import _register_worker
from verification_run_helpers import post_verification_run as _post_verification_run
from verification_run_helpers import profile_verifier_digest as _profile_verifier_digest
from verification_run_helpers import receipt_artifact as _receipt_artifact


def _complete_one_audit(client, token, auth, candidate, task_id):
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
    assignment = lease["activity"]["verification_assignments"][0]
    binding = lease["activity"]["binding"]
    project_id = UUID(lease["activity"]["project_id"])
    receipt_id = _receipt_artifact(client.app.state.engine, client.app.state.objects, project_id)
    verifier_digest = _profile_verifier_digest(
        client, auth, str(project_id), assignment["verification_profile_id"]
    )
    run_id = _post_verification_run(
        client,
        worker_auth,
        lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=verifier_digest,
        receipt_id=receipt_id,
    )
    outcome = client.post(
        f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {
                "target_type": "CANDIDATE",
                "audit": {
                    "subject_candidate_manifest_id": candidate["id"],
                    "goal_contract_revision": binding["goal_contract_revision"],
                    "task_contract_revision": binding["task_contract_revision"],
                    "verification_profile_id": assignment["verification_profile_id"],
                    "layer": assignment["layer"],
                    "audit_round": assignment["audit_round"],
                    "verifier_run_ids": [run_id],
                    "verdict": "PASS",
                    "criterion_results": [
                        {
                            "criterion_id": "A1",
                            "verdict": "PASS",
                            "evidence_ids": [],
                            "reason": "机械检查通过",
                        }
                    ],
                    "evidence_ids": [],
                    "reason": "验收通过",
                },
            },
        },
        headers=worker_auth,
    )
    assert outcome.status_code == 200, outcome.text
    return lease["activity"]


def test_list_task_audits_cursor_pagination(api, objects):
    """limit=1 翻页；坏 cursor → 400。第二行用同形 SQL 种入（独立 activity）。"""
    store, _, _ = objects
    client, token, auth, _goal, candidate, task_id = _seal_and_finish_execute(api, objects)
    client.app.state.objects = store
    first_activity = _complete_one_audit(client, token, auth, candidate, task_id)

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    try:
        with engine.begin() as db:
            first = (
                db.execute(
                    text("SELECT * FROM audits WHERE task_id=:t ORDER BY created_at,id"),
                    {"t": task_id},
                )
                .mappings()
                .one()
            )
            # 种入第二条：独立 AUDIT 活动 + attempt（UNIQUE activity_id）
            act_id = uuid4()
            attempt_id = uuid4()
            db.execute(
                text(
                    """INSERT INTO activities(
                      id,project_id,goal_id,task_id,kind,status,target_type,target_id,
                      budget_scope_id,binding,resources,retry_count,state_revision)
                    SELECT :id,project_id,goal_id,task_id,'AUDIT','SUCCEEDED',
                      target_type,target_id,budget_scope_id,binding,resources,0,1
                    FROM activities WHERE id=:src"""
                ),
                {"id": act_id, "src": first_activity["id"]},
            )
            db.execute(
                text(
                    """INSERT INTO activity_attempts(
                      id,activity_id,project_id,worker_id,binding_digest,fencing_epoch,
                      lease_expires_at,renewal_seq,status,context_digest,model_snapshot,
                      skill_versions,started_at,finished_at)
                    SELECT :id,:act,project_id,worker_id,binding_digest,fencing_epoch,
                      lease_expires_at,renewal_seq,'COMPLETED',context_digest,model_snapshot,
                      skill_versions,started_at,finished_at
                    FROM activity_attempts WHERE id=:src"""
                ),
                {"id": attempt_id, "act": act_id, "src": first["attempt_id"]},
            )
            content = {
                "subject_candidate_manifest_id": str(first["subject_candidate_manifest_id"]),
                "goal_contract_revision": first["goal_contract_revision"],
                "task_contract_revision": first["task_contract_revision"],
                "verification_profile_id": str(first["verification_profile_id"]),
                "layer": first["layer"],
                "audit_round": int(first["audit_round"]) + 1,
                "verifier_run_ids": [],
                "verdict": "PASS",
                "criterion_results": first["criterion_results"],
                "evidence_ids": [],
                "reason": "cursor-page-fixture",
            }
            digest = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        {
                            "schema_version": 3,
                            "object_type": "Audit",
                            "content": content,
                            "reference_bindings": [],
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
            )
            db.execute(
                text(
                    """INSERT INTO audits(
                      id,project_id,goal_id,task_id,activity_id,attempt_id,
                      subject_candidate_manifest_id,goal_contract_revision,task_contract_revision,
                      verification_profile_id,layer,audit_round,verifier_run_ids,verdict,
                      criterion_results,evidence_ids,reason,content_digest,created_at)
                    VALUES(
                      :id,:project,:goal,:task,:act,:attempt,
                      :cand,:grev,:trev,
                      :profile,:layer,:round,'{}',:verdict,
                      CAST(:results AS jsonb),'{}',:reason,:digest,
                      :created)"""
                ),
                {
                    "id": uuid4(),
                    "project": first["project_id"],
                    "goal": first["goal_id"],
                    "task": task_id,
                    "act": act_id,
                    "attempt": attempt_id,
                    "cand": first["subject_candidate_manifest_id"],
                    "grev": first["goal_contract_revision"],
                    "trev": first["task_contract_revision"],
                    "profile": first["verification_profile_id"],
                    "layer": first["layer"],
                    "round": int(first["audit_round"]) + 1,
                    "verdict": "PASS",
                    "results": json.dumps(first["criterion_results"]),
                    "reason": "cursor-page-fixture",
                    "digest": digest,
                    "created": datetime.now(UTC),
                },
            )
    finally:
        engine.dispose()

    page1 = client.get(
        f"/api/v1/tasks/{task_id}/audits",
        params={"limit": 1},
        headers=auth,
    )
    assert page1.status_code == 200, page1.text
    body1 = page1.json()
    assert len(body1["data"]) == 1
    cursor = body1["meta"]["next_cursor"]
    assert cursor

    page2 = client.get(
        f"/api/v1/tasks/{task_id}/audits",
        params={"limit": 1, "cursor": cursor},
        headers=auth,
    )
    assert page2.status_code == 200, page2.text
    body2 = page2.json()
    assert len(body2["data"]) == 1
    assert body2["data"][0]["id"] != body1["data"][0]["id"]
    assert body2["meta"].get("next_cursor") in (None, "")

    bad = client.get(
        f"/api/v1/tasks/{task_id}/audits",
        params={"limit": 1, "cursor": "nope"},
        headers=auth,
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_REQUEST"
