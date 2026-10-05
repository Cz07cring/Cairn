"""Quarantine obligation inbox：QUARANTINED 义务可列表读取，SystemStatus 信号 DEGRADED。"""

from __future__ import annotations

import hashlib
import os
from io import BytesIO
from uuid import UUID, uuid4

from control_kernel.storage.artifacts import Artifacts
from control_kernel.storage.obligations import settle_obligation_after_assessment
from sqlalchemy import create_engine, text
from test_effects import _publish_and_claim_execute


def test_quarantine_obligation_inbox_list_and_system_status(api, objects):
    """UNKNOWN settle → QUARANTINED 后，internal list 与 system status 可见。"""
    store, _, _ = objects
    client, _token, auth, goal, _worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    activity_id = UUID(exec_lease["activity"]["id"])
    attempt_id = UUID(exec_lease["lease"]["attempt_id"])
    project_id = UUID(exec_lease["activity"]["project_id"])
    goal_id = UUID(goal["id"])

    blob = b'{"path":"src/obl-inbox.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    artifact = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity="test:obl-inbox",
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
                "digest": "sha256:" + "c" * 64,
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

    listed = client.get(
        "/internal/v1/verification-obligations",
        params={"project_id": str(project_id), "status": "QUARANTINED"},
        headers=auth,
    )
    assert listed.status_code == 200, listed.text
    rows = listed.json()["data"]
    assert any(r["id"] == str(obligation_id) for r in rows)
    hit = next(r for r in rows if r["id"] == str(obligation_id))
    assert hit["status"] == "QUARANTINED"
    assert hit["effect_ids"] == [str(effect_id)]

    public = client.get(
        "/api/v1/verification-obligations",
        params={"project_id": str(project_id), "status": "QUARANTINED"},
        headers=auth,
    )
    assert public.status_code == 200, public.text
    assert any(r["id"] == str(obligation_id) for r in public.json()["data"])

    one = client.get(
        f"/internal/v1/verification-obligations/{obligation_id}",
        headers=auth,
    )
    assert one.status_code == 200, one.text
    assert one.json()["data"]["status"] == "QUARANTINED"

    public_one = client.get(
        f"/api/v1/verification-obligations/{obligation_id}",
        headers=auth,
    )
    assert public_one.status_code == 200, public_one.text
    assert public_one.json()["data"]["status"] == "QUARANTINED"

    status = client.get(
        "/api/v1/system/status",
        params={"project_id": str(project_id)},
        headers=auth,
    )
    assert status.status_code == 200, status.text
    components = {c["name"]: c for c in status.json()["data"]["components"]}
    obl = components["verification_obligation_quarantine"]
    assert obl["state"] == "DEGRADED"
    assert obl["reason_code"] == "QUARANTINED_OBLIGATIONS:1"
