"""第三百五十一批：Goal Nudge 预算 Kernel 持久；进程重启可恢复；≠ DONE。"""

from __future__ import annotations

from uuid import UUID, uuid4

from control_kernel.storage.nudge_budgets import (
    read_goal_nudge_budget,
    try_consume_goal_nudge_budget,
)
from sqlalchemy import text
from test_claims import _register_worker, _start_goal


def test_nudge_budget_consume_and_restart_hydrate(api, objects):
    """消耗至耗尽；模拟进程丢失后只读仍见 consumed；再 consume accepted=false；≠DONE。"""
    client, token, auth, goal, _plan = _start_goal(api, objects)
    engine = client.app.state.engine
    goal_id = UUID(goal["id"])

    subject = str(uuid4())
    _register_worker(subject, kinds=("PLAN", "EXECUTE"))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}

    empty = client.get(
        f"/internal/v1/goals/{goal_id}/nudge-budget",
        params={"max_budget": 1},
        headers=worker_auth,
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["data"]["consumed"] == 0
    assert empty.json()["data"]["marks_goal_done"] is False

    first = client.post(
        f"/internal/v1/goals/{goal_id}/nudge-budget/consume",
        json={"max_budget": 1},
        headers=worker_auth,
    )
    assert first.status_code == 200, first.text
    body = first.json()["data"]
    assert body["accepted"] is True
    assert body["consumed"] == 1
    assert body["marks_goal_done"] is False

    # 「进程重启」：直接读库 / GET，不依赖 Runner registry
    with engine.connect() as db:
        row = db.execute(
            text(
                "SELECT consumed, max_budget FROM goal_nudge_budgets WHERE goal_id=:g"
            ),
            {"g": goal_id},
        ).mappings().one()
        assert int(row["consumed"]) == 1

    hydrated = client.get(
        f"/internal/v1/goals/{goal_id}/nudge-budget",
        params={"max_budget": 1},
        headers=worker_auth,
    )
    assert hydrated.status_code == 200, hydrated.text
    assert hydrated.json()["data"]["consumed"] == 1

    denied = client.post(
        f"/internal/v1/goals/{goal_id}/nudge-budget/consume",
        json={"max_budget": 1},
        headers=worker_auth,
    )
    assert denied.status_code == 200, denied.text
    assert denied.json()["data"]["accepted"] is False
    assert denied.json()["data"]["consumed"] == 1
    assert denied.json()["data"]["marks_goal_done"] is False

    snap = read_goal_nudge_budget(
        engine, goal_id, subject=subject, project_ids=[], max_budget=1
    )
    assert snap["consumed"] == 1
    again = try_consume_goal_nudge_budget(
        engine, goal_id, subject=subject, project_ids=[], max_budget=1
    )
    assert again["accepted"] is False

    got = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert got["status"] != "DONE"


def test_nudge_budget_viewer_forbidden(api, objects):
    client, token, _auth, goal, _plan = _start_goal(api, objects)
    viewer = {"Authorization": "Bearer " + token(str(uuid4()), ["viewer"])}
    res = client.get(
        f"/internal/v1/goals/{goal['id']}/nudge-budget",
        params={"max_budget": 1},
        headers=viewer,
    )
    assert res.status_code == 403, res.text
