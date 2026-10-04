import json
import sqlite3

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from cairn.server.db import get_conn
from cairn.server.integration.bindings import (
    binding_for, project_role, require_unbound, verified_goal,
)
from cairn.server.integration.identity import product_mode
from cairn.server.integration.intent_bridge import canonical, digest, graph_digest, validate_candidate
from cairn.server.integration.plan_snapshot import (
    decode_snapshot, read_candidate_summary, seal_snapshot,
)
from cairn.server.integration.ring_client import (
    RingContractUnknown, RingDenied, RingUnavailable, RingWriteRejected,
    read_collection, submit_plan_candidate,
)
from cairn.server.models import (
    ConcludeRequest,
    ConcludeResponse,
    CreateIntentRequest,
    Fact,
    HeartbeatRequest,
    Intent,
    PlanCandidateRequest,
    PlanSnapshotRequest,
)
from cairn.server.services import (
    check_project_active,
    get_claimable_open_intent_or_404,
    get_releasable_open_intent_or_404,
    intent_to_model,
    next_fact_id,
    next_intent_id,
    utcnow,
    validate_facts_exist,
    validate_intent_creator_worker,
    validate_goal_not_in_sources,
)

router = APIRouter(tags=["intents"])


def _snapshot_for_request(conn: sqlite3.Connection, fingerprint: str, request_json: bytes,
                          project_id: str, ring_project_id: str, ring_goal_id: str,
                          actor: str) -> tuple[str, bytes] | None:
    row = conn.execute(
        """SELECT r.project_id, r.ring_project_id, r.ring_goal_id, r.actor,
                  r.request_json, r.snapshot_digest, s.digest, s.canonical_json,
                  s.project_id AS snapshot_project_id,
                  s.ring_project_id AS snapshot_ring_project_id,
                  s.ring_goal_id AS snapshot_ring_goal_id,
                  s.created_by AS snapshot_actor
           FROM ring_plan_snapshot_requests r
           LEFT JOIN ring_plan_snapshots s ON s.digest = r.snapshot_digest
           WHERE r.request_fingerprint = ?""",
        (fingerprint,),
    ).fetchone()
    if row is None:
        return None
    scope = (project_id, ring_project_id, ring_goal_id, actor)
    if (tuple(row[key] for key in ("project_id", "ring_project_id", "ring_goal_id", "actor")) != scope
            or bytes(row["request_json"]) != request_json):
        raise HTTPException(409, "Plan snapshot request fingerprint collision")
    if (row["digest"] != row["snapshot_digest"]
            or tuple(row[key] for key in ("snapshot_project_id", "snapshot_ring_project_id",
                                           "snapshot_ring_goal_id", "snapshot_actor")) != scope):
        raise HTTPException(503, "Stored plan snapshot request is inconsistent")
    raw = bytes(row["canonical_json"])
    decode_snapshot(raw, row["snapshot_digest"])
    return row["snapshot_digest"], raw


