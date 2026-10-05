"""orchestration_abandonments：编排层放弃恢复的可审计业务事实（Issue #24）。"""

from alembic import op

revision = "0035_orchestration_abandonments"
down_revision = "0034_release_validity_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE orchestration_abandonments (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        goal_id uuid NOT NULL REFERENCES goals(id),
        generation int NOT NULL CHECK(generation >= 0),
        reason text NOT NULL CHECK(char_length(reason) BETWEEN 1 AND 200),
        prior_run_id text CHECK(
          prior_run_id IS NULL OR char_length(prior_run_id) BETWEEN 1 AND 200),
        worker_id uuid NOT NULL REFERENCES workers(id),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(goal_id, generation),
        UNIQUE(project_id, id),
        FOREIGN KEY (project_id, goal_id) REFERENCES goals(project_id, id)
      );
      CREATE INDEX orchestration_abandonments_goal_page
        ON orchestration_abandonments(goal_id, created_at DESC, id);
      CREATE TRIGGER immutable_orchestration_abandonment
        BEFORE UPDATE OR DELETE ON orchestration_abandonments
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();
    """)


def downgrade() -> None:
    raise RuntimeError("orchestration_abandonments 不可就地回滚")
