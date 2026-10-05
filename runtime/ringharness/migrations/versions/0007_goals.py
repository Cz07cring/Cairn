"""Goal 表：DRAFT 合同持久化；start/Activity 后续切片再接入。"""

from alembic import op

revision = "0007_goals"
down_revision = "0006_skills"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE goals (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        status text NOT NULL CHECK(status IN (
          'DRAFT','PLANNING','RUNNING','VERIFYING','PAUSING','PAUSED',
          'CANCELLING','CANCELLED','BLOCKED','FAILED','DONE')),
        state_revision bigint NOT NULL CHECK(state_revision>0),
        contract_revision bigint NOT NULL CHECK(contract_revision>0),
        plan_revision bigint CHECK(plan_revision IS NULL OR plan_revision>0),
        contract jsonb NOT NULL,
        contract_digest text NOT NULL CHECK(contract_digest ~ '^sha256:[0-9a-f]{64}$'),
        previous_status text,
        integration_commit text,
        criterion_verified int NOT NULL CHECK(criterion_verified>=0),
        criterion_total int NOT NULL CHECK(criterion_total>=1),
        budget_usage jsonb NOT NULL,
        block_reason text,
        write_epoch text NOT NULL CHECK(write_epoch ~ '^(0|[1-9][0-9]*)$'),
        release_manifest_id uuid,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id,id),
        CHECK(contract->>'project_id'=project_id::text),
        CHECK(criterion_verified<=criterion_total),
        CHECK(previous_status IS NULL OR previous_status IN (
          'DRAFT','PLANNING','RUNNING','VERIFYING','PAUSING','PAUSED',
          'CANCELLING','CANCELLED','BLOCKED','FAILED','DONE'))
      );
      CREATE INDEX goals_page ON goals(project_id,created_at,id);
      CREATE INDEX goals_status ON goals(project_id,status,created_at,id);
    """)


def downgrade() -> None:
    raise RuntimeError("Goal 历史不可就地回滚，需单独评审迁移")
