"""finalization_barriers + release_manifests。"""

from alembic import op

revision = "0016_finalization"
down_revision = "0015_audits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE finalization_barriers (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        goal_id uuid NOT NULL REFERENCES goals(id),
        write_epoch text NOT NULL CHECK(write_epoch ~ '^(0|[1-9][0-9]*)$'),
        status text NOT NULL CHECK(status IN ('DRAINING','SEALED','RELEASED','ABORTED')),
        contract_revision int NOT NULL,
        plan_revision int NOT NULL,
        candidate_manifest_id uuid REFERENCES candidate_manifests(id),
        in_flight_engineering int NOT NULL DEFAULT 0 CHECK(in_flight_engineering>=0),
        unknown_effects int NOT NULL DEFAULT 0 CHECK(unknown_effects>=0),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(goal_id, write_epoch),
        UNIQUE(project_id, id)
      );
      CREATE INDEX barriers_goal ON finalization_barriers(goal_id, created_at DESC);

      CREATE TABLE release_manifests (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        goal_id uuid NOT NULL REFERENCES goals(id),
        barrier_id uuid NOT NULL REFERENCES finalization_barriers(id),
        write_epoch text NOT NULL,
        goal_contract_digest text NOT NULL CHECK(goal_contract_digest ~ '^sha256:[0-9a-f]{64}$'),
        plan_digest text NOT NULL CHECK(plan_digest ~ '^sha256:[0-9a-f]{64}$'),
        candidate_manifest_id uuid NOT NULL REFERENCES candidate_manifests(id),
        verification_profile_ids uuid[] NOT NULL,
        audit_ids uuid[] NOT NULL,
        evidence_ids uuid[] NOT NULL DEFAULT '{}',
        content_digest text NOT NULL CHECK(content_digest ~ '^sha256:[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(goal_id),
        UNIQUE(project_id, id),
        UNIQUE(content_digest)
      );
      CREATE TRIGGER immutable_release BEFORE UPDATE OR DELETE ON release_manifests
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();

      ALTER TABLE goals
        ADD CONSTRAINT goals_release_fk
        FOREIGN KEY (release_manifest_id) REFERENCES release_manifests(id);
    """)


def downgrade() -> None:
    op.execute("""
      ALTER TABLE goals DROP CONSTRAINT IF EXISTS goals_release_fk;
      DROP TABLE IF EXISTS release_manifests;
      DROP TABLE IF EXISTS finalization_barriers;
    """)