@router.post("/projects/{project_id}/plan-snapshots", status_code=201)
def create_plan_snapshot(project_id: str, body: PlanSnapshotRequest, request: Request):
    if not product_mode():
        raise HTTPException(404, "Ring plan snapshots are unavailable")
    principal = request.state.ring_principal
    if "operator" not in principal["roles"]:
        raise HTTPException(403, "Ring operator role required")
    actor = principal["user_id"]
    request_body = body.model_dump()
    request_json = canonical(request_body).encode("utf-8")
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if project_role(conn, project_id, actor) != "owner":
            raise HTTPException(403, "Project owner required")
        binding = binding_for(conn, project_id)
        if binding is None:
            raise HTTPException(404, "Ring binding not found")
        if binding["ring_project_id"] not in principal["project_ids"]:
            raise HTTPException(404, "Project not found")
        ring_project_id, ring_goal_id = binding["ring_project_id"], binding["ring_goal_id"]
        fingerprint = digest({"schema": "CairnPlanSnapshotRequest/v1",
                              "project_id": project_id, "ring_project_id": ring_project_id,
                              "ring_goal_id": ring_goal_id, "actor": actor,
                              "body": request_body})
        sealed = _snapshot_for_request(conn, fingerprint, request_json, project_id,
                                       ring_project_id, ring_goal_id, actor)
        if sealed is not None:
            snapshot_digest, raw = sealed
            return {"digest": snapshot_digest, "snapshot": decode_snapshot(raw, snapshot_digest)}
    candidate = read_candidate_summary(
        request.state.ring_config, request.state.ring_cookie,
        ring_goal_id, ring_project_id, body.candidate_plan_id,
    )
    goal, _ = verified_goal(
        request.state.ring_config, request.state.ring_cookie, ring_project_id, ring_goal_id,
    )
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if project_role(conn, project_id, actor) != "owner":
            raise HTTPException(403, "Project owner required")
        binding = binding_for(conn, project_id)
        if (binding is None or binding["ring_project_id"] != ring_project_id
                or binding["ring_goal_id"] != ring_goal_id):
            raise HTTPException(409, "Project binding changed")
        if ring_project_id not in principal["project_ids"]:
            raise HTTPException(404, "Project not found")
        sealed = _snapshot_for_request(conn, fingerprint, request_json, project_id,
                                       ring_project_id, ring_goal_id, actor)
        if sealed is not None:
            snapshot_digest, raw = sealed
            return {"digest": snapshot_digest, "snapshot": decode_snapshot(raw, snapshot_digest)}
        check_project_active(conn, project_id)
        snapshot_digest, raw = seal_snapshot(
            conn, project_id, binding, actor, body, goal, candidate,
        )
        existing = conn.execute(
            """SELECT project_id, ring_project_id, ring_goal_id, created_by, canonical_json
               FROM ring_plan_snapshots WHERE digest=?""", (snapshot_digest,)
        ).fetchone()
        if existing is not None and (
            bytes(existing["canonical_json"]) != raw
            or tuple(existing[key] for key in ("project_id", "ring_project_id", "ring_goal_id", "created_by"))
            != (project_id, ring_project_id, ring_goal_id, actor)
        ):
            raise HTTPException(409, "Plan snapshot digest collision")
        if existing is None:
            conn.execute(
                """INSERT INTO ring_plan_snapshots
                   (digest, project_id, ring_project_id, ring_goal_id, created_by, canonical_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (snapshot_digest, project_id, ring_project_id, ring_goal_id,
                 actor, raw, utcnow()),
            )
        conn.execute(
            """INSERT INTO ring_plan_snapshot_requests
               (request_fingerprint, project_id, ring_project_id, ring_goal_id,
                actor, request_json, snapshot_digest)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (fingerprint, project_id, ring_project_id, ring_goal_id, actor,
             request_json, snapshot_digest),
        )
    return {"digest": snapshot_digest, "snapshot": decode_snapshot(raw, snapshot_digest)}


@router.get("/projects/{project_id}/plan-snapshots/{snapshot_digest}")
def get_plan_snapshot(project_id: str, snapshot_digest: str, request: Request):
    if not product_mode():
        raise HTTPException(404, "Ring plan snapshots are unavailable")
    principal = request.state.ring_principal
    with get_conn() as conn:
        if project_role(conn, project_id, principal["user_id"]) is None:
            raise HTTPException(404, "Project not found")
        binding = binding_for(conn, project_id)
        if binding is None or binding["ring_project_id"] not in principal["project_ids"]:
            raise HTTPException(404, "Project not found")
        row = conn.execute(
            """SELECT canonical_json FROM ring_plan_snapshots
               WHERE project_id=? AND ring_project_id=? AND ring_goal_id=? AND digest=?""",
            (project_id, binding["ring_project_id"], binding["ring_goal_id"], snapshot_digest),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Plan snapshot not found")
        raw = bytes(row["canonical_json"])
    decode_snapshot(raw, snapshot_digest)
    return Response(content=raw, media_type="application/json",
                    headers={"X-Content-Digest": snapshot_digest})


@router.post(
    "/projects/{project_id}/intents",
    response_model=Intent,
    status_code=201,
)
def create_intent(project_id: str, body: CreateIntentRequest, request: Request):
    with get_conn() as conn:
        binding = binding_for(conn, project_id)
        if binding:
            if not product_mode() or body.worker is not None:
                raise HTTPException(409, "Ring Intent must remain an unclaimed proposal")
            creator = request.state.ring_principal["user_id"]
        else:
            creator = body.creator
        check_project_active(conn, project_id)
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)
        validate_intent_creator_worker(creator, body.worker)

        now = utcnow()
        iid = next_intent_id(conn, project_id)
        claimed = body.worker is not None
        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL)",
            (
                iid,
                project_id,
                body.description,
                creator,
                body.worker,
                now if claimed else None,
                now,
            ),
        )
        for fid in body.from_:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (iid, project_id, fid),
            )

        return Intent(
            id=iid,
            **{"from": body.from_},
            to=None,
            description=body.description,
            creator=creator,
            worker=body.worker,
            last_heartbeat_at=now if claimed else None,
            created_at=now,
            concluded_at=None,
        )


