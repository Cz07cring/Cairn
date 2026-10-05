"""Plan / Task 表；支持 PLAN outcome 发布。"""

from alembic import op

revision = "0010_plans"
down_revision = "0009_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE plans (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        goal_id uuid NOT NULL,
        plan_revision bigint CHECK(plan_revision IS NULL OR plan_revision>0),
        status text NOT NULL CHECK(status IN ('CANDIDATE','PUBLISHED','REJECTED')),
        reason text NOT NULL,
        tasks jsonb NOT NULL,
        coverage jsonb NOT NULL,
        content_digest text CHECK(content_digest IS NULL OR content_digest ~ '^sha256:[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id,id),
        FOREIGN KEY(project_id,goal_id) REFERENCES goals(project_id,id),
        UNIQUE(project_id,goal_id,plan_revision)
      );
      CREATE INDEX plans_goal_page ON plans(project_id,goal_id,created_at,id);

      CREATE TABLE tasks (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        goal_id uuid NOT NULL,
        contract jsonb NOT NULL,
        contract_digest text NOT NULL CHECK(contract_digest ~ '^sha256:[0-9a-f]{64}$'),
        contract_revision bigint NOT NULL CHECK(contract_revision>0),
        plan_revision bigint NOT NULL CHECK(plan_revision>0),
        state_revision bigint NOT NULL CHECK(state_revision>0),
        status text NOT NULL,
        block_reason text,
        resume_state text,
        work_lineage_id uuid NOT NULL,
        execution_round int NOT NULL CHECK(execution_round>0),
        latest_checkpoint_id uuid,
        replaces_task_id uuid,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id,id),
        FOREIGN KEY(project_id,goal_id) REFERENCES goals(project_id,id)
      );
      CREATE INDEX tasks_goal_page ON tasks(project_id,goal_id,created_at,id);

      CREATE TABLE task_edges (
        project_id uuid NOT NULL,
        goal_id uuid NOT NULL,
        plan_revision bigint NOT NULL,
        from_task_id uuid NOT NULL,
        to_task_id uuid NOT NULL,
        PRIMARY KEY(project_id,goal_id,plan_revision,from_task_id,to_task_id)
      );

      CREATE TABLE criterion_coverage (
        project_id uuid NOT NULL,
        goal_id uuid NOT NULL,
        plan_revision bigint NOT NULL,
        goal_criterion_id text NOT NULL,
        task_id uuid NOT NULL,
        task_acceptance_id text NOT NULL,
        verification_profile_id uuid NOT NULL,
        PRIMARY KEY(project_id,goal_id,plan_revision,goal_criterion_id,task_id,task_acceptance_id)
      );
    """)


def downgrade() -> None:
    raise RuntimeError("Plan/Task 历史不可就地回滚，需单独评审迁移")
