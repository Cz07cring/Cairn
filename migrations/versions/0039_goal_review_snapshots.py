"""goal_review_snapshots：Critic 权威运行快照字节（digest 钉扎，不可伪造）。"""

from alembic import op

revision = "0039_goal_review_snapshots"
down_revision = "0038_goal_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
      CREATE TABLE goal_review_snapshots (
        content_digest text PRIMARY KEY
          CHECK(content_digest ~ '^sha256:[0-9a-f]{64}$'),
        project_id uuid NOT NULL REFERENCES projects(id),
        goal_id uuid NOT NULL REFERENCES goals(id),
        payload jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY (project_id, goal_id) REFERENCES goals(project_id, id)
      );
      CREATE INDEX goal_review_snapshots_goal ON goal_review_snapshots(goal_id, created_at);
      CREATE TRIGGER immutable_goal_review_snapshot
        BEFORE UPDATE OR DELETE ON goal_review_snapshots
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();
    """
    )


def downgrade() -> None:
    raise RuntimeError("goal_review_snapshots 不可变快照账本不可就地回滚")
