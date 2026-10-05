"""Activity steps 与 EffectIntent；PLAN 禁止工具路径。"""

from alembic import op

revision = "0012_steps_effects"
down_revision = "0011_model_probe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE activity_steps (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        activity_id uuid NOT NULL REFERENCES activities(id),
        attempt_id uuid NOT NULL REFERENCES activity_attempts(id),
        logical_step_id uuid NOT NULL,
        predecessor_step_id uuid REFERENCES activity_steps(id),
        purpose text NOT NULL CHECK(char_length(purpose) BETWEEN 1 AND 2000),
        tool_ref text NOT NULL CHECK(char_length(tool_ref) BETWEEN 1 AND 200),
        intent_revision int NOT NULL DEFAULT 1 CHECK(intent_revision=1),
        effect_id uuid,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(activity_id, logical_step_id),
        CHECK(id = logical_step_id)
      );
      CREATE UNIQUE INDEX activity_steps_root
        ON activity_steps(activity_id) WHERE predecessor_step_id IS NULL;
      CREATE UNIQUE INDEX activity_steps_successor
        ON activity_steps(activity_id, predecessor_step_id)
        WHERE predecessor_step_id IS NOT NULL;

      CREATE TABLE effect_intents (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        goal_id uuid REFERENCES goals(id),
        activity_id uuid NOT NULL REFERENCES activities(id),
        logical_step_id uuid NOT NULL,
        intent_revision int NOT NULL CHECK(intent_revision=1),
        payload_digest text NOT NULL CHECK(payload_digest ~ '^sha256:[0-9a-f]{64}$'),
        tool_ref text NOT NULL,
        replay_class text NOT NULL CHECK(replay_class IN (
          'READ_ONLY','IDEMPOTENT','RECONCILABLE','NON_REPLAYABLE')),
        scope text NOT NULL CHECK(scope IN (
          'ENGINEERING','VERIFICATION','RECONCILIATION','READ_ONLY')),
        write_epoch text,
        status text NOT NULL CHECK(status IN (
          'PREPARED','AUTHORIZED','DISPATCHED','SUCCEEDED','FAILED','UNKNOWN','CANCELLED')),
        state_revision int NOT NULL DEFAULT 1 CHECK(state_revision>=1),
        approval_id uuid,
        reservation_id uuid,
        external_ref text,
        evidence_ids uuid[] NOT NULL DEFAULT '{}',
        input_artifact_id uuid NOT NULL REFERENCES artifacts(id),
        producer_attempt_id uuid NOT NULL REFERENCES activity_attempts(id),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(activity_id, logical_step_id, intent_revision)
      );

      ALTER TABLE activity_steps
        ADD CONSTRAINT activity_steps_effect_fk
        FOREIGN KEY (effect_id) REFERENCES effect_intents(id);

      CREATE INDEX effect_intents_activity ON effect_intents(activity_id, id);
      CREATE INDEX effect_intents_project ON effect_intents(project_id, id);
    """)


def downgrade() -> None:
    op.execute("""
      ALTER TABLE activity_steps DROP CONSTRAINT IF EXISTS activity_steps_effect_fk;
      DROP TABLE IF EXISTS effect_intents;
      DROP TABLE IF EXISTS activity_steps;
    """)