@router.get("/projects/{project_id}/plan-context")
def plan_context(project_id: str, request: Request):
    if not product_mode():
        raise HTTPException(404, "Ring plan context is unavailable")
    with get_conn() as conn:
        binding = binding_for(conn, project_id)
        if binding is None:
            raise HTTPException(404, "Ring binding not found")
        graph = graph_digest(conn, project_id)
    goal, _ = verified_goal(request.state.ring_config, request.state.ring_cookie,
                            binding["ring_project_id"], binding["ring_goal_id"])
    return {
        "graph_digest": graph,
        "goal_contract_revision": goal.get("contract_revision"),
        "goal_contract_digest": goal.get("contract_digest"),
        "expected_plan_revision": goal.get("plan_revision"),
        "goal_criteria": goal.get("contract", {}).get("success_criteria"),
        "goal_budget": goal.get("contract", {}).get("budget"),
    }


@router.get("/projects/{project_id}/plan-candidates")
def list_plan_candidates(project_id: str, request: Request):
    if not product_mode():
        raise HTTPException(404, "Ring plan candidates are unavailable")
    with get_conn() as conn:
        binding = binding_for(conn, project_id)
        if binding is None:
            raise HTTPException(404, "Ring binding not found")
        rows = conn.execute(
            """SELECT intent_id, state, detail, ring_plan_id, request_digest, updated_at
               FROM ring_plan_submissions WHERE project_id = ? ORDER BY created_at""",
            (project_id,),
        ).fetchall()
        items = [dict(row) for row in rows]
    if items:
        try:
            # This Ring endpoint is scoped by goal_id; it accepts limit/cursor, not project_id.
            plans = read_collection(request.state.ring_config, request.state.ring_cookie,
                                    f"/api/v1/goals/{binding['ring_goal_id']}/plans")
            by_id = {plan.get("id"): plan for plan in plans}
            for item in items:
                plan = by_id.get(item["ring_plan_id"])
                item["ring_state"] = plan.get("status") if plan else "UNKNOWN"
                item["ring_plan_revision"] = plan.get("plan_revision") if plan else None
        except (RingDenied, RingUnavailable, RingContractUnknown):
            for item in items:
                item["ring_state"] = "UNKNOWN"
    return items


