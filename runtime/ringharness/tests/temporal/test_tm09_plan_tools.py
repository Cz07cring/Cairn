"""TM09 部分举证：PLAN 注入工具 → Kernel/Harness 拒绝。

真实 PG + TEMPORAL admit；Runner ``authorizeToolProposal`` 经 tsx 实测。
禁把 PLAN effect 写成可派发；无 Goal DONE stub。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import UUID, uuid4

from control_kernel.storage.goals import set_goal_orchestration_backend
from orchestration import (
    FakeTemporalClient,
    OrchestrationBindingContent,
    ensure_workflow,
)
from sqlalchemy import text
from test_claims import _drain_ready_plans, _register_worker
from test_orchestration_backend import _start_temporal_goal

_REPO = Path(__file__).resolve().parents[2]


def _binding_content(row) -> OrchestrationBindingContent:
    return OrchestrationBindingContent(
        project_id=row["project_id"],
        goal_id=row["goal_id"],
        budget_scope_id=row["budget_scope_id"],
        backend=row["backend"],
        owner_epoch=row["owner_epoch"],
        namespace=row["namespace"],
        workflow_id=row["workflow_id"],
        active_run_id=row["active_run_id"],
        worker_build_id=row["worker_build_id"],
        contract_digest=row["contract_digest"],
    )


def test_tm09_temporal_plan_prepare_effect_and_runner_forbid_tools(api, objects):
    """TEMPORAL PLAN admit 后 steps/prepare → ROLE_TOOL_FORBIDDEN；Runner 同步拒绝。"""
    client, token, _auth, goal, plan, command, engine = _start_temporal_goal(api, objects)
    try:
        with engine.connect() as db:
            binding_row = (
                db.execute(
                    text("SELECT * FROM orchestration_bindings WHERE goal_id=:goal"),
                    {"goal": goal["id"]},
                )
                .mappings()
                .one()
            )
        binding = _binding_content(binding_row)
        receipt = ensure_workflow(engine, binding, UUID(command["id"]), FakeTemporalClient())
        assert receipt.delivery_status == "ACKNOWLEDGED"

        subject = str(uuid4())
        _register_worker(subject)
        worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
        admitted = client.post(
            "/internal/v1/runtime/admit",
            json={"activity_id": plan["id"]},
            headers={**worker_auth, "Idempotency-Key": str(uuid4())},
        )
        assert admitted.status_code == 200, admitted.text
        lease_body = admitted.json()["data"]
        assert lease_body["activity"]["kind"] == "PLAN"
        assert lease_body["activity"]["status"] == "RUNNING"
        lease = lease_body["lease"]

        # Kernel：PLAN 禁止登记步骤
        denied_step = client.post(
            f"/internal/v1/activities/{plan['id']}/steps",
            json={
                "lease": lease,
                "predecessor_step_id": None,
                "purpose": "inject",
                "tool_ref": "read_file",
            },
            headers=worker_auth,
        )
        assert denied_step.status_code == 403
        assert denied_step.json()["error"]["code"] == "ROLE_TOOL_FORBIDDEN"

        # Kernel：PLAN 禁止 prepare effect（伪造 tool / 队列）
        denied_prep = client.post(
            "/internal/v1/effects/prepare",
            json={
                "lease": lease,
                "logical_step_id": str(uuid4()),
                "intent_revision": 1,
                "tool_ref": "read_file",
                "input_artifact_id": str(uuid4()),
            },
            headers=worker_auth,
        )
        assert denied_prep.status_code == 403
        assert denied_prep.json()["error"]["code"] == "ROLE_TOOL_FORBIDDEN"

        # 无 StepRecord / EffectIntent 落库
        with engine.connect() as db:
            steps = db.execute(
                text("SELECT count(*) FROM activity_steps WHERE activity_id=:id"),
                {"id": plan["id"]},
            ).scalar_one()
            effects = db.execute(
                text("SELECT count(*) FROM effect_intents WHERE goal_id=:goal"),
                {"goal": goal["id"]},
            ).scalar_one()
        assert steps == 0
        assert effects == 0

        # Runner / Harness 本地防御：authorizeToolProposal(PLAN, …)
        script = (
            "import { authorizeToolProposal } from './src/roles.ts';\n"
            "try {\n"
            "  authorizeToolProposal('PLAN', 'read_file', new Set(['read_file']));\n"
            "  console.log('UNEXPECTED_OK');\n"
            "  process.exit(2);\n"
            "} catch (e) {\n"
            "  const msg = e instanceof Error ? e.message : String(e);\n"
            "  if (msg.includes('ROLE_TOOL_FORBIDDEN')) {\n"
            "    console.log('ROLE_TOOL_FORBIDDEN');\n"
            "    process.exit(0);\n"
            "  }\n"
            "  console.error(msg);\n"
            "  process.exit(1);\n"
            "}\n"
        )
        proc = subprocess.run(
            ["pnpm", "exec", "tsx", "-e", script],
            cwd=_REPO / "apps" / "runner",
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr or proc.stdout
        assert "ROLE_TOOL_FORBIDDEN" in proc.stdout
    finally:
        _drain_ready_plans(client, token)
        set_goal_orchestration_backend(
            engine, UUID(goal["id"]), "LEGACY", owner_epoch="1", force=True
        )
        engine.dispose()
