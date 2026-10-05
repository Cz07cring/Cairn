"""候选封存与 ProtectedBaseline。"""

from alembic import op

revision = "0014_candidates"
down_revision = "0013_effect_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE protected_baselines (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        goal_id uuid NOT NULL REFERENCES goals(id),
        goal_contract_revision int NOT NULL CHECK(goal_contract_revision>=1),
        content_digest text NOT NULL CHECK(content_digest ~ '^sha256:[0-9a-f]{64}$'),
        content jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(goal_id, goal_contract_revision),
        UNIQUE(project_id, id)
      );
      CREATE TRIGGER immutable_protected_baseline BEFORE UPDATE OR DELETE ON protected_baselines
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();

      CREATE TABLE candidate_manifests (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        goal_id uuid NOT NULL REFERENCES goals(id),
        task_id uuid REFERENCES tasks(id),
        activity_id uuid NOT NULL REFERENCES activities(id),
        attempt_id uuid NOT NULL REFERENCES activity_attempts(id),
        protected_baseline_digest text NOT NULL,
        content_digest text NOT NULL CHECK(content_digest ~ '^sha256:[0-9a-f]{64}$'),
        content jsonb NOT NULL,
        workspace_snapshot_artifact_id uuid NOT NULL REFERENCES artifacts(id),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id, id),
        UNIQUE(content_digest)
      );
      CREATE INDEX candidate_manifests_goal ON candidate_manifests(goal_id, id);
      CREATE INDEX candidate_manifests_task ON candidate_manifests(task_id, id);
      CREATE TRIGGER immutable_candidate_manifest BEFORE UPDATE OR DELETE ON candidate_manifests
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();

      ALTER TABLE activities DROP CONSTRAINT IF EXISTS activities_kind_target_check;
      ALTER TABLE activities ADD CONSTRAINT activities_kind_target_check CHECK(
        (kind='PLAN' AND target_type='GOAL_PLAN' AND goal_id IS NOT NULL AND task_id IS NULL)
        OR (kind='EXECUTE' AND target_type='TASK_WORK' AND goal_id IS NOT NULL AND task_id IS NOT NULL)
        OR (kind='PROBE_MODEL' AND target_type='MODEL_PROFILE' AND goal_id IS NULL AND task_id IS NULL)
        OR (kind='AUDIT' AND target_type='CANDIDATE' AND goal_id IS NOT NULL AND task_id IS NOT NULL)
        OR kind NOT IN ('PLAN','EXECUTE','PROBE_MODEL','AUDIT')
      );
    """)


def downgrade() -> None:
    op.execute("""
      DROP TABLE IF EXISTS candidate_manifests;
      DROP TABLE IF EXISTS protected_baselines;
    """)
