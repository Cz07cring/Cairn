"""goal_reviews：周期诊断落库（≠ criterion PASS / ≠ Goal DONE）。"""

from alembic import op

revision = "0038_goal_reviews"
down_revision = "0037_model_exposed_tools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # AUDIT 允许 target=GOAL_REVIEW（无 Task）；候选 AUDIT 约束保持不变。
    op.execute(
        """
      ALTER TABLE activities DROP CONSTRAINT IF EXISTS activities_kind_target_check;
      ALTER TABLE activities ADD CONSTRAINT activities_kind_target_check CHECK(
        (kind='PLAN' AND target_type='GOAL_PLAN' AND goal_id IS NOT NULL AND task_id IS NULL)
        OR (kind='EXECUTE' AND target_type='TASK_WORK' AND goal_id IS NOT NULL AND task_id IS NOT NULL)
        OR (kind='PROBE_MODEL' AND target_type='MODEL_PROFILE' AND goal_id IS NULL AND task_id IS NULL)
        OR (kind='AUDIT' AND target_type='CANDIDATE' AND goal_id IS NOT NULL AND task_id IS NOT NULL)
        OR (kind='AUDIT' AND target_type='GOAL_REVIEW' AND goal_id IS NOT NULL AND task_id IS NULL
            AND target_id=goal_id)
        OR (kind='VALIDATE_SKILL' AND target_type='SKILL_VERSION' AND goal_id IS NULL AND task_id IS NULL)
        OR (kind='INDEX_MEMORY' AND target_type='MEMORY_INDEX' AND goal_id IS NULL AND task_id IS NULL)
        OR kind NOT IN ('PLAN','EXECUTE','PROBE_MODEL','AUDIT','VALIDATE_SKILL','INDEX_MEMORY')
      );
    """
    )
    op.execute(
        """
      CREATE TABLE goal_reviews (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        goal_id uuid NOT NULL REFERENCES goals(id),
        activity_id uuid NOT NULL REFERENCES activities(id),
        attempt_id uuid NOT NULL REFERENCES activity_attempts(id),
        goal_contract_revision bigint NOT NULL CHECK(goal_contract_revision > 0),
        plan_revision bigint CHECK(plan_revision IS NULL OR plan_revision > 0),
        review_snapshot_digest text NOT NULL
          CHECK(review_snapshot_digest ~ '^sha256:[0-9a-f]{64}$'),
        findings jsonb NOT NULL,
        content_digest text NOT NULL CHECK(content_digest ~ '^sha256:[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id, id),
        UNIQUE(activity_id),
        FOREIGN KEY (project_id, goal_id) REFERENCES goals(project_id, id),
        FOREIGN KEY (project_id, activity_id) REFERENCES activities(project_id, id),
        FOREIGN KEY (project_id, attempt_id) REFERENCES activity_attempts(project_id, id)
      );
      CREATE INDEX goal_reviews_goal ON goal_reviews(goal_id, created_at, id);
      CREATE TRIGGER immutable_goal_review BEFORE UPDATE OR DELETE ON goal_reviews
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();
    """
    )


def downgrade() -> None:
    raise RuntimeError("goal_reviews 不可变诊断账本不可就地回滚")
