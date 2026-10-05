"""Cairn PlanInput/v1 协议（R1a）：固定字段 + 固定边界，逐字节可复现。

权威来源：Cairn fork 04a7647
``cairn/src/cairn/server/integration/plan_snapshot.py:seal_snapshot``。
Ring 只接收/校验/持久化/读取；候选正文与摘要一律以 Ring DB 重取为准，不信
Cairn 复制的 digest。R1a 不做 PLAN admit、不建 ContextBundle、不派发
Runner/Temporal、绝不写 Goal/Task DONE。
"""

import json
import re
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, StrictBool, model_validator

from .projects import Contract

PositiveInt = Annotated[int, Field(strict=True, ge=1, le=9007199254740991)]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
IdText = Annotated[str, Field(min_length=1, max_length=100)]

# 与 Cairn plan_snapshot.py 完全同值的固定边界（不得放宽）。
PLAN_INPUT_SCHEMA = "PlanInput/v1"
MAX_SNAPSHOT_BYTES = 8192
MAX_TEXT_BYTES = 2000
MAX_TASKS = 32
MAX_COVERAGE = 64

# 与 Cairn _SECRET 同口径的凭据样式拦截（cookie/key 不得混进规划输入）。
_SECRET = re.compile(
    r"(?i)(?:bearer\s+\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(?:api[_ -]?key|secret|password|cookie|access[_ -]?token|refresh[_ -]?token)\s*[:=]\s*\S+)"
)


def canonical_json(value: Any) -> str:
    """与 Cairn intent_bridge.canonical 同口径：sort_keys + 紧凑分隔符 + 非转义。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _bounded_text(value: str, name: str) -> None:
    if not value or value != value.strip():
        raise ValueError(f"{name} must be nonempty text")
    if len(value.encode("utf-8")) > MAX_TEXT_BYTES or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} exceeds text bounds")
    if _SECRET.search(value):
        raise ValueError(f"{name} contains credential-like text")


def _sorted_unique_ids(values: list[str], name: str) -> None:
    if any(not isinstance(v, str) or not v or len(v) > 100 for v in values):
        raise ValueError(f"{name} contains an invalid ID")
    if len(set(values)) != len(values):
        raise ValueError(f"{name} contains duplicate IDs")
    if values != sorted(values):
        raise ValueError(f"{name} must be sorted")


class SelectedText(Contract):
    id: IdText
    text: str
    source_kind: Literal["intent", "fact", "hint"]


class CandidateTaskSummary(Contract):
    id: str = Field(min_length=1, max_length=100)
    objective: str
    depends_on: list[str] = Field(max_length=MAX_TASKS)


class CandidateCoverageSummary(Contract):
    goal_criterion_id: str = Field(min_length=1, max_length=10000)
    task_id: str = Field(min_length=1, max_length=100)
    task_acceptance_id: str = Field(min_length=1, max_length=10000)
    verification_profile_id: str = Field(min_length=1, max_length=100)


class CandidateSummary(Contract):
    task_count: PositiveInt
    tasks: list[CandidateTaskSummary] = Field(min_length=1, max_length=MAX_TASKS)
    coverage: list[CandidateCoverageSummary] = Field(min_length=1, max_length=MAX_COVERAGE)


class PlanInputPayload(Contract):
    """Cairn seal_snapshot 的固定 payload；字段集合与边界逐一对齐，禁止增删。

    线上字段名必须是 `schema`（与 Cairn canonical 字节一致）；Python 属性改名
    为 protocol_schema 以免遮蔽 BaseModel.schema，序列化统一走 by_alias。
    """

    model_config = ConfigDict(extra="forbid")

    protocol_schema: Literal["PlanInput/v1"] = Field(alias="schema")
    cairn_project_id: str = Field(min_length=1, max_length=200)
    ring_project_id: UUID
    ring_goal_id: UUID
    graph_digest: Digest
    selected_intent_id: IdText
    # Cairn PlanSnapshotRequest 限定 Fact/Hint 各最多 32 条；文本另含一个 Intent。
    source_fact_ids: list[IdText] = Field(min_length=1, max_length=32)
    hint_ids: list[IdText] = Field(max_length=32)
    selected_text: list[SelectedText] = Field(min_length=2, max_length=65)
    candidate_plan_id: UUID
    candidate_content_digest: Digest
    candidate_summary: CandidateSummary
    goal_contract_revision: PositiveInt
    goal_contract_digest: Digest
    expected_plan_revision: PositiveInt | None
    created_by: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_bounds(self):
        _sorted_unique_ids(self.source_fact_ids, "source_fact_ids")
        _sorted_unique_ids(self.hint_ids, "hint_ids")
        expected_text_ids = (
            [("intent", self.selected_intent_id)]
            + [("fact", item_id) for item_id in self.source_fact_ids]
            + [("hint", item_id) for item_id in self.hint_ids]
        )
        if [(item.source_kind, item.id) for item in self.selected_text] != expected_text_ids:
            raise ValueError("selected_text must match the selected Intent, Facts and Hints")
        _bounded_text(self.selected_intent_id, "selected_intent_id")
        _bounded_text(self.created_by, "created_by")
        _bounded_text(self.cairn_project_id, "cairn_project_id")
        for item in self.selected_text:
            _bounded_text(item.text, "selected_text.text")
        for task in self.candidate_summary.tasks:
            _bounded_text(task.objective, "candidate objective")
            _sorted_unique_ids(task.depends_on, "candidate dependencies")
        if self.candidate_summary.task_count != len(self.candidate_summary.tasks):
            raise ValueError("candidate_summary.task_count mismatch")
        task_ids = [task.id for task in self.candidate_summary.tasks]
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("candidate_summary task IDs are ambiguous")
        if any(c.task_id not in set(task_ids) for c in self.candidate_summary.coverage):
            raise ValueError("candidate_summary coverage references an unknown task")
        # 总字节闸门：与 Cairn 同一 canonical 口径复算，超限拒绝、不裁剪。
        raw = canonical_json(self.model_dump(mode="json", by_alias=True)).encode("utf-8")
        if len(raw) > MAX_SNAPSHOT_BYTES:
            raise ValueError("Plan snapshot exceeds byte and token bounds")
        return self


class PlanInputResource(Contract):
    """已登记 PlanInput 的只读投影；R1a 恒 STAGED，marks_goal_done 恒 false。"""

    id: UUID
    project_id: UUID
    goal_id: UUID
    status: Literal["STAGED", "BOUND", "STALE"]
    created_by: str
    created_at: datetime
    content_digest: Digest
    candidate_plan_id: UUID
    candidate_content_digest: Digest
    goal_contract_revision: PositiveInt
    expected_plan_revision: PositiveInt | None
    payload: dict[str, Any]
    marks_goal_done: StrictBool = False
