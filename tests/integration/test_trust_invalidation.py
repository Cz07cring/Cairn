"""Issue #23 / doc/05：POST /internal/v1/trust/invalidations 封锁信任。"""

from uuid import UUID, uuid4

from sqlalchemy import text
from test_audits import _seal_and_finish_execute


def test_trust_invalidation_blocks_and_bumps_revision(api, objects):
    """第154批：ops 登记 INVALID → BLOCKED + trust_revision+1；幂等返回同 job。"""
    store, _, _ = objects
    client, token, _auth, _goal, candidate, _task_id = _seal_and_finish_execute(
        api, objects
    )
    client.app.state.objects = store
    engine = client.app.state.engine
    with engine.connect() as db:
        project_id = db.execute(
            text("""SELECT project_id FROM candidate_manifests WHERE id=:id"""),
            {"id": UUID(candidate["id"])},
        ).scalar_one()
        evidence_id = db.execute(
            text(
                """SELECT id FROM evidence_envelopes
                WHERE project_id=:p ORDER BY created_at DESC LIMIT 1"""
            ),
            {"p": project_id},
        ).scalar()
        rev = db.execute(
            text(
                """SELECT trust_revision FROM project_trust_states WHERE project_id=:p"""
            ),
            {"p": project_id},
        ).scalar_one()
    assert evidence_id is not None, "seal 路径应产生 EvidenceEnvelope"

    ops = str(uuid4())
    with engine.begin() as db:
        db.execute(
            text(
                """INSERT INTO project_memberships(project_id, subject)
                VALUES(:p,:s) ON CONFLICT DO NOTHING"""
            ),
            {"p": project_id, "s": ops},
        )
    ops_auth = {"Authorization": "Bearer " + token(ops, ["ops"])}

    created = client.post(
        "/internal/v1/trust/invalidations",
        json={
            "project_id": str(project_id),
            "expected_trust_revision": str(rev),
            "evidence_id": str(evidence_id),
            "reason_code": "SOURCE_REVOKED",
            "proof_artifact_ids": [],
        },
        headers=ops_auth,
    )
    assert created.status_code == 202, created.text
    job = created.json()["data"]
    assert job["status"] == "PENDING"
    assert len(job["decision_ids"]) >= 1

    with engine.connect() as db:
        trust = (
            db.execute(
                text(
                    """SELECT status, trust_revision, decision_ids, propagation_job_id
                    FROM project_trust_states WHERE project_id=:p"""
                ),
                {"p": project_id},
            )
            .mappings()
            .one()
        )
    assert trust["status"] == "BLOCKED"
    assert int(trust["trust_revision"]) == int(rev) + 1
    assert str(trust["propagation_job_id"]) == job["id"]

    again = client.post(
        "/internal/v1/trust/invalidations",
        json={
            "project_id": str(project_id),
            "expected_trust_revision": str(rev),
            "evidence_id": str(evidence_id),
            "reason_code": "SOURCE_REVOKED",
            "proof_artifact_ids": [],
        },
        headers=ops_auth,
    )
    assert again.status_code == 202, again.text
    assert again.json()["data"]["id"] == job["id"]

    missing = client.post(
        "/internal/v1/trust/invalidations",
        json={
            "project_id": str(project_id),
            "expected_trust_revision": str(trust["trust_revision"]),
            "evidence_id": str(uuid4()),
            "reason_code": "SOURCE_REVOKED",
            "proof_artifact_ids": [],
        },
        headers=ops_auth,
    )
    assert missing.status_code == 404, missing.text

    got = client.get(f"/internal/v1/trust/jobs/{job['id']}", headers=ops_auth)
    assert got.status_code == 200, got.text
    assert got.json()["data"]["status"] == "PENDING"

    worker = {"Authorization": "Bearer " + token(str(uuid4()), ["worker"])}
    denied = client.post(
        "/internal/v1/trust/invalidations",
        json={
            "project_id": str(project_id),
            "expected_trust_revision": str(trust["trust_revision"]),
            "evidence_id": str(evidence_id),
            "reason_code": "X",
            "proof_artifact_ids": [],
        },
        headers=worker,
    )
    assert denied.status_code == 403


