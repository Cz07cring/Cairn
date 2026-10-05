"""goal_nudge_budgets：AB06 Nudge 预算跨进程持久（≠ Goal DONE）。"""

from alembic import op

revision = "0041_goal_nudge_budgets"
down_revision = "0040_activation_terminations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
      CREATE TABLE goal_nudge_budgets (
        goal_id uuid PRIMARY KEY REFERENCES goals(id),
        project_id uuid NOT NULL,
        consumed int NOT NULL CHECK(consumed >= 0),
        max_budget int NOT NULL CHECK(max_budget >= 1),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY (project_id, goal_id) REFERENCES goals(project_id, id),
        CHECK(consumed <= max_budget)
      );
    """
    )


def downgrade() -> None:
    raise RuntimeError("goal_nudge_budgets 不可就地回滚")
