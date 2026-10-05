"""第三百五十二批：Goal 软禁同参键 Kernel 持久；进程重启可恢复；≠ DONE。"""

from __future__ import annotations

from uuid import UUID, uuid4

from control_kernel.storage.ban_call_keys import (
    add_goal_ban_call_key,
    read_goal_ban_call_keys,
)
from sqlalchemy import text
from test_claims import _register_worker, _start_goal


def test_ban_call_keys_add_and_restart_hydrate(api, objects):
    """登记软禁键；模拟进程丢失后 GET 仍见；幂等；≠DONE。"""
    client, token, auth, goal, _plan = _start_goal(api, objects)
    engine = client.app.state.engine
    goal_id = UUID(goal["id"])
    call_key = "read_file|sha256:" + "ab" * 32

    subject = str(uuid4())
    _register_worker(subject, kinds=("PLAN", "EXECUTE"))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}

    empty = client.get(
        f"/internal/v1/goals/{goal_id}/ban-call-keys",
        headers=worker_auth,
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["data"]["call_keys"] == []
    assert empty.json()["data"]["marks_goal_done"] is False

    first = client.post(
        f"/internal/v1/goals/{goal_id}/ban-call-keys",
        json={"call_key": call_key},
        headers=worker_auth,
    )
    assert first.status_code == 201, first.text
    body = first.json()["data"]
    assert body["inserted"] is True
    assert body["call_key"] == call_key
    assert call_key in body["call_keys"]
    assert body["marks_goal_done"] is False

    with engine.connect() as db:
        n = db.execute(
            text("SELECT count(*) FROM goal_ban_call_keys WHERE goal_id=:g"),
            {"g": goal_id},
        ).scalar_one()
        assert int(n) == 1

    hydrated = client.get(
        f"/internal/v1/goals/{goal_id}/ban-call-keys",
        headers=worker_auth,
    )
    assert hydrated.status_code == 200, hydrated.text
    assert hydrated.json()["data"]["call_keys"] == [call_key]

    again = client.post(
        f"/internal/v1/goals/{goal_id}/ban-call-keys",
        json={"call_key": call_key},
        headers=worker_auth,
    )
    assert again.status_code == 201, again.text
    assert again.json()["data"]["inserted"] is False
    assert again.json()["data"]["call_keys"] == [call_key]

    snap = read_goal_ban_call_keys(
        engine, goal_id, subject=subject, project_ids=[]
    )
    assert snap["call_keys"] == [call_key]
    dup = add_goal_ban_call_key(
        engine,
        goal_id,
        subject=subject,
        project_ids=[],
        call_key=call_key,
    )
    assert dup["inserted"] is False

    got = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert got["status"] != "DONE"


def test_ban_call_keys_viewer_forbidden(api, objects):
    client, token, _auth, goal, _plan = _start_goal(api, objects)
    viewer = {"Authorization": "Bearer " + token(str(uuid4()), ["viewer"])}
    res = client.get(
        f"/internal/v1/goals/{goal['id']}/ban-call-keys",
        headers=viewer,
    )
    assert res.status_code == 403, res.text
