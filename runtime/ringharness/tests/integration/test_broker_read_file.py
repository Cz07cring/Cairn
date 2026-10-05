"""Broker 真实 read_file：沙箱读工作区 → collector 上传 → TrustedReceipt。"""

import hashlib
import os
import subprocess
import sys
import textwrap
from io import BytesIO
from pathlib import Path
from uuid import UUID

from control_kernel.storage.artifacts import Artifacts
from execution_broker import run_read_file_effect
from test_effects import _publish_and_claim_execute


def test_broker_read_file_real_bytes(api, objects, tmp_path: Path):
    """遗留 {path} 与 ToolPayload（schema digest）均可经 prepare → Broker 真实读字节。"""
    import json

    from control_kernel.domain.tool_capability_manifest import READ_FILE_SCHEMA_DIGEST

    store, _, _ = objects
    workspace = tmp_path / "repo"
    (workspace / "src").mkdir(parents=True)
    file_bytes = b"print('broker-ok')\n"
    (workspace / "src" / "main.py").write_bytes(file_bytes)

    optimized_probe = textwrap.dedent(
        f"""
        from pathlib import Path
        from uuid import UUID

        from execution_broker.host import run_read_file_effect


        class Response:
            def __init__(self, status_code, data):
                self.status_code = status_code
                self.text = "synthetic protocol failure"
                self._data = data

            def json(self):
                return self._data


        class Client:
            def __init__(self):
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append(("post", url))
                if url.endswith("/dispatch"):
                    return Response(409, {{"data": {{"id": "effect", "state_revision": 2}}}})
                return Response(500, {{"data": {{"id": "receipt"}}}})

            def put(self, url, **kwargs):
                self.calls.append(("put", url))
                return Response(500, {{"data": {{"id": "artifact"}}}})


        client = Client()
        try:
            run_read_file_effect(
                client,
                worker_auth={{}},
                lease={{
                    "activity_id": "00000000-0000-0000-0000-000000000001",
                    "attempt_id": "00000000-0000-0000-0000-000000000002",
                    "fencing_epoch": 1,
                }},
                effect={{"id": "effect", "state_revision": 1}},
                project_id=UUID("00000000-0000-0000-0000-000000000003"),
                workspace_root=Path({str(workspace)!r}),
                allowed_paths=["src/**"],
                input_bytes=b'{{"path":"src/main.py"}}',
            )
        except RuntimeError as exc:
            if len(client.calls) != 1 or "派发" not in str(exc):
                raise RuntimeError(f"unexpected failure boundary: {{client.calls}} {{exc}}")
        else:
            raise RuntimeError(f"failed dispatch accepted under -O: {{client.calls}}")
        """
    )
    optimized = subprocess.run(
        [sys.executable, "-O", "-c", optimized_probe],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert optimized.returncode == 0, optimized.stderr

    client, _token, auth, _goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    engine = client.app.state.engine

    def _run_once(
        input_blob: bytes,
        purpose: str,
        predecessor: str | None,
        *,
        expected_outcome: str = "SUCCEEDED",
        expected_content: bytes | None = file_bytes,
    ):
        step = client.post(
            f"/internal/v1/activities/{activity_id}/steps",
            json={
                "lease": exec_lease["lease"],
                "predecessor_step_id": predecessor,
                "purpose": purpose,
                "tool_ref": "read_file",
            },
            headers=worker_auth,
        )
        assert step.status_code == 201, step.text
        step_data = step.json()["data"]
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
                "logical_step_id": step_data["logical_step_id"],
                "intent_revision": 1,
                "tool_ref": "read_file",
                "input_artifact_id": str(input_art.id),
            },
            headers=worker_auth,
        )
        assert prepared.status_code == 201, prepared.text
        effect = prepared.json()["data"]
        ran = run_read_file_effect(
            client,
            worker_auth=worker_auth,
            lease=exec_lease["lease"],
            effect=effect,
            project_id=project_id,
            workspace_root=workspace,
            allowed_paths=["src/**"],
            input_bytes=input_blob,
        )
        assert ran["result"].observed_outcome == expected_outcome
        assert ran["result"].content == expected_content
        assert (
            client.get(f"/api/v1/effects/{effect['id']}", headers=auth).json()["data"][
                "status"
            ]
            == expected_outcome
        )
        return step_data["logical_step_id"], effect, ran

    # 1) 遗留参数对象
    pred, effect, ran = _run_once(b'{"path":"src/main.py"}', "遗留 path", None)
    art_id = ran["result_artifact_ids"][0]
    content = client.get(f"/api/v1/artifacts/{art_id}/content", headers=auth)
    assert content.status_code == 200
    assert content.content == file_bytes
    assert ran["receipt"]["disposition"] == "APPLIED"

    # 2) Runner ToolPayload（同 activation 链）
    payload = json.dumps(
        {
            "tool_ref": "read_file",
            "tool_schema_digest": READ_FILE_SCHEMA_DIGEST,
            "parameters": {"path": "src/main.py"},
        },
        separators=(",", ":"),
    ).encode()
    pred, _, _ = _run_once(payload, "ToolPayload", pred)

    host_secret = tmp_path / "host-secret.txt"
    host_secret.write_bytes(b"host-only-secret")
    os.link(host_secret, workspace / "src" / "host-link.txt")
    blocked_payload = json.dumps(
        {
            "tool_ref": "read_file",
            "tool_schema_digest": READ_FILE_SCHEMA_DIGEST,
            "parameters": {"path": "src/host-link.txt"},
        },
        separators=(",", ":"),
    ).encode()
    blocked_pred, _, blocked = _run_once(
        blocked_payload,
        "拒绝 hardlink 读取",
        pred,
        expected_outcome="FAILED",
        expected_content=None,
    )
    assert "硬链接" in (blocked["result"].error or "")
    assert host_secret.read_bytes() == b"host-only-secret"

    real_dir = workspace / "src" / "real"
    real_dir.mkdir()
    (real_dir / "through-parent.txt").write_bytes(b"parent-symlink-secret")
    (workspace / "src" / "linked").symlink_to(real_dir, target_is_directory=True)
    symlink_payload = json.dumps(
        {
            "tool_ref": "read_file",
            "tool_schema_digest": READ_FILE_SCHEMA_DIGEST,
            "parameters": {"path": "src/linked/through-parent.txt"},
        },
        separators=(",", ":"),
    ).encode()
    _, _, symlink_blocked = _run_once(
        symlink_payload,
        "拒绝父目录 symlink 读取",
        blocked_pred,
        expected_outcome="FAILED",
        expected_content=None,
    )
    assert "符号链接" in (symlink_blocked["result"].error or "")

    task_id = exec_lease["activity"]["task_id"]
    evidence = client.get(f"/api/v1/tasks/{task_id}/evidence", headers=auth)
    assert evidence.status_code == 200, evidence.text
    rows = evidence.json()["data"]
    assert len(rows) >= 1
    env = rows[0]
    assert env["effect_id"] == effect["id"]
    assert env["producer_identity"].startswith("worker:")
    assert env["command_argv"] == ["execution_broker", "read_file"]


def test_broker_path_outside_policy_fails(api, objects, tmp_path: Path):
    """越权 path：M4 Manifest prepare 门禁失败关闭（不得进入 Broker 执行）。"""
    store, _, _ = objects
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "secret.txt").write_bytes(b"leak")

    client, _token, _auth, _goal, worker_auth, exec_lease = _publish_and_claim_execute(
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
            "purpose": "越权读",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    input_blob = b'{"path":"secret.txt"}'
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
    assert prepared.status_code == 422, prepared.text
    assert prepared.json()["error"]["code"] == "TOOL_PATH_DENIED"
