"""ContextCompiler 集成：claim → context-compile → bind；合同与 Skill 进入 input_bindings。"""

from uuid import UUID, uuid4

from control_kernel.storage.claims import binding_digest_of
from sqlalchemy import text
from test_claims import _register_worker, _start_goal


def test_context_compile_includes_contract_and_plan_skill(api, objects):
    client, token, _auth, goal, _plan = _start_goal(api, objects)
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

    compiled = client.post(
        f"/internal/v1/activities/{activity_id}/context-compile",
        json={"lease": lease["lease"], "max_input_tokens": 8192},
        headers=worker_auth,
    )
    assert compiled.status_code == 201, compiled.text
    data = compiled.json()["data"]
    content = data["content"]
    assert content["role"] == "PLANNER"
    assert content["goal_id"] == goal["id"]
    classifications = {b["classification"] for b in content["input_bindings"]}
    assert "CONTRACT" in classifications
    assert "SKILL" in classifications
    assert content["planning_feedback_ids"] == []

    bound = client.post(
        f"/internal/v1/activities/{activity_id}/context",
        json={
            "lease": lease["lease"],
            "binding_digest": binding_digest_of(lease["activity"]["binding"]),
            "context_bundle_id": data["id"],
        },
        headers=worker_auth,
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["data"]["context_digest"] == data["content_digest"]


def test_context_compile_rejected_when_trust_blocked(api, objects):
    """第161批：doc/05 §3.11 — TrustState BLOCKED 拒新 ContextBundle 编译。"""
    store, _, _ = objects
    client, token, _auth, goal, _plan = _start_goal(api, objects)
    client.app.state.objects = store
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
        f"/internal/v1/activities/{activity_id}/context-compile",
        json={"lease": lease["lease"], "max_input_tokens": 8192},
        headers=worker_auth,
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["error"]["code"] == "INVALID_STATE"
    assert "信任" in denied.json()["error"]["message"]


def test_context_compile_rejected_when_skill_revoked(api, objects):
    """第161批：SkillSet 成员 REVOKED 后拒编译（闭包新绑定失败关闭）。"""
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

    with engine.begin() as db:
        skill_set_id = UUID(goal["contract"]["skill_set_id"])
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
        f"/internal/v1/activities/{activity_id}/context-compile",
        json={"lease": lease["lease"], "max_input_tokens": 8192},
        headers=worker_auth,
    )
    assert denied.status_code == 422, denied.text
    assert "ACTIVE" in denied.json()["error"]["message"]
