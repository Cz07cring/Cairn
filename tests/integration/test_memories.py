"""GET /api/v1/memories 与 POST /internal/v1/memories：ACL、过滤与分页。"""

from uuid import uuid4

from test_claims import _start_goal


def test_memories_acl_empty_create_list_filters_and_pagination(api, objects):
    client, token, auth, goal, plan = _start_goal(api, objects)
    project_id = goal["project_id"]
    activity_id = plan["id"]

    # 无读角色拒绝
    denied = client.get(
        "/api/v1/memories",
        params={"project_id": project_id},
        headers={"Authorization": "Bearer " + token(str(uuid4()), ["worker"])},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "FORBIDDEN"

    # 空列表
    empty = client.get("/api/v1/memories", params={"project_id": project_id}, headers=auth)
    assert empty.status_code == 200, empty.text
    assert empty.json()["data"] == []
    assert empty.json()["meta"]["next_cursor"] is None

    # viewer 不能写
    viewer_only = {
        "Authorization": "Bearer " + token(str(uuid4()), ["viewer"]),
    }
    # 非成员 viewer 读跨项目 → 404
    outsider = client.get(
        "/api/v1/memories",
        params={"project_id": project_id},
        headers=viewer_only,
    )
    assert outsider.status_code == 404

    # operator 写 PROPOSED
    key = str(uuid4())
    created = client.post(
        "/internal/v1/memories",
        json={
            "project_id": project_id,
            "activity_id": activity_id,
            "kind": "fact",
            "statement": "部署必须先跑迁移",
            "source_evidence_ids": [],
            "confidence_bp": 8000,
        },
        headers={**auth, "Idempotency-Key": key},
    )
    assert created.status_code == 201, created.text
    memory = created.json()["data"]
    assert memory["status"] == "PROPOSED"
    assert memory["kind"] == "fact"
    assert memory["statement"] == "部署必须先跑迁移"
    assert memory["confidence_bp"] == 8000
    assert memory["supersedes_id"] is None
    assert memory["valid_from"].endswith("Z")
    assert memory["project_id"] == project_id
    assert memory["activity_id"] == activity_id

    # 同键幂等
    again = client.post(
        "/internal/v1/memories",
        json={
            "project_id": project_id,
            "activity_id": activity_id,
            "kind": "fact",
            "statement": "部署必须先跑迁移",
            "source_evidence_ids": [],
            "confidence_bp": 8000,
        },
        headers={**auth, "Idempotency-Key": key},
    )
    assert again.status_code == 201
    assert again.json()["data"]["id"] == memory["id"]

    # 再写一条不同 kind / statement，便于过滤与分页
    second = client.post(
        "/internal/v1/memories",
        json={
            "project_id": project_id,
            "activity_id": activity_id,
            "kind": "hypothesis",
            "statement": "可能需要回滚脚本",
            "source_evidence_ids": [],
            "confidence_bp": 4000,
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert second.status_code == 201, second.text
    third = client.post(
        "/internal/v1/memories",
        json={
            "project_id": project_id,
            "activity_id": activity_id,
            "kind": "decision",
            "statement": "采用蓝绿部署",
            "source_evidence_ids": [],
            "confidence_bp": 9000,
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert third.status_code == 201, third.text

    listed = client.get("/api/v1/memories", params={"project_id": project_id}, headers=auth)
    assert listed.status_code == 200, listed.text
    rows = listed.json()["data"]
    assert len(rows) == 3
    # 新→旧
    assert rows[0]["id"] == third.json()["data"]["id"]
    assert rows[-1]["id"] == memory["id"]

    by_kind = client.get(
        "/api/v1/memories",
        params={"project_id": project_id, "kind": "hypothesis"},
        headers=auth,
    )
    assert by_kind.status_code == 200
    assert [r["kind"] for r in by_kind.json()["data"]] == ["hypothesis"]

    by_q = client.get(
        "/api/v1/memories",
        params={"project_id": project_id, "q": "迁移"},
        headers=auth,
    )
    assert by_q.status_code == 200
    assert len(by_q.json()["data"]) == 1
    assert by_q.json()["data"][0]["id"] == memory["id"]

    page1 = client.get(
        "/api/v1/memories",
        params={"project_id": project_id, "limit": 2},
        headers=auth,
    )
    assert page1.status_code == 200
    assert len(page1.json()["data"]) == 2
    cursor = page1.json()["meta"]["next_cursor"]
    assert cursor
    page2 = client.get(
        "/api/v1/memories",
        params={"project_id": project_id, "limit": 2, "cursor": cursor},
        headers=auth,
    )
    assert page2.status_code == 200
    assert len(page2.json()["data"]) == 1
    assert page2.json()["data"][0]["id"] == memory["id"]
    assert page2.json()["meta"]["next_cursor"] is None

    # 非法 activity
    bad = client.post(
        "/internal/v1/memories",
        json={
            "project_id": project_id,
            "activity_id": str(uuid4()),
            "kind": "fact",
            "statement": "无效活动",
            "source_evidence_ids": [],
            "confidence_bp": 1,
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "activity" in bad.json()["error"]["message"].lower() or "活动" in bad.json()["error"][
        "message"
    ]

    # confidence_bp 上限
    too_high = client.post(
        "/internal/v1/memories",
        json={
            "project_id": project_id,
            "activity_id": activity_id,
            "kind": "fact",
            "statement": "超限置信度",
            "source_evidence_ids": [],
            "confidence_bp": 10001,
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert too_high.status_code == 422

    # supersedes：旧行须标 SUPERSEDED
    replacement = client.post(
        "/internal/v1/memories",
        json={
            "project_id": project_id,
            "activity_id": activity_id,
            "kind": "fact",
            "statement": "部署必须先跑迁移（修订）",
            "source_evidence_ids": [],
            "confidence_bp": 8500,
            "supersedes_id": memory["id"],
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()["data"]["supersedes_id"] == memory["id"]
    listed_after = client.get("/api/v1/memories", params={"project_id": project_id}, headers=auth)
    by_id = {row["id"]: row for row in listed_after.json()["data"]}
    assert by_id[memory["id"]]["status"] == "SUPERSEDED"
    assert by_id[replacement.json()["data"]["id"]]["status"] == "PROPOSED"

    # viewer / operator 无写权限（暂限 admin）
    no_write = client.post(
        "/internal/v1/memories",
        json={
            "project_id": project_id,
            "activity_id": activity_id,
            "kind": "fact",
            "statement": "viewer 不可写",
            "source_evidence_ids": [],
            "confidence_bp": 1,
        },
        headers={
            "Authorization": "Bearer " + token(str(uuid4()), ["viewer"]),
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert no_write.status_code == 403
    op_only = client.post(
        "/internal/v1/memories",
        json={
            "project_id": project_id,
            "activity_id": activity_id,
            "kind": "fact",
            "statement": "operator 暂不可写",
            "source_evidence_ids": [],
            "confidence_bp": 1,
        },
        headers={
            "Authorization": "Bearer " + token(str(uuid4()), ["operator"]),
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert op_only.status_code == 403
    assert "admin" in op_only.json()["error"]["message"] or "记忆" in op_only.json()["error"][
        "message"
    ]