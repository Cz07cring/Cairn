"""审批资源表；prepare 绑定，dispatch 消费。"""

from alembic import op

revision = "0020_approvals"
down_revision = "0019_command_subject"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE approvals (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        goal_id uuid REFERENCES goals(id),
        state_revision int NOT NULL DEFAULT 1 CHECK(state_revision >= 1),
        subject_type text NOT NULL CHECK(subject_type IN ('EFFECT','MODEL_INVOCATION')),
        subject_id uuid NOT NULL,
        payload_digest text NOT NULL CHECK(payload_digest ~ '^sha256:[0-9a-f]{64}$'),
        policy_version int NOT NULL CHECK(policy_version >= 1),
        scope jsonb NOT NULL,
        max_cost_usd text NOT NULL CHECK(max_cost_usd ~ '^(0|[1-9][0-9]*)(\\.[0-9]{1,8})?$'),
        expires_at timestamptz NOT NULL,
        status text NOT NULL CHECK(status IN (
          'PENDING','APPROVED','DENIED','EXPIRED','REVOKED')),
        consumed_subject_id uuid,
        decision_reason text,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id, id),
        -- 同一 subject 同时最多一条未终态审批
        UNIQUE(subject_type, subject_id)
      );
      CREATE INDEX approvals_project_page
        ON approvals(project_id, created_at, id);
      CREATE INDEX approvals_status
        ON approvals(project_id, status, created_at, id);

      -- effect 对审批一对一绑定
      ALTER TABLE effect_intents
        ADD CONSTRAINT effect_intents_approval_fk
        FOREIGN KEY (approval_id) REFERENCES approvals(id);
      CREATE UNIQUE INDEX effect_intents_approval_uid
        ON effect_intents(approval_id) WHERE approval_id IS NOT NULL;
    """)


def downgrade() -> None:
    raise RuntimeError("审批表不可就地回滚，需单独评审迁移")
