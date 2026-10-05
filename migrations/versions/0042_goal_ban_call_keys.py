"""goal_ban_call_keys：AB06 软禁同参键跨进程持久（≠ Goal DONE）。"""

from alembic import op

revision = "0042_goal_ban_call_keys"
down_revision = "0041_goal_nudge_budgets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
      CREATE TABLE goal_ban_call_keys (
        goal_id uuid NOT NULL,
        project_id uuid NOT NULL,
        call_key text NOT NULL CHECK(
          char_length(call_key) BETWEEN 1 AND 512),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (goal_id, call_key),
        FOREIGN KEY (project_id, goal_id) REFERENCES goals(project_id, id)
      );
      CREATE INDEX goal_ban_call_keys_goal_page
        ON goal_ban_call_keys(goal_id, created_at DESC, call_key);
    """
    )


def downgrade() -> None:
    raise RuntimeError("goal_ban_call_keys 不可就地回滚")
