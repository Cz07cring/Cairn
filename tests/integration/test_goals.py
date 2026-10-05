"""Goal DRAFT 与 start→PLANNING；PLAN Activity 无工具、无 claim。"""

import hashlib
import json
import os
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from control_kernel.storage.artifacts import Artifacts
from evidence_ledger.content import encode
from sqlalchemy import create_engine, text


def _seed_verifier(project_id: str, ref: str = "approved.goal"):
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
            VALUES(:project,:ref,:digest,CAST(:content AS jsonb),:approval)
            ON CONFLICT DO NOTHING"""),
            {
                "project": project_id,
                "ref": ref,
                "digest": digest,
                "content": json.dumps(definition),
                "approval": "sha256:" + "b" * 64,
            },
        )
    engine.dispose()
    return digest


def _inject_skill_pass(version_id, project_id, subject_digest, profile_id):
    record_id = uuid4()
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
                "reason": "fixture",
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
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
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


def _ready_project(api, objects):
    """装配 GoalCreate 所需的全部不可变配置。"""
    client, token = api
    subject = str(uuid4())
    auth = {
        "Authorization": "Bearer "
        + token(subject, ["admin", "operator", "viewer"])
    }
    project = client.post(
        "/api/v1/projects",
        json={"name": "目标", "repository_ref": "fixture"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    ).json()["data"]["id"]
    verifier_digest = _seed_verifier(project)
    task_profile = client.post(
        "/api/v1/verification-profiles",
        json={
            "project_id": project,
            "name": "task-c1",
            "target_scope": "TASK",
            "verifier_ref": "approved.goal",
            "verifier_digest": verifier_digest,
            "required_layers": ["MECHANICAL"],
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
    assert task_profile.status_code == 201, task_profile.text
    criterion_profile_id = task_profile.json()["data"]["id"]
    skill_profile = client.post(
        "/api/v1/verification-profiles",
        json={
            "project_id": project,
            "name": "skill-c1",
            "target_scope": "SKILL",
            "verifier_ref": "approved.goal",
            "verifier_digest": verifier_digest,
            "required_layers": ["MECHANICAL"],
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
    ).json()["data"]
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
    ).json()["data"]
    model = client.post(
        "/api/v1/model-profiles",
        json={
            "project_id": project,
            "name": "local-qwen",
            "local_provider_ref": "pm2:omlx-flashnext",
            "model_id": (
                os.environ.get("RING_LOCAL_QWEN_MODEL")
                if os.environ.get("RING_TEST_ALLOW_CLOUD") == "1"
                else "Qwen3.8-Flash-Next-Uncensored-Mixed-omlx"
            )
            or "Qwen3.8-Flash-Next-Uncensored-Mixed-omlx",
            "context_window": 8192,
            "output_reserve": 1024,
            "inference_slots": 1,
            # 普通集成夹具固定 DENY，避免宿主 chat.env 污染合同并误外呼
            "cloud_provider_refs": (
                [os.environ.get("RING_CHAT_CLOUD_PROVIDER_REF", "deepseek:api")]
                if os.environ.get("RING_TEST_ALLOW_CLOUD") == "1"
                else []
            ),
            "cloud_mode": (
                "PREAUTHORIZED"
                if os.environ.get("RING_TEST_ALLOW_CLOUD") == "1"
                else "DENY"
            ),
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    ).json()["data"]
    store, _, _ = objects
    client.app.state.objects = store
    artifact = Artifacts(client.app.state.engine, store).ingest_raw(
        UUID(project),
        "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        BytesIO(b"abc"),
        mime="text/markdown",
        producer_identity="fixture:collector",
    )
    skill = client.post(
        "/api/v1/skills",
        json={
            "project_id": project,
            "name": "notes",
            "source_ref": "skills/notes",
            "content_artifact_id": str(artifact.id),
            "capabilities": ["planning"],
            "role_scopes": ["PLAN"],
            "required_tools": [],
            "verification_profile_id": skill_profile["id"],
            "acceptance": [
                {
                    "id": "S1",
                    "description": "ok",
                    "required": True,
                    "verification_profile_id": skill_profile["id"],
                }
            ],
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    ).json()["data"]
    audit = _inject_skill_pass(
        skill["id"], project, skill["content_digest"], skill_profile["id"]
    )
    activated = client.post(
        f"/api/v1/skills/{skill['id']}/activate",
        json={"audit_id": audit, "reason": "fixture"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert activated.status_code == 200, activated.text
    skill_set = client.post(
        "/api/v1/skill-sets",
        json={
            "project_id": project,
            "name": "default",
            "skill_version_ids": [skill["id"]],
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    ).json()["data"]
    goal_body = {
        "project_id": project,
        "objective": "修复示例并验证",
        "success_criteria": [
            {
                "id": "C1",
                "description": "机械验收通过",
                "required": True,
                "verification_profile_id": criterion_profile_id,
            }
        ],
        "constraints": ["仅修改隔离工作区"],
        "budget": {
            "wall_clock_seconds": 3600,
            "max_tokens": 100000,
            "max_cost_usd": "0",
            "max_tool_calls": 100,
            "max_network_calls": 10,
            "max_disk_bytes": 1048576,
            "max_gpu_seconds": None,
        },
        "retry_policy": {
            "max_execution_rounds": 4,
            "max_audit_attempts_per_candidate": 3,
            "max_activity_retries": 3,
            "max_plan_revisions": 10,
        },
        "policy_id": policy["id"],
        "model_profile_id": model["id"],
        "skill_set_id": skill_set["id"],
        "base_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    }
    return client, token, auth, project, goal_body


def test_goal_create_requires_configs_and_stays_draft(api, objects):
    client, token, auth, project, goal_body = _ready_project(api, objects)
    missing = {**goal_body, "policy_id": str(uuid4())}
    assert (
        client.post(
            "/api/v1/goals",
            json=missing,
            headers={**auth, "Idempotency-Key": str(uuid4())},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/goals",
            json=goal_body,
            headers={
                "Authorization": "Bearer " + token(str(uuid4()), ["viewer"]),
                "Idempotency-Key": str(uuid4()),
            },
        ).status_code
        == 403
    )
    headers = {**auth, "Idempotency-Key": str(uuid4())}
    created = client.post("/api/v1/goals", json=goal_body, headers=headers)
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["status"] == "DRAFT"
    assert data["state_revision"] == 1
    assert data["contract_revision"] == 1
    assert data["plan_revision"] is None
    assert data["criterion_summary"] == {"verified": 0, "total": 1}
    assert data["write_epoch"] == "1"
    assert data["barrier"] is None
    assert data["contract"]["skill_set_id"] == goal_body["skill_set_id"]
    assert client.post("/api/v1/goals", json=goal_body, headers=headers).json()["data"] == data
    got = client.get(f"/api/v1/goals/{data['id']}", headers=auth)
    assert got.status_code == 200
    assert got.json()["data"] == data
    listed = client.get(
        "/api/v1/goals", params={"project_id": project, "status": "DRAFT"}, headers=auth
    )
    assert listed.json()["data"][0]["id"] == data["id"]
    outsider = {"Authorization": "Bearer " + token(str(uuid4()), ["operator", "viewer"])}
    assert client.get(f"/api/v1/goals/{data['id']}", headers=outsider).status_code == 404


def test_goal_start_creates_plan_activity(api, objects):
    client, token, auth, _project, goal_body = _ready_project(api, objects)
    created = client.post(
        "/api/v1/goals",
        json=goal_body,
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    goal = created.json()["data"]
    assert (
        client.post(
            f"/api/v1/goals/{goal['id']}/start",
            json={"expected_state_revision": 1, "reason": "start"},
            headers={
                "Authorization": "Bearer " + token(str(uuid4()), ["viewer"]),
                "Idempotency-Key": str(uuid4()),
            },
        ).status_code
        == 403
    )
    conflict = client.post(
        f"/api/v1/goals/{goal['id']}/start",
        json={"expected_state_revision": 99, "reason": "stale"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "STATE_REVISION_CONFLICT"
    key = str(uuid4())
    started = client.post(
        f"/api/v1/goals/{goal['id']}/start",
        json={"expected_state_revision": 1, "reason": "begin planning"},
        headers={**auth, "Idempotency-Key": key},
    )
    assert started.status_code == 202, started.text
    command = started.json()["data"]
    assert command["kind"] == "START"
    assert command["status"] == "SUCCEEDED"
    assert command["goal_id"] == goal["id"]
    assert command["result"] == {"goal_id": goal["id"], "final_status": "PLANNING"}
    assert client.post(
        f"/api/v1/goals/{goal['id']}/start",
        json={"expected_state_revision": 1, "reason": "begin planning"},
        headers={**auth, "Idempotency-Key": key},
    ).json()["data"] == command
    got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert got["status"] == "PLANNING"
    assert got["previous_status"] == "DRAFT"
    assert got["state_revision"] == 2
    again = client.post(
        f"/api/v1/goals/{goal['id']}/start",
        json={"expected_state_revision": 2, "reason": "again"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "INVALID_STATE"
    activities = client.get(
        f"/api/v1/goals/{goal['id']}/activities",
        params={"kind": "PLAN"},
        headers=auth,
    )
    assert activities.status_code == 200, activities.text
    rows = activities.json()["data"]
    assert len(rows) == 1
    plan = rows[0]
    assert plan["kind"] == "PLAN"
    assert plan["status"] == "READY"
    assert plan["target"] == {"type": "GOAL_PLAN", "id": goal["id"]}
    assert plan["task_id"] is None
    assert plan["binding"]["goal_contract_digest"] == got["contract_digest"]
    assert plan["resources"]["browser_slots"] == 0
    assert plan["resources"]["model_slots"] == 1
    detail = client.get(f"/api/v1/activities/{plan['id']}", headers=auth)
    assert detail.status_code == 200
    assert detail.json()["data"]["id"] == plan["id"]
    cmd = client.get(f"/api/v1/commands/{command['id']}", headers=auth)
    assert cmd.status_code == 200
    assert cmd.json()["data"] == command
    outsider = {"Authorization": "Bearer " + token(str(uuid4()), ["operator", "viewer"])}
    assert client.get(f"/api/v1/activities/{plan['id']}", headers=outsider).status_code == 404
