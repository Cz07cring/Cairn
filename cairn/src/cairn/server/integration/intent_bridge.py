"""Fail-closed Cairn graph proposal to Ring PlanCreate candidate bridge."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal, InvalidOperation
from fnmatch import fnmatchcase
from typing import Any

from fastapi import HTTPException

from cairn.server.integration.ring_client import RingConfig, read_collection


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def graph_digest(conn: sqlite3.Connection, project_id: str) -> str:
    facts = [tuple(row) for row in conn.execute(
        "SELECT id, description FROM facts WHERE project_id = ? ORDER BY id", (project_id,)
    )]
    intents = [tuple(row) for row in conn.execute(
        "SELECT id, description, creator, created_at, concluded_at FROM intents WHERE project_id = ? ORDER BY id",
        (project_id,),
    )]
    sources = [tuple(row) for row in conn.execute(
        "SELECT intent_id, fact_id FROM intent_sources WHERE project_id = ? ORDER BY intent_id, fact_id",
        (project_id,),
    )]
    hints = [tuple(row) for row in conn.execute(
        "SELECT id, content, creator, created_at FROM hints WHERE project_id = ? ORDER BY id",
        (project_id,),
    )]
    return digest({"facts": facts, "intents": intents, "sources": sources, "hints": hints})


def _reject(message: str) -> None:
    raise HTTPException(422, message)


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _reject(f"{name} is missing or invalid")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        _reject(f"{name} is missing or empty")
    return value


def _string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _reject(f"{name} is missing or invalid")
    return value


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        _reject(f"{name} is missing or invalid")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        _reject(f"{name} is invalid")
    if not result.is_finite() or result < 0:
        _reject(f"{name} is invalid")
    return result


def _safe_path(path: Any) -> bool:
    return (isinstance(path, str) and bool(path) and path == path.strip()
            and "\\" not in path and not any(ord(char) < 32 for char in path)
            and all(part not in {"", ".", ".."} for part in path.split("/")))


def _configuration(
    config: RingConfig, cookie: str, project_id: str, path: str, resource_id: str,
) -> dict[str, Any]:
    for item in read_collection(config, cookie, path, project_id=project_id):
        item_config = item.get("config")
        if item.get("id") == resource_id and isinstance(item_config, dict) and item_config.get("project_id") == project_id:
            return item
    _reject(f"Ring configuration {resource_id} is missing or inaccessible")


def validate_candidate(
    config: RingConfig, cookie: str, goal: dict[str, Any], body: dict[str, Any],
) -> None:
    """Check visible policy, skill and Goal boundaries before Ring validates the full contract."""
    contract = _object(goal.get("contract"), "GoalContract")
    if not isinstance(goal.get("contract_revision"), int) or goal["contract_revision"] < 1:
        _reject("Goal contract version is unknown")
    if "expected_plan_revision" not in body or body["expected_plan_revision"] != goal.get("plan_revision"):
        raise HTTPException(409, "Ring plan revision changed")
    tasks = _list(body.get("tasks"), "Plan tasks")
    coverage = _list(body.get("coverage"), "Plan coverage")
    criteria = _list(contract.get("success_criteria"), "Goal criteria")
    goal_budget = _object(contract.get("budget"), "Goal budget")
    usage = _object(goal.get("budget_usage"), "Goal budget usage")
    if usage.get("cost_status") == "UNKNOWN":
        _reject("Goal remaining budget is unknown")
    policy_id = contract.get("policy_id")
    skill_set_id = contract.get("skill_set_id")
    if not isinstance(policy_id, str) or not isinstance(skill_set_id, str):
        _reject("Goal policy or skill set is missing")
    project_id = goal["project_id"]
    policy = _configuration(config, cookie, project_id, "/api/v1/policies", policy_id)
    skill_set = _configuration(config, cookie, project_id, "/api/v1/skill-sets", skill_set_id)
    policy_config = _object(policy.get("config"), "Ring policy")
    skill_config = _object(skill_set.get("config"), "Ring skill set")
    allowed_policy = set(_string_list(policy_config.get("allowed_paths"), "Policy allowed paths"))
    protected_policy = set(_string_list(policy_config.get("protected_paths"), "Policy protected paths"))
    skill_versions = _string_list(skill_config.get("skill_version_ids"), "Skill versions")
    if not skill_versions:
        _reject("Skill versions are empty")
    skill_ids = set(skill_versions)
    skills = read_collection(config, cookie, "/api/v1/skills", project_id=project_id)
    capabilities: set[str] = set()
    found: set[str] = set()
    for skill in skills:
        if skill.get("id") in skill_ids and skill.get("status") == "ACTIVE":
            found.add(skill["id"])
            capabilities.update(_string_list(_object(skill.get("config"), "Skill config").get("capabilities"), "Skill capabilities"))
    if found != skill_ids:
        _reject("Required Ring skill version is missing or inactive")
    criterion_by_id = {c.get("id"): c for c in criteria if isinstance(c, dict)}
    task_by_id: dict[str, dict[str, Any]] = {}
    for node in tasks:
        node = _object(node, "Task node")
        task_id = node.get("id")
        if not isinstance(task_id, str) or task_id in task_by_id:
            _reject("Task ID is missing or duplicated")
        task_by_id[task_id] = _object(node.get("contract"), "TaskContract")
        task = task_by_id[task_id]
        acceptance = _list(task.get("acceptance"), "Task acceptance")
        if not any(isinstance(a, dict) and a.get("required") is True for a in acceptance):
            _reject("Task required acceptance is missing")
        allowed = _string_list(task.get("allowed_paths"), "Task allowed paths")
        protected = _string_list(task.get("protected_paths"), "Task protected paths")
        required_caps = _string_list(task.get("required_capabilities"), "Task capabilities")
        if not all(_safe_path(path) for path in allowed + protected):
            _reject("Task path is not a canonical relative path")
        if any("*" in path or "?" in path or "[" in path for path in allowed):
            _reject("Task allowed paths must name exact files")
        if (any(not any(fnmatchcase(path, rule) for rule in allowed_policy) for path in allowed)
                or any(any(fnmatchcase(path, rule) for rule in set(protected) | protected_policy)
                       for path in allowed)):
            _reject("Task path exceeds Ring policy")
        if not protected_policy <= set(protected):
            _reject("Task protected paths omit Ring policy protections")
        if not set(required_caps) <= capabilities:
            _reject("Task capability exceeds active Ring skill set")
        budget = _object(task.get("budget"), "Task budget")
        usage_fields = {
            "wall_clock_seconds": ("elapsed_wall_seconds", None),
            "max_tokens": ("consumed_tokens", "reserved_tokens"),
            "max_cost_usd": ("consumed_cost_usd", "reserved_cost_usd"),
            "max_tool_calls": ("tool_calls", None),
            "max_network_calls": ("network_calls", None),
            "max_disk_bytes": ("disk_bytes", None),
        }
        for field, (used_field, reserved_field) in usage_fields.items():
            remaining = _decimal(goal_budget.get(field), f"Goal {field}") - _decimal(usage.get(used_field), f"Goal {used_field}")
            if reserved_field:
                remaining -= _decimal(usage.get(reserved_field), f"Goal {reserved_field}")
            if _decimal(budget.get(field), f"Task {field}") > remaining:
                _reject(f"Task budget exceeds remaining Goal {field}")
        if goal_budget.get("max_gpu_seconds") is not None and budget.get("max_gpu_seconds") is not None:
            if _decimal(budget["max_gpu_seconds"], "Task GPU budget") > _decimal(goal_budget["max_gpu_seconds"], "Goal GPU budget"):
                _reject("Task GPU budget exceeds Goal")
    covered: set[str] = set()
    for entry in coverage:
        entry = _object(entry, "Coverage entry")
        criterion = criterion_by_id.get(entry.get("goal_criterion_id"))
        task = task_by_id.get(entry.get("task_id"))
        if criterion is None or task is None:
            _reject("Coverage references unknown criterion or task")
        acceptance = next((a for a in task["acceptance"] if isinstance(a, dict)
                           and a.get("id") == entry.get("task_acceptance_id")), None)
        if not acceptance or acceptance.get("required") is not True or acceptance.get("verification_profile_id") != entry.get("verification_profile_id"):
            _reject("Coverage has no matching required Task acceptance")
        if entry["goal_criterion_id"] not in task.get("covers_goal_criterion_ids", []):
            _reject("Task does not declare Goal criterion coverage")
        covered.add(entry["goal_criterion_id"])
    profiles = read_collection(config, cookie, "/api/v1/verification-profiles", project_id=project_id)
    scopes = {profile.get("id"): profile.get("config", {}).get("target_scope")
              for profile in profiles if isinstance(profile.get("config"), dict)}
    if any(c.get("verification_profile_id") not in scopes for c in criteria):
        _reject("Goal verification profile is missing or inaccessible")
    if any(scopes[c["verification_profile_id"]] not in {"TASK", "GOAL"} for c in criteria):
        _reject("Goal verification profile scope is invalid")
    missing = {c["id"] for c in criteria if c.get("required") is True
               and scopes[c["verification_profile_id"]] != "GOAL"} - covered
    if missing:
        _reject("Required Goal criteria lack coverage: " + ", ".join(sorted(missing)))
