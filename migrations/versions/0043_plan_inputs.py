"""plan_inputs：Cairn PlanInput/v1 登记（R1a；≠ PLAN admit，≠ Goal DONE）。

append-only 封存：canonical 原始字节（payload_bytes，digest 权威账本）+ jsonb
投影 + 从 Ring DB 核验后的 CANDIDATE ID/digest 钉扎。不建 Plan/Task、不改 Goal
修订、不派发 Runner/Temporal。
"""

from alembic import op

revision = "0043_plan_inputs"
down_revision = "0042_goal_ban_call_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
      CREATE TABLE plan_inputs (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL,
        goal_id uuid NOT NULL,
        status text NOT NULL CHECK(status IN ('STAGED','BOUND','STALE')),
        payload jsonb NOT NULL,
        payload_bytes bytea NOT NULL,
        content_digest text NOT NULL CHECK(content_digest ~ '^sha256:[0-9a-f]{64}$'),
        candidate_plan_id uuid NOT NULL,
        candidate_content_digest text NOT NULL
          CHECK(candidate_content_digest ~ '^sha256:[0-9a-f]{64}$'),
        goal_contract_revision bigint NOT NULL CHECK(goal_contract_revision>0),
        expected_plan_revision bigint
          CHECK(expected_plan_revision IS NULL OR expected_plan_revision>0),
        created_by text NOT NULL CHECK(char_length(created_by) BETWEEN 1 AND 200),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id,id),
        FOREIGN KEY(project_id,goal_id) REFERENCES goals(project_id,id),
        FOREIGN KEY(project_id,candidate_plan_id) REFERENCES plans(project_id,id),
        -- 有界性兜底：协议层已按 Cairn canonical ≤8192B 校验，此处防旁路写入。
        -- digest 复现性由读取侧（get_plan_input）逐字节校验，jsonb 只是投影。
        CHECK(octet_length(payload_bytes)<=8192)
      );
      CREATE INDEX plan_inputs_goal_page
        ON plan_inputs(project_id,goal_id,created_at DESC,id);
      CREATE TRIGGER immutable_plan_input
        BEFORE UPDATE OR DELETE ON plan_inputs
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();
    """
    )


def downgrade() -> None:
    raise RuntimeError("plan_inputs 不可变登记账本不可就地回滚")
