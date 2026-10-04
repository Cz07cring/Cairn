import sqlite3

from fastapi import APIRouter, HTTPException, Request

from cairn.server.db import get_conn
from cairn.server.models import (
    CompleteRequest,
    CreateProjectRequest,
    Fact,
    Hint,
    HeartbeatRequest,
    Intent,
    ProjectDetail,
    ProjectMeta,
    ProjectMemberRequest,
    ProjectSummary,
    ReopenRequest,
    ReopenResponse,
    ReasonClaimRequest,
    RingBindingRequest,
    RingBindingResponse,
    UpdateProjectTitleRequest,
    UpdateProjectStatusRequest,
)
from cairn.server.services import (
    build_intents,
    check_project_completed,
    check_project_active,
    clear_project_reason,
    expire_reason_leases,
    expire_workers,
    get_completion_intent_or_409,
    get_project_or_404,
    intent_to_model,
    next_fact_id,
    next_hint_id,
    next_intent_id,
    next_project_id,
    project_meta_from_row,
    project_reason_from_row,
    utcnow,
    validate_facts_exist,
    validate_goal_not_in_sources,
)
from cairn.server.integration.bindings import (
    binding_for,
    canonical_uuid,
    project_role,
    require_project_access,
    require_unbound,
    ring_status,
    verified_goal,
)
from cairn.server.integration.identity import product_mode

router = APIRouter(tags=["projects"])


@router.get("/projects", response_model=list[ProjectSummary])
def list_projects(request: Request):
    with get_conn() as conn:
        expire_workers(conn)
        expire_reason_leases(conn)
        query = """
            SELECT p.*,
                CASE WHEN b.cairn_project_id IS NULL THEN 'standalone' ELSE 'ring' END AS execution_mode,
                b.ring_project_id, b.ring_goal_id,
                a.role AS access_role,
                (SELECT COUNT(*) FROM facts WHERE project_id = p.id) AS fact_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id) AS intent_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NOT NULL) AS working_intent_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NULL) AS unclaimed_intent_count,
                (SELECT COUNT(*) FROM hints WHERE project_id = p.id) AS hint_count
            FROM projects p
            LEFT JOIN ring_bindings b ON b.cairn_project_id = p.id
            LEFT JOIN project_acl a ON a.project_id = p.id AND a.user_id = ?
            {filter_clause}
            ORDER BY p.created_at
        """
        if product_mode():
            rows = conn.execute(
                query.format(filter_clause="WHERE a.role IS NOT NULL"),
                (request.state.ring_principal["user_id"],),
            ).fetchall()
        else:
            rows = conn.execute(query.format(filter_clause=""), ("",)).fetchall()
        if product_mode():
            ring_project_ids = set(request.state.ring_principal["project_ids"])
            rows = [
                row for row in rows
                if row["execution_mode"] != "ring" or row["ring_project_id"] in ring_project_ids
            ]
        return [
            ProjectSummary(
                id=row["id"],
                title=row["title"],
                status=row["status"],
                bootstrap_enabled=bool(row["bootstrap_enabled"]),
                created_at=row["created_at"],
                reason=project_reason_from_row(row),
                execution_mode=row["execution_mode"],
                access_role=row["access_role"] if product_mode() else None,
                fact_count=row["fact_count"],
                intent_count=row["intent_count"],
                working_intent_count=row["working_intent_count"],
                unclaimed_intent_count=row["unclaimed_intent_count"],
                hint_count=row["hint_count"],
            )
            for row in rows
        ]


