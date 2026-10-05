"""编译输入快照：无 IO，由控制面从固定 binding 装配。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

Role = Literal["PLANNER", "EXECUTOR", "AUDITOR", "SYSTEM"]
Classification = Literal["CONTRACT", "CANDIDATE", "BASELINE", "EVIDENCE", "MEMORY", "SKILL"]


@dataclass(frozen=True)
class InputArtifactRef:
    artifact_id: UUID
    digest: str
    classification: Classification


@dataclass(frozen=True)
class ActivitySnapshot:
    """一次 attempt 的固定编译输入；合同/Skill 变更须另开 attempt。"""

    project_id: UUID
    activity_id: UUID
    role: Role
    goal_id: UUID | None
    goal_contract_revision: int | None
    task_contract_revision: int | None
    plan_revision: int | None
    input_bindings: tuple[InputArtifactRef, ...] = ()
    excluded_refs: tuple[tuple[str, str], ...] = ()
    session_generation: int = 0
    compaction_source_digest: str | None = None
    planning_feedback_ids: tuple[UUID, ...] = ()


# Activity.kind → ContextBundle.role；模型不能自报身份。
KIND_TO_ROLE: dict[str, Role] = {
    "PLAN": "PLANNER",
    "EXECUTE": "EXECUTOR",
    "AUDIT": "AUDITOR",
    "FINALIZE": "AUDITOR",
    "INTEGRATE": "SYSTEM",
    "PROBE_MODEL": "SYSTEM",
    "INDEX_MEMORY": "SYSTEM",
    "VALIDATE_SKILL": "SYSTEM",
    "RECOVER_FINALIZATION": "SYSTEM",
    "STOP": "SYSTEM",
}

# Skill.role_scopes 历史取值与 Bundle role 对齐。
ROLE_SCOPE_ALIASES: dict[Role, frozenset[str]] = {
    "PLANNER": frozenset({"PLAN", "PLANNER"}),
    "EXECUTOR": frozenset({"EXECUTE", "EXECUTOR"}),
    "AUDITOR": frozenset({"AUDIT", "AUDITOR", "FINALIZE"}),
    "SYSTEM": frozenset({"SYSTEM"}),
}


def role_for_kind(kind: str) -> Role:
    try:
        return KIND_TO_ROLE[kind]
    except KeyError as exc:
        raise ValueError(f"未知 Activity kind: {kind}") from exc


def skill_allowed_for_role(role: Role, role_scopes: list[str]) -> bool:
    aliases = ROLE_SCOPE_ALIASES[role]
    return any(scope in aliases for scope in role_scopes)