def test_trust_propagation_advance_unlocks_open(api, objects):
    """第155批：advance 扫描后 COMPLETE，CAS 将 TrustState 置回 OPEN。"""
    store, _, _ = objects
    client, token, _auth, goal, candidate, _task_id = _seal_and_finish_execute(
        api, objects
    )
    client.app.state.objects = store
    engine = client.app.state.engine
    with engine.connect() as db:
        project_id = db.execute(
            text("""SELECT project_id FROM candidate_manifests WHERE id=:id"""),
            {"id": UUID(candidate["id"])},
        ).scalar_one()
        evidence_id = db.execute(
            text(
                """SELECT id FROM evidence_envelopes
                WHERE project_id=:p ORDER BY created_at DESC LIMIT 1"""
            ),
            {"p": project_id},
        ).scalar_one()
        rev = db.execute(
            text(
                """SELECT trust_revision FROM project_trust_states WHERE project_id=:p"""
            ),
            {"p": project_id},
        ).scalar_one()

    ops = str(uuid4())
    with engine.begin() as db:
        db.execute(
            text(
                """INSERT INTO project_memberships(project_id, subject)
                VALUES(:p,:s) ON CONFLICT DO NOTHING"""
            ),
            {"p": project_id, "s": ops},
        )
    ops_auth = {"Authorization": "Bearer " + token(ops, ["ops"])}

    created = client.post(
        "/internal/v1/trust/invalidations",
        json={
            "project_id": str(project_id),
            "expected_trust_revision": str(rev),
            "evidence_id": str(evidence_id),
            "reason_code": "SOURCE_REVOKED",
            "proof_artifact_ids": [],
        },
        headers=ops_auth,
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["data"]["id"]

    advanced = client.post(
        f"/internal/v1/trust/jobs/{job_id}/advance",
        headers=ops_auth,
    )
    assert advanced.status_code == 200, advanced.text
    body = advanced.json()["data"]
    assert body["status"] == "COMPLETE"
    assert body["cursor"] == "scan:goals+skills+releases+drain:done"
    assert UUID(goal["id"]) in [UUID(g) for g in body["affected_goal_ids"]]

    with engine.connect() as db:
        trust = (
            db.execute(
                text(
                    """SELECT status, propagation_job_id, trust_revision
                    FROM project_trust_states WHERE project_id=:p"""
                ),
                {"p": project_id},
            )
            .mappings()
            .one()
        )
    assert trust["status"] == "OPEN"
    assert trust["propagation_job_id"] is None
    assert int(trust["trust_revision"]) == int(rev) + 1

    # 幂等再 advance：保持 OPEN
    again = client.post(
        f"/internal/v1/trust/jobs/{job_id}/advance",
        headers=ops_auth,
    )
    assert again.status_code == 200, again.text
    assert again.json()["data"]["status"] == "COMPLETE"
    with engine.connect() as db:
        assert (
            db.execute(
                text("SELECT status FROM project_trust_states WHERE project_id=:p"),
                {"p": project_id},
            ).scalar_one()
            == "OPEN"
        )


def test_trust_propagation_revokes_active_skill_on_invalid_evidence(api, objects):
    """第156批：校验记录引用失效 evidence 的 ACTIVE Skill → REVOKED。"""
    import hashlib
    import json

    from evidence_ledger.content import encode

    store, _, _ = objects
    client, token, _auth, _goal, candidate, _task_id = _seal_and_finish_execute(
        api, objects
    )
    client.app.state.objects = store
    engine = client.app.state.engine
    with engine.connect() as db:
        project_id = db.execute(
            text("""SELECT project_id FROM candidate_manifests WHERE id=:id"""),
            {"id": UUID(candidate["id"])},
        ).scalar_one()
        evidence_id = db.execute(
            text(
                """SELECT id FROM evidence_envelopes
                WHERE project_id=:p ORDER BY created_at DESC LIMIT 1"""
            ),
            {"p": project_id},
        ).scalar_one()
        profile_id = db.execute(
            text(
                """SELECT id FROM verification_profiles
                WHERE project_id=:p ORDER BY version DESC LIMIT 1"""
            ),
            {"p": project_id},
        ).scalar_one()
        rev = db.execute(
            text(
                """SELECT trust_revision FROM project_trust_states WHERE project_id=:p"""
            ),
            {"p": project_id},
        ).scalar_one()

    skill_id = uuid4()
    version_id = uuid4()
    record_id = uuid4()
    subject_digest = "sha256:" + "e" * 64
    content = {
        "project_id": str(project_id),
        "producer_activity_id": str(uuid4()),
        "producer_attempt_id": str(uuid4()),
        "subject_skill_version_id": str(version_id),
        "subject_digest": subject_digest,
        "verification_profile_id": str(profile_id),
        "audit_round": 1,
        "verifier_run_ids": [str(uuid4())],
        "verdict": "PASS",
        "criterion_results": [
            {
                "criterion_id": "S1",
                "verdict": "PASS",
                "evidence_ids": [str(evidence_id)],
                "reason": "planted",
            }
        ],
        "evidence_ids": [str(evidence_id)],
        "reason": "planted for trust revoke",
    }
    content_digest = (
        "sha256:"
        + hashlib.sha256(
            encode(
                json.dumps(
                    {
                        "schema_version": 3,
                        "object_type": "SkillValidationRecord",
                        "content": content,
                        "reference_bindings": [],
                    }
                )
            )
        ).hexdigest()
    )
    with engine.begin() as db:
        db.execute(
            text(
                """INSERT INTO skill_versions(
                  id,skill_id,project_id,name,version,content_digest,config,status)
                VALUES(
                  :id,:skill,:project,'trust-revoke-skill',1,:digest,
                  CAST(:config AS jsonb),'CANDIDATE')"""
            ),
            {
                "id": version_id,
                "skill": skill_id,
                "project": project_id,
                "digest": "sha256:" + "f" * 64,
                "config": json.dumps(
                    {
                        "project_id": str(project_id),
                        "name": "trust-revoke-skill",
                        "source_ref": "skills/trust-revoke",
                        "content_artifact_id": str(uuid4()),
                        "capabilities": [],
                        "role_scopes": ["PLAN"],
                        "required_tools": [],
                        "verification_profile_id": str(profile_id),
                        "acceptance": [],
                    }
                ),
            },
        )
        db.execute(
            text(
                """INSERT INTO skill_validation_records(
                  id,project_id,subject_skill_version_id,subject_digest,
                  verification_profile_id,content_digest,content,verdict)
                VALUES(
                  :id,:project,:version,:subject,:profile,:digest,
                  CAST(:content AS jsonb),'PASS')"""
            ),
            {
                "id": record_id,
                "project": project_id,
                "version": version_id,
                "subject": subject_digest,
                "profile": profile_id,
                "digest": content_digest,
                "content": json.dumps(content),
            },
        )
        db.execute(
            text(
                """UPDATE skill_versions
                SET status='ACTIVE', audit_id=:audit, updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": version_id, "audit": record_id},
        )

    ops = str(uuid4())
    with engine.begin() as db:
        db.execute(
            text(
                """INSERT INTO project_memberships(project_id, subject)
                VALUES(:p,:s) ON CONFLICT DO NOTHING"""
            ),
            {"p": project_id, "s": ops},
        )
    ops_auth = {"Authorization": "Bearer " + token(ops, ["ops"])}

    created = client.post(
        "/internal/v1/trust/invalidations",
        json={
            "project_id": str(project_id),
            "expected_trust_revision": str(rev),
            "evidence_id": str(evidence_id),
            "reason_code": "SOURCE_REVOKED",
            "proof_artifact_ids": [],
        },
        headers=ops_auth,
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["data"]["id"]
    advanced = client.post(
        f"/internal/v1/trust/jobs/{job_id}/advance",
        headers=ops_auth,
    )
    assert advanced.status_code == 200, advanced.text
    body = advanced.json()["data"]
    assert body["status"] == "COMPLETE"
    assert str(version_id) in body["affected_skill_ids"]

    with engine.connect() as db:
        skill = (
            db.execute(
                text(
                    """SELECT status, revocation_reason FROM skill_versions WHERE id=:id"""
                ),
                {"id": version_id},
            )
            .mappings()
            .one()
        )
        trust_status = db.execute(
            text("SELECT status FROM project_trust_states WHERE project_id=:p"),
            {"p": project_id},
        ).scalar_one()
    assert skill["status"] == "REVOKED"
    assert skill["revocation_reason"] == "VALIDATION_EVIDENCE_INVALID"
    assert trust_status == "OPEN"


def test_trust_propagation_revokes_candidate_skill(api, objects):
    """第158批：CANDIDATE 校验依赖失效 evidence → 亦 REVOKED（不必先 ACTIVE）。"""
    import hashlib
    import json

    from evidence_ledger.content import encode

    store, _, _ = objects
    client, token, _auth, _goal, candidate, _task_id = _seal_and_finish_execute(
        api, objects
    )
    client.app.state.objects = store
    engine = client.app.state.engine
    with engine.connect() as db:
        project_id = db.execute(
            text("""SELECT project_id FROM candidate_manifests WHERE id=:id"""),
            {"id": UUID(candidate["id"])},
        ).scalar_one()
        evidence_id = db.execute(
            text(
                """SELECT id FROM evidence_envelopes
                WHERE project_id=:p ORDER BY created_at DESC LIMIT 1"""
            ),
            {"p": project_id},
        ).scalar_one()
        profile_id = db.execute(
            text(
                """SELECT id FROM verification_profiles
                WHERE project_id=:p ORDER BY version DESC LIMIT 1"""
            ),
            {"p": project_id},
        ).scalar_one()
        rev = db.execute(
            text(
                """SELECT trust_revision FROM project_trust_states WHERE project_id=:p"""
            ),
            {"p": project_id},
        ).scalar_one()

    skill_id = uuid4()
    version_id = uuid4()
    record_id = uuid4()
    subject_digest = "sha256:" + "c" * 64
    content = {
        "project_id": str(project_id),
        "producer_activity_id": str(uuid4()),
        "producer_attempt_id": str(uuid4()),
        "subject_skill_version_id": str(version_id),
        "subject_digest": subject_digest,
        "verification_profile_id": str(profile_id),
        "audit_round": 1,
        "verifier_run_ids": [str(uuid4())],
        "verdict": "PASS",
        "criterion_results": [
            {
                "criterion_id": "S1",
                "verdict": "PASS",
                "evidence_ids": [str(evidence_id)],
                "reason": "planted",
            }
        ],
        "evidence_ids": [str(evidence_id)],
        "reason": "candidate for trust revoke",
    }
    content_digest = (
        "sha256:"
        + hashlib.sha256(
            encode(
                json.dumps(
                    {
                        "schema_version": 3,
                        "object_type": "SkillValidationRecord",
                        "content": content,
                        "reference_bindings": [],
                    }
                )
            )
        ).hexdigest()
    )
    with engine.begin() as db:
        db.execute(
            text(
                """INSERT INTO skill_versions(
                  id,skill_id,project_id,name,version,content_digest,config,status)
                VALUES(
                  :id,:skill,:project,'trust-candidate-skill',1,:digest,
                  CAST(:config AS jsonb),'CANDIDATE')"""
            ),
            {
                "id": version_id,
                "skill": skill_id,
                "project": project_id,
                "digest": "sha256:" + "d" * 64,
                "config": json.dumps(
                    {
                        "project_id": str(project_id),
                        "name": "trust-candidate-skill",
                        "source_ref": "skills/trust-candidate",
                        "content_artifact_id": str(uuid4()),
                        "capabilities": [],
                        "role_scopes": ["PLAN"],
                        "required_tools": [],
                        "verification_profile_id": str(profile_id),
                        "acceptance": [],
                    }
                ),
            },
        )
        db.execute(
            text(
                """INSERT INTO skill_validation_records(
                  id,project_id,subject_skill_version_id,subject_digest,
                  verification_profile_id,content_digest,content,verdict)
                VALUES(
                  :id,:project,:version,:subject,:profile,:digest,
                  CAST(:content AS jsonb),'PASS')"""
            ),
            {
                "id": record_id,
                "project": project_id,
                "version": version_id,
                "subject": subject_digest,
                "profile": profile_id,
                "digest": content_digest,
                "content": json.dumps(content),
            },
        )

    ops = str(uuid4())
    with engine.begin() as db:
        db.execute(
            text(
                """INSERT INTO project_memberships(project_id, subject)
                VALUES(:p,:s) ON CONFLICT DO NOTHING"""
            ),
            {"p": project_id, "s": ops},
        )
    ops_auth = {"Authorization": "Bearer " + token(ops, ["ops"])}
    created = client.post(
        "/internal/v1/trust/invalidations",
        json={
            "project_id": str(project_id),
            "expected_trust_revision": str(rev),
            "evidence_id": str(evidence_id),
            "reason_code": "SOURCE_REVOKED",
            "proof_artifact_ids": [],
        },
        headers=ops_auth,
    )
    assert created.status_code == 202, created.text
    advanced = client.post(
        f"/internal/v1/trust/jobs/{created.json()['data']['id']}/advance",
        headers=ops_auth,
    )
    assert advanced.status_code == 200, advanced.text
    assert str(version_id) in advanced.json()["data"]["affected_skill_ids"]
    with engine.connect() as db:
        status = db.execute(
            text("SELECT status, revocation_reason FROM skill_versions WHERE id=:id"),
            {"id": version_id},
        ).mappings().one()
    assert status["status"] == "REVOKED"
    assert status["revocation_reason"] == "VALIDATION_EVIDENCE_INVALID"


def test_revoked_skill_in_set_blocks_new_goal_binding(api, objects):
    """第158批：SkillSet 摘要未变但成员已 REVOKED → 禁止新 Goal 绑定。"""
    from test_goals import _ready_project

    client, _token, auth, project, goal_body = _ready_project(api, objects)
    engine = client.app.state.engine
    skill_set_id = UUID(goal_body["skill_set_id"])
    with engine.begin() as db:
        version_id = db.execute(
            text(
                """SELECT (config->'skill_version_ids'->>0)::uuid
                FROM skill_sets WHERE id=:id"""
            ),
            {"id": skill_set_id},
        ).scalar_one()
        db.execute(
            text(
                """UPDATE skill_versions
                SET status='REVOKED',
                    revocation_reason='VALIDATION_EVIDENCE_INVALID',
                    updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": version_id},
        )

    denied = client.post(
        "/api/v1/goals",
        json=goal_body,
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert denied.status_code == 422, denied.text
    err = denied.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert "非 ACTIVE" in err["message"]
    listed = client.get(
        "/api/v1/goals",
        params={"project_id": project},
        headers=auth,
    )
    assert listed.status_code == 200
    assert listed.json()["data"] == []


def test_trust_propagation_invalidates_release_after_done(api, objects, monkeypatch):
    """第157批：DONE 后失效 evidence → ReleaseValidity INVALIDATED；拒 OFFLINE 导出。"""
    from test_finalization import _goal_done_with_release

    client, token, auth, goal_done, release_id, _candidate, _worker = (
        _goal_done_with_release(api, objects)
    )
    engine = client.app.state.engine
    with engine.connect() as db:
        project_id = UUID(goal_done["project_id"])
        evidence_id = db.execute(
            text(
                """SELECT id FROM evidence_envelopes
                WHERE project_id=:p AND goal_id=:g
                ORDER BY created_at DESC LIMIT 1"""
            ),
            {"p": project_id, "g": UUID(goal_done["id"])},
        ).scalar()
        rev = db.execute(
            text(
                """SELECT trust_revision FROM project_trust_states WHERE project_id=:p"""
            ),
            {"p": project_id},
        ).scalar_one()
    assert evidence_id is not None, "DONE 路径应留下 Goal 关联 EvidenceEnvelope"

    before = client.get(f"/api/v1/goals/{goal_done['id']}/release", headers=auth)
    assert before.status_code == 200, before.text
    assert before.json()["data"]["validity"]["status"] == "VALID"

    ops = str(uuid4())
    with engine.begin() as db:
        db.execute(
            text(
                """INSERT INTO project_memberships(project_id, subject)
                VALUES(:p,:s) ON CONFLICT DO NOTHING"""
            ),
            {"p": project_id, "s": ops},
        )
    ops_auth = {"Authorization": "Bearer " + token(ops, ["ops"])}

    created = client.post(
        "/internal/v1/trust/invalidations",
        json={
            "project_id": str(project_id),
            "expected_trust_revision": str(rev),
            "evidence_id": str(evidence_id),
            "reason_code": "SOURCE_REVOKED",
            "proof_artifact_ids": [],
        },
        headers=ops_auth,
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["data"]["id"]
    advanced = client.post(
        f"/internal/v1/trust/jobs/{job_id}/advance",
        headers=ops_auth,
    )
    assert advanced.status_code == 200, advanced.text
    body = advanced.json()["data"]
    assert body["status"] == "COMPLETE"
    assert body["cursor"] == "scan:goals+skills+releases+drain:done"
    assert release_id in body["affected_release_ids"]

    after = client.get(f"/api/v1/goals/{goal_done['id']}/release", headers=auth)
    assert after.status_code == 200, after.text
    validity = after.json()["data"]["validity"]
    assert validity["status"] == "INVALIDATED"
    assert len(validity["decision_ids"]) >= 1
    # 历史 Manifest / Goal 终态不变
    assert after.json()["data"]["manifest"]["id"] == release_id
    goal = client.get(f"/api/v1/goals/{goal_done['id']}", headers=auth)
    assert goal.json()["data"]["status"] == "DONE"

    monkeypatch.setenv("RING_ATTESTATION_PROVIDER_REF", "test.attestation.provider")
    monkeypatch.setenv("RING_TRUST_BUNDLE_REF", "test.trust.bundle")
    denied = client.post(
        f"/api/v1/goals/{goal_done['id']}/evidence-exports",
        json={"release_manifest_id": release_id, "trust_mode": "OFFLINE_VERIFIABLE"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert denied.status_code == 422, denied.text
    assert "失效" in denied.json()["error"]["message"]

    # INTERNAL_COPY 仍可排队（不冒充可信离线包）
    allowed = client.post(
        f"/api/v1/goals/{goal_done['id']}/evidence-exports",
        json={"release_manifest_id": release_id, "trust_mode": "INTERNAL_COPY"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert allowed.status_code == 202, allowed.text


def test_trust_propagation_drains_running_writers_before_unlock(api, objects):
    """第159/162批：受影响 Goal 取消 READY、RUNNING 发 TRUST_INVALIDATION Stop；确认前不解锁。"""
    import hashlib
    import json
    from datetime import UTC, datetime
    from io import BytesIO

    from control_kernel.storage.artifacts import Artifacts
    from test_effects import _publish_and_claim_execute

    store, _, _ = objects
    client, token, _auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    engine = client.app.state.engine
    activity_id = exec_lease["activity"]["id"]
    attempt_id = exec_lease["lease"]["attempt_id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    goal_id = UUID(goal["id"])

    blob = b'{"path":"src/drain.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    input_art = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity="test:drain",
    )
    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "trust-drain",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "read_file",
            "input_artifact_id": str(input_art.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    effect = prepared.json()["data"]
    dispatched = client.post(
        f"/internal/v1/effects/{effect['id']}/dispatch",
        json={
            "lease": exec_lease["lease"],
            "effect_state_revision": effect["state_revision"],
        },
        headers=worker_auth,
    )
    assert dispatched.status_code == 200, dispatched.text
    receipt = client.post(
        f"/internal/v1/effects/{effect['id']}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "effect_id": effect["id"],
            "producer_activity_id": activity_id,
            "producer_attempt_id": attempt_id,
            "fencing_epoch": exec_lease["lease"]["fencing_epoch"],
            "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "exit_code": 0,
            "timed_out": False,
            "result_artifact_ids": [str(input_art.id)],
            "observed_outcome": "SUCCEEDED",
        },
        headers=worker_auth,
    )
    assert receipt.status_code == 201, receipt.text

    # 另植一条 READY PLAN，advance 时应取消（非假取消 RUNNING）
    ready_plan_id = uuid4()
    with engine.begin() as db:
        evidence_id = db.execute(
            text(
                """SELECT id FROM evidence_envelopes
                WHERE project_id=:p AND goal_id=:g
                ORDER BY created_at DESC LIMIT 1"""
            ),
            {"p": project_id, "g": goal_id},
        ).scalar_one()
        rev = db.execute(
            text(
                """SELECT trust_revision FROM project_trust_states WHERE project_id=:p"""
            ),
            {"p": project_id},
        ).scalar_one()
        binding = db.execute(
            text("SELECT binding FROM activities WHERE id=:id"),
            {"id": activity_id},
        ).scalar_one()
        db.execute(
            text(
                """INSERT INTO activities(
                  id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                  binding,verification_assignments,status,state_revision,depends_on_activity_ids,
                  retry_count,resources)
                VALUES(
                  :id,:project,:goal,NULL,:budget,'PLAN','GOAL_PLAN',:goal,
                  CAST(:binding AS jsonb),'[]'::jsonb,'READY',1,'{}',0,
                  CAST(:resources AS jsonb))"""
            ),
            {
                "id": ready_plan_id,
                "project": project_id,
                "goal": goal_id,
                "budget": goal_id,
                "binding": json.dumps(binding)
                if not isinstance(binding, str)
                else binding,
                "resources": json.dumps(
                    {
                        "cpu_millicores": 100,
                        "memory_bytes": 1048576,
                        "disk_bytes": 1048576,
                        "model_slots": 0,
                        "browser_slots": 0,
                    }
                ),
            },
        )

    ops = str(uuid4())
    with engine.begin() as db:
        db.execute(
            text(
                """INSERT INTO project_memberships(project_id, subject)
                VALUES(:p,:s) ON CONFLICT DO NOTHING"""
            ),
            {"p": project_id, "s": ops},
        )
    ops_auth = {"Authorization": "Bearer " + token(ops, ["ops"])}

    created = client.post(
        "/internal/v1/trust/invalidations",
        json={
            "project_id": str(project_id),
            "expected_trust_revision": str(rev),
            "evidence_id": str(evidence_id),
            "reason_code": "SOURCE_REVOKED",
            "proof_artifact_ids": [],
        },
        headers=ops_auth,
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["data"]["id"]
    advanced = client.post(
        f"/internal/v1/trust/jobs/{job_id}/advance",
        headers=ops_auth,
    )
    assert advanced.status_code == 200, advanced.text
    assert advanced.json()["data"]["status"] == "COMPLETE"
    assert advanced.json()["data"]["cursor"] == "scan:goals+skills+releases+drain:done"

    with engine.connect() as db:
        ready_status = db.execute(
            text("SELECT status FROM activities WHERE id=:id"),
            {"id": ready_plan_id},
        ).scalar_one()
        exec_status = db.execute(
            text("SELECT status FROM activities WHERE id=:id"),
            {"id": activity_id},
        ).scalar_one()
        stop = (
            db.execute(
                text(
                    """SELECT id, status, reason FROM stops
                    WHERE attempt_id=:a AND reason='TRUST_INVALIDATION'"""
                ),
                {"a": attempt_id},
            )
            .mappings()
            .one()
        )
        trust = db.execute(
            text("SELECT status FROM project_trust_states WHERE project_id=:p"),
            {"p": project_id},
        ).scalar_one()
    assert ready_status == "CANCELLED"
    assert exec_status == "RUNNING"  # 不假取消
    assert stop["status"] == "REQUESTED"
    assert trust == "BLOCKED"  # Stop 未确认前不解锁

    # 持有者确认 Stop → 再 advance 才 OPEN
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

    with engine.connect() as db:
        exec_after = db.execute(
            text("SELECT status FROM activities WHERE id=:id"),
            {"id": activity_id},
        ).scalar_one()
    assert exec_after == "CANCELLED"  # TRUST_INVALIDATION EXITED 泊入，非假取消于确认前

    unlocked = client.post(
        f"/internal/v1/trust/jobs/{job_id}/advance",
        headers=ops_auth,
    )
    assert unlocked.status_code == 200, unlocked.text
    with engine.connect() as db:
        trust2 = db.execute(
            text("SELECT status, propagation_job_id FROM project_trust_states WHERE project_id=:p"),
            {"p": project_id},
        ).mappings().one()
        goal_status = db.execute(
            text("SELECT status FROM goals WHERE id=:id"),
            {"id": goal_id},
        ).scalar_one()
    assert trust2["status"] == "OPEN"
    assert trust2["propagation_job_id"] is None
    assert goal_status != "DONE"


def test_effect_dispatch_rejected_when_trust_blocked(api, objects):
    """第163批：doc/05 §3.11 — Trust BLOCKED 拒工具 dispatch（已 PREPARED 亦不得发出）。"""
    import hashlib
    from io import BytesIO

    from control_kernel.storage.artifacts import Artifacts
    from test_effects import _publish_and_claim_execute

    store, _, _ = objects
    client, _token, _auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    engine = client.app.state.engine
    activity_id = UUID(exec_lease["activity"]["id"])
    project_id = UUID(goal["project_id"])

    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "trust-block-dispatch",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    blob = b'{"path":"src/trust_block.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    input_art = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity="test:trust-dispatch",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "read_file",
            "input_artifact_id": str(input_art.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    effect = prepared.json()["data"]
    assert effect["status"] == "PREPARED"

    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE project_trust_states
                SET status='BLOCKED', trust_revision=trust_revision+1
                WHERE project_id=:p"""
            ),
            {"p": project_id},
        )

    denied = client.post(
        f"/internal/v1/effects/{effect['id']}/dispatch",
        json={
            "lease": exec_lease["lease"],
            "effect_state_revision": effect["state_revision"],
        },
        headers=worker_auth,
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["error"]["code"] == "INVALID_STATE"
    assert "信任" in denied.json()["error"]["message"]

    with engine.connect() as db:
        status = db.execute(
            text("SELECT status FROM effect_intents WHERE id=:id"),
            {"id": UUID(effect["id"])},
        ).scalar_one()
    assert status == "PREPARED"


def test_model_dispatch_rejected_when_trust_blocked(api, objects):
    """第163批：doc/05 §3.11 — Trust BLOCKED 拒模型 dispatch（AUTHORIZED 不得变 DISPATCHED）。"""
    import hashlib
    import json
    from unittest.mock import patch

    from control_kernel.storage.claims import binding_digest_of
    from test_claims import _register_worker, _start_goal

    client, token, _auth, goal, _plan = _start_goal(api, objects)
    engine = client.app.state.engine
    subject = str(uuid4())
    _register_worker(subject)
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}

    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    activity_id = lease["activity"]["id"]
    project_id = UUID(goal["project_id"])

    context_content = {
        "project_id": goal["project_id"],
        "goal_id": goal["id"],
        "activity_id": activity_id,
        "role": "PLANNER",
        "goal_contract_revision": None,
        "task_contract_revision": None,
        "plan_revision": None,
        "input_bindings": [],
        "excluded_refs": [],
        "session_generation": 0,
        "compaction_source_digest": None,
        "planning_feedback_ids": [],
    }
    bundle = client.post(
        f"/internal/v1/activities/{activity_id}/context-bundles",
        json={"lease": lease["lease"], "content": context_content},
        headers=worker_auth,
    )
    assert bundle.status_code == 201, bundle.text
    bound = client.post(
        f"/internal/v1/activities/{activity_id}/context",
        json={
            "lease": lease["lease"],
            "binding_digest": binding_digest_of(lease["activity"]["binding"]),
            "context_bundle_id": bundle.json()["data"]["id"],
        },
        headers=worker_auth,
    )
    assert bound.status_code == 200, bound.text
    context_digest = bound.json()["data"]["context_digest"]
    prompt = json.dumps(
        {"messages": [{"role": "user", "content": "ping"}]},
        separators=(",", ":"),
    ).encode()
    input_digest = "sha256:" + hashlib.sha256(prompt).hexdigest()

    created_inv = client.post(
        "/internal/v1/model-invocations",
        json={
            "lease": lease["lease"],
            "invocation_seq": 1,
            "context_digest": context_digest,
            "input_digest": input_digest,
            "provider_ref": "pm2:omlx-flashnext",
            "model_id": "Qwen3.8-Flash-Next-Uncensored-Mixed-omlx",
            "max_output_tokens": 32,
            "max_cost_usd": "0",
            "data_categories": [],
        },
        headers=worker_auth,
    )
    assert created_inv.status_code == 201, created_inv.text
    invocation = created_inv.json()["data"]
    assert invocation["status"] == "AUTHORIZED"

    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE project_trust_states
                SET status='BLOCKED', trust_revision=trust_revision+1
                WHERE project_id=:p"""
            ),
            {"p": project_id},
        )

    with patch("control_api.routes.probe.chat_completion") as mocked:
        denied = client.post(
            f"/internal/v1/model-invocations/{invocation['id']}/dispatch",
            json={
                "lease": lease["lease"],
                "expected_state_revision": invocation["state_revision"],
            },
            headers=worker_auth,
        )
        assert denied.status_code == 409, denied.text
        assert denied.json()["error"]["code"] == "INVALID_STATE"
        assert "信任" in denied.json()["error"]["message"]
        mocked.assert_not_called()

    with engine.connect() as db:
        status = db.execute(
            text("SELECT status FROM model_invocations WHERE id=:id"),
            {"id": UUID(invocation["id"])},
        ).scalar_one()
    assert status == "AUTHORIZED"


def test_effect_prepare_rejected_when_trust_blocked(api, objects):
    """第164批：BLOCKED 拒新 effect prepare（不得落 PREPARED）。"""
    import hashlib
    from io import BytesIO

    from control_kernel.storage.artifacts import Artifacts
    from test_effects import _publish_and_claim_execute

    store, _, _ = objects
    client, _token, _auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    engine = client.app.state.engine
    activity_id = UUID(exec_lease["activity"]["id"])
    project_id = UUID(goal["project_id"])

    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "trust-block-prepare",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    blob = b'{"path":"src/trust_prepare.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    input_art = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity="test:trust-prepare",
    )

    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE project_trust_states
                SET status='BLOCKED', trust_revision=trust_revision+1
                WHERE project_id=:p"""
            ),
            {"p": project_id},
        )

    denied = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "read_file",
            "input_artifact_id": str(input_art.id),
        },
        headers=worker_auth,
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["error"]["code"] == "INVALID_STATE"
    assert "信任" in denied.json()["error"]["message"]

    with engine.connect() as db:
        n = db.execute(
            text(
                """SELECT count(*) FROM effect_intents
                WHERE activity_id=:a"""
            ),
            {"a": activity_id},
        ).scalar_one()
    assert n == 0


def test_model_invocation_create_rejected_when_trust_blocked(api, objects):
    """第164批：BLOCKED 拒新模型调用登记（不得落 AUTHORIZED）。"""
    import hashlib
    import json

    from control_kernel.storage.claims import binding_digest_of
    from test_claims import _register_worker, _start_goal

    client, token, _auth, goal, _plan = _start_goal(api, objects)
    engine = client.app.state.engine
    subject = str(uuid4())
    _register_worker(subject)
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}

    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    activity_id = lease["activity"]["id"]
    project_id = UUID(goal["project_id"])

    context_content = {
        "project_id": goal["project_id"],
        "goal_id": goal["id"],
        "activity_id": activity_id,
        "role": "PLANNER",
        "goal_contract_revision": None,
        "task_contract_revision": None,
        "plan_revision": None,
        "input_bindings": [],
        "excluded_refs": [],
        "session_generation": 0,
        "compaction_source_digest": None,
        "planning_feedback_ids": [],
    }
    bundle = client.post(
        f"/internal/v1/activities/{activity_id}/context-bundles",
        json={"lease": lease["lease"], "content": context_content},
        headers=worker_auth,
    )
    assert bundle.status_code == 201, bundle.text
    bound = client.post(
        f"/internal/v1/activities/{activity_id}/context",
        json={
            "lease": lease["lease"],
            "binding_digest": binding_digest_of(lease["activity"]["binding"]),
            "context_bundle_id": bundle.json()["data"]["id"],
        },
        headers=worker_auth,
    )
    assert bound.status_code == 200, bound.text
    context_digest = bound.json()["data"]["context_digest"]
    prompt = json.dumps(
        {"messages": [{"role": "user", "content": "ping"}]},
        separators=(",", ":"),
    ).encode()
    input_digest = "sha256:" + hashlib.sha256(prompt).hexdigest()

    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE project_trust_states
                SET status='BLOCKED', trust_revision=trust_revision+1
                WHERE project_id=:p"""
            ),
            {"p": project_id},
        )

    denied = client.post(
        "/internal/v1/model-invocations",
        json={
            "lease": lease["lease"],
            "invocation_seq": 1,
            "context_digest": context_digest,
            "input_digest": input_digest,
            "provider_ref": "pm2:omlx-flashnext",
            "model_id": "Qwen3.8-Flash-Next-Uncensored-Mixed-omlx",
            "max_output_tokens": 32,
            "max_cost_usd": "0",
            "data_categories": [],
        },
        headers=worker_auth,
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["error"]["code"] == "INVALID_STATE"
    assert "信任" in denied.json()["error"]["message"]

    with engine.connect() as db:
        n = db.execute(
            text(
                """SELECT count(*) FROM model_invocations
                WHERE activity_id=:a"""
            ),
            {"a": UUID(activity_id)},
        ).scalar_one()
    assert n == 0


def test_create_step_rejected_when_trust_blocked(api, objects):
    """第165批：BLOCKED 拒新工程步骤登记。"""
    from test_effects import _publish_and_claim_execute

    client, _token, _auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    engine = client.app.state.engine
    activity_id = UUID(exec_lease["activity"]["id"])
    project_id = UUID(goal["project_id"])

    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE project_trust_states
                SET status='BLOCKED', trust_revision=trust_revision+1
                WHERE project_id=:p"""
            ),
            {"p": project_id},
        )

    denied = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "trust-block-step",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["error"]["code"] == "INVALID_STATE"
    assert "信任" in denied.json()["error"]["message"]

    with engine.connect() as db:
        n = db.execute(
            text("SELECT count(*) FROM activity_steps WHERE activity_id=:a"),
            {"a": activity_id},
        ).scalar_one()
    assert n == 0


def test_seal_candidate_rejected_when_trust_blocked(api, objects):
    """第166批：BLOCKED 拒新候选封存。"""
    import hashlib
    import json
    from io import BytesIO

    from control_kernel.storage.artifacts import Artifacts
    from test_effects import _publish_and_claim_execute

    store, _, _ = objects
    client, _token, _auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    engine = client.app.state.engine
    project_id = UUID(goal["project_id"])

    file_bytes = b"print('blocked')\n"
    file_digest = "sha256:" + hashlib.sha256(file_bytes).hexdigest()
    Artifacts(engine, store).ingest_raw(
        project_id,
        file_digest,
        BytesIO(file_bytes),
        mime="text/x-python",
        producer_identity="test:candidate-file",
    )
    snapshot = {
        "files": [{"path": "src/blocked.py", "digest": file_digest, "mode": "100644"}],
        "git_commit": None,
        "dependency_lock_digests": [],
        "submodules": [],
        "lfs_objects": [],
        "image_digests": [],
    }
    snap_raw = json.dumps(snapshot, separators=(",", ":")).encode()
    snap_digest = "sha256:" + hashlib.sha256(snap_raw).hexdigest()
    snap_art = Artifacts(engine, store).ingest_raw(
        project_id,
        snap_digest,
        BytesIO(snap_raw),
        mime="application/json",
        producer_identity="test:trust-seal",
    )
    profile_id = goal["contract"]["success_criteria"][0]["verification_profile_id"]

    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE project_trust_states
                SET status='BLOCKED', trust_revision=trust_revision+1
                WHERE project_id=:p"""
            ),
            {"p": project_id},
        )

    denied = client.post(
        "/internal/v1/candidates/seal",
        json={
            "lease": exec_lease["lease"],
            "workspace_snapshot_artifact_id": str(snap_art.id),
            "verification_profile_ids": [profile_id],
        },
        headers=worker_auth,
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["error"]["code"] == "INVALID_STATE"
    assert "信任" in denied.json()["error"]["message"]

    with engine.connect() as db:
        n = db.execute(
            text(
                """SELECT count(*) FROM candidate_manifests
                WHERE project_id=:p AND activity_id=:a"""
            ),
            {"p": project_id, "a": UUID(exec_lease["activity"]["id"])},
        ).scalar_one()
    assert n == 0
