"""可信 EvidenceEnvelope：由 TrustedReceipt + Effect 生成，模型不可自报。"""

from alembic import op

revision = "0021_evidence_envelopes"
down_revision = "0020_approvals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE evidence_envelopes (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        goal_id uuid REFERENCES goals(id),
        task_id uuid REFERENCES tasks(id),
        producer_activity_id uuid NOT NULL REFERENCES activities(id),
        producer_attempt_id uuid NOT NULL REFERENCES activity_attempts(id),
        effect_id uuid NOT NULL REFERENCES effect_intents(id),
        receipt_id uuid NOT NULL,
        contract_digest text CHECK(contract_digest IS NULL OR contract_digest ~ '^sha256:[0-9a-f]{64}$'),
        candidate_manifest_id uuid REFERENCES candidate_manifests(id),
        verification_profile_id uuid,
        command_argv text[] NOT NULL,
        input_digest text NOT NULL CHECK(input_digest ~ '^sha256:[0-9a-f]{64}$'),
        environment_digest text NOT NULL CHECK(environment_digest ~ '^sha256:[0-9a-f]{64}$'),
        started_at timestamptz NOT NULL,
        finished_at timestamptz NOT NULL,
        exit_code int,
        signal text,
        timed_out bool NOT NULL,
        artifact_ids uuid[] NOT NULL DEFAULT '{}',
        producer_identity text NOT NULL CHECK(char_length(producer_identity) BETWEEN 1 AND 10000),
        content_digest text NOT NULL CHECK(content_digest ~ '^sha256:[0-9a-f]{64}$'),
        schema_version int NOT NULL DEFAULT 3 CHECK(schema_version = 3),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id, id),
        UNIQUE(effect_id, receipt_id),
        FOREIGN KEY (effect_id, receipt_id)
          REFERENCES effect_receipts(effect_id, receipt_id)
      );
      CREATE INDEX evidence_envelopes_task_page
        ON evidence_envelopes(task_id, created_at, id)
        WHERE task_id IS NOT NULL;
      CREATE INDEX evidence_envelopes_goal_page
        ON evidence_envelopes(goal_id, created_at, id)
        WHERE goal_id IS NOT NULL;
      CREATE TRIGGER evidence_envelopes_immutable
        BEFORE UPDATE OR DELETE ON evidence_envelopes
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();
    """)


def downgrade() -> None:
    raise RuntimeError("证据信封不可就地回滚，需单独评审迁移")
