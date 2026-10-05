"""PlanningFeedback 不可变表与消费关系。"""

from alembic import op

revision = "0018_planning_feedback"
down_revision = "0017_goal_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE planning_feedbacks (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        goal_id uuid NOT NULL REFERENCES goals(id),
        task_id uuid REFERENCES tasks(id),
        candidate_manifest_id uuid NOT NULL REFERENCES candidate_manifests(id),
        goal_contract_revision bigint NOT NULL CHECK(goal_contract_revision > 0),
        task_contract_revision bigint CHECK(task_contract_revision IS NULL OR task_contract_revision > 0),
        plan_revision bigint NOT NULL CHECK(plan_revision > 0),
        aggregation_digest text NOT NULL CHECK(aggregation_digest ~ '^sha256:[0-9a-f]{64}$'),
        verdict text NOT NULL CHECK(verdict IN ('PASS','INSUFFICIENT','FAIL')),
        public_criterion_results jsonb NOT NULL,
        blocking_reason_codes text[] NOT NULL DEFAULT '{}',
        content_digest text NOT NULL CHECK(content_digest ~ '^sha256:[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id, id)
      );
      -- task_id NULL 采用 NULLS NOT DISTINCT，避免空 Task 反馈去重失效。
      CREATE UNIQUE INDEX planning_feedbacks_dedupe
        ON planning_feedbacks (
          goal_id, task_id, candidate_manifest_id,
          goal_contract_revision, plan_revision, aggregation_digest
        ) NULLS NOT DISTINCT;
      CREATE INDEX planning_feedbacks_goal ON planning_feedbacks(goal_id, created_at DESC, id);
      CREATE TRIGGER planning_feedbacks_immutable BEFORE UPDATE OR DELETE ON planning_feedbacks
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();

      CREATE TABLE planning_feedback_consumptions (
        plan_activity_id uuid NOT NULL REFERENCES activities(id),
        feedback_id uuid NOT NULL REFERENCES planning_feedbacks(id),
        attempt_id uuid NOT NULL REFERENCES activity_attempts(id),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(plan_activity_id, feedback_id)
      );
      CREATE TRIGGER planning_feedback_consumptions_immutable
        BEFORE UPDATE OR DELETE ON planning_feedback_consumptions
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();
    """)


def downgrade() -> None:
    raise RuntimeError("Immutable feedback history requires reviewed migration")
