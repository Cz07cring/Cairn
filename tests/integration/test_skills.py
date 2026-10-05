"""Skill/SkillSet 是 GoalCreate 前置配置；本测不冒充 VALIDATE_SKILL Activity 已接通。"""

import hashlib
import json
import os
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from control_kernel.storage.artifacts import Artifacts
from evidence_ledger.content import encode
from sqlalchemy import create_engine, text


def _seed_skill_profile(project_id: str):
    """测试库显式注入已批准验证器；验证的是引用约束，不是部署加载器。"""
    example = json.loads(
        (Path(__file__).parents[2] / "doc/contracts/verifier-fixtures-v2.json").read_text()
    )["examples"][0]
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
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text("""INSERT INTO verifier_definitions(project_id,ref,content_digest,content,approval_record_digest)
            VALUES(:project,'approved.skill',:digest,CAST(:content AS jsonb),:approval)"""),
            {
                "project": project_id,
                "digest": digest,
                "content": json.dumps(definition),
                "approval": "sha256:" + "b" * 64,
            },
        )
    engine.dispose()
    return {
        "project_id": project_id,
        "name": "skill-mech",
        "target_scope": "SKILL",
        "verifier_ref": "approved.skill",
        "verifier_digest": digest,
        "required_layers": ["MECHANICAL"],
        "thresholds": [
            {"metric": "checks_passed", "operator": "EQ", "expected": "true", "unit": "boolean"}
        ],
        "required_evidence_kinds": ["trusted_verifier_result"],
        "applicability_rule_ref": "all_required",
    }


