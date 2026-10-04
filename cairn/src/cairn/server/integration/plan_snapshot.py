"""Seal bounded, untrusted Cairn graph proposals for a later Ring PLAN input."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from cairn.server.integration.intent_bridge import canonical, graph_digest
from cairn.server.integration.ring_client import (
    RingConfig, RingContractUnknown, RingDenied, RingUnavailable, read_collection,
)
from cairn.server.models import PlanSnapshotRequest

MAX_TEXT_BYTES = 2000
MAX_SNAPSHOT_BYTES = 8192  # UTF-8 byte count is a conservative token upper bound.
MAX_TASKS = 32
MAX_COVERAGE = 64
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET = re.compile(
    r"(?i)(?:bearer\s+\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(?:api[_ -]?key|secret|password|cookie|access[_ -]?token|refresh[_ -]?token)\s*[:=]\s*\S+)"
)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise HTTPException(422, f"{name} must be nonempty text")
    if len(value.encode("utf-8")) > MAX_TEXT_BYTES or any(ord(c) < 32 for c in value):
        raise HTTPException(422, f"{name} exceeds text bounds")
    if _SECRET.search(value):
        raise HTTPException(422, f"{name} contains credential-like text")
    return value


def _ids(values: list[str], name: str, *, required: bool = False) -> list[str]:
    if required and not values:
        raise HTTPException(422, f"{name} must not be empty")
    if any(not isinstance(value, str) or not value or len(value) > 100 for value in values):
        raise HTTPException(422, f"{name} contains an invalid ID")
    if len(set(values)) != len(values):
        raise HTTPException(422, f"{name} contains duplicate IDs")
    return sorted(values)


def _candidate_summary(plan: dict[str, Any], goal_id: str, project_id: str) -> dict[str, Any]:
    if (plan.get("goal_id") != goal_id or plan.get("project_id") != project_id
            or plan.get("status") != "CANDIDATE" or plan.get("plan_revision") is not None
            or not isinstance(plan.get("content_digest"), str)
            or _DIGEST.fullmatch(plan["content_digest"]) is None):
        raise HTTPException(409, "Ring candidate is no longer a current candidate")
    tasks = plan.get("tasks")
    coverage = plan.get("coverage")
    if (not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS
            or not isinstance(coverage, list) or not 1 <= len(coverage) <= MAX_COVERAGE):
        raise HTTPException(422, "Ring candidate exceeds summary bounds")
    task_items: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("contract"), dict):
            raise HTTPException(503, "Ring candidate shape is unknown")
        contract = task["contract"]
        if not isinstance(task.get("id"), str) or not task["id"]:
            raise HTTPException(503, "Ring candidate task ID is unknown")
        if not isinstance(contract.get("depends_on"), list):
            raise HTTPException(503, "Ring candidate dependencies are unknown")
        task_items.append({
            "id": task["id"],
            "objective": _text(contract.get("objective"), "Candidate objective"),
            "depends_on": _ids(contract["depends_on"], "Candidate dependencies"),
        })
    if len({item["id"] for item in task_items}) != len(task_items):
        raise HTTPException(503, "Ring candidate task IDs are ambiguous")
    coverage_items: list[dict[str, str]] = []
    for entry in coverage:
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(key), str) and entry[key] for key in
            ("goal_criterion_id", "task_id", "task_acceptance_id", "verification_profile_id")
        ):
            raise HTTPException(503, "Ring candidate coverage is unknown")
        coverage_items.append({key: entry[key] for key in
                               ("goal_criterion_id", "task_id", "task_acceptance_id", "verification_profile_id")})
    known_tasks = {item["id"] for item in task_items}
    if any(entry["task_id"] not in known_tasks for entry in coverage_items):
        raise HTTPException(503, "Ring candidate coverage references an unknown task")
    return {
        "task_count": len(task_items),
        "tasks": sorted(task_items, key=lambda item: item["id"]),
        "coverage": sorted(coverage_items, key=lambda item: tuple(item.values())),
    }


def read_candidate_summary(
    config: RingConfig, cookie: str, goal_id: str, project_id: str, candidate_id: str,
) -> tuple[str, dict[str, Any]]:
    try:
        candidate_id = str(UUID(candidate_id))
    except ValueError as exc:
        raise HTTPException(422, "Candidate ID must be a UUID") from exc
    try:
        plans = read_collection(config, cookie, f"/api/v1/goals/{goal_id}/plans")
    except RingDenied as exc:
        raise HTTPException(404, "Ring candidate not visible") from exc
    except (RingUnavailable, RingContractUnknown) as exc:
        raise HTTPException(503, "Ring candidate state is UNKNOWN") from exc
    plan = next((item for item in plans if item.get("id") == candidate_id), None)
    if plan is None:
        raise HTTPException(409, "Ring candidate is no longer visible")
    summary = _candidate_summary(plan, goal_id, project_id)
    return plan["content_digest"], summary


def seal_snapshot(
    conn: sqlite3.Connection, project_id: str, binding: sqlite3.Row,
    actor: str, body: PlanSnapshotRequest, goal: dict[str, Any],
    candidate: tuple[str, dict[str, Any]],
) -> tuple[str, bytes]:
    """Read graph and sources under the caller's SQLite write transaction, then seal."""
    current_graph = graph_digest(conn, project_id)
    if current_graph != body.graph_digest:
        raise HTTPException(409, "Cairn graph changed; reload plan context")
    if (goal.get("contract_revision") != body.goal_contract_revision
            or goal.get("contract_digest") != body.goal_contract_digest
            or goal.get("plan_revision") != body.expected_plan_revision):
        raise HTTPException(409, "Ring Goal version changed")
    if goal.get("status") not in {"PLANNING", "RUNNING"}:
        raise HTTPException(409, "Ring Goal is not accepting plan proposals")
    intent = conn.execute(
        "SELECT description, worker, concluded_at FROM intents WHERE project_id=? AND id=?",
        (project_id, body.selected_intent_id),
    ).fetchone()
    if intent is None:
        raise HTTPException(404, "Intent not found")
    if intent["worker"] is not None or intent["concluded_at"] is not None:
        raise HTTPException(409, "Intent is already claimed or concluded")
    source_ids = [row["fact_id"] for row in conn.execute(
        "SELECT fact_id FROM intent_sources WHERE project_id=? AND intent_id=? ORDER BY fact_id",
        (project_id, body.selected_intent_id),
    )]
    selected_facts = _ids(body.fact_ids, "fact_ids", required=True)
    if source_ids != selected_facts:
        raise HTTPException(409, "Selected facts must equal current Intent sources")
    selected_hints = _ids(body.hint_ids, "hint_ids")
    texts = [{"id": body.selected_intent_id, "text": _text(intent["description"], "Intent"),
              "source_kind": "intent"}]
    for fact_id in selected_facts:
        row = conn.execute(
            "SELECT description FROM facts WHERE project_id=? AND id=?", (project_id, fact_id)
        ).fetchone()
        if row is None:
            raise HTTPException(409, "Intent source Fact is missing")
        texts.append({"id": fact_id, "text": _text(row["description"], "Fact"),
                      "source_kind": "fact"})
    for hint_id in selected_hints:
        row = conn.execute(
            "SELECT content FROM hints WHERE project_id=? AND id=?", (project_id, hint_id)
        ).fetchone()
        if row is None:
            raise HTTPException(409, "Selected Hint is missing")
        texts.append({"id": hint_id, "text": _text(row["content"], "Hint"),
                      "source_kind": "hint"})
    row = conn.execute(
        "SELECT state, ring_plan_id FROM ring_plan_submissions WHERE project_id=? AND intent_id=?",
        (project_id, body.selected_intent_id),
    ).fetchone()
    if (row is None or row["state"] != "CANDIDATE"
            or row["ring_plan_id"] != body.candidate_plan_id):
        raise HTTPException(409, "Candidate is not the selected Intent's B2 candidate")
    payload = {
        "schema": "PlanInput/v1",
        "cairn_project_id": project_id,
        "ring_project_id": binding["ring_project_id"],
        "ring_goal_id": binding["ring_goal_id"],
        "graph_digest": current_graph,
        "selected_intent_id": body.selected_intent_id,
        "source_fact_ids": selected_facts,
        "hint_ids": selected_hints,
        "selected_text": texts,
        "candidate_plan_id": body.candidate_plan_id,
        "candidate_content_digest": candidate[0],
        "candidate_summary": candidate[1],
        "goal_contract_revision": body.goal_contract_revision,
        "goal_contract_digest": body.goal_contract_digest,
        "expected_plan_revision": body.expected_plan_revision,
        "created_by": actor,
    }
    raw = canonical(payload).encode("utf-8")
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise HTTPException(422, "Plan snapshot exceeds byte and token bounds")
    return "sha256:" + hashlib.sha256(raw).hexdigest(), raw


def decode_snapshot(raw: bytes, expected_digest: str) -> dict[str, Any]:
    if "sha256:" + hashlib.sha256(raw).hexdigest() != expected_digest:
        raise HTTPException(503, "Stored plan snapshot digest is invalid")
    return json.loads(raw)
