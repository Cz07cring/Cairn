"""Broker 长驻入口：可导入、拒绝 Goal DONE、缺 control URL 时空闲；有 URL 则 poll。"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from execution_broker.service import (
    BrokerSettings,
    health,
    load_settings,
    process_dispatchable_once,
    refuse_business_db_credentials,
    refuse_mark_goal_done,
    run_loop,
)


def test_entry_module_imports():
    import execution_broker.__main__ as main_mod

    assert callable(main_mod.main)
    view = health()
    assert view["status"] == "ok"
    assert view["scheduler"] is False
    assert view["marks_goal_done"] is False


def test_refuses_mark_goal_done():
    with pytest.raises(PermissionError, match="DONE"):
        refuse_mark_goal_done(goal_id="g1", status="DONE")


def test_fail_closed_on_business_database_url():
    with pytest.raises(RuntimeError, match="业务库"):
        refuse_business_db_credentials({"RING_DATABASE_URL": "postgresql://x"})
    with pytest.raises(RuntimeError, match="业务库"):
        load_settings({"RING_CONTROL_DATABASE_URL": "postgresql://y"})


def test_once_logs_broker_idle_without_control_url(caplog):
    caplog.set_level(logging.INFO)
    code = run_loop(
        once=True,
        settings=BrokerSettings(control_url=None, worker_jwt=None),
    )
    assert code == 0
    assert any("broker-idle" in record.message for record in caplog.records)


def test_once_with_control_url_polls_effects_not_goals(caplog):
    """有 control_url+jwt 时尝试 poll effects；不发明 Goal/Task 调度。"""
    caplog.set_level(logging.INFO)

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"data": [], "error": None, "meta": {"request_id": str(uuid4())}}

    fake = MagicMock()
    fake.get.return_value = _Resp()

    code = run_loop(
        once=True,
        settings=BrokerSettings(
            control_url="http://127.0.0.1:58101",
            worker_jwt="test-token",
        ),
        client=fake,
    )
    assert code == 0
    assert any("no Goal/Task poller" in record.message for record in caplog.records)
    assert any("broker-effects-pending count=0" in record.message for record in caplog.records)
    assert health()["scheduler"] is False
    fake.get.assert_called()
    called_url = fake.get.call_args.args[0]
    assert called_url.endswith("/internal/v1/broker/effects")


def test_process_read_file_calls_runner_when_workspace_set(tmp_path: Path):
    effect_id = str(uuid4())
    activity_id = str(uuid4())
    attempt_id = str(uuid4())
    project_id = str(uuid4())
    input_id = str(uuid4())
    payload = {
        "effect": {
            "id": effect_id,
            "project_id": project_id,
            "tool_ref": "read_file",
            "input_artifact_id": input_id,
            "state_revision": 1,
            "status": "PREPARED",
        },
        "lease": {
            "activity_id": activity_id,
            "attempt_id": attempt_id,
            "fencing_epoch": "1",
        },
        "lease_expires_at": "2099-01-01T00:00:00.000Z",
    }

    class _ListResp:
        status_code = 200
        text = ""

        def json(self):
            return {"data": [payload]}

    class _ContentResp:
        status_code = 200
        content = b'{"path":"src/main.py"}'
        text = ""

    fake = MagicMock()

    def _get(url, **_kwargs):
        if "broker/effects" in str(url):
            return _ListResp()
        return _ContentResp()

    fake.get.side_effect = _get
    ran = MagicMock(return_value={"ok": True})
    workspace = tmp_path / "ws"
    workspace.mkdir()
    items = process_dispatchable_once(
        fake,
        base_url="",
        worker_auth={"Authorization": "Bearer t"},
        settings=BrokerSettings(
            control_url="http://control",
            worker_jwt="t",
            workspace_root=str(workspace),
            allowed_paths=("src/**",),
        ),
        run_read_file=ran,
    )
    assert len(items) == 1
    ran.assert_called_once()
    kwargs = ran.call_args.kwargs
    assert kwargs["effect"]["id"] == effect_id
    assert kwargs["input_bytes"] == b'{"path":"src/main.py"}'
    assert kwargs["workspace_root"] == workspace


def test_run_loop_continues_after_connect_error(caplog):
    """Control 不可达时长驻循环续跑，不把 ConnectError 当成进程退出（≠ DONE）。"""
    import httpx

    caplog.set_level(logging.WARNING)
    ticks = {"n": 0}

    class _Boom:
        def get(self, *_args, **_kwargs):
            raise httpx.ConnectError("connection refused")

    def _sleep(_seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 2:
            raise StopIteration

    with pytest.raises(StopIteration):
        run_loop(
            settings=BrokerSettings(
                control_url="http://127.0.0.1:9",
                worker_jwt="test-token",
            ),
            client=_Boom(),
            sleep=_sleep,
        )
    assert ticks["n"] >= 2
    assert any("broker-control-unreachable" in r.message for r in caplog.records)
