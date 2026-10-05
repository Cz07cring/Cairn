"""Effect 回执表；失租不丢回执。"""

from alembic import op

revision = "0013_effect_receipts"
down_revision = "0012_steps_effects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE effect_receipts (
        receipt_id uuid NOT NULL,
        effect_id uuid NOT NULL REFERENCES effect_intents(id),
        project_id uuid NOT NULL REFERENCES projects(id),
        producer_activity_id uuid NOT NULL,
        producer_attempt_id uuid NOT NULL,
        fencing_epoch bigint NOT NULL,
        started_at timestamptz NOT NULL,
        finished_at timestamptz NOT NULL,
        exit_code int,
        signal text,
        timed_out bool NOT NULL DEFAULT false,
        stdout_artifact_id uuid,
        stderr_artifact_id uuid,
        result_artifact_ids uuid[] NOT NULL DEFAULT '{}',
        external_ref text,
        observed_outcome text NOT NULL CHECK(observed_outcome IN ('SUCCEEDED','FAILED','UNKNOWN')),
        disposition text NOT NULL CHECK(disposition IN (
          'APPLIED','PENDING_RECONCILIATION','DUPLICATE')),
        content_digest text NOT NULL CHECK(content_digest ~ '^sha256:[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (effect_id, receipt_id)
      );
      CREATE INDEX effect_receipts_project ON effect_receipts(project_id, effect_id);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS effect_receipts;")
