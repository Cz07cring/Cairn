"""orchestration_deliveries：START→ENSURE_WORKFLOW 投递记账（Temporal M1）。"""

from alembic import op

revision = "0031_orchestration_deliveries"
down_revision = "0030_orchestration_backend"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE orchestration_deliveries (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL,
        goal_id uuid NOT NULL,
        command_id uuid NOT NULL,
        event_kind text NOT NULL,
        workflow_id text NOT NULL,
        run_id text,
        delivery_status text NOT NULL CHECK (delivery_status IN ('PENDING','ACKNOWLEDGED')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (command_id, event_kind),
        UNIQUE (project_id, id),
        FOREIGN KEY (project_id, goal_id) REFERENCES goals(project_id, id),
        FOREIGN KEY (project_id, command_id) REFERENCES command_operations(project_id, id)
      );
      CREATE INDEX orchestration_deliveries_pending
        ON orchestration_deliveries(project_id, delivery_status, created_at, id)
        WHERE delivery_status = 'PENDING';
    """)


def downgrade() -> None:
    raise RuntimeError("orchestration_deliveries 不可就地回滚，需单独评审迁移")