@router.post("/projects", response_model=ProjectDetail, status_code=201)
def create_project(body: CreateProjectRequest, request: Request):
    with get_conn() as conn:
        pid = next_project_id(conn)
        now = utcnow()

        conn.execute(
            "INSERT INTO projects (id, title, status, bootstrap_enabled, created_at) VALUES (?, ?, 'active', ?, ?)",
            (pid, body.title, False if product_mode() else body.bootstrap_enabled, now),
        )
        if product_mode():
            conn.execute(
                "INSERT INTO project_acl (project_id, user_id, role) VALUES (?, ?, 'owner')",
                (pid, request.state.ring_principal["user_id"]),
            )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            ("origin", pid, body.origin),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            ("goal", pid, body.goal),
        )

        hints = []
        if body.hints:
            for h in body.hints:
                hid = next_hint_id(conn, pid)
                conn.execute(
                    "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
                    (hid, pid, h.content, request.state.ring_principal["user_id"] if product_mode() else h.creator, now),
                )
                hints.append(Hint(id=hid, content=h.content, creator=request.state.ring_principal["user_id"] if product_mode() else h.creator, created_at=now))

        return ProjectDetail(
            project=ProjectMeta(
                id=pid,
                title=body.title,
                status="active",
                bootstrap_enabled=False if product_mode() else body.bootstrap_enabled,
                created_at=now,
                reason=None,
                access_role="owner" if product_mode() else None,
            ),
            facts=[
                Fact(id="origin", description=body.origin),
                Fact(id="goal", description=body.goal),
            ],
            intents=[],
            hints=hints,
        )


