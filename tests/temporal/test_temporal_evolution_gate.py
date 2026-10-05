"""Temporal 演变门：patch/CAN 清单与 Workflow 非确定性静态扫描。"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from orchestration.temporal_workflows import GoalWorkflow
from temporalio import activity
from temporalio.client import WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = Path(__file__).parent / "fixtures" / "evolution" / "manifest.json"

_FORBIDDEN_IMPORT_ROOTS = {
    "aiohttp",
    "asyncpg",
    "boto3",
    "httpx",
    "os",
    "pathlib",
    "psycopg",
    "random",
    "requests",
    "secrets",
    "socket",
    "sqlalchemy",
    "subprocess",
    "time",
    "uuid",
}
_FORBIDDEN_IMPORT_PREFIXES = {
    "control_kernel.storage",
    "orchestration.client",
    "orchestration.kernel_activities",
}
_FORBIDDEN_CALLS = {
    "builtins.open",
    "datetime.datetime.now",
    "datetime.datetime.utcnow",
    "datetime.now",
    "datetime.utcnow",
    "open",
    "Path.open",
    "Path.read_bytes",
    "Path.read_text",
    "Path.write_bytes",
    "Path.write_text",
    "pathlib.Path.open",
    "pathlib.Path.read_bytes",
    "pathlib.Path.read_text",
    "pathlib.Path.write_bytes",
    "pathlib.Path.write_text",
}

_CONTROL_QUEUE = "ring-control"
_RUNNER_QUEUE = "ring-runner"
_ACTIVITY_ID = "activity-double-can"
_ATTEMPT_ID = "attempt-double-can"
_list_calls: list[str] = []
_admit_calls: list[str] = []
_run_calls: list[str] = []
_carried_observations: list[tuple[str, str, str]] = []
_execute_list_calls: list[str] = []


@activity.defn(name="ensure_goal_delivery")
async def _ensure(command_id: str, goal_id: str) -> dict[str, str]:
    return {
        "command_id": command_id,
        "workflow_id": f"goal-{goal_id}",
        "run_id": "run-double-can",
        "delivery_status": "ACKNOWLEDGED",
    }


@activity.defn(name="list_runtime_actions")
async def _list_once(goal_id: str, owner_epoch: str) -> dict:
    _list_calls.append(goal_id)
    if len(_list_calls) > 1:
        return {"actions": [], "wait_hint": {"code": "NO_READY_ACTIVITIES"}}
    return {
        "actions": [
            {
                "activity_id": _ACTIVITY_ID,
                "action_id": _ACTIVITY_ID,
                "goal_id": goal_id,
                "project_id": "project-double-can",
                "owner_epoch": owner_epoch,
                "kind": "PLAN",
            }
        ],
        "wait_hint": None,
    }


@activity.defn(name="list_runtime_actions")
async def _list_execute_once(goal_id: str, owner_epoch: str) -> dict:
    _execute_list_calls.append(goal_id)
    return {
        "actions": [
            {
                "activity_id": _ACTIVITY_ID,
                "action_id": _ACTIVITY_ID,
                "goal_id": goal_id,
                "project_id": "project-new-m3-history",
                "owner_epoch": owner_epoch,
                "kind": "EXECUTE",
            }
        ],
        "wait_hint": None,
    }


@activity.defn(name="admit_plan_action")
async def _admit(activity_id: str, idempotency_key: str) -> dict[str, str]:
    _admit_calls.append(activity_id)
    assert idempotency_key == f"goal-wf-admit:{activity_id}"
    return {
        "activity_id": activity_id,
        "attempt_id": _ATTEMPT_ID,
        "fencing_epoch": "7",
    }


@activity.defn(name="admit_execute_action")
async def _admit_execute(activity_id: str, idempotency_key: str) -> dict[str, str]:
    return await _admit(activity_id, idempotency_key)


@activity.defn(name="RunActivation")
async def _run_activation(payload: dict) -> dict:
    _run_calls.append(str(payload.get("attempt_id") or ""))
    return {
        "status": "ACTIVATION_SUBMITTED",
        "pending_harness": False,
        "kind": str(payload.get("kind") or ""),
    }


@activity.defn(name="observe_activity_status")
async def _observe_running(activity_id: str) -> dict[str, str]:
    return {"activity_id": activity_id, "status": "RUNNING", "kind": "PLAN"}


@activity.defn(name="observe_activity_status")
async def _observe_succeeded(activity_id: str) -> dict[str, str]:
    return {"activity_id": activity_id, "status": "SUCCEEDED", "kind": "EXECUTE"}


@activity.defn(name="observe_carried_activation_activity_status")
async def _observe_carried(
    goal_id: str, owner_epoch: str, activity_id: str
) -> dict[str, str]:
    _carried_observations.append((goal_id, owner_epoch, activity_id))
    status = "SUCCEEDED" if len(_carried_observations) >= 2 else "RUNNING"
    return {"activity_id": activity_id, "status": status, "kind": "PLAN"}


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        return _qualified_name(node.func)
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _resolve_alias(name: str | None, aliases: dict[str, str]) -> str | None:
    if not name:
        return None
    first, separator, remainder = name.partition(".")
    resolved = aliases.get(first, first)
    return f"{resolved}.{remainder}" if separator else resolved


class _StripDocstrings(ast.NodeTransformer):
    """去掉纯说明变更，只对可执行 AST 形成稳定摘要。"""

    def _without_docstring(self, node: ast.AST) -> ast.AST:
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:]
        return self.generic_visit(node)

    visit_Module = _without_docstring
    visit_ClassDef = _without_docstring
    visit_FunctionDef = _without_docstring
    visit_AsyncFunctionDef = _without_docstring


def _semantic_ast_sha256(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    normalized = _StripDocstrings().visit(tree)
    ast.fix_missing_locations(normalized)
    payload = ast.dump(normalized, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _module_index(local_source_roots: list[Path]) -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for root in local_source_roots:
        for path in root.rglob("*.py"):
            relative = path.relative_to(root).with_suffix("")
            parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
            if parts:
                modules[".".join(parts)] = path
    return modules


def _absolute_import_module(
    node: ast.ImportFrom, current_module: str
) -> str:
    if node.level == 0:
        return node.module or ""
    package = current_module.split(".")[:-1]
    keep = max(0, len(package) - (node.level - 1))
    parts = package[:keep]
    if node.module:
        parts.extend(node.module.split("."))
    return ".".join(parts)


def _is_forbidden_module(name: str) -> bool:
    root = name.split(".", 1)[0]
    return root in _FORBIDDEN_IMPORT_ROOTS or any(
        name == prefix or name.startswith(f"{prefix}.")
        for prefix in _FORBIDDEN_IMPORT_PREFIXES
    )


def _collect_determinism_violations(
    *, entry_paths: list[Path], local_source_roots: list[Path]
) -> list[str]:
    """扫描 Workflow 的本地 import 闭包，并解析别名后的禁止调用。"""

    modules = _module_index(local_source_roots)
    module_by_path = {path.resolve(): name for name, path in modules.items()}
    pending = [path.resolve() for path in entry_paths]
    visited: set[Path] = set()
    violations: list[str] = []
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        current_module = module_by_path.get(path, path.stem)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases: dict[str, str] = {}
        local_dependencies: set[str] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    bound = alias.asname or alias.name.split(".", 1)[0]
                    aliases[bound] = alias.name if alias.asname else bound
                    if _is_forbidden_module(alias.name):
                        violations.append(
                            f"{path}:{node.lineno}: forbidden import {alias.name}"
                        )
                    if alias.name in modules:
                        local_dependencies.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = _absolute_import_module(node, current_module)
                if module and _is_forbidden_module(module):
                    violations.append(
                        f"{path}:{node.lineno}: forbidden import {module}"
                    )
                if module in modules:
                    local_dependencies.add(module)
                for alias in node.names:
                    imported = f"{module}.{alias.name}" if module else alias.name
                    aliases[alias.asname or alias.name] = imported
                    if imported in modules:
                        local_dependencies.add(imported)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _resolve_alias(_qualified_name(node.func), aliases)
            if name in _FORBIDDEN_CALLS:
                violations.append(f"{path}:{node.lineno}: forbidden call {name}")
            if name in {"__import__", "importlib.import_module"}:
                violations.append(f"{path}:{node.lineno}: forbidden dynamic import")

        pending.extend(modules[name].resolve() for name in local_dependencies)
    return sorted(set(violations))


def _load_manifest() -> dict:
    assert _MANIFEST.is_file(), f"Temporal 演变清单缺失: {_MANIFEST.relative_to(_ROOT)}"
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def _patch_contract(patch_id: str) -> dict:
    for contract in _load_manifest().get("patches") or []:
        if contract.get("id") == patch_id:
            return contract
    raise AssertionError(f"Temporal patch 未登记: {patch_id}")


def _assert_history_expectations(history: WorkflowHistory, patch_id: str, role: str) -> None:
    contract = _patch_contract(patch_id)
    expectations = contract["history_expectations"][role]
    scheduled = [
        event.activity_task_scheduled_event_attributes
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    scheduled_names = [item.activity_type.name for item in scheduled]
    expected_sequence = expectations.get("activity_sequence")
    assert isinstance(expected_sequence, list) and expected_sequence
    assert scheduled_names == expected_sequence, (
        f"{patch_id}/{role} Activity 序列漂移: {scheduled_names}"
    )
    for name in expectations.get("required_activities") or []:
        assert name in scheduled_names, f"{patch_id}/{role} 缺少 Activity: {name}"
    for name in expectations.get("forbidden_activities") or []:
        assert name not in scheduled_names, f"{patch_id}/{role} 出现禁止 Activity: {name}"
    by_name = {item.activity_type.name: item for item in scheduled}
    for name, maximum_attempts in (
        expectations.get("retry_maximum_attempts") or {}
    ).items():
        assert name in by_name, f"{patch_id}/{role} 缺少 retry 目标 Activity: {name}"
        assert by_name[name].retry_policy.maximum_attempts == maximum_attempts
    for name, task_queue in (expectations.get("task_queues") or {}).items():
        assert name in by_name, f"{patch_id}/{role} 缺少 task queue 目标 Activity: {name}"
        assert by_name[name].task_queue.name == task_queue, (
            f"{patch_id}/{role} {name} 队列漂移: {by_name[name].task_queue.name}"
        )


def _load_immutable_history(contract: dict) -> WorkflowHistory:
    relative = str(contract.get("old_history_fixture") or "")
    expected_digest = str(contract.get("old_history_sha256") or "")
    assert relative and expected_digest and expected_digest != "PENDING"
    path = _ROOT / relative
    assert path.is_file(), f"不可变旧历史夹具缺失: {relative}"
    payload = path.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == expected_digest
    assert contract.get("sanitize_profile") == "temporal-history-v1"
    producer_commit = str(contract.get("producer_commit") or "")
    assert len(producer_commit) == 40 and all(
        character in "0123456789abcdef" for character in producer_commit
    )
    assert contract.get("workflow_type") == "GoalWorkflow"
    assert contract.get("temporal_sdk") == "temporalio==1.32.0"
    assert "::" in str(contract.get("producer_node") or "")
    assert len(payload) < 200_000
    lowered = payload.lower()
    for needle in (b"authorization", b"bearer ", b"api_key", b"private_key"):
        assert needle not in lowered, f"旧历史夹具含敏感字段: {needle!r}"
    return WorkflowHistory.from_json(f"fixture-{contract['id']}", payload.decode())


@activity.defn(name="record_recovery_abandonment")
async def _record_recovery_abandonment(
    goal_id: str, reason: str, generation: int, prior_run_id: str | None
) -> dict[str, str | int | None]:
    """放弃裁决持久化活动桩：只回描述，不写库、不影响任何业务态。"""
    return {
        "abandonment_id": f"abandon-{goal_id}",
        "goal_id": goal_id,
        "reason": reason,
        "generation": generation,
        "prior_run_id": prior_run_id,
        "marks_goal_done": False,
    }


def test_temporal_workflow_manifest_and_determinism_gate() -> None:
    """Workflow 模块必须入清单，且不得直接使用 IO/系统时间随机。"""

    manifest = _load_manifest()
    workflow_modules = manifest.get("workflow_modules")
    assert isinstance(workflow_modules, list) and workflow_modules

    discovered = set()
    for path in (_ROOT / "packages" / "orchestration" / "src").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "@workflow.defn" in source:
            discovered.add(path.relative_to(_ROOT).as_posix())
    assert set(workflow_modules) == discovered

    patch_contracts = manifest.get("patches")
    assert isinstance(patch_contracts, list) and patch_contracts
    declared_patches = {
        str(contract.get("id") or "")
        for contract in patch_contracts
        if isinstance(contract, dict)
    }
    assert "" not in declared_patches
    source_patches: set[str] = set()
    source_can_fields: set[str] = set()
    entry_paths = [_ROOT / relative for relative in workflow_modules]
    violations = _collect_determinism_violations(
        entry_paths=entry_paths,
        local_source_roots=[_ROOT / "packages" / "orchestration" / "src"],
    )
    for relative in workflow_modules:
        path = _ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        string_constants = {
            target.id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance((target := node.targets[0]), ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _qualified_name(node.func)
                if name == "workflow.patched" and node.args:
                    marker = node.args[0]
                    if isinstance(marker, ast.Constant) and isinstance(marker.value, str):
                        source_patches.add(marker.value)
                    elif isinstance(marker, ast.Name) and marker.id in string_constants:
                        source_patches.add(string_constants[marker.id])
                if name == "workflow.continue_as_new" and node.args:
                    payload = node.args[0]
                    if isinstance(payload, ast.Dict):
                        for key in payload.keys:
                            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                                source_can_fields.add(key.value)

    assert not violations, "\n".join(violations)
    assert declared_patches == source_patches

    can_fields = manifest.get("continue_as_new_fields")
    assert isinstance(can_fields, list) and can_fields
    assert all(isinstance(field, str) and field for field in can_fields)
    assert set(can_fields) == source_can_fields


def test_determinism_scan_follows_local_imports_and_aliases(tmp_path: Path) -> None:
    """Workflow 本地 helper 与 import alias 都不能绕过 IO 静态门。"""

    package = tmp_path / "sample"
    package.mkdir()
    entry = package / "workflow_entry.py"
    helper = package / "helper.py"
    entry.write_text(
        "from .helper import unsafe_read\n"
        "from datetime import datetime as dt\n"
        "import importlib as il\n"
        "module_name = 'o' + 's'\n"
        "def run():\n"
        "    unsafe_read()\n"
        "    il.import_module(module_name)\n"
        "    return dt.now()\n",
        encoding="utf-8",
    )
    helper.write_text(
        "from pathlib import Path as P\n"
        "def unsafe_read():\n"
        "    return P('state.json').read_text()\n",
        encoding="utf-8",
    )

    violations = _collect_determinism_violations(
        entry_paths=[entry],
        local_source_roots=[tmp_path],
    )
    assert any("forbidden import pathlib" in item for item in violations)
    assert any("forbidden call pathlib.Path.read_text" in item for item in violations)
    assert any("forbidden call datetime.datetime.now" in item for item in violations)
    assert any("forbidden dynamic import" in item for item in violations)


def test_workflow_semantic_snapshot_requires_patch_revision() -> None:
    """Workflow 行为 AST 漂移时，必须新增 patch revision，而非静默改命令路径。"""

    manifest = _load_manifest()
    revisions = manifest.get("workflow_revisions")
    assert isinstance(revisions, list) and revisions
    declared_patches = {contract["id"] for contract in manifest["patches"]}
    previous_patches: set[str] = set()
    for revision in revisions:
        revision_patches = set(revision.get("patch_ids") or [])
        assert revision_patches <= declared_patches
        if previous_patches:
            assert revision_patches > previous_patches, (
                "Workflow 新 revision 必须至少新增一个 patch id"
            )
        previous_patches = revision_patches

    latest = revisions[-1]
    relative = str(latest.get("workflow_module") or "")
    assert relative in manifest["workflow_modules"]
    assert _semantic_ast_sha256(_ROOT / relative) == latest.get("semantic_ast_sha256")
    assert previous_patches == declared_patches


def test_every_temporal_patch_declares_old_and_new_history_tests() -> None:
    """每个 patch 都须绑定 CI 会收集的旧历史 replay 与新历史行为用例。"""

    manifest = _load_manifest()
    contracts = manifest.get("patches")
    assert isinstance(contracts, list) and contracts

    for contract in contracts:
        assert isinstance(contract, dict)
        patch_id = str(contract.get("id") or "")
        for role in ("old_history_test", "new_history_test"):
            node_id = str(contract.get(role) or "")
            assert "::" in node_id, f"{patch_id} 缺 {role}"
            relative, test_name = node_id.split("::", 1)
            path = _ROOT / relative
            assert path.is_file(), f"{patch_id} 的 {role} 文件不存在: {relative}"
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            functions = {
                node.name: node
                for node in tree.body
                if isinstance(node, ast.FunctionDef)
            }
            assert test_name in functions, f"{patch_id} 的 {role} 不存在: {node_id}"
            referenced_names = {
                _qualified_name(node)
                for node in ast.walk(functions[test_name])
                if isinstance(node, (ast.Name, ast.Attribute))
            }
            assert "Replayer" in referenced_names, f"{node_id} 未执行 history replay"
            if role == "new_history_test":
                assert "GoalWorkflow.run" in referenced_names, (
                    f"{node_id} 未执行当前 GoalWorkflow"
                )
                bound_contracts = {
                    arg.value
                    for call in ast.walk(functions[test_name])
                    if isinstance(call, ast.Call)
                    and _qualified_name(call.func) == "_assert_history_expectations"
                    for arg in call.args
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                }
                assert {patch_id, "new"}.issubset(bound_contracts), (
                    f"{node_id} 未绑定 {patch_id} 的 new history 契约"
                )


def test_every_temporal_patch_replays_immutable_old_history_fixture() -> None:
    """旧历史必须是带摘要锁的持久夹具，并由当前 Workflow 直接重放。"""

    async def _run() -> None:
        for contract in _load_manifest()["patches"]:
            history = _load_immutable_history(contract)
            _assert_history_expectations(history, contract["id"], "old")
            replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
            assert replay.replay_failure is None

    asyncio.run(_run())


def test_recovery_gate_replays_pre_change_generation_history() -> None:
    """恢复闸 patch 必须重放发布前 generation>0 的旧命令序列。"""

    async def _run() -> None:
        patch_id = "recovery-safety-gate-v1"
        history = _load_immutable_history(_patch_contract(patch_id))
        _assert_history_expectations(history, patch_id, "old")
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_continue_as_new_contract_declares_double_rollover_test() -> None:
    """CAN 清单必须绑定连续两次 rollover 的行为证据。"""

    manifest = _load_manifest()
    contract = manifest.get("continue_as_new_contract")
    assert isinstance(contract, dict)
    node_id = str(contract.get("double_rollover_test") or "")
    assert "::" in node_id
    relative, test_name = node_id.split("::", 1)
    path = _ROOT / relative
    assert path.is_file()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert test_name in functions


def test_two_continue_as_new_runs_preserve_evolution_watermark() -> None:
    """连续两次 CAN 后仍保留命令、owner epoch、运行 attempt 与 action identity。"""

    async def _run() -> dict:
        _list_calls.clear()
        _admit_calls.clear()
        _run_calls.clear()
        _carried_observations.clear()
        goal_id = str(uuid4())
        command_id = str(uuid4())
        async with (
            await WorkflowEnvironment.start_time_skipping() as env,
            Worker(
                env.client,
                task_queue=_CONTROL_QUEUE,
                workflows=[GoalWorkflow],
                activities=[_ensure, _list_once, _admit, _observe_running, _observe_carried],
            ),
            Worker(
                env.client,
                task_queue=_RUNNER_QUEUE,
                activities=[_run_activation],
            ),
        ):
            result = await env.client.execute_workflow(
                GoalWorkflow.run,
                {
                    "command_id": command_id,
                    "goal_id": goal_id,
                    "owner_epoch": "7",
                    "observe_max_ticks": 1,
                    "enable_continue_as_new": True,
                    "continue_as_new_on_observe_timeout": True,
                },
                id=f"goal-{goal_id}",
                task_queue=_CONTROL_QUEUE,
                execution_timeout=timedelta(seconds=60),
            )
        return {"command_id": command_id, "goal_id": goal_id, "result": result}

    observed = asyncio.run(_run())
    result = observed["result"]
    assert result["generation"] == 2
    assert result["command_ids_consumed"] == [observed["command_id"]]
    assert result["admitted_attempt_ids"] == [_ATTEMPT_ID]
    assert result["prior_run_id"]
    assert result["plan_terminal_statuses"] == [
        {"activity_id": _ACTIVITY_ID, "status": "SUCCEEDED"}
    ]
    assert _admit_calls == [_ACTIVITY_ID]
    assert _run_calls == [_ATTEMPT_ID]
    assert _carried_observations == [
        (observed["goal_id"], "7", _ACTIVITY_ID),
        (observed["goal_id"], "7", _ACTIVITY_ID),
    ]
    assert result["marks_goal_done"] is False


def test_b086_new_history_replays_with_single_activation_attempt() -> None:
    """b086 新历史带单次 RunActivation retry policy，且可由当前定义重放。"""

    async def _run() -> None:
        _list_calls.clear()
        _admit_calls.clear()
        _run_calls.clear()
        goal_id = str(uuid4())
        async with (
            await WorkflowEnvironment.start_time_skipping() as env, Worker(
                env.client,
                task_queue=_CONTROL_QUEUE,
                workflows=[GoalWorkflow],
                activities=[_ensure, _list_once, _admit, _observe_succeeded],
            ),
            Worker(
                env.client,
                task_queue=_RUNNER_QUEUE,
                activities=[_run_activation],
            ),
        ):
            handle = await env.client.start_workflow(
                GoalWorkflow.run,
                {
                    "command_id": str(uuid4()),
                    "goal_id": goal_id,
                    "owner_epoch": "7",
                    "observe_max_ticks": 1,
                },
                id=f"goal-b086-new-{goal_id}",
                task_queue=_CONTROL_QUEUE,
                execution_timeout=timedelta(seconds=30),
            )
            result = await handle.result()
            history = await handle.fetch_history()

        scheduled = [
            event.activity_task_scheduled_event_attributes
            for event in history.events
            if event.HasField("activity_task_scheduled_event_attributes")
            and event.activity_task_scheduled_event_attributes.activity_type.name
            == "RunActivation"
        ]
        assert len(scheduled) == 1
        assert scheduled[0].retry_policy.maximum_attempts == 1
        _assert_history_expectations(
            history, "b086-temporal-recovery-v1", "new"
        )
        assert result["marks_goal_done"] is False
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_m3_new_execute_history_replays_with_execute_admission() -> None:
    """m3 新历史确实调度 EXECUTE 准入，且可由当前定义重放。"""

    async def _run() -> None:
        _execute_list_calls.clear()
        _admit_calls.clear()
        _run_calls.clear()
        goal_id = str(uuid4())
        async with (
            await WorkflowEnvironment.start_time_skipping() as env, Worker(
                env.client,
                task_queue=_CONTROL_QUEUE,
                workflows=[GoalWorkflow],
                activities=[
                    _ensure,
                    _list_execute_once,
                    _admit_execute,
                    _observe_succeeded,
                ],
            ),
            Worker(
                env.client,
                task_queue=_RUNNER_QUEUE,
                activities=[_run_activation],
            ),
        ):
            handle = await env.client.start_workflow(
                GoalWorkflow.run,
                {
                    "command_id": str(uuid4()),
                    "goal_id": goal_id,
                    "owner_epoch": "7",
                    "observe_max_ticks": 1,
                },
                id=f"goal-m3-new-{goal_id}",
                task_queue=_CONTROL_QUEUE,
                execution_timeout=timedelta(seconds=30),
            )
            result = await handle.result()
            history = await handle.fetch_history()

        scheduled_names = [
            event.activity_task_scheduled_event_attributes.activity_type.name
            for event in history.events
            if event.HasField("activity_task_scheduled_event_attributes")
        ]
        assert "admit_execute_action" in scheduled_names
        _assert_history_expectations(
            history, "m3-execute-orchestration-v1", "new"
        )
        assert _admit_calls == [_ACTIVITY_ID]
        assert _run_calls == [_ATTEMPT_ID]
        assert result["marks_goal_done"] is False
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_recovery_gate_new_history_abandons_before_runtime_actions() -> None:
    """新恢复历史遇到不兼容 checkpoint 时，必须在 runtime action 前放弃。"""

    async def _run() -> None:
        goal_id = str(uuid4())
        async with (
            await WorkflowEnvironment.start_time_skipping() as env,
            Worker(
                env.client,
                task_queue=_CONTROL_QUEUE,
                workflows=[GoalWorkflow],
                activities=[_ensure, _record_recovery_abandonment],
            ),
        ):
            handle = await env.client.start_workflow(
                GoalWorkflow.run,
                {
                    "command_id": str(uuid4()),
                    "goal_id": goal_id,
                    "owner_epoch": "7",
                    "generation": 1,
                    "checkpoint_schema_version": 999,
                    "recovery_attempts": 1,
                    "max_recovery_attempts": 3,
                    # 恢复总开关（recovery-enabled-switch-v1，默认关）先于 schema 检查：
                    # 不显式开启会先判 RECOVERY_DISABLED，测不到本用例要测的 schema 分支。
                    "recovery_enabled": True,
                    "intent_valid_until": "2099-01-01T00:00:00+00:00",
                    "observe_max_ticks": 1,
                },
                id=f"goal-recovery-new-{goal_id}",
                task_queue=_CONTROL_QUEUE,
                execution_timeout=timedelta(seconds=30),
            )
            result = await handle.result()
            history = await handle.fetch_history()

        assert result["recovery_status"] == "RECOVERY_ABANDONED"
        assert result["recovery_reason"] == "CHECKPOINT_SCHEMA_INCOMPATIBLE"
        assert result["marks_goal_done"] is False
        _assert_history_expectations(history, "recovery-safety-gate-v1", "new")
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def _scheduled_activities(history: WorkflowHistory) -> dict[str, object]:
    """把已调度的 Activity 按类型名建索引（重名取最后一次调度）。"""
    found: dict[str, object] = {}
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            attrs = event.activity_task_scheduled_event_attributes
            found[attrs.activity_type.name] = attrs
    return found


async def _run_standard_history(
    entry: object,
    *,
    runner_task_queue: str = _RUNNER_QUEUE,
    payload_updates: dict | None = None,
) -> tuple[WorkflowHistory, dict]:
    """跑一次标准 GoalWorkflow（generation=0，无恢复），返回 (history, result)。

    各新历史用例把 `GoalWorkflow.run` 传进来 —— 演变门要求用例**直接引用当前入口**，
    以此证明断言的对象是被测 Workflow 本身，而不是某个替身。

    **必须清理模块级桩计数器**：`_list_once`/`_admit`/`_run_activation` 用模块级列表
    记录「只回一次」的动作；若不清空，本助手在同文件内**第二个**用例起就会拿到空动作表，
    Activity 序列随之漂移 —— 表现为「单独跑绿、全量跑红」的假隔离失败。
    """
    _list_calls.clear()
    _admit_calls.clear()
    _run_calls.clear()
    goal_id = str(uuid4())
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=_CONTROL_QUEUE,
            workflows=[GoalWorkflow],
            activities=[
                _ensure,
                _list_once,
                _admit,
                _observe_succeeded,
                _record_recovery_abandonment,
            ],
        ),
        Worker(env.client, task_queue=runner_task_queue, activities=[_run_activation]),
    ):
        payload = {
            "command_id": str(uuid4()),
            "goal_id": goal_id,
            "owner_epoch": "7",
            "observe_max_ticks": 1,
        }
        payload.update(payload_updates or {})
        handle = await env.client.start_workflow(
            entry,
            payload,
            id=f"goal-evolution-{goal_id}",
            task_queue=_CONTROL_QUEUE,
            execution_timeout=timedelta(seconds=60),
        )
        result = await handle.result()
        history = await handle.fetch_history()
    return history, result


def _run_new_history_case(patch_id: str, specific_source: str) -> None:
    """新历史用例的公共执行体（各用例仍各自持有独立的函数名与契约绑定）。"""
    raise AssertionError("不应被调用：请见各用例内联实现")


def test_observe_backoff_replays_pre_change_history() -> None:
    """observe-backoff-v1（观察退避）：变更前历史必须能被**当前** Workflow 直接重放，不得出现 replay 失败。

    所用夹具为不含任何 patch 标记的历史（35 事件），故对其而言本补丁取**旧分支** ——
    这正是「新代码必须仍能重放旧历史」的判据。
    """

    async def _run() -> None:
        patch_id = "observe-backoff-v1"
        history = _load_immutable_history(_patch_contract(patch_id))
        _assert_history_expectations(history, patch_id, "old")
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_observe_backoff_new_history_keeps_observe_retry_bounded() -> None:
    """observe-backoff-v1（观察退避）：当前代码产生的新历史须符合登记期望，并可自我重放。"""

    async def _run() -> None:
        history, result = await _run_standard_history(GoalWorkflow.run)
        _assert_history_expectations(history, "observe-backoff-v1", "new")
        assert result["marks_goal_done"] is False
        _scheduled = _scheduled_activities(history)
        assert _scheduled["observe_activity_status"].retry_policy.maximum_attempts == 1
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_activity_retry_bounded_replays_pre_change_history() -> None:
    """activity-retry-bounded-v1（活动重试上界）：变更前历史必须能被**当前** Workflow 直接重放，不得出现 replay 失败。

    所用夹具为不含任何 patch 标记的历史（35 事件），故对其而言本补丁取**旧分支** ——
    这正是「新代码必须仍能重放旧历史」的判据。
    """

    async def _run() -> None:
        patch_id = "activity-retry-bounded-v1"
        history = _load_immutable_history(_patch_contract(patch_id))
        _assert_history_expectations(history, patch_id, "old")
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_activity_retry_bounded_new_history_bounds_run_activation_retry() -> None:
    """activity-retry-bounded-v1（活动重试上界）：当前代码产生的新历史须符合登记期望，并可自我重放。"""

    async def _run() -> None:
        history, result = await _run_standard_history(GoalWorkflow.run)
        _assert_history_expectations(history, "activity-retry-bounded-v1", "new")
        assert result["marks_goal_done"] is False
        _scheduled = _scheduled_activities(history)
        assert _scheduled["RunActivation"].retry_policy.maximum_attempts == 1
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_recovery_enabled_switch_replays_pre_change_history() -> None:
    """recovery-enabled-switch-v1（恢复总开关）：变更前历史必须能被**当前** Workflow 直接重放，不得出现 replay 失败。

    所用夹具为不含任何 patch 标记的历史（35 事件），故对其而言本补丁取**旧分支** ——
    这正是「新代码必须仍能重放旧历史」的判据。
    """

    async def _run() -> None:
        patch_id = "recovery-enabled-switch-v1"
        history = _load_immutable_history(_patch_contract(patch_id))
        _assert_history_expectations(history, patch_id, "old")
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_recovery_enabled_switch_new_history_exposes_budget_notes() -> None:
    """recovery-enabled-switch-v1（恢复总开关）：当前代码产生的新历史须符合登记期望，并可自我重放。"""

    async def _run() -> None:
        history, result = await _run_standard_history(GoalWorkflow.run)
        _assert_history_expectations(history, "recovery-enabled-switch-v1", "new")
        assert result["marks_goal_done"] is False
        # 总开关的语义：正常（generation=0）运行**绝不**被误判为放弃
        assert result.get("recovery_status") != "RECOVERY_ABANDONED"
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_abandon_persistence_replays_pre_change_history() -> None:
    """abandon-persistence-v1（放弃持久化）：变更前历史必须能被**当前** Workflow 直接重放，不得出现 replay 失败。

    所用夹具为不含任何 patch 标记的历史（35 事件），故对其而言本补丁取**旧分支** ——
    这正是「新代码必须仍能重放旧历史」的判据。
    """

    async def _run() -> None:
        patch_id = "abandon-persistence-v1"
        history = _load_immutable_history(_patch_contract(patch_id))
        _assert_history_expectations(history, patch_id, "old")
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_abandon_persistence_new_history_marks_no_goal_done() -> None:
    """abandon-persistence-v1（放弃持久化）：当前代码产生的新历史须符合登记期望，并可自我重放。"""

    async def _run() -> None:
        history, result = await _run_standard_history(GoalWorkflow.run)
        _assert_history_expectations(history, "abandon-persistence-v1", "new")
        assert result["marks_goal_done"] is False
        # 持久化只在放弃路径发生；正常运行不得调用该活动
        assert "record_recovery_abandonment" not in _scheduled_activities(history)
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_activation_budget_replays_pre_change_history() -> None:
    """activation-budget-v1（激活预算）：变更前历史必须能被**当前** Workflow 直接重放，不得出现 replay 失败。

    所用夹具为不含任何 patch 标记的历史（35 事件），故对其而言本补丁取**旧分支** ——
    这正是「新代码必须仍能重放旧历史」的判据。
    """

    async def _run() -> None:
        patch_id = "activation-budget-v1"
        history = _load_immutable_history(_patch_contract(patch_id))
        _assert_history_expectations(history, patch_id, "old")
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_activation_budget_new_history_dispatches_run_activation() -> None:
    """activation-budget-v1（激活预算）：当前代码产生的新历史须符合登记期望，并可自我重放。"""

    async def _run() -> None:
        history, result = await _run_standard_history(GoalWorkflow.run)
        _assert_history_expectations(history, "activation-budget-v1", "new")
        assert result["marks_goal_done"] is False
        # 该补丁把预算决策的原因回填到结果，供误配可见
        assert "activation_budget_notes" in result
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())

def test_goal_review_periodic_replays_pre_change_history() -> None:
    """goal-review-periodic-v1（周期复盘）：变更前历史必须能被**当前** Workflow 直接重放。

    夹具不含任何 patch 标记 ⇒ 对本补丁取**旧分支**（不产生复盘命令），
    这正是「新代码必须仍能重放旧历史」的判据。
    """

    async def _run() -> None:
        patch_id = "goal-review-periodic-v1"
        history = _load_immutable_history(_patch_contract(patch_id))
        _assert_history_expectations(history, patch_id, "old")
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_goal_review_periodic_new_history_keeps_default_off() -> None:
    """goal-review-periodic-v1：未配间隔/上限时新历史**不得**出现复盘活动（失败关闭）。"""

    async def _run() -> None:
        history, result = await _run_standard_history(GoalWorkflow.run)
        _assert_history_expectations(history, "goal-review-periodic-v1", "new")
        # 默认关闭：零触发、零创建，且绝不写 DONE
        assert result["goal_reviews_requested"] == 0
        assert result["goal_reviews_created"] == []
        assert result["marks_goal_done"] is False
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())

def test_goal_review_seq_base_replays_pre_change_history() -> None:
    """goal-review-seq-base-v1（seq 基数修复）：变更前历史必须能被**当前** Workflow 重放。

    夹具不含本 patch 标记 ⇒ 取**旧分支**（外发 0 基 seq，与历史命令参数逐字一致）。
    这正是「修复命令参数必须 gating」的判据：若直接把 0 改成 1，
    重放时 ScheduleActivityTask 的 args 与历史不符 ⇒ 非确定性失败。
    """

    async def _run() -> None:
        patch_id = "goal-review-seq-base-v1"
        history = _load_immutable_history(_patch_contract(patch_id))
        _assert_history_expectations(history, patch_id, "old")
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_goal_review_seq_base_new_history_default_off() -> None:
    """goal-review-seq-base-v1：新历史默认不触发复盘，且重放无失败、绝不写 DONE。"""

    async def _run() -> None:
        history, result = await _run_standard_history(GoalWorkflow.run)
        _assert_history_expectations(history, "goal-review-seq-base-v1", "new")
        assert result["goal_reviews_requested"] == 0
        assert result["goal_reviews_created"] == []
        assert result["marks_goal_done"] is False
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_admit_all_kinds_replays_pre_change_history() -> None:
    """admit-all-kinds-v1（全类活动准入）：变更前历史必须能被**当前** Workflow 直接重放。

    夹具不含本 patch 标记 ⇒ 维持「仅 PLAN/EXECUTE」的既有准入分支与命令参数。
    这正是「放宽准入面必须 gating」的判据：若不加 patch 门，旧历史里的
    `admit_execute_action` 会变成 `admit_runtime_action`、args 也多一个 kind
    ⇒ 与历史 ScheduleActivityTask 不符 ⇒ 非确定性失败。

    背景：Kernel 在 EXECUTE 成功后**自动创建** AUDIT / INTEGRATE / FINALIZE，
    而编排原先只准入 PLAN/EXECUTE ⇒ 这三类无人准入、目标到不了 DONE
    （实测停在 `EXECUTE:SUCCEEDED → AUDIT:READY`）。
    """

    async def _run() -> None:
        patch_id = "admit-all-kinds-v1"
        history = _load_immutable_history(_patch_contract(patch_id))
        _assert_history_expectations(history, patch_id, "old")
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_admit_all_kinds_new_history_unchanged_for_plan_only() -> None:
    """admit-all-kinds-v1：桩只报 PLAN 时，新历史的准入行为与既有契约**逐字一致**。

    这是本补丁的**安全边界**：放宽只是「多认三类」，不得改变 PLAN/EXECUTE 的
    准入入口与参数（否则等于在放宽的同时改动了既有路径）。
    """

    async def _run() -> None:
        history, result = await _run_standard_history(GoalWorkflow.run)
        _assert_history_expectations(history, "admit-all-kinds-v1", "new")
        # 只报 PLAN ⇒ 仍走 admit_plan_action，且只准入一次
        assert _admit_calls == [_ACTIVITY_ID]
        assert result["marks_goal_done"] is False
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_runner_authority_queues_replays_pre_change_history() -> None:
    """runner-authority-queues-v1：旧 history 继续调度 legacy runner 队列。"""

    async def _run() -> None:
        patch_id = "runner-authority-queues-v1"
        history = _load_immutable_history(_patch_contract(patch_id))
        _assert_history_expectations(history, patch_id, "old")
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_runner_authority_queues_new_history_routes_plan() -> None:
    """runner-authority-queues-v1：新 PLAN history 必须进入 Manager 专属队列。"""

    async def _run() -> None:
        history, result = await _run_standard_history(
            GoalWorkflow.run,
            runner_task_queue="ring-runner-manager",
            payload_updates={"runner_authority_queues": True},
        )
        _assert_history_expectations(history, "runner-authority-queues-v1", "new")
        assert result["marks_goal_done"] is False
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_advance_on_ready_replays_pre_change_history() -> None:
    """advance-on-ready-v1（跨活动推进）：变更前历史必须能被**当前** Workflow 直接重放。

    夹具不含本 patch 标记 ⇒ 取**旧分支**（末段不追加 list_runtime_actions、不续跑），
    与历史命令序列逐字一致。这正是「新增末段命令必须 gating」的判据：
    若不加 patch 门，旧历史重放时多出一条 ScheduleActivityTask ⇒ 非确定性问题。

    背景：本工作流原本「一次运行 = 一批活动」，末段直接 return；而新 run 只由
    ENSURE_WORKFLOW 投递触发、该投递全仓仅在 START 入队 ⇒ PLAN 成功后新 READY 的
    EXECUTE 无人推进（实测 Goal 静默停在 READY 二十余分钟）。
    """

    async def _run() -> None:
        patch_id = "advance-on-ready-v1"
        history = _load_immutable_history(_patch_contract(patch_id))
        _assert_history_expectations(history, patch_id, "old")
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_advance_on_ready_new_history_lists_once_more() -> None:
    """advance-on-ready-v1：新历史末段多一次 list；工作已排空即收束，且绝不续跑空转。

    断言：
      1. 末段确实多了 `list_runtime_actions`（推进机制真的接上了，否则只能读到常量读数）；
      2. 标准桩第二次 list 已无就绪工作 ⇒ `advance_note == "NO_READY_WORK"`、
         `advance_hops == 0`，即**不续跑**（避免无意义空转）；
      3. 绝不写 DONE，且重放无失败。

    **防自激守卫**（同一 READY 活动不得被反复推进）由 m3 用例覆盖：
    `test_m3_execute_admits_once_submitted_not_goal_done` 的桩每次返回同一 EXECUTE，
    其 `_admit_calls == [f"EXECUTE:{_FIXED_EXECUTE_ID}"]` 断言即该守卫的判据。
    """

    async def _run() -> None:
        history, result = await _run_standard_history(GoalWorkflow.run)
        _assert_history_expectations(history, "advance-on-ready-v1", "new")
        assert result["advance_note"] == "NO_READY_WORK"
        assert result["advance_hops"] == 0
        assert result["marks_goal_done"] is False
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_can_budget_carry_replays_pre_change_history() -> None:
    """can-budget-carry-v1：变更前历史（CAN 载荷无预算键）必须能被当前 Workflow 重放。"""

    async def _run() -> None:
        patch_id = "can-budget-carry-v1"
        history = _load_immutable_history(_patch_contract(patch_id))
        _assert_history_expectations(history, patch_id, "old")
        replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
        assert replay.replay_failure is None

    asyncio.run(_run())


def test_can_budget_carry_new_history_carries_budget() -> None:
    """can-budget-carry-v1：新历史的 CAN 续跑载荷必须携带激活预算。

    漏带时续跑 RunActivation 回退钉扎 360s（2026-09-25 实测：DeepSeek 多轮
    工具循环在续跑 run 再次被 StartToClose 打断）。终 run 启动载荷须含
    activation_budget_seconds=900，且该历史可被 Replayer 重放。
    """

    async def _run() -> None:
        from datetime import timedelta
        from uuid import uuid4

        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Worker

        from tests.temporal.test_goal_workflow_continue_as_new import (
            _CONTROL_QUEUE,
            _RUNNER_QUEUE,
            _reset_counters,
            _stub_admit,
            _stub_ensure,
            _stub_list_fixed,
            _stub_observe_carried_activation_then_succeed,
            _stub_observe_carried_then_succeed,
            _stub_observe_then_succeed,
            _stub_run_activation,
        )

        _reset_counters()
        command_id = f"cmd-can-budget-{uuid4()}"
        goal_id = str(uuid4())
        async with await WorkflowEnvironment.start_time_skipping() as env:
            assert env.client is not None
            async with (
                Worker(
                    env.client,
                    task_queue=_CONTROL_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=[
                        _stub_ensure,
                        _stub_list_fixed,
                        _stub_admit,
                        _stub_observe_then_succeed,
                        _stub_observe_carried_then_succeed,
                        _stub_observe_carried_activation_then_succeed,
                    ],
                ),
                Worker(
                    env.client,
                    task_queue=_RUNNER_QUEUE,
                    activities=[_stub_run_activation],
                ),
            ):
                result = await env.client.execute_workflow(
                    GoalWorkflow.run,
                    {
                        "command_id": command_id,
                        "goal_id": goal_id,
                        "owner_epoch": "1",
                        "observe_max_ticks": 2,
                        "activation_budget_seconds": 900,
                        "enable_continue_as_new": True,
                        "continue_as_new_on_observe_timeout": True,
                    },
                    id=f"goal-{goal_id}",
                    task_queue=_CONTROL_QUEUE,
                    execution_timeout=timedelta(seconds=60),
                )
                assert result["ok"] is True
                assert int(result["generation"]) >= 1
                # 终 run 的启动载荷 = 最后一次 CAN 的输出，必须携带预算
                handle = env.client.get_workflow_handle(f"goal-{goal_id}")
                history = await handle.fetch_history()
                started = history.events[0].workflow_execution_started_event_attributes
                decoded = await env.client.data_converter.decode(started.input.payloads)
                payload = decoded[0] if decoded else {}
                assert isinstance(payload, dict)
                assert payload.get("activation_budget_seconds") == 900
                # 新历史契约：终 run 的 Activity 序列与队列须与声明一致
                _assert_history_expectations(history, "can-budget-carry-v1", "new")
                # 新历史须可被当前 Workflow 无漂移重放
                replay = await Replayer(workflows=[GoalWorkflow]).replay_workflow(history)
                assert replay.replay_failure is None

    asyncio.run(_run())
