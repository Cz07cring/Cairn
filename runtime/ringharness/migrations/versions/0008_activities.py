"""Activity 与 CommandOperation 表；支持 Goal.start 同事务创建 PLAN。"""

from alembic import op

revision = "0008_activities"
down_revision = "0007_goals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE activities (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        goal_id uuid,
        task_id uuid,
        budget_scope_id uuid NOT NULL,
        kind text NOT NULL CHECK(kind IN (
          'PLAN','EXECUTE','AUDIT','INTEGRATE','RECONCILE','FINALIZE',
          'PROBE_MODEL','VALIDATE_SKILL','INDEX_MEMORY','EXPORT_EVIDENCE')),
        target_type text NOT NULL,
        target_id uuid NOT NULL,
        binding jsonb NOT NULL,
        verification_assignments jsonb NOT NULL DEFAULT '[]'::jsonb,
        status text NOT NULL CHECK(status IN (
          'PENDING','READY','RUNNING','WAITING','RECOVERING','SUCCEEDED','FAILED','CANCELLED')),
        state_revision bigint NOT NULL CHECK(state_revision>0),
        depends_on_activity_ids uuid[] NOT NULL DEFAULT '{}',
        wait_reason text,
        wake_at timestamptz,
        wait_deadline_at timestamptz,
        resume_state text CHECK(resume_state IS NULL OR resume_state='READY'),
        retry_count int NOT NULL DEFAULT 0 CHECK(retry_count>=0),
        current_attempt_id uuid,
        resources jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id,id),
        FOREIGN KEY(project_id,goal_id) REFERENCES goals(project_id,id),
        CHECK((kind='PLAN' AND target_type='GOAL_PLAN' AND goal_id IS NOT NULL AND task_id IS NULL)
           OR kind<>'PLAN')
      );
      CREATE INDEX activities_goal_page ON activities(project_id,goal_id,created_at,id);
      CREATE INDEX activities_claim ON activities(kind,status,created_at,id);

      CREATE TABLE command_operations (
        id uuid PRIMARY KEY,
        project_id uuid REFERENCES projects(id),
        goal_id uuid,
        kind text NOT NULL,
        status text NOT NULL CHECK(status IN ('ACCEPTED','RUNNING','SUCCEEDED','FAILED')),
        request_digest text NOT NULL CHECK(request_digest ~ '^sha256:[0-9a-f]{64}$'),
        result jsonb,
        error jsonb,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id,id),
        CHECK((goal_id IS NULL) OR (project_id IS NOT NULL))
      );
      CREATE INDEX commands_goal ON command_operations(project_id,goal_id,created_at,id);
    """)


def downgrade() -> None:
    raise RuntimeError("Activity 历史不可就地回滚，需单独评审迁移")
