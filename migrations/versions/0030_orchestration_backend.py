"""Goal orchestration_backend / owner_epoch + orchestration_bindings（Temporal M0）。"""

from alembic import op

revision = "0030_orchestration_backend"
down_revision = "0029_verification_obligations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE goals
        ADD COLUMN orchestration_backend text NOT NULL DEFAULT 'LEGACY'
          CHECK (orchestration_backend IN ('LEGACY','TEMPORAL')),
        ADD COLUMN owner_epoch text NOT NULL DEFAULT '1'
          CHECK (owner_epoch ~ '^(0|[1-9][0-9]*)$');

      -- 每 Goal 至多一条编排绑定；budget_scope_id 与 activities 同为裸 uuid（无独立 budget_scopes 表）
      CREATE TABLE orchestration_bindings (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        goal_id uuid NOT NULL,
        budget_scope_id uuid NOT NULL,
        backend text NOT NULL CHECK (backend IN ('LEGACY','TEMPORAL')),
        owner_epoch text NOT NULL CHECK (owner_epoch ~ '^(0|[1-9][0-9]*)$'),
        namespace text NOT NULL,
        workflow_id text NOT NULL,
        active_run_id text,
        worker_build_id text NOT NULL,
        contract_digest text NOT NULL CHECK (contract_digest ~ '^sha256:[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (goal_id),
        UNIQUE (project_id, workflow_id),
        UNIQUE (project_id, id),
        FOREIGN KEY (project_id, goal_id) REFERENCES goals(project_id, id)
      );
    """)


def downgrade() -> None:
    raise RuntimeError("orchestration_backend / bindings 不可就地回滚，需单独评审迁移")
