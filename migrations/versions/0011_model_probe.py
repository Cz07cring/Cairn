"""模型探测：capability 可变列、ModelInvocation/Receipt、ContextBundle。"""

from alembic import op

revision = "0011_model_probe"
down_revision = "0010_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE model_profiles
        ADD COLUMN capability_status text NOT NULL DEFAULT 'UNVERIFIED'
          CHECK(capability_status IN ('UNVERIFIED','VERIFIED','FAILED')),
        ADD COLUMN probe_evidence_ids uuid[] NOT NULL DEFAULT '{}';

      DROP TRIGGER IF EXISTS immutable_model_profile ON model_profiles;
      CREATE FUNCTION reject_model_profile_content_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.project_id IS DISTINCT FROM OLD.project_id
           OR NEW.name IS DISTINCT FROM OLD.name
           OR NEW.version IS DISTINCT FROM OLD.version
           OR NEW.content_digest IS DISTINCT FROM OLD.content_digest
           OR NEW.config IS DISTINCT FROM OLD.config THEN
          RAISE EXCEPTION 'immutable model profile content';
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER model_profile_content_immutable BEFORE UPDATE ON model_profiles
        FOR EACH ROW EXECUTE FUNCTION reject_model_profile_content_mutation();
      CREATE TRIGGER model_profile_no_delete BEFORE DELETE ON model_profiles
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();

      ALTER TABLE activities DROP CONSTRAINT IF EXISTS activities_check;
      ALTER TABLE activities ADD CONSTRAINT activities_kind_target_check CHECK(
        (kind='PLAN' AND target_type='GOAL_PLAN' AND goal_id IS NOT NULL AND task_id IS NULL)
        OR (kind='EXECUTE' AND target_type='TASK_WORK' AND goal_id IS NOT NULL AND task_id IS NOT NULL)
        OR (kind='PROBE_MODEL' AND target_type='MODEL_PROFILE' AND goal_id IS NULL AND task_id IS NULL)
        OR kind NOT IN ('PLAN','EXECUTE','PROBE_MODEL')
      );

      CREATE TABLE context_bundles (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        activity_id uuid NOT NULL REFERENCES activities(id),
        attempt_id uuid NOT NULL REFERENCES activity_attempts(id),
        content_digest text NOT NULL CHECK(content_digest ~ '^sha256:[0-9a-f]{64}$'),
        content jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(attempt_id),
        UNIQUE(project_id,id)
      );

      CREATE TABLE model_invocations (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        goal_id uuid,
        activity_id uuid NOT NULL REFERENCES activities(id),
        producer_attempt_id uuid NOT NULL REFERENCES activity_attempts(id),
        invocation_seq bigint NOT NULL CHECK(invocation_seq>=1),
        payload_digest text NOT NULL CHECK(payload_digest ~ '^sha256:[0-9a-f]{64}$'),
        binding_digest text NOT NULL CHECK(binding_digest ~ '^sha256:[0-9a-f]{64}$'),
        context_digest text NOT NULL CHECK(context_digest ~ '^sha256:[0-9a-f]{64}$'),
        input_digest text NOT NULL CHECK(input_digest ~ '^sha256:[0-9a-f]{64}$'),
        provider_ref text NOT NULL,
        model_id text NOT NULL,
        max_output_tokens int NOT NULL CHECK(max_output_tokens>0),
        max_cost_usd text NOT NULL,
        data_categories text[] NOT NULL DEFAULT '{}',
        status text NOT NULL CHECK(status IN (
          'PREPARED','AWAITING_APPROVAL','AUTHORIZED','DISPATCHED',
          'SUCCEEDED','FAILED','UNKNOWN','CANCELLED')),
        state_revision bigint NOT NULL CHECK(state_revision>0),
        approval_id uuid,
        reservation_id uuid NOT NULL,
        usage_status text NOT NULL CHECK(usage_status IN ('CONFIRMED','UNKNOWN')),
        result_artifact_id uuid,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(activity_id,invocation_seq),
        UNIQUE(project_id,id)
      );
      CREATE INDEX model_invocations_activity ON model_invocations(project_id,activity_id,created_at,id);

      CREATE TABLE model_receipts (
        receipt_id uuid PRIMARY KEY,
        invocation_id uuid NOT NULL REFERENCES model_invocations(id),
        project_id uuid NOT NULL REFERENCES projects(id),
        producer_attempt_id uuid NOT NULL,
        observed_result text NOT NULL CHECK(observed_result IN ('SUCCEEDED','FAILED','UNKNOWN')),
        usage_status text NOT NULL CHECK(usage_status IN ('CONFIRMED','UNKNOWN')),
        input_tokens bigint,
        output_tokens bigint,
        cost_usd text,
        result_artifact_id uuid,
        observed_at timestamptz NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id,receipt_id)
      );
    """)


def downgrade() -> None:
    raise RuntimeError("模型探测账本不可就地回滚，需单独评审迁移")