def _setup(api, objects):
    client, token = api
    subject = str(uuid4())
    auth = {"Authorization": "Bearer " + token(subject, ["admin", "viewer"])}
    project = client.post(
        "/api/v1/projects",
        json={"name": "技能", "repository_ref": "fixture"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    ).json()["data"]["id"]
    profile_body = _seed_skill_profile(project)
    profile = client.post(
        "/api/v1/verification-profiles",
        json=profile_body,
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert profile.status_code == 201, profile.text
    profile_id = profile.json()["data"]["id"]
    store, _, _ = objects
    client.app.state.objects = store
    digest = "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    artifact = Artifacts(client.app.state.engine, store).ingest_raw(
        UUID(project),
        digest,
        BytesIO(b"abc"),
        mime="text/markdown",
        producer_identity="fixture:collector",
    )
    skill_body = {
        "project_id": project,
        "name": "planner-notes",
        "source_ref": "skills/planner-notes",
        "content_artifact_id": str(artifact.id),
        "capabilities": ["planning"],
        "role_scopes": ["PLAN"],
        "required_tools": [],
        "verification_profile_id": profile_id,
        "acceptance": [
            {
                "id": "S1",
                "description": "机械检查通过",
                "required": True,
                "verification_profile_id": profile_id,
            }
        ],
    }
    return client, auth, project, skill_body, artifact


def _inject_pass_validation(version_id: str, project_id: str, subject_digest: str, profile_id: str):
    """受信 VALIDATE_SKILL outcome 边界未接通前，用 fixture 注入有效验收记录。"""
    record_id = uuid4()
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    content = {
        "project_id": project_id,
        "producer_activity_id": str(uuid4()),
        "producer_attempt_id": str(uuid4()),
        "subject_skill_version_id": version_id,
        "subject_digest": subject_digest,
        "verification_profile_id": profile_id,
        "audit_round": 1,
        "verifier_run_ids": [str(uuid4())],
        "verdict": "PASS",
        "criterion_results": [
            {
                "criterion_id": "S1",
                "verdict": "PASS",
                "evidence_ids": [str(uuid4())],
                "reason": "fixture pass",
            }
        ],
        "evidence_ids": [str(uuid4())],
        "reason": "fixture",
    }
    digest = (
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
            text("""INSERT INTO skill_validation_records
            (id,project_id,subject_skill_version_id,subject_digest,verification_profile_id,
             content_digest,content,verdict)
            VALUES(:id,:project,:version,:subject,:profile,:digest,CAST(:content AS jsonb),:verdict)"""),
            {
                "id": record_id,
                "project": project_id,
                "version": version_id,
                "subject": subject_digest,
                "profile": profile_id,
                "digest": digest,
                "content": json.dumps(content),
                "verdict": "PASS",
            },
        )
    engine.dispose()
    return str(record_id)


def test_skill_create_requires_artifact_and_skill_profile(api, objects):
    client, auth, project, skill_body, _artifact = _setup(api, objects)
    missing = {**skill_body, "content_artifact_id": str(uuid4())}
    assert (
        client.post(
            "/api/v1/skills", json=missing, headers={**auth, "Idempotency-Key": str(uuid4())}
        ).status_code
        == 422
    )
    # 复用已注入的 approved.skill，再建 TASK 配置：Skill 必须拒绝非 SKILL 作用域。
    skill_profile = client.get(
        "/api/v1/verification-profiles", params={"project_id": project}, headers=auth
    ).json()["data"][0]
    task_profile = client.post(
        "/api/v1/verification-profiles",
        json={
            "project_id": project,
            "name": "task-mech",
            "target_scope": "TASK",
            "verifier_ref": skill_profile["config"]["verifier_ref"],
            "verifier_digest": skill_profile["config"]["verifier_digest"],
            "required_layers": ["MECHANICAL"],
            "thresholds": skill_profile["config"]["thresholds"],
            "required_evidence_kinds": skill_profile["config"]["required_evidence_kinds"],
            "applicability_rule_ref": "all_required",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert task_profile.status_code == 201, task_profile.text
    task_id = task_profile.json()["data"]["id"]
    bad = {
        **skill_body,
        "verification_profile_id": task_id,
        "acceptance": [
            {
                "id": "S1",
                "description": "x",
                "required": True,
                "verification_profile_id": task_id,
            }
        ],
    }
    assert (
        client.post(
            "/api/v1/skills", json=bad, headers={**auth, "Idempotency-Key": str(uuid4())}
        ).status_code
        == 422
    )
    created = client.post(
        "/api/v1/skills", json=skill_body, headers={**auth, "Idempotency-Key": str(uuid4())}
    )
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["status"] == "CANDIDATE"
    assert data["version"] == 1
    assert data["audit_id"] is None
    listed = client.get("/api/v1/skills", params={"project_id": project}, headers=auth)
    assert listed.json()["data"][0]["id"] == data["id"]


def test_skill_create_rejects_dangerous_or_unregistered_required_tools(api, objects):
    """ToolCapabilityManifest fixture：危险/未登记 required_tools 创建失败关闭。"""
    client, auth, _project, skill_body, _artifact = _setup(api, objects)
    denied = client.post(
        "/api/v1/skills",
        json={**skill_body, "required_tools": ["http_fetch"]},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert denied.status_code == 422, denied.text
    assert denied.json()["error"]["code"].startswith("TOOL_")

    unknown = client.post(
        "/api/v1/skills",
        json={**skill_body, "required_tools": ["not_a_registered_tool"]},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert unknown.status_code == 422, unknown.text
    assert unknown.json()["error"]["code"] == "TOOL_NOT_REGISTERED"

    ok = client.post(
        "/api/v1/skills",
        json={**skill_body, "required_tools": ["read_file"]},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert ok.status_code == 201, ok.text


def test_activate_requires_matching_pass_validation_then_skill_set_binds_active_only(api, objects):
    client, auth, project, skill_body, _artifact = _setup(api, objects)
    created = client.post(
        "/api/v1/skills", json=skill_body, headers={**auth, "Idempotency-Key": str(uuid4())}
    ).json()["data"]
    assert (
        client.post(
            f"/api/v1/skills/{created['id']}/activate",
            json={"audit_id": str(uuid4()), "reason": "no record"},
            headers={**auth, "Idempotency-Key": str(uuid4())},
        ).status_code
        == 422
    )
    audit_id = _inject_pass_validation(
        created["id"],
        project,
        created["content_digest"],
        skill_body["verification_profile_id"],
    )
    activated = client.post(
        f"/api/v1/skills/{created['id']}/activate",
        json={"audit_id": audit_id, "reason": "验收通过"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["data"]["status"] == "ACTIVE"
    assert activated.json()["data"]["audit_id"] == audit_id
    validations = client.get(
        f"/api/v1/skills/{created['id']}/validations", headers=auth
    ).json()["data"]
    assert validations[0]["id"] == audit_id
    assert (
        client.post(
            "/api/v1/skill-sets",
            json={
                "project_id": project,
                "name": "default",
                "skill_version_ids": [created["id"], str(uuid4())],
            },
            headers={**auth, "Idempotency-Key": str(uuid4())},
        ).status_code
        == 422
    )
    skill_set = client.post(
        "/api/v1/skill-sets",
        json={"project_id": project, "name": "default", "skill_version_ids": [created["id"]]},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert skill_set.status_code == 201, skill_set.text
    assert skill_set.json()["data"]["config"]["skill_version_ids"] == [created["id"]]
    listed = client.get("/api/v1/skill-sets", params={"project_id": project}, headers=auth)
    assert listed.json()["data"][0]["id"] == skill_set.json()["data"]["id"]
    revoked = client.post(
        f"/api/v1/skills/{created['id']}/revoke",
        json={"reason": "紧急撤销"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert revoked.status_code == 200
    assert revoked.json()["data"]["status"] == "REVOKED"
    assert (
        client.post(
            "/api/v1/skill-sets",
            json={"project_id": project, "name": "after-revoke", "skill_version_ids": [created["id"]]},
            headers={**auth, "Idempotency-Key": str(uuid4())},
        ).status_code
        == 422
    )


def test_skill_is_project_scoped(api, objects):
    client, auth, project, skill_body, _artifact = _setup(api, objects)
    outsider = {
        "Authorization": "Bearer "
        + api[1](str(uuid4()), ["admin", "viewer"]),
        "Idempotency-Key": str(uuid4()),
    }
    assert client.post("/api/v1/skills", json=skill_body, headers=outsider).status_code == 404
    created = client.post(
        "/api/v1/skills", json=skill_body, headers={**auth, "Idempotency-Key": str(uuid4())}
    ).json()["data"]
    assert (
        client.get("/api/v1/skills", params={"project_id": project}, headers=outsider).status_code
        == 404
    )
    assert client.get(f"/api/v1/skills/{created['id']}/validations", headers=outsider).status_code == 404