@router.post("/projects/{project_id}/intents/{intent_id}/plan-candidate")
def submit_intent_candidate(project_id: str, intent_id: str, body: PlanCandidateRequest, request: Request):
    if not product_mode():
        raise HTTPException(404, "Ring candidate bridge is unavailable")
    principal = request.state.ring_principal
    if "operator" not in principal["roles"]:
        raise HTTPException(403, "Ring operator role required")
    with get_conn() as conn:
        binding = binding_for(conn, project_id)
        if binding is None:
            raise HTTPException(404, "Ring binding not found")
        intent = conn.execute(
            "SELECT * FROM intents WHERE project_id = ? AND id = ?", (project_id, intent_id)
        ).fetchone()
        if intent is None:
            raise HTTPException(404, "Intent not found")
        if intent["worker"] is not None or intent["concluded_at"] is not None:
            raise HTTPException(409, "Only an unclaimed Intent proposal can be submitted")
        source_ids = [row["fact_id"] for row in conn.execute(
            "SELECT fact_id FROM intent_sources WHERE project_id = ? AND intent_id = ? ORDER BY fact_id",
            (project_id, intent_id),
        )]
        if not source_ids:
            raise HTTPException(422, "Intent has no source facts")
        existing = conn.execute(
            "SELECT * FROM ring_plan_submissions WHERE project_id = ? AND intent_id = ?",
            (project_id, intent_id),
        ).fetchone()
        current_graph = graph_digest(conn, project_id)
    if existing:
        if existing["submitted_by"] != principal["user_id"]:
            raise HTTPException(403, "Original Ring operator must reconcile this candidate")
        if body.graph_digest != current_graph or digest(body.model_dump()) != existing["request_digest"]:
            raise HTTPException(409, "Intent already has a different candidate request")
        if existing["state"] != "UNKNOWN":
            return {k: existing[k] for k in ("intent_id", "state", "detail", "ring_plan_id", "request_digest", "updated_at")}
        return reconcile_intent_candidate(project_id, intent_id, request)
    else:
        if body.graph_digest != current_graph:
            raise HTTPException(409, "Cairn graph changed; reload plan context")
        goal, _ = verified_goal(request.state.ring_config, request.state.ring_cookie,
                                binding["ring_project_id"], binding["ring_goal_id"])
        if goal.get("contract_revision") != body.goal_contract_revision or goal.get("contract_digest") != body.goal_contract_digest:
            raise HTTPException(409, "Ring Goal contract version changed")
        reason = body.plan.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise HTTPException(422, "Plan reason is required")
        source = {"schema": "CairnSource/v1", "project_id": project_id,
                  "intent_id": intent_id, "fact_ids": source_ids, "graph_digest": current_graph,
                  "goal_contract_revision": body.goal_contract_revision,
                  "goal_contract_digest": body.goal_contract_digest}
        plan_body = dict(body.plan)
        plan_body["reason"] = canonical(source) + "\n" + reason.strip()
        if len(plan_body["reason"]) > 10000:
            raise HTTPException(422, "Plan reason and source exceed Ring limit")
        key = "cairn-plan-" + digest([binding["ring_goal_id"], project_id, intent_id]).split(":", 1)[1]
        now = utcnow()
        with get_conn() as conn:
            if graph_digest(conn, project_id) != current_graph:
                raise HTTPException(409, "Cairn graph changed; reload plan context")
            try:
                conn.execute(
                    """INSERT INTO ring_plan_submissions
                       (project_id, intent_id, submitted_by, request_digest, idempotency_key, request_body,
                        state, detail, ring_plan_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'UNKNOWN', 'Ring result pending', NULL, ?, ?)""",
                    (project_id, intent_id, principal["user_id"], digest(body.model_dump()), key, canonical(plan_body), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, "Intent candidate submission is already in progress") from exc
        try:
            validate_candidate(request.state.ring_config, request.state.ring_cookie, goal, plan_body)
        except HTTPException as exc:
            now = utcnow()
            with get_conn() as conn:
                conn.execute(
                    """UPDATE ring_plan_submissions SET state = 'REJECTED', detail = ?, updated_at = ?
                       WHERE project_id = ? AND intent_id = ?""",
                    (str(exc.detail), now, project_id, intent_id),
                )
            return {"intent_id": intent_id, "state": "REJECTED", "detail": str(exc.detail),
                    "ring_plan_id": None, "request_digest": digest(body.model_dump()), "updated_at": now}
        except (RingDenied, RingUnavailable, RingContractUnknown):
            with get_conn() as conn:
                conn.execute(
                    """UPDATE ring_plan_submissions SET detail = ? WHERE project_id = ? AND intent_id = ?""",
                    ("Ring validation context unavailable; reconcile with the same key", project_id, intent_id),
                )
            return {"intent_id": intent_id, "state": "UNKNOWN",
                    "detail": "Ring validation context unavailable; reconcile with the same key",
                    "ring_plan_id": None, "request_digest": digest(body.model_dump()), "updated_at": now}
        with get_conn() as conn:
            conn.execute(
                "UPDATE ring_plan_submissions SET validated = 1 WHERE project_id = ? AND intent_id = ?",
                (project_id, intent_id),
            )
    try:
        result = submit_plan_candidate(request.state.ring_config, request.state.ring_cookie,
                                       principal["csrf_token"], binding["ring_goal_id"], key, plan_body)
        state, detail, plan_id = "CANDIDATE", "Ring accepted a plan candidate; publication is pending", result["id"]
    except RingWriteRejected as exc:
        state, detail, plan_id = "REJECTED", str(exc), None
    except (RingUnavailable, RingContractUnknown):
        state, detail, plan_id = "UNKNOWN", "Ring result unknown; reconcile with the same key", None
    now = utcnow()
    with get_conn() as conn:
        conn.execute(
            """UPDATE ring_plan_submissions SET state = ?, detail = ?, ring_plan_id = ?, updated_at = ?
               WHERE project_id = ? AND intent_id = ? AND state = 'UNKNOWN'""",
            (state, detail, plan_id, now, project_id, intent_id),
        )
    return {"intent_id": intent_id, "state": state, "detail": detail,
            "ring_plan_id": plan_id, "request_digest": digest(body.model_dump()), "updated_at": now}


@router.post("/projects/{project_id}/intents/{intent_id}/plan-candidate/reconcile")
def reconcile_intent_candidate(project_id: str, intent_id: str, request: Request):
    if not product_mode():
        raise HTTPException(404, "Ring candidate bridge is unavailable")
    principal = request.state.ring_principal
    if "operator" not in principal["roles"]:
        raise HTTPException(403, "Ring operator role required")
    with get_conn() as conn:
        binding = binding_for(conn, project_id)
        row = conn.execute(
            "SELECT * FROM ring_plan_submissions WHERE project_id = ? AND intent_id = ?",
            (project_id, intent_id),
        ).fetchone()
    if binding is None or row is None:
        raise HTTPException(404, "Candidate submission not found")
    if row["submitted_by"] != principal["user_id"]:
        raise HTTPException(403, "Original Ring operator must reconcile this candidate")
    if row["state"] != "UNKNOWN":
        return {k: row[k] for k in ("intent_id", "state", "detail", "ring_plan_id", "request_digest", "updated_at")}
    plan_body = json.loads(row["request_body"])
    if not row["validated"]:
        goal, _ = verified_goal(request.state.ring_config, request.state.ring_cookie,
                                binding["ring_project_id"], binding["ring_goal_id"])
        try:
            validate_candidate(request.state.ring_config, request.state.ring_cookie, goal, plan_body)
        except HTTPException as exc:
            now = utcnow()
            with get_conn() as conn:
                conn.execute(
                    """UPDATE ring_plan_submissions SET state = 'REJECTED', detail = ?, updated_at = ?
                       WHERE project_id = ? AND intent_id = ?""",
                    (str(exc.detail), now, project_id, intent_id),
                )
            return {"intent_id": intent_id, "state": "REJECTED", "detail": str(exc.detail),
                    "ring_plan_id": None, "request_digest": row["request_digest"], "updated_at": now}
        except (RingDenied, RingUnavailable, RingContractUnknown):
            return {"intent_id": intent_id, "state": "UNKNOWN",
                    "detail": "Ring validation context unavailable; reconcile with the same key",
                    "ring_plan_id": None, "request_digest": row["request_digest"], "updated_at": row["updated_at"]}
        with get_conn() as conn:
            conn.execute(
                "UPDATE ring_plan_submissions SET validated = 1 WHERE project_id = ? AND intent_id = ?",
                (project_id, intent_id),
            )
    try:
        result = submit_plan_candidate(request.state.ring_config, request.state.ring_cookie,
                                       principal["csrf_token"], binding["ring_goal_id"],
                                       row["idempotency_key"], plan_body)
        state, detail, plan_id = "CANDIDATE", "Ring accepted a plan candidate; publication is pending", result["id"]
    except RingWriteRejected as exc:
        state, detail, plan_id = "REJECTED", str(exc), None
    except (RingUnavailable, RingContractUnknown):
        state, detail, plan_id = "UNKNOWN", "Ring result unknown; reconcile with the same key", None
    now = utcnow()
    with get_conn() as conn:
        conn.execute(
            """UPDATE ring_plan_submissions SET state = ?, detail = ?, ring_plan_id = ?, updated_at = ?
               WHERE project_id = ? AND intent_id = ? AND state = 'UNKNOWN'""",
            (state, detail, plan_id, now, project_id, intent_id),
        )
    return {"intent_id": intent_id, "state": state, "detail": detail,
            "ring_plan_id": plan_id, "request_digest": row["request_digest"], "updated_at": now}


@router.post(
    "/projects/{project_id}/intents/{intent_id}/heartbeat",
    response_model=Intent,
)
def heartbeat(project_id: str, intent_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        require_unbound(conn, project_id)
        check_project_active(conn, project_id)
        get_claimable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        now = utcnow()
        conn.execute(
            "UPDATE intents SET worker = ?, last_heartbeat_at = ? WHERE id = ? AND project_id = ?",
            (body.worker, now, intent_id, project_id),
        )

        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        return intent_to_model(conn, updated, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/release",
    response_model=Intent,
)
def release(project_id: str, intent_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        require_unbound(conn, project_id)
        check_project_active(conn, project_id)
        row = get_releasable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        if row["worker"] == body.worker:
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE id = ? AND project_id = ?",
                (intent_id, project_id),
            )
            row = conn.execute(
                "SELECT * FROM intents WHERE id = ? AND project_id = ?",
                (intent_id, project_id),
            ).fetchone()

        return intent_to_model(conn, row, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/conclude",
    response_model=ConcludeResponse,
)
def conclude(project_id: str, intent_id: str, body: ConcludeRequest):
    with get_conn() as conn:
        require_unbound(conn, project_id)
        check_project_active(conn, project_id)
        get_claimable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        now = utcnow()
        fid = next_fact_id(conn, project_id)

        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            (fid, project_id, body.description),
        )
        conn.execute(
            "UPDATE intents SET to_fact_id = ?, worker = ?, last_heartbeat_at = ?, concluded_at = ? WHERE id = ? AND project_id = ?",
            (fid, body.worker, now, now, intent_id, project_id),
        )

        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()

        return ConcludeResponse(
            fact=Fact(id=fid, description=body.description),
            intent=intent_to_model(conn, updated, project_id),
        )
