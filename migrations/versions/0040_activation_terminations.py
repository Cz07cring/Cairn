"""activation_terminations：per-attempt 确定性终止事实（M3.5）；≠ Goal DONE。"""

from alembic import op

revision = "0040_activation_terminations"
down_revision = "0039_goal_review_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
      CREATE TABLE activation_terminations (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        goal_id uuid NOT NULL REFERENCES goals(id),
        activity_id uuid NOT NULL REFERENCES activities(id),
        attempt_id uuid NOT NULL REFERENCES activity_attempts(id),
        reason text NOT NULL CHECK(
          reason IN (
            'NO_PROGRESS_STOP',
            'BUDGET_EXHAUSTED',
            'CANCELLED',
            'GOAL_REQUIRES_REVIEW'
          )
        ),
        detail text CHECK(
          detail IS NULL OR char_length(detail) BETWEEN 1 AND 2000),
        summary_artifact_id uuid REFERENCES artifacts(id),
        closeout_artifact_id uuid REFERENCES artifacts(id),
        worker_id uuid NOT NULL REFERENCES workers(id),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(attempt_id),
        UNIQUE(project_id, id),
        FOREIGN KEY (project_id, goal_id) REFERENCES goals(project_id, id),
        FOREIGN KEY (project_id, activity_id)
          REFERENCES activities(project_id, id)
      );
      CREATE INDEX activation_terminations_goal_page
        ON activation_terminations(goal_id, created_at DESC, id);
      CREATE TRIGGER immutable_activation_termination
        BEFORE UPDATE OR DELETE ON activation_terminations
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();
    """
    )


def downgrade() -> None:
    raise RuntimeError("activation_terminations 不可就地回滚")
