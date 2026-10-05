"""Broker 经 Control HTTP 拉取可派发 effect；无业务库、不标 Goal DONE。"""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from uuid import UUID

from control_kernel.storage.artifacts import Artifacts
from execution_broker.service import BrokerSettings, process_dispatchable_once
from test_effects import _publish_and_claim_execute


def _prepare_read_file(api, objects, tmp_path: Path):
    store, _, _ = objects
    workspace = tmp_path / "repo"
    (workspace / "src").mkdir(parents=True)
    file_bytes = b"print('broker-poll-ok')\n"
    (workspace / "src" / "main.py").write_bytes(file_bytes)

    client, _token, auth, _goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    engine = client.app.state.engine

    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "poll 读取入口",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text

    input_blob = b'{"path":"src/main.py"}'
    input_digest = "sha256:" + hashlib.sha256(input_blob).hexdigest()
    input_art = Artifacts(engine, store).ingest_raw(
        project_id,
        input_digest,
        BytesIO(input_blob),
        mime="application/json",
        producer_identity="test:input",
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
    return (
        client,
        auth,
        worker_auth,
        exec_lease,
        prepared.json()["data"],
        workspace,
        file_bytes,
        project_id,
    )


def test_broker_effects_poll_returns_prepared_with_lease(api, objects, tmp_path: Path):
    client, _auth, worker_auth, exec_lease, effect, _ws, _fb, _pid = _prepare_read_file(
        api, objects, tmp_path
    )
    listed = client.get("/internal/v1/broker/effects", headers=worker_auth)
    assert listed.status_code == 200, listed.text
    rows = listed.json()["data"]
    assert len(rows) >= 1
    match = next(r for r in rows if r["effect"]["id"] == effect["id"])
    assert match["effect"]["status"] == "PREPARED"
    assert match["effect"]["tool_ref"] == "read_file"
    assert match["lease"]["activity_id"] == exec_lease["lease"]["activity_id"]
    assert match["lease"]["attempt_id"] == exec_lease["lease"]["attempt_id"]
    assert match["lease"]["fencing_epoch"] == exec_lease["lease"]["fencing_epoch"]
    assert match["lease_expires_at"]


def test_broker_effects_poll_forbidden_for_unregistered_worker(api, objects):
    client, token = api
    auth = {"Authorization": "Bearer " + token("nobody-" + "x" * 8, ["worker"])}
    listed = client.get("/internal/v1/broker/effects", headers=auth)
    assert listed.status_code == 403
    assert listed.json()["error"]["code"] == "FORBIDDEN"


def test_broker_process_poll_read_file_succeeds(api, objects, tmp_path: Path):
    (
        client,
        auth,
        worker_auth,
        _lease,
        effect,
        workspace,
        file_bytes,
        _project_id,
    ) = _prepare_read_file(api, objects, tmp_path)

    settings = BrokerSettings(
        control_url="",  # TestClient 用绝对路径
        worker_jwt="unused",
        workspace_root=str(workspace),
        allowed_paths=("src/**",),
    )
    process_dispatchable_once(
        client,
        base_url="",
        worker_auth=worker_auth,
        settings=settings,
    )
    effect_got = client.get(f"/api/v1/effects/{effect['id']}", headers=auth)
    assert effect_got.status_code == 200
    assert effect_got.json()["data"]["status"] == "SUCCEEDED"

    # 输入工件亦可由 worker 下载（poll 路径依赖）
    content = client.get(
        f"/api/v1/artifacts/{effect['input_artifact_id']}/content", headers=worker_auth
    )
    assert content.status_code == 200
    assert b"src/main.py" in content.content
    assert file_bytes  # 工作区真实字节已存在，供沙箱读取