@router.post("/projects/{project_id}/ring-binding", response_model=RingBindingResponse)
def bind_ring_goal(project_id: str, body: RingBindingRequest, request: Request):
    if not product_mode():
        raise HTTPException(404, "Ring binding is unavailable in standalone mode")
    ring_project_id = canonical_uuid(body.ring_project_id)
    ring_goal_id = canonical_uuid(body.ring_goal_id)
    principal = request.state.ring_principal
    if ring_project_id not in principal["project_ids"]:
        raise HTTPException(404, "Ring project not found")
    goal, snapshot = verified_goal(
        request.state.ring_config, request.state.ring_cookie, ring_project_id, ring_goal_id
    )
    with get_conn() as conn:
        require_project_access(conn, project_id, principal["user_id"], owner=True)
        row = get_project_or_404(conn, project_id)
        existing = binding_for(conn, project_id)
        if existing:
            if existing["ring_project_id"] != ring_project_id or existing["ring_goal_id"] != ring_goal_id:
                raise HTTPException(409, "Project already has a different Ring binding")
            return RingBindingResponse(**dict(existing))
        if row["status"] != "active" or row["reason_worker"] is not None:
            raise HTTPException(409, "Project must be active and idle before binding")
        open_intent = conn.execute(
            "SELECT 1 FROM intents WHERE project_id = ? AND concluded_at IS NULL LIMIT 1",
            (project_id,),
        ).fetchone()
        if open_intent:
            raise HTTPException(409, "Project has an unfinished Cairn intent")
        now = utcnow()
        try:
            conn.execute(
                """INSERT INTO ring_bindings
                   (cairn_project_id, ring_project_id, ring_goal_id, bound_by, bound_at, state_revision, latest_seq)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (project_id, ring_project_id, ring_goal_id, principal["user_id"], now, goal["state_revision"], snapshot["latest_seq"]),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Ring Goal is already bound") from exc
        conn.execute("UPDATE projects SET bootstrap_enabled = 0 WHERE id = ?", (project_id,))
        return RingBindingResponse(
            cairn_project_id=project_id,
            ring_project_id=ring_project_id,
            ring_goal_id=ring_goal_id,
            bound_by=principal["user_id"],
            bound_at=now,
            state_revision=goal["state_revision"],
            latest_seq=snapshot["latest_seq"],
        )


@router.get("/projects/{project_id}/ring-status")
def get_ring_status(project_id: str, request: Request):
    if not product_mode():
        raise HTTPException(404, "Ring status is unavailable in standalone mode")
    with get_conn() as conn:
        require_project_access(conn, project_id, request.state.ring_principal["user_id"])
        binding = binding_for(conn, project_id)
        if binding is None:
            raise HTTPException(404, "Project has no Ring binding")
        if binding["ring_project_id"] not in request.state.ring_principal["project_ids"]:
            raise HTTPException(404, "Project not found")
        return {
            "ring_project_id": binding["ring_project_id"],
            "ring_goal_id": binding["ring_goal_id"],
            **ring_status(request.state.ring_config, request.state.ring_cookie, binding),
        }


@router.get("/projects/{project_id}/members")
def list_project_members(project_id: str, request: Request):
    if not product_mode():
        raise HTTPException(404, "Project members are unavailable in standalone mode")
    with get_conn() as conn:
        require_project_access(conn, project_id, request.state.ring_principal["user_id"], owner=True)
        return [dict(row) for row in conn.execute(
            "SELECT user_id, role FROM project_acl WHERE project_id = ? ORDER BY user_id",
            (project_id,),
        ).fetchall()]


@router.put("/projects/{project_id}/members/{user_id}")
def set_project_member(project_id: str, user_id: str, body: ProjectMemberRequest, request: Request):
    if not product_mode():
        raise HTTPException(404, "Project members are unavailable in standalone mode")
    if not user_id or len(user_id) > 200:
        raise HTTPException(422, "Invalid user ID")
    with get_conn() as conn:
        require_project_access(conn, project_id, request.state.ring_principal["user_id"], owner=True)
        existing = conn.execute(
            "SELECT role FROM project_acl WHERE project_id = ? AND user_id = ?",
            (project_id, user_id),
        ).fetchone()
        if existing and existing["role"] == "owner":
            raise HTTPException(409, "Project owner cannot be changed")
        conn.execute(
            """INSERT INTO project_acl (project_id, user_id, role) VALUES (?, ?, ?)
               ON CONFLICT(project_id, user_id) DO UPDATE SET role = excluded.role""",
            (project_id, user_id, body.role),
        )
        return {"user_id": user_id, "role": body.role}


@router.delete("/projects/{project_id}/members/{user_id}", status_code=204)
def remove_project_member(project_id: str, user_id: str, request: Request):
    if not product_mode():
        raise HTTPException(404, "Project members are unavailable in standalone mode")
    with get_conn() as conn:
        require_project_access(conn, project_id, request.state.ring_principal["user_id"], owner=True)
        row = conn.execute(
            "SELECT role FROM project_acl WHERE project_id = ? AND user_id = ?",
            (project_id, user_id),
        ).fetchone()
        if row and row["role"] == "owner":
            raise HTTPException(409, "Project owner cannot be removed")
        conn.execute(
            "DELETE FROM project_acl WHERE project_id = ? AND user_id = ?",
            (project_id, user_id),
        )


@router.get("/projects/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str, request: Request):
    with get_conn() as conn:
        expire_workers(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)

        facts = conn.execute(
            "SELECT * FROM facts WHERE project_id = ?", (project_id,)
        ).fetchall()
        hints = conn.execute(
            "SELECT * FROM hints WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()

        return ProjectDetail(
            project=project_meta_from_row(row).model_copy(
                update={"access_role": project_role(conn, project_id, request.state.ring_principal["user_id"])}
            ) if product_mode() else project_meta_from_row(row),
            facts=[Fact(**dict(f)) for f in facts],
            intents=build_intents(conn, project_id),
            hints=[Hint(**dict(h)) for h in hints],
        )


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        require_unbound(conn, project_id)
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


@router.put("/projects/{project_id}/title", response_model=ProjectMeta)
def update_project_title(project_id: str, body: UpdateProjectTitleRequest, request: Request):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        conn.execute(
            "UPDATE projects SET title = ? WHERE id = ?",
            (body.title, project_id),
        )
        updated = get_project_or_404(conn, project_id)
        meta = project_meta_from_row(updated)
        if product_mode():
            meta.access_role = project_role(conn, project_id, request.state.ring_principal["user_id"])
        return meta


@router.put("/projects/{project_id}/status", response_model=ProjectMeta)
def update_project_status(project_id: str, body: UpdateProjectStatusRequest):
    with get_conn() as conn:
        require_unbound(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_status = row["status"]
        if current_status == "completed":
            raise HTTPException(409, "Completed projects cannot change status")
        if current_status == body.status:
            return project_meta_from_row(row)

        conn.execute(
            "UPDATE projects SET status = ? WHERE id = ?",
            (body.status, project_id),
        )
        if body.status == "stopped":
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE project_id = ? AND concluded_at IS NULL",
                (project_id,),
            )
            clear_project_reason(conn, project_id)
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/reason/claim", response_model=ProjectMeta)
def claim_project_reason(project_id: str, body: ReasonClaimRequest):
    with get_conn() as conn:
        require_unbound(conn, project_id)
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is not None and current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")
        if current_worker == body.worker:
            return project_meta_from_row(row)

        now = utcnow()
        conn.execute(
            """
            UPDATE projects
            SET reason_worker = ?,
                reason_trigger = ?,
                reason_started_at = ?,
                reason_last_heartbeat_at = ?
            WHERE id = ?
            """,
            (body.worker, body.trigger, now, now, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/reason/heartbeat", response_model=ProjectMeta)
def heartbeat_project_reason(project_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        require_unbound(conn, project_id)
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is None:
            raise HTTPException(409, "Project reason is not currently claimed")
        if current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")

        now = utcnow()
        conn.execute(
            "UPDATE projects SET reason_last_heartbeat_at = ? WHERE id = ?",
            (now, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/reason/release", response_model=ProjectMeta)
def release_project_reason(project_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        require_unbound(conn, project_id)
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is None:
            return project_meta_from_row(row)
        if current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")

        clear_project_reason(conn, project_id)
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/complete", response_model=Intent)
def complete_project(project_id: str, body: CompleteRequest):
    with get_conn() as conn:
        require_unbound(conn, project_id)
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)

        now = utcnow()
        iid = next_intent_id(conn, project_id)

        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, 'goal', ?, ?, ?, ?, ?, ?)",
            (iid, project_id, body.description, body.worker, body.worker, now, now, now),
        )
        for fid in body.from_:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (iid, project_id, fid),
            )
        conn.execute(
            """
            UPDATE projects
            SET status = 'completed',
                reason_worker = NULL,
                reason_trigger = NULL,
                reason_started_at = NULL,
                reason_last_heartbeat_at = NULL
            WHERE id = ?
            """,
            (project_id,),
        )

        return Intent(
            id=iid,
            **{"from": body.from_},
            to="goal",
            description=body.description,
            creator=body.worker,
            worker=body.worker,
            last_heartbeat_at=now,
            created_at=now,
            concluded_at=now,
        )


@router.post("/projects/{project_id}/reopen", response_model=ReopenResponse)
def reopen_project(project_id: str, body: ReopenRequest):
    with get_conn() as conn:
        require_unbound(conn, project_id)
        expire_reason_leases(conn, project_id)
        check_project_completed(conn, project_id)
        completion = get_completion_intent_or_409(conn, project_id)

        source_rows = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
            (completion["id"], project_id),
        ).fetchall()
        source_ids = [row["fact_id"] for row in source_rows]
        if not source_ids:
            raise HTTPException(409, "Completion intent is missing its source facts")

        now = utcnow()
        fact_id = next_fact_id(conn, project_id)
        intent_id = next_intent_id(conn, project_id)
        description = body.description
        creator = body.creator

        conn.execute(
            "DELETE FROM intents WHERE id = ? AND project_id = ?",
            (completion["id"], project_id),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            (fact_id, project_id, description),
        )
        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (intent_id, project_id, fact_id, "external_feedback", creator, creator, now, now, now),
        )
        for source_id in source_ids:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (intent_id, project_id, source_id),
            )
        clear_project_reason(conn, project_id)
        conn.execute(
            "UPDATE projects SET status = 'active' WHERE id = ?",
            (project_id,),
        )

        updated_project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        updated_intent = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        assert updated_project is not None
        assert updated_intent is not None
        return ReopenResponse(
            project=project_meta_from_row(updated_project),
            fact=Fact(id=fact_id, description=description),
            intent=intent_to_model(conn, updated_intent, project_id),
        )
