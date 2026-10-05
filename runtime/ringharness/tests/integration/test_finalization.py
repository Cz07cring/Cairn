"""最终屏障：AUDIT PASS → FINALIZE → Goal DONE + ReleaseManifest。

诚实边界：无 INTEGRATE；Goal 须含 GOAL/GLOBAL success_criterion 才开启屏障。
"""

import hashlib
import json
import os
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from control_kernel.storage.artifacts import Artifacts
from evidence_ledger.content import encode
from sqlalchemy import create_engine, text
from test_claims import _drain_ready_plans, _register_worker
from test_goals import _ready_project
from test_plans import _plan_body
from verification_run_helpers import (
    post_verification_run,
    profile_verifier_digest,
    receipt_artifact,
)


def _ready_project_with_global(api, objects):
    """在标准夹具上追加 GLOBAL Goal 标准与验证配置。"""
    client, token, auth, project, goal_body = _ready_project(api, objects)
    _drain_ready_plans(client, token)
    example = json.loads(
        (Path(__file__).parents[2] / "doc/contracts/verifier-fixtures-v2.json").read_text()
    )["examples"][3]
    definition = {**example["definition"], "project_id": project}
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
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text("""INSERT INTO verifier_definitions(project_id,ref,content_digest,content,approval_record_digest)
            VALUES(:project,:ref,:digest,CAST(:content AS jsonb),:approval)
            ON CONFLICT DO NOTHING"""),
            {
                "project": project,
                "ref": "approved.global",
                "digest": digest,
                "content": json.dumps(definition),
                "approval": "sha256:" + "b" * 64,
            },
        )
    engine.dispose()

    goal_profile = client.post(
        "/api/v1/verification-profiles",
        json={
            "project_id": project,
            "name": "goal-global",
            "target_scope": "GOAL",
            "verifier_ref": "approved.global",
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
    assert goal_profile.status_code == 201, goal_profile.text
    global_profile_id = goal_profile.json()["data"]["id"]
    mechanical_id = goal_body["success_criteria"][0]["verification_profile_id"]
    goal_body = {
        **goal_body,
        "success_criteria": [
            {
                "id": "C1",
                "description": "全局验收通过",
                "required": True,
                "verification_profile_id": global_profile_id,
            }
        ],
    }
    return client, token, auth, project, goal_body, mechanical_id, global_profile_id


def _claim_finalize_lease(api, objects):
    """AUDIT PASS → INTEGRATE → claim FINALIZE；尚未提交 VerificationRun / outcome。

    供快乐路径与 TM10 STRICT 拒绝共用，避免重复搭脚手架。
    """
    client, token, auth, _project, goal_body, mechanical_id, _global_profile_id = (
        _ready_project_with_global(api, objects)
    )
    _drain_ready_plans(client, token)
    created = client.post(
        "/api/v1/goals",
        json=goal_body,
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    goal = created.json()["data"]
    started = client.post(
        f"/api/v1/goals/{goal['id']}/start",
        json={"expected_state_revision": 1, "reason": "plan"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert started.status_code == 202, started.text
    plan_activity = client.get(
        f"/api/v1/goals/{goal['id']}/activities",
        params={"kind": "PLAN"},
        headers=auth,
    ).json()["data"][0]

    subject = str(uuid4())
    _register_worker(
        subject, kinds=("PLAN", "EXECUTE", "AUDIT", "INTEGRATE", "FINALIZE", "EXPORT_EVIDENCE")
    )
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    # 再排空一次，避免与 start 竞态留下的其他 READY PLAN。
    _drain_ready_plans(client, token)
    # 恢复本 Goal 的 PLAN（drain 会误伤刚创建的 READY）。
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text(
                "UPDATE activities SET status='READY', updated_at=clock_timestamp() WHERE id=:id"
            ),
            {"id": plan_activity["id"]},
        )
    engine.dispose()

    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert lease["activity"]["id"] == plan_activity["id"]
    plan = _plan_body(goal, mechanical_id)
    # coverage 必须与 acceptance 同为 Task MECHANICAL profile，不能用 Goal GLOBAL。
    plan["coverage"][0]["verification_profile_id"] = mechanical_id
    done = client.post(
        f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {"plan": plan},
        },
        headers=worker_auth,
    )
    assert done.status_code == 200, done.text

    store, _, _ = objects
    client.app.state.objects = store
    exec_claim = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert exec_claim.status_code == 200, exec_claim.text
    exec_lease = exec_claim.json()["data"]
    activity_id = exec_lease["activity"]["id"]
    assert exec_lease["activity"]["goal_id"] == goal["id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    engine = client.app.state.engine

    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "读取入口",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    blob = b'{"path":"src/main.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    input_art = Artifacts(engine, store).ingest_raw(
        project_id, digest, BytesIO(blob), mime="application/json", producer_identity="t"
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
    assert (
        client.post(
            f"/internal/v1/effects/{effect['id']}/dispatch",
            json={
                "lease": exec_lease["lease"],
                "effect_state_revision": effect["state_revision"],
            },
            headers=worker_auth,
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/internal/v1/effects/{effect['id']}/receipts",
            json={
                "receipt_id": str(uuid4()),
                "effect_id": effect["id"],
                "producer_activity_id": activity_id,
                "producer_attempt_id": exec_lease["lease"]["attempt_id"],
                "fencing_epoch": exec_lease["lease"]["fencing_epoch"],
                "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "exit_code": 0,
                "timed_out": False,
                "result_artifact_ids": [str(input_art.id)],
                "observed_outcome": "SUCCEEDED",
            },
            headers=worker_auth,
        ).status_code
        == 201
    )
    file_bytes = b"print('ok')\n"
    file_digest = "sha256:" + hashlib.sha256(file_bytes).hexdigest()
    Artifacts(engine, store).ingest_raw(
        project_id,
        file_digest,
        BytesIO(file_bytes),
        mime="text/x-python",
        producer_identity="test:candidate-file",
    )
    snapshot = {
        "files": [{"path": "src/main.py", "digest": file_digest, "mode": "100644"}],
        "git_commit": None,
        "dependency_lock_digests": [],
        "submodules": [],
        "lfs_objects": [],
        "image_digests": [],
    }
    snap_raw = json.dumps(snapshot, separators=(",", ":")).encode()
    snap_art = Artifacts(engine, store).ingest_raw(
        project_id,
        "sha256:" + hashlib.sha256(snap_raw).hexdigest(),
        BytesIO(snap_raw),
        mime="application/json",
        producer_identity="snap",
    )
    sealed = client.post(
        "/internal/v1/candidates/seal",
        json={
            "lease": exec_lease["lease"],
            "workspace_snapshot_artifact_id": str(snap_art.id),
            "verification_profile_ids": [mechanical_id],
        },
        headers=worker_auth,
    )
    assert sealed.status_code == 201, sealed.text
    candidate = sealed.json()["data"]
    assert (
        client.post(
            f"/internal/v1/activities/{activity_id}/outcomes",
            json={
                "lease": exec_lease["lease"],
                "expected_state_revision": exec_lease["activity"]["state_revision"],
                "outcome": {
                    "candidate_manifest_id": candidate["id"],
                    "evidence_ids": [str(input_art.id)],
                },
            },
            headers=worker_auth,
        ).status_code
        == 200
    )

    audit_claim = client.post(
        "/internal/v1/claims",
        json={"kinds": ["AUDIT"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert audit_claim.status_code == 200, audit_claim.text
    audit_lease = audit_claim.json()["data"]
    assignment = audit_lease["activity"]["verification_assignments"][0]
    binding = audit_lease["activity"]["binding"]
    project_id = UUID(audit_lease["activity"]["project_id"])
    receipt_id = receipt_artifact(client.app.state.engine, store, project_id)
    verifier_digest = profile_verifier_digest(
        client, auth, str(project_id), assignment["verification_profile_id"]
    )
    run_id = post_verification_run(
        client,
        worker_auth,
        audit_lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=verifier_digest,
        receipt_id=receipt_id,
    )
    audit_resp = client.post(
        f"/internal/v1/activities/{audit_lease['activity']['id']}/outcomes",
        json={
            "lease": audit_lease["lease"],
            "expected_state_revision": audit_lease["activity"]["state_revision"],
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
                    "evidence_ids": [],
                    "reason": "ok",
                    "criterion_results": [
                        {
                            "criterion_id": "A1",
                            "verdict": "PASS",
                            "evidence_ids": [],
                            "reason": "ok",
                        }
                    ],
                },
            },
        },
        headers=worker_auth,
    )
    assert audit_resp.status_code == 200, audit_resp.text

    goal_after_audit = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_after_audit["status"] == "RUNNING"
    assert goal_after_audit["barrier"] is None

    int_claim = client.post(
        "/internal/v1/claims",
        json={"kinds": ["INTEGRATE"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert int_claim.status_code == 200, int_claim.text
    integ = int_claim.json()["data"]
    assert integ["activity"]["kind"] == "INTEGRATE"
    integrated = client.post(
        f"/internal/v1/activities/{integ['activity']['id']}/outcomes",
        json={
            "lease": integ["lease"],
            "expected_state_revision": integ["activity"]["state_revision"],
            "outcome": {
                "candidate_manifest_id": candidate["id"],
                "integration_commit": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "evidence_ids": [],
            },
        },
        headers=worker_auth,
    )
    assert integrated.status_code == 200, integrated.text

    goal_mid = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_mid["status"] == "VERIFYING"
    assert goal_mid["barrier"] is not None
    assert goal_mid["barrier"]["status"] == "SEALED"
    assert goal_mid["integration_commit"] == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    fin_claim = client.post(
        "/internal/v1/claims",
        json={"kinds": ["FINALIZE"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert fin_claim.status_code == 200, fin_claim.text
    fin = fin_claim.json()["data"]
    assert fin["activity"]["kind"] == "FINALIZE"
    barrier_id = goal_mid["barrier"]["id"]
    return client, token, auth, goal, candidate, worker_auth, fin, barrier_id, store


def _goal_done_with_release(api, objects):
    """跑通 finalize 快乐路径，返回 DONE Goal 与 release_manifest_id。"""
    client, token, auth, goal, candidate, worker_auth, fin, barrier_id, store = (
        _claim_finalize_lease(api, objects)
    )
    fin_assign = fin["activity"]["verification_assignments"][0]
    fin_project_id = UUID(fin["activity"]["project_id"])
    fin_receipt_id = receipt_artifact(client.app.state.engine, store, fin_project_id)
    fin_verifier_digest = profile_verifier_digest(
        client, auth, str(fin_project_id), fin_assign["verification_profile_id"]
    )
    # GLOBAL 准则 id 固定为 Goal success_criteria「C1」，与 FINALIZE assignment 对齐。
    fin_run_id = post_verification_run(
        client,
        worker_auth,
        fin,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=fin_verifier_digest,
        receipt_id=fin_receipt_id,
        criterion_id="C1",
    )

    finalized = client.post(
        f"/internal/v1/activities/{fin['activity']['id']}/outcomes",
        json={
            "lease": fin["lease"],
            "expected_state_revision": fin["activity"]["state_revision"],
            "outcome": {
                "barrier_id": barrier_id,
                "candidate_manifest_id": candidate["id"],
                "global_audits": [
                    {
                        "subject_candidate_manifest_id": candidate["id"],
                        "goal_contract_revision": goal["contract_revision"],
                        "task_contract_revision": None,
                        "verification_profile_id": fin_assign["verification_profile_id"],
                        "layer": "GLOBAL",
                        "audit_round": fin_assign["audit_round"],
                        "verifier_run_ids": [fin_run_id],
                        "verdict": "PASS",
                        "criterion_results": [
                            {
                                "criterion_id": "C1",
                                "verdict": "PASS",
                                "evidence_ids": [],
                                "reason": "全局通过",
                            }
                        ],
                        "evidence_ids": [],
                        "reason": "最终验收通过",
                    }
                ],
                "evidence_ids": [],
            },
        },
        headers=worker_auth,
    )
    assert finalized.status_code == 200, finalized.text

    goal_done = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_done["status"] == "DONE"
    assert goal_done["barrier"]["status"] == "RELEASED"
    assert goal_done["release_manifest_id"] is not None
    return client, token, auth, goal_done, goal_done["release_manifest_id"], candidate, worker_auth


def test_finalize_marks_goal_done_and_release(api, objects):
    client, _token, auth, goal_done, release_manifest_id, candidate, _worker_auth = (
        _goal_done_with_release(api, objects)
    )
    release = client.get(f"/api/v1/goals/{goal_done['id']}/release", headers=auth)
    assert release.status_code == 200, release.text
    body = release.json()["data"]
    assert body["validity"]["status"] == "VALID"
    assert body["manifest"]["id"] == release_manifest_id
    assert body["manifest"]["candidate_manifest_id"] == candidate["id"]
    assert body["manifest"]["content_digest"].startswith("sha256:")
