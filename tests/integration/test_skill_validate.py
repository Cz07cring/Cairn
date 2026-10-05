"""VALIDATE_SKILL：validate → claim → outcome(PASS) → activate。"""

from uuid import UUID, uuid4

from test_claims import _register_worker
from test_skills import _setup
from verification_run_helpers import (
    post_verification_run,
    profile_verifier_digest,
    receipt_artifact,
)


def test_validate_skill_pass_then_activate(api, objects):
    store, _, _ = objects
    client, auth, project, skill_body, _artifact = _setup(api, objects)
    client.app.state.objects = store
    # validate 绑定需要项目策略
    policy = client.post(
        "/api/v1/policies",
        json={
            "project_id": project,
            "name": "default",
            "allowed_tools": ["read_file"],
            "allowed_paths": ["src/**"],
            "protected_paths": [],
            "network_allowlist": [],
            "external_actions": [],
            "secret_scope_refs": [],
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert policy.status_code == 201, policy.text

    created = client.post(
        "/api/v1/skills",
        json=skill_body,
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    version = created.json()["data"]
    assert version["status"] == "CANDIDATE"

    started = client.post(
        f"/api/v1/skills/{version['id']}/validate",
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert started.status_code == 202, started.text
    command = started.json()["data"]
    assert command["kind"] == "VALIDATE_SKILL"
    assert command["status"] == "RUNNING"
    activity_id = command["result"]["activity_id"]
    assert command["result"]["version_id"] == version["id"]

    got = client.get("/api/v1/skills", params={"project_id": project}, headers=auth)
    assert any(
        row["id"] == version["id"] and row["status"] == "VALIDATING"
        for row in got.json()["data"]
    )

    subject = str(uuid4())
    _register_worker(subject, kinds=("VALIDATE_SKILL",))
    _, token = api
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["VALIDATE_SKILL"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert lease["activity"]["id"] == activity_id
    assignment = lease["activity"]["verification_assignments"][0]

    receipt_id = receipt_artifact(client.app.state.engine, store, UUID(project))
    verifier_digest = profile_verifier_digest(
        client, auth, project, assignment["verification_profile_id"]
    )
    run_id = post_verification_run(
        client,
        worker_auth,
        lease,
        subject_id=version["id"],
        subject_digest=version["content_digest"],
        verifier_digest=verifier_digest,
        receipt_id=receipt_id,
        subject_type="SKILL_VERSION",
        criterion_id="S1",
    )

    evidence_id = str(uuid4())
    done = client.post(
        f"/internal/v1/activities/{activity_id}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {
                "version_id": version["id"],
                "validation": {
                    "subject_digest": version["content_digest"],
                    "verification_profile_id": assignment["verification_profile_id"],
                    "audit_round": assignment["audit_round"],
                    "verifier_run_ids": [run_id],
                    "verdict": "PASS",
                    "criterion_results": [
                        {
                            "criterion_id": "S1",
                            "verdict": "PASS",
                            "evidence_ids": [evidence_id],
                            "reason": "机械检查通过",
                        }
                    ],
                    "evidence_ids": [evidence_id],
                    "reason": "验证通过",
                },
            },
        },
        headers=worker_auth,
    )
    assert done.status_code == 200, done.text
    assert done.json()["data"]["status"] == "SUCCEEDED"

    listed = client.get(
        f"/api/v1/skills/{version['id']}/validations",
        headers=auth,
    )
    assert listed.status_code == 200, listed.text
    records = listed.json()["data"]
    assert len(records) == 1
    audit_id = records[0]["id"]
    assert records[0]["verdict"] == "PASS"

    skill = client.get(
        "/api/v1/skills",
        params={"project_id": project},
        headers=auth,
    ).json()["data"]
    matched = next(row for row in skill if row["id"] == version["id"])
    assert matched["status"] == "CANDIDATE"

    activated = client.post(
        f"/api/v1/skills/{version['id']}/activate",
        json={"audit_id": audit_id, "reason": "验收通过后激活"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["data"]["status"] == "ACTIVE"
    assert activated.json()["data"]["audit_id"] == audit_id
