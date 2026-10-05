"""E2E：物化/Auditor 树上真实 pytest 红 → VerificationAssessment FAIL（不得手填绿）。"""

from __future__ import annotations

import hashlib
import json
import os
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from control_kernel.domain.tool_capability_manifest import SEAL_CANDIDATE_SCHEMA_DIGEST
from control_kernel.storage.artifacts import Artifacts
from e2e3_order_helpers import ALLOW, boot_execute_for_seal, run_pytest
from execution_broker import run_seal_candidate_effect
from sqlalchemy import text
from test_claims import _register_worker
from verification_run_helpers import (
    post_verification_run_from_pytest,
    profile_verifier_digest,
)


def test_e2e3_real_pytest_drives_fail_assessment(api, objects, tmp_path: Path):
    """buggy 候选：Auditor 真实 pytest 红 → checks_passed=false → Assessment FAIL ≠ DONE。"""
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")

    ctx = boot_execute_for_seal(api, objects, tmp_path, run_id="e2e3-pytest-fail")
    run = ctx["run"]
    client, auth = ctx["client"], ctx["auth"]
    red = run_pytest(run.auditor_worktree, "tests/", "tests_hidden/")
    assert red.returncode != 0

    step = client.post(
        f"/internal/v1/activities/{ctx['exec_lease']['activity']['id']}/steps",
        json={
            "lease": ctx["exec_lease"]["lease"],
            "predecessor_step_id": None,
            "purpose": "seal buggy for pytest-fail",
            "tool_ref": "seal_candidate",
        },
        headers=ctx["exec_auth"],
    )
    assert step.status_code == 201, step.text
    input_blob = json.dumps(
        {
            "tool_ref": "seal_candidate",
            "tool_schema_digest": SEAL_CANDIDATE_SCHEMA_DIGEST,
            "parameters": {"verification_profile_ids": [ctx["profile_id"]]},
        },
        separators=(",", ":"),
    ).encode()
    input_art = Artifacts(ctx["engine"], ctx["store"]).ingest_raw(
        ctx["project_id"],
        "sha256:" + hashlib.sha256(input_blob).hexdigest(),
        BytesIO(input_blob),
        mime="application/json",
        producer_identity="test:pytest-fail-seal",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": ctx["exec_lease"]["lease"],
            "logical_step_id": step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "seal_candidate",
            "input_artifact_id": str(input_art.id),
        },
        headers=ctx["exec_auth"],
    )
    assert prepared.status_code == 201, prepared.text
    sealed = run_seal_candidate_effect(
        client,
        worker_auth=ctx["exec_auth"],
        lease=ctx["exec_lease"]["lease"],
        effect=prepared.json()["data"],
        project_id=ctx["project_id"],
        workspace_root=run.executor_worktree,
        allowed_paths=ALLOW,
        input_bytes=input_blob,
    )
    candidate = sealed["candidate"]
    assert candidate is not None

    activity_now = client.get(
        f"/api/v1/activities/{ctx['exec_lease']['activity']['id']}",
        headers=auth,
    ).json()["data"]
    assert (
        client.post(
            f"/internal/v1/activities/{ctx['exec_lease']['activity']['id']}/outcomes",
            json={
                "lease": ctx["exec_lease"]["lease"],
                "expected_state_revision": activity_now["state_revision"],
                "outcome": {
                    "candidate_manifest_id": candidate["id"],
                    "evidence_ids": sealed["result_artifact_ids"],
                },
            },
            headers=ctx["exec_auth"],
        ).status_code
        == 200
    )

    audit_subject = str(uuid4())
    _register_worker(audit_subject, kinds=("AUDIT",))
    audit_auth = {"Authorization": "Bearer " + ctx["token"](audit_subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["AUDIT"], "capabilities": []},
        headers={**audit_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    audit_lease = claimed.json()["data"]
    assignment = audit_lease["activity"]["verification_assignments"][0]
    run_id, checks = post_verification_run_from_pytest(
        client,
        audit_auth,
        audit_lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=profile_verifier_digest(
            client, auth, str(ctx["project_id"]), assignment["verification_profile_id"]
        ),
        completed=red,
        engine=ctx["engine"],
        store=ctx["store"],
        project_id=ctx["project_id"],
        producer_identity="e2e:auditor-pytest-red",
    )
    assert checks == "false"
    with ctx["engine"].connect() as db:
        verdict = db.execute(
            text("SELECT verdict FROM verification_assessments WHERE run_id=:id"),
            {"id": run_id},
        ).scalar_one()
    assert verdict == "FAIL"
    goal = client.get(f"/api/v1/goals/{ctx['goal']['id']}", headers=auth).json()["data"]
    assert goal["status"] != "DONE"
