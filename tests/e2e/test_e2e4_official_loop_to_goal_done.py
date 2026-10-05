"""E2E-4 桥：官方 Loop scripted write→run→seal × 独立 Broker → AUDIT/FINALIZE → Goal DONE。

诚实：scriptedOrder ≠ 模型自主；DONE 仅经 Kernel FINALIZE + GLOBAL VerificationProfile。
前置：Harness peers 已 build；RING_TEST_* 可用。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
import uvicorn
from control_api.app import Settings, create_app
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from e2e3_order_helpers import ALLOW
from e2e_tool_helpers import load_reference_fix_store, prepare_tool_effect
from evidence_ledger.objects import S3Objects
from execution_broker import (
    attach_holdout_tests,
    materialize_candidate_worktree,
    run_run_tests_effect,
)
from live_chat_gate import skip_or_fail_live as _skip_or_fail_live
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from test_claims import _drain_ready, _register_worker
from test_finalization import _ready_project_with_global
from test_plans import _plan_body
from verification_run_helpers import (
    post_verification_run_from_broker_tests,
    profile_verifier_digest,
)

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import seed_business_e2e as seed

_HOLDOUT = _ROOT / "tests" / "fixtures" / "business_e2e" / "order_service" / "tests_hidden"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _harness_checkout() -> Path | None:
    raw = (os.environ.get("RING_HARNESS_CHECKOUT") or "").strip()
    root = Path(raw) if raw else _ROOT / ".runtime" / "deepseek-harness"
    if not root.is_dir():
        return None
    needed = [
        root / "vendor" / "cordis" / "lib" / "index.js",
        root / "packages" / "core" / "agent-loop" / "lib" / "index.js",
        root / "packages" / "llm" / "llm" / "lib" / "index.js",
        root / "packages" / "core" / "session" / "lib" / "index.js",
        root / "packages" / "session" / "session-projection" / "lib" / "index.js",
        root / "packages" / "core" / "system-prompt" / "lib" / "index.js",
        root / "packages" / "core" / "tools" / "lib" / "index.js",
        root / "packages" / "core" / "agent" / "lib" / "index.js",
    ]
    if not all(p.is_file() for p in needed):
        return None
    return root.resolve()


class _HttpApi:
    def __init__(self, client: httpx.Client, app):
        self._client = client
        self.app = app

    def post(self, url, **kwargs):
        return self._client.post(url, **kwargs)

    def get(self, url, **kwargs):
        return self._client.get(url, **kwargs)

    def put(self, url, **kwargs):
        return self._client.put(url, **kwargs)


def _broker_env(control_url: str, worker_jwt: str, workspace: Path) -> dict[str, str]:
    env = {
        **os.environ,
        "RING_BROKER_CONTROL_URL": control_url,
        "RING_BROKER_WORKER_JWT": worker_jwt,
        "RING_BROKER_WORKSPACE_ROOT": str(workspace),
        "RING_BROKER_ALLOWED_PATHS": ",".join(ALLOW),
    }
    for key in ("RING_DATABASE_URL", "RING_CONTROL_DATABASE_URL"):
        env.pop(key, None)
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        env.pop(key, None)
    return env


def _candidate_from_seal_effect(client, auth, seal_effect_id: str) -> tuple[dict, list[str]]:
    """从 seal Effect 证据工件解析 CandidateManifest。"""
    got = client.get(f"/api/v1/effects/{seal_effect_id}", headers=auth)
    assert got.status_code == 200, got.text
    effect = got.json()["data"]
    assert effect["status"] == "SUCCEEDED"
    evidence_ids = effect.get("evidence_ids") or []
    assert evidence_ids, "seal effect 缺 evidence_ids"
    body = client.get(f"/api/v1/artifacts/{evidence_ids[0]}/content", headers=auth)
    assert body.status_code == 200, body.text
    meta = json.loads(body.text)
    # ToolResult 可能包一层 text JSON
    if isinstance(meta, dict) and "candidate_manifest_id" not in meta and "text" in meta:
        meta = json.loads(meta["text"]) if isinstance(meta["text"], str) else meta
    cand_id = meta.get("candidate_manifest_id")
    assert cand_id, f"seal 证据无 candidate_manifest_id: {meta!r}"
    cand = client.get(f"/api/v1/candidates/{cand_id}", headers=auth)
    assert cand.status_code == 200, cand.text
    return cand.json()["data"], evidence_ids


def _heartbeat(client, exec_auth: dict, lease_bundle: dict) -> None:
    """续租，避免共享库失租扫描把 attempt 打成非 ACTIVE。"""
    activity_id = lease_bundle["activity"]["id"]
    attempt = lease_bundle.get("attempt") or {}
    renewal = int(attempt.get("renewal_seq") or 0) + 1
    hb = client.post(
        f"/internal/v1/activities/{activity_id}/heartbeat",
        json={
            "lease": lease_bundle["lease"],
            "renewal_seq": renewal,
        },
        headers=exec_auth,
    )
    assert hb.status_code == 200, hb.text
    # 同步本地 renewal，供循环续租
    if "attempt" not in lease_bundle:
        lease_bundle["attempt"] = {}
    lease_bundle["attempt"]["renewal_seq"] = renewal


def _assert_attempt_active(attempt_id: str) -> None:
    eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with eng.connect() as db:
        status = db.execute(
            text("SELECT status FROM activity_attempts WHERE id=:id"),
            {"id": attempt_id},
        ).scalar_one()
    eng.dispose()
    assert status == "ACTIVE", f"attempt 非 ACTIVE: {status}"


def test_e2e4_official_loop_seal_then_goal_done(objects, tmp_path: Path, monkeypatch):
    """scripted e2e2 缝：官方 Loop diagnose×独立 Broker → Goal DONE。"""
    _e2e4_official_loop_seal_then_goal_done(
        objects, tmp_path, monkeypatch, via_run_activation=False, live_chat=False
    )


def test_e2e4_run_activation_diagnose_seal_then_goal_done(
    objects, tmp_path: Path, monkeypatch
):
    """经 RunActivation Activity 入口 + FSM diagnose×独立 Broker → Goal DONE。

    诚实：FSM ≠ 模型自主；via=runActivation；DONE 仅 Kernel FINALIZE。
    """
    _e2e4_official_loop_seal_then_goal_done(
        objects, tmp_path, monkeypatch, via_run_activation=True, live_chat=False
    )


def test_e2e4_run_activation_live_diagnose_seal_then_goal_done(
    objects, tmp_path: Path, monkeypatch
):
    """经 RunActivation + 真实 chat diagnose×独立 Broker → Goal DONE。

    诚实：零 USER_PROMPT / 零 SEAL_PROFILE_IDS 注入；profile 从 Goal 合同进工具 schema；
    DONE 仅 Kernel。
    """
    _e2e4_official_loop_seal_then_goal_done(
        objects, tmp_path, monkeypatch, via_run_activation=True, live_chat=True
    )


def _load_chat_env_into_os() -> None:
    """合并 .runtime：与 serve_local/probe 同序（显式 export > chat.env > qwen.env）。"""
    import sys

    scripts = str(_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from runtime_env import apply_model_runtime_env

    apply_model_runtime_env(_ROOT, os.environ)


def _assert_live_chat_model_aligned() -> None:
    """live 前核对 MODEL∈/v1/models；错配硬失败（禁止带着 model not found 空转）。"""
    import sys

    scripts = str(_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from chat_model_gate import ChatModelMismatch, assert_chat_model_configured

    try:
        assert_chat_model_configured(dict(os.environ), timeout=15.0)
    except ChatModelMismatch as exc:
        pytest.fail(exc.message)


def _e2e4_official_loop_seal_then_goal_done(
    objects,
    tmp_path: Path,
    monkeypatch,
    *,
    via_run_activation: bool,
    live_chat: bool,
):
    if live_chat and not via_run_activation:
        raise AssertionError("live_chat 仅支持 via_run_activation")
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")
    if not os.environ.get("RING_TEST_S3_ENDPOINT"):
        pytest.skip("RING_TEST_S3_ENDPOINT required")
    checkout = _harness_checkout()
    if checkout is None:
        if live_chat:
            _skip_or_fail_live("RING_HARNESS_CHECKOUT AgentLoop peers 未 build:lib:host")
        pytest.skip("RING_HARNESS_CHECKOUT AgentLoop peers 未 build:lib:host")
    if live_chat:
        _load_chat_env_into_os()
        if (os.environ.get("RING_E2E4_LIVE_CHAT") or "").strip() not in (
            "1",
            "true",
            "yes",
        ):
            _skip_or_fail_live(
                "live_chat 需显式 RING_E2E4_LIVE_CHAT=1（模型/租约耗时长，默认关闭）"
            )
        if not (
            (os.environ.get("RING_LOCAL_QWEN_BASE") or "").strip()
            and (os.environ.get("RING_LOCAL_QWEN_API_KEY") or "").strip()
            and (os.environ.get("RING_LOCAL_QWEN_MODEL") or "").strip()
        ):
            _skip_or_fail_live(
                "live_chat 需要 RING_LOCAL_QWEN_BASE/API_KEY/MODEL（或 .runtime/chat.env）"
            )
        _assert_live_chat_model_aligned()

    _store, s3, bucket = objects
    store = S3Objects(s3, bucket, max_bytes=64 * 1024)
    run = seed.seed_run(
        run_id=(
            "e2e4-ra-live-done"
            if live_chat
            else ("e2e4-ra-done" if via_run_activation else "e2e4-loop-done")
        ),
        runtime_root=tmp_path,
    )
    fix_body = load_reference_fix_store()
    fix_file = run.artifacts / "reference_fix_store.py"
    fix_file.write_text(fix_body, encoding="utf-8")

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    def token(subject, roles):
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "sub": subject,
                "roles": roles,
                "project_ids": [],
                "iss": "ring-test",
                "aud": "ring-api",
                "iat": now,
                "exp": now + timedelta(minutes=30),
            },
            private,
            algorithm="RS256",
        )

    monkeypatch.delenv("RING_S3_ENDPOINT", raising=False)
    if live_chat:
        # 账本 create 冻结校验：Profile 须与 RING_LOCAL_QWEN_* 对齐（DeepSeek 等）
        monkeypatch.setenv("RING_TEST_ALLOW_CLOUD", "1")
    settings = Settings(
        database_url=os.environ["RING_TEST_DATABASE_URL"],
        jwt_public_key=public,
        jwt_issuer="ring-test",
        jwt_audience="ring-api",
        repository_refs=["fixture"],
        cursor_secret="test-only-cursor-secret-at-least-32-bytes",
        local_qwen_base=os.environ.get("RING_LOCAL_QWEN_BASE", "http://127.0.0.1:8001"),
        local_qwen_api_key=os.environ.get("RING_LOCAL_QWEN_API_KEY"),
        cloud_mode=(
            os.environ.get("RING_CLOUD_MODE", "DENY")
            if os.environ.get("RING_TEST_ALLOW_CLOUD") == "1"
            else "DENY"
        ),
        s3_endpoint=os.environ["RING_TEST_S3_ENDPOINT"],
        s3_bucket=bucket,
        s3_access_key=SecretStr(os.environ["RING_TEST_S3_ACCESS_KEY"]),
        s3_secret_key=SecretStr(os.environ["RING_TEST_S3_SECRET_KEY"]),
        artifact_max_bytes=16 * 1024 * 1024,
        # live 长轮次：避免 90s 默认 TTL + 外部 Temporal expire 扫库误杀
        lease_ttl_seconds=600,
    )
    app = create_app(settings)
    port = _free_port()
    control_url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    httpx_client = httpx.Client(base_url=control_url, timeout=120.0, trust_env=False)
    http = _HttpApi(httpx_client, app)
    stop_broker = threading.Event()
    broker_logs: list[str] = []
    broker_thread: threading.Thread | None = None
    trail: list[str] = []

    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if httpx_client.get("/health/live").status_code == 200:
                    break
            except (httpx.HTTPError, OSError):
                time.sleep(0.05)
        else:
            pytest.fail("Control uvicorn 未就绪")

        app.state.objects = store
        app.state._force_objects = store
        client, tok, auth, _project, goal_body, mechanical_id, global_profile_id = (
            _ready_project_with_global((http, token), objects)
        )
        policy = client.post(
            "/api/v1/policies",
            json={
                "project_id": goal_body["project_id"],
                "name": "e2e4-loop-done",
                "allowed_tools": [
                    "read_file",
                    "write_file",
                    "run_tests",
                    "git_diff",
                    "seal_candidate",
                ],
                "allowed_paths": list(ALLOW),
                "protected_paths": [],
                "network_allowlist": [],
                "external_actions": [],
                "secret_scope_refs": [],
            },
            headers={**auth, "Idempotency-Key": str(uuid4())},
        )
        assert policy.status_code == 201, policy.text
        goal_body = {
            **goal_body,
            "policy_id": policy.json()["data"]["id"],
            "objective": (
                "修复订单 create_order 幂等："
                "同 idempotency_key 顺序请求两次须返回相同 order_id 且库存只扣一次；"
                "同 key 并发请求亦须同一 order_id；不同 key 创建不同订单。"
            ),
        }
        # 覆盖默认「机械验收」文案，使 Task acceptance 对 Runner 可读（≠ 粘贴修复正文）
        if goal_body.get("success_criteria"):
            goal_body["success_criteria"] = [
                {
                    **goal_body["success_criteria"][0],
                    "description": (
                        "订单幂等公开与隐藏测试通过（顺序/并发同 key 同一 order_id）"
                    ),
                },
                *goal_body["success_criteria"][1:],
            ]
        _drain_ready(client, tok, kinds=("EXECUTE", "AUDIT", "FINALIZE", "INTEGRATE", "PLAN"))
        created = client.post(
            "/api/v1/goals",
            json=goal_body,
            headers={**auth, "Idempotency-Key": str(uuid4())},
        )
        assert created.status_code == 201, created.text
        goal = created.json()["data"]
        assert goal["contract"]["success_criteria"][0]["verification_profile_id"] == (
            global_profile_id
        )
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

        eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
        with eng.begin() as db:
            db.execute(
                text("""UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                  WHERE status='READY' AND kind='PLAN' AND id<>:id"""),
                {"id": plan_activity["id"]},
            )
            db.execute(
                text(
                    """UPDATE activities SET status='READY', updated_at=clock_timestamp()
                    WHERE id=:id AND status IN ('CANCELLED','READY')"""
                ),
                {"id": plan_activity["id"]},
            )
        eng.dispose()

        exec_subject = str(uuid4())
        _register_worker(exec_subject, kinds=("PLAN", "EXECUTE"))
        exec_auth = {"Authorization": "Bearer " + token(exec_subject, ["worker"])}
        claimed = client.post(
            "/internal/v1/claims",
            json={"kinds": ["PLAN"], "capabilities": []},
            headers={**exec_auth, "Idempotency-Key": str(uuid4())},
        )
        lease = claimed.json()["data"]
        plan = _plan_body(goal, mechanical_id)
        plan["coverage"][0]["verification_profile_id"] = mechanical_id
        plan["tasks"][0]["contract"]["allowed_paths"] = list(ALLOW)
        plan["tasks"][0]["contract"]["acceptance"][0]["description"] = (
            "同 idempotency_key 顺序请求两次返回相同 order_id 且库存只扣一次；"
            "同 key 并发请求亦须同一 order_id；不同 key 创建不同订单。"
        )
        done = client.post(
            f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
            json={
                "lease": lease["lease"],
                "expected_state_revision": lease["activity"]["state_revision"],
                "outcome": {"plan": plan},
            },
            headers=exec_auth,
        )
        assert done.status_code == 200, done.text
        eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
        with eng.begin() as db:
            db.execute(
                text(
                    """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                    WHERE status='READY' AND kind='EXECUTE' AND goal_id<>:goal"""
                ),
                {"goal": goal["id"]},
            )
        eng.dispose()
        exec_lease = client.post(
            "/internal/v1/claims",
            json={"kinds": ["EXECUTE"], "capabilities": []},
            headers={**exec_auth, "Idempotency-Key": str(uuid4())},
        ).json()["data"]
        app.state.objects = store
        project_id = UUID(exec_lease["activity"]["project_id"])
        worker_jwt = exec_auth["Authorization"].removeprefix("Bearer ").strip()

        def _broker_poller() -> None:
            env = _broker_env(control_url, worker_jwt, run.executor_worktree)
            while not stop_broker.is_set():
                proc = subprocess.run(
                    [sys.executable, "-m", "broker_app", "--once"],
                    cwd=str(_ROOT),
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if proc.stdout:
                    broker_logs.append(proc.stdout)
                if proc.stderr:
                    broker_logs.append(proc.stderr)
                stop_broker.wait(0.15)

        broker_thread = threading.Thread(target=_broker_poller, daemon=True)
        broker_thread.start()

        # 续租 + 确认 ACTIVE（共享开发库可能有他方 drain）
        # via_run_activation：Runner 独占 renewal_seq，禁止 Python 预续期抢号
        if not via_run_activation:
            _heartbeat(client, exec_auth, exec_lease)
        _assert_attempt_active(exec_lease["lease"]["attempt_id"])

        hb_stop = threading.Event()
        hb_thread: threading.Thread | None = None

        # via_run_activation：Runner 官方路径自带租约心跳；禁止双端抢 renewal_seq
        if not via_run_activation:

            def _hb_loop() -> None:
                while not hb_stop.is_set():
                    try:
                        _heartbeat(client, exec_auth, exec_lease)
                    except AssertionError:
                        break
                    hb_stop.wait(20)

            hb_thread = threading.Thread(target=_hb_loop, daemon=True)
            hb_thread.start()

        if via_run_activation:
            turn_script = (
                _ROOT
                / "apps"
                / "runner"
                / "src"
                / "harness"
                / "e2eRunActivationDiagnoseTurn.ts"
            )
            # 零 prompt：不注入 USER_PROMPT / SEAL_PROFILE_IDS；
            # Runner：Task acceptance → seal schema；Goal objective → 文案
            ts_env = {
                **os.environ,
                "RING_HARNESS_CHECKOUT": str(checkout),
                "RING_CONTROL_URL": control_url,
                "RING_RUNNER_CONTROL_URL": control_url,
                "RING_WORKER_JWT": worker_jwt,
                "RING_RUNNER_WORKER_JWT": worker_jwt,
                "RING_E2E_RA_GOAL_ID": str(goal["id"]),
                "RING_E2E_RA_LEASE_JSON": json.dumps(exec_lease["lease"]),
                "RING_E2E_RA_OWNER_EPOCH": "e2e4-ra-live" if live_chat else "e2e4-ra",
                "RING_HARNESS_EXECUTE_RUNTIME": "deepseek-official-agent-loop",
                "RING_HARNESS_EXECUTE_OFFICIAL_MODE": "diagnose",
                "RING_HARNESS_EXECUTE_READ_PATH": "order_service/store.py",
                "RING_HARNESS_EXECUTE_WRITE_PATH": "order_service/store.py",
            }
            # 禁止子进程看见本机 Temporal，避免 admit/expire 扫共享库误杀租约
            ts_env.pop("RING_TEMPORAL_TARGET", None)
            if live_chat:
                # 禁止 FSM；不注入 WRITE_CONTENT / USER_PROMPT / SEAL_PROFILE_IDS
                ts_env.pop("RING_HARNESS_EXECUTE_OFFICIAL_FSM", None)
                ts_env.pop("RING_HARNESS_EXECUTE_WRITE_CONTENT", None)
                ts_env.pop("RING_HARNESS_EXECUTE_WRITE_CONTENT_FILE", None)
                ts_env.pop("RING_HARNESS_EXECUTE_USER_PROMPT", None)
                ts_env.pop("RING_HARNESS_EXECUTE_SEAL_PROFILE_IDS", None)
                # claim 后未预续期：下一拍从 1 起；5s 心跳
                ts_env["RING_HARNESS_EXECUTE_NEXT_RENEWAL_SEQ"] = "1"
                ts_env["RING_HARNESS_EXECUTE_HEARTBEAT_MS"] = "5000"
            else:
                ts_env["RING_HARNESS_EXECUTE_OFFICIAL_FSM"] = "1"
                ts_env["RING_HARNESS_EXECUTE_WRITE_CONTENT_FILE"] = str(fix_file)
                ts_env["RING_HARNESS_EXECUTE_SEAL_PROFILE_IDS"] = str(mechanical_id)
                ts_env["RING_HARNESS_EXECUTE_NEXT_RENEWAL_SEQ"] = "1"
        else:
            turn_script = (
                _ROOT
                / "apps"
                / "runner"
                / "src"
                / "harness"
                / "e2e2OfficialMultistepOrderTurn.ts"
            )
            ts_env = {
                **os.environ,
                "RING_HARNESS_CHECKOUT": str(checkout),
                "RING_CONTROL_URL": control_url,
                "RING_WORKER_JWT": worker_jwt,
                "RING_E2E2_PROJECT_ID": str(project_id),
                "RING_E2E2_LEASE_JSON": json.dumps(exec_lease["lease"]),
                "RING_E2E2_WRITE_PATH": "order_service/store.py",
                "RING_E2E2_WRITE_CONTENT_FILE": str(fix_file),
                "RING_E2E2_DIAGNOSE_READ_PATH": "order_service/store.py",
                "RING_E2E2_SEAL_PROFILE_IDS": str(mechanical_id),
                "RING_E2E2_POLL_DELAY_MS": "150",
                "RING_E2E2_POLL_MAX_ATTEMPTS": "300",
                "RING_E2E2_IDLE_TIMEOUT_MS": "180000",
                "RING_HARNESS_EXECUTE_RUNTIME": "deepseek-official-agent-loop",
            }
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            ts_env.pop(key, None)

        ts_timeout = 600 if live_chat else 240
        ts_proc = subprocess.run(
            [
                "pnpm",
                "--filter",
                "@ring/runner",
                "exec",
                "tsx",
                str(turn_script),
            ],
            cwd=str(_ROOT),
            env=ts_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=ts_timeout,
        )
        assert ts_proc.returncode == 0, (
            f"官方 Loop 退出码={ts_proc.returncode}\n"
            f"stdout={ts_proc.stdout[-2000:]}\nstderr={ts_proc.stderr[-2000:]}\n"
            f"broker={''.join(broker_logs)[-2000:]}"
        )
        hb_stop.set()
        if hb_thread is not None:
            hb_thread.join(timeout=5)
        lines = [ln for ln in ts_proc.stdout.splitlines() if ln.strip().startswith("{")]
        evidence = json.loads(lines[-1])
        if via_run_activation:
            assert evidence.get("via") == "runActivation"
            assert evidence.get("scripted_order") is False
            assert evidence.get("chat_driven_order") is True
            assert evidence.get("marks_goal_done") is False
            names = evidence.get("tool_names") or []
            for want in ("read_file", "run_tests", "write_file", "seal_candidate"):
                assert want in names, f"缺工具 {want}: {names}"
            write_at = names.index("write_file")
            seal_at = names.index("seal_candidate")
            assert write_at < seal_at, f"write 须在 seal 前: {names}"
            assert any(
                n == "run_tests" for n in names[write_at + 1 :]
            ), f"write 后须有 run_tests: {names}"
            assert evidence.get("readEffectId")
            assert evidence.get("redTestsEffectId")
            assert evidence.get("greenTestsEffectId")
            assert evidence.get("sealEffectId")
            trail.append(
                "run_activation_live_diagnose_fix_seal"
                if live_chat
                else "run_activation_diagnose_fsm_fix_seal"
            )
        else:
            assert evidence["scriptedOrder"] is True
            assert evidence["marksGoalDone"] is False
            assert [t["toolName"] for t in evidence["trail"]] == [
                "read_file",
                "run_tests",
                "write_file",
                "run_tests",
                "seal_candidate",
            ]
            assert evidence.get("readEffectId")
            assert evidence.get("redTestsEffectId")
            assert evidence.get("greenTestsEffectId")
            trail.append("official_loop_diagnose_fix_seal")

        on_disk = (run.executor_worktree / "order_service" / "store.py").read_text(
            encoding="utf-8"
        )
        if live_chat:
            # 无 reference 正文 hint：不要求字节等于 fixture；须已改且后续 AUDIT 绿
            buggy = (
                _ROOT
                / "tests"
                / "fixtures"
                / "business_e2e"
                / "order_service"
                / "order_service"
                / "store.py"
            ).read_text(encoding="utf-8")
            assert on_disk != buggy, "live 未改写 buggy store.py"
            assert "idempotency_key" in on_disk
        else:
            assert on_disk == fix_body

        candidate, seal_evidence_ids = _candidate_from_seal_effect(
            client, auth, evidence["sealEffectId"]
        )
        # live 零 prompt：seal 须用 Task MECHANICAL，不得用 Goal GLOBAL
        assert mechanical_id in candidate["verification_profile_ids"], candidate
        if live_chat:
            assert global_profile_id not in candidate["verification_profile_ids"], (
                "live seal 不得用 Goal GLOBAL profile"
            )
        trail.append("seal_candidate_via_loop_broker")

        # EXECUTE 侧独立 Broker 已完成；后续 AUDIT/FINALIZE 用 in-process Broker 助手（物化树）
        stop_broker.set()
        if broker_thread is not None:
            broker_thread.join(timeout=10)
            broker_thread = None

        mat_root = run.artifacts / "auditor_materialized"
        if mat_root.exists():
            shutil.rmtree(mat_root)

        def read_bytes(digest: str) -> bytes:
            return store.read(project_id, digest)

        materialize_candidate_worktree(
            mat_root, candidate["files"], read_bytes=read_bytes, read_only=True
        )
        assert (mat_root / "tests_hidden").exists() is False
        attach_holdout_tests(mat_root, _HOLDOUT)
        trail.append("materialize_holdout")

        activity_now = client.get(
            f"/api/v1/activities/{exec_lease['activity']['id']}",
            headers=auth,
        ).json()["data"]
        outcome = client.post(
            f"/internal/v1/activities/{exec_lease['activity']['id']}/outcomes",
            json={
                "lease": exec_lease["lease"],
                "expected_state_revision": activity_now["state_revision"],
                "outcome": {
                    "candidate_manifest_id": candidate["id"],
                    "evidence_ids": seal_evidence_ids,
                },
            },
            headers=exec_auth,
        )
        assert outcome.status_code == 200, outcome.text
        task_id = exec_lease["activity"]["task_id"]
        trail.append("execute_outcome_candidate")

        ctx = {
            "client": client,
            "engine": app.state.engine,
            "store": store,
            "project_id": project_id,
            "token": token,
            "auth": auth,
            "goal": goal,
        }

        # AUDIT
        audit_subject = str(uuid4())
        _register_worker(audit_subject, kinds=("AUDIT",))
        audit_auth = {"Authorization": "Bearer " + token(audit_subject, ["worker"])}
        claimed_a = client.post(
            "/internal/v1/claims",
            json={"kinds": ["AUDIT"], "capabilities": []},
            headers={**audit_auth, "Idempotency-Key": str(uuid4())},
        )
        assert claimed_a.status_code == 200, claimed_a.text
        audit_lease = claimed_a.json()["data"]
        ctx["audit_lease"] = audit_lease
        ctx["audit_auth"] = audit_auth
        assignment = audit_lease["activity"]["verification_assignments"][0]
        binding = audit_lease["activity"]["binding"]

        from control_kernel.domain.tool_capability_manifest import RUN_TESTS_SCHEMA_DIGEST

        _step, effect, blob = prepare_tool_effect(
            ctx,
            tool_ref="run_tests",
            purpose="loop-bridge auditor suite",
            parameters={"suite": "auditor"},
            schema_digest=RUN_TESTS_SCHEMA_DIGEST,
            predecessor_step_id=None,
            producer="e2e4-loop-audit",
            lease_key="audit_lease",
            auth_key="audit_auth",
        )
        assert effect["scope"] == "VERIFICATION"
        audited = run_run_tests_effect(
            client,
            worker_auth=audit_auth,
            lease=audit_lease["lease"],
            effect=effect,
            project_id=project_id,
            workspace_root=mat_root,
            input_bytes=blob,
        )
        assert audited["result"].exit_code == 0, audited["result"].stdout_text
        trail.append("broker_auditor_run_tests")

        pass_run_id, audit_checks = post_verification_run_from_broker_tests(
            client,
            audit_auth,
            audit_lease,
            subject_id=candidate["id"],
            subject_digest=candidate["content_digest"],
            verifier_digest=profile_verifier_digest(
                client, auth, str(project_id), assignment["verification_profile_id"]
            ),
            broker_ran=audited,
            engine=app.state.engine,
            store=store,
            project_id=project_id,
        )
        assert audit_checks == "true"
        pass_outcome = client.post(
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
                        "verifier_run_ids": [pass_run_id],
                        "verdict": "PASS",
                        "criterion_results": [
                            {
                                "criterion_id": "A1",
                                "verdict": "PASS",
                                "evidence_ids": [],
                                "reason": "Loop-bridge auditor suite 已绿",
                            }
                        ],
                        "evidence_ids": [],
                        "reason": "E2E-4 loop bridge audit pass",
                    },
                },
            },
            headers=audit_auth,
        )
        assert pass_outcome.status_code == 200, pass_outcome.text
        assert (
            client.get(f"/api/v1/tasks/{task_id}", headers=auth).json()["data"]["status"]
            == "DONE"
        )
        assert (
            client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]["status"]
            == "RUNNING"
        )
        trail.append("audit_pass_task_done")

        # INTEGRATE
        integ_subject = str(uuid4())
        _register_worker(integ_subject, kinds=("INTEGRATE", "FINALIZE"))
        integ_auth = {"Authorization": "Bearer " + token(integ_subject, ["worker"])}
        int_claim = client.post(
            "/internal/v1/claims",
            json={"kinds": ["INTEGRATE"], "capabilities": []},
            headers={**integ_auth, "Idempotency-Key": str(uuid4())},
        )
        assert int_claim.status_code == 200, int_claim.text
        integ = int_claim.json()["data"]
        integration_commit = candidate.get("git_commit") or ("b" * 40)
        integrated = client.post(
            f"/internal/v1/activities/{integ['activity']['id']}/outcomes",
            json={
                "lease": integ["lease"],
                "expected_state_revision": integ["activity"]["state_revision"],
                "outcome": {
                    "candidate_manifest_id": candidate["id"],
                    "integration_commit": integration_commit,
                    "evidence_ids": [],
                },
            },
            headers=integ_auth,
        )
        assert integrated.status_code == 200, integrated.text
        goal_verifying = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()[
            "data"
        ]
        assert goal_verifying["status"] == "VERIFYING"
        barrier_id = goal_verifying["barrier"]["id"]
        trail.append("integrate")

        # FINALIZE
        fin_claim = client.post(
            "/internal/v1/claims",
            json={"kinds": ["FINALIZE"], "capabilities": []},
            headers={**integ_auth, "Idempotency-Key": str(uuid4())},
        )
        assert fin_claim.status_code == 200, fin_claim.text
        fin = fin_claim.json()["data"]
        fin_assign = fin["activity"]["verification_assignments"][0]
        ctx["fin_lease"] = fin
        ctx["fin_auth"] = integ_auth
        _step, effect, blob = prepare_tool_effect(
            ctx,
            tool_ref="run_tests",
            purpose="loop-bridge finalize auditor",
            parameters={"suite": "auditor"},
            schema_digest=RUN_TESTS_SCHEMA_DIGEST,
            predecessor_step_id=None,
            producer="e2e4-loop-finalize",
            lease_key="fin_lease",
            auth_key="fin_auth",
        )
        assert effect["scope"] == "VERIFICATION"
        finalized_tests = run_run_tests_effect(
            client,
            worker_auth=integ_auth,
            lease=fin["lease"],
            effect=effect,
            project_id=project_id,
            workspace_root=mat_root,
            input_bytes=blob,
        )
        assert finalized_tests["result"].exit_code == 0, finalized_tests["result"].stdout_text
        trail.append("broker_finalize_run_tests")
        fin_run_id, fin_checks = post_verification_run_from_broker_tests(
            client,
            integ_auth,
            fin,
            subject_id=candidate["id"],
            subject_digest=candidate["content_digest"],
            verifier_digest=profile_verifier_digest(
                client, auth, str(project_id), fin_assign["verification_profile_id"]
            ),
            broker_ran=finalized_tests,
            engine=app.state.engine,
            store=store,
            project_id=project_id,
            criterion_id="C1",
        )
        assert fin_checks == "true"
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
                                    "reason": "Loop-bridge 全局验收",
                                }
                            ],
                            "evidence_ids": [],
                            "reason": "E2E-4 loop bridge finalize",
                        }
                    ],
                    "evidence_ids": [],
                },
            },
            headers=integ_auth,
        )
        assert finalized.status_code == 200, finalized.text
        goal_done = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
        assert goal_done["status"] == "DONE"
        assert goal_done["barrier"]["status"] == "RELEASED"
        release = client.get(f"/api/v1/goals/{goal['id']}/release", headers=auth).json()[
            "data"
        ]
        assert release["validity"]["status"] == "VALID"
        trail.append("finalize_goal_done")

        summary = {
            "phase": (
                "E2E-4-run-activation-live-diagnose-to-goal-done"
                if live_chat
                else (
                    "E2E-4-run-activation-diagnose-to-goal-done"
                    if via_run_activation
                    else "E2E-4-official-loop-to-goal-done"
                )
            ),
            "run_id": run.run_id,
            "goal_id": goal["id"],
            "candidate_manifest_id": candidate["id"],
            "trail": trail,
            "driver": evidence["driver"],
            "via": evidence.get("via", "e2e2OfficialMultistep"),
            "scripted_order": not via_run_activation,
            "chat_driven_order": bool(via_run_activation),
            "live_chat": live_chat,
            "injected_fsm": bool(via_run_activation and not live_chat),
            "diagnose_cycle": [
                "read_file",
                "run_tests",
                "write_file",
                "run_tests",
                "seal_candidate",
            ],
            "fix_via": (
                "runActivation+live_chat_diagnose+independent_broker"
                if live_chat
                else (
                    "runActivation+fsm_diagnose+independent_broker"
                    if via_run_activation
                    else "official_loop+broker_diagnose_cycle+reference_fix"
                )
            ),
            "done_authority": "control_kernel_finalization",
            "marks_goal_done_by_loop": False,
            "non_goals": [
                "default_live_ci_opt_in"
                if live_chat
                else "model_autonomous_tool_choice",
                "e2e5_recovery_redteam",
                "full_profile_layers",
            ],
            "prompt_nudge": (
                "zero_prompt_goal_contract_seal_via_tool_schema"
                if live_chat
                else None
            ),
        }
        (run.artifacts / "run-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    finally:
        stop_broker.set()
        if broker_thread is not None:
            broker_thread.join(timeout=10)
        server.should_exit = True
        thread.join(timeout=10)
        httpx_client.close()
