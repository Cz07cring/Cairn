"""PROBE_MODEL live 路径：真实打本地 Qwen；缺 key 则 skip。"""

import hashlib
import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from test_claims import _register_worker


def _register_probe_worker(subject: str):
    return _register_worker(subject, kinds=("PROBE_MODEL",), model=4)


@pytest.fixture
def qwen_key():
    key = os.environ.get("RING_LOCAL_QWEN_API_KEY")
    if not key:
        pytest.skip("RING_LOCAL_QWEN_API_KEY required for live model probe")
    return key


def test_model_probe_live_marks_verified(api, objects, qwen_key):
    client, token = api
    store, _, _ = objects
    client.app.state.objects = store
    subject_admin = str(uuid4())
    auth = {"Authorization": "Bearer " + token(subject_admin, ["admin", "operator", "viewer"])}
    project = client.post(
        "/api/v1/projects",
        json={"name": "probe", "repository_ref": "fixture"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    ).json()["data"]["id"]
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
    model_id = "Qwen3.8-Flash-Next-Uncensored-Mixed-omlx"
    provider_ref = "pm2:omlx-flashnext"
    cloud_mode = "DENY"
    cloud_refs: list[str] = []
    base = os.environ.get("RING_LOCAL_QWEN_BASE", "http://127.0.0.1:8001")
    from control_kernel.domain.cloud_endpoint import is_loopback_base

    if not is_loopback_base(base):
        # 远程连接器须合同 PREAUTHORIZED；provider_ref 命中 cloud_provider_refs
        cloud_mode = "PREAUTHORIZED"
        provider_ref = os.environ.get("RING_CHAT_CLOUD_PROVIDER_REF", "deepseek:api")
        cloud_refs = [provider_ref]
        model_id = os.environ.get("RING_LOCAL_QWEN_MODEL", "deepseek-flash")
    profile = client.post(
        "/api/v1/model-profiles",
        json={
            "project_id": project,
            "name": "local-qwen",
            "local_provider_ref": "pm2:omlx-flashnext",
            "model_id": model_id,
            "context_window": 8192,
            "output_reserve": 1024,
            "inference_slots": 1,
            "cloud_provider_refs": cloud_refs,
            "cloud_mode": cloud_mode,
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert profile.status_code == 201, profile.text
    profile_data = profile.json()["data"]
    assert profile_data["capability_status"] == "UNVERIFIED"

    probed = client.post(
        f"/api/v1/model-profiles/{profile_data['id']}/probe",
        json={},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert probed.status_code == 202, probed.text
    command = probed.json()["data"]
    assert command["kind"] == "PROBE_MODEL"
    assert command["status"] == "RUNNING"
    activity_id = command["result"]["activity_id"]

    worker_sub = str(uuid4())
    _register_probe_worker(worker_sub)
    worker_auth = {"Authorization": "Bearer " + token(worker_sub, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PROBE_MODEL"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert lease["activity"]["id"] == activity_id
    assert lease["activity"]["kind"] == "PROBE_MODEL"

    context_content = {
        "project_id": project,
        "goal_id": None,
        "activity_id": activity_id,
        "role": "SYSTEM",
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
    binding_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(lease["activity"]["binding"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    # 使用服务端同一算法：从 claim 返回的 binding 计算
    from control_kernel.storage.claims import binding_digest_of

    binding_digest = binding_digest_of(lease["activity"]["binding"])
    bound = client.post(
        f"/internal/v1/activities/{activity_id}/context",
        json={
            "lease": lease["lease"],
            "binding_digest": binding_digest,
            "context_bundle_id": bundle.json()["data"]["id"],
        },
        headers=worker_auth,
    )
    assert bound.status_code == 200, bound.text
    context_digest = bound.json()["data"]["context_digest"]

    prompt = json.dumps(
        {"messages": [{"role": "user", "content": "ping; reply with pong"}]},
        separators=(",", ":"),
    ).encode()
    input_digest = "sha256:" + hashlib.sha256(prompt).hexdigest()
    created = client.post(
        "/internal/v1/model-invocations",
        json={
            "lease": lease["lease"],
            "invocation_seq": 1,
            "context_digest": context_digest,
            "input_digest": input_digest,
            "provider_ref": provider_ref,
            "model_id": model_id,
            "max_output_tokens": 32,
            "max_cost_usd": "0",
            "data_categories": [],
        },
        headers=worker_auth,
    )
    assert created.status_code == 201, created.text
    invocation = created.json()["data"]
    assert invocation["status"] == "AUTHORIZED"

    dispatched = client.post(
        f"/internal/v1/model-invocations/{invocation['id']}/dispatch",
        json={
            "lease": lease["lease"],
            "expected_state_revision": invocation["state_revision"],
        },
        headers=worker_auth,
    )
    assert dispatched.status_code == 200, dispatched.text
    assert dispatched.json()["data"]["status"] == "SUCCEEDED"
    evidence_id = dispatched.json()["data"]["result_artifact_id"]
    assert evidence_id

    done = client.post(
        f"/internal/v1/activities/{activity_id}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {
                "profile_id": profile_data["id"],
                "capability_status": "VERIFIED",
                "evidence_ids": [evidence_id],
            },
        },
        headers=worker_auth,
    )
    assert done.status_code == 200, done.text
    assert done.json()["data"]["status"] == "SUCCEEDED"

    listed = client.get(
        "/api/v1/model-profiles",
        params={"project_id": project},
        headers=auth,
    ).json()["data"]
    matched = next(item for item in listed if item["id"] == profile_data["id"])
    assert matched["capability_status"] == "VERIFIED"
    assert matched["probe_evidence_ids"] == [evidence_id]

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.connect() as db:
        status = db.execute(
            text("SELECT capability_status FROM model_profiles WHERE id=:id"),
            {"id": profile_data["id"]},
        ).scalar_one()
        assert status == "VERIFIED"
    engine.dispose()
