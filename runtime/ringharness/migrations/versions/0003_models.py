"""Model profiles remain unverified until the probe lifecycle is implemented."""

from alembic import op

revision = "0003_models"
down_revision = "0002_policies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE model_profiles (
      id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects(id), name text NOT NULL,
      version bigint NOT NULL CHECK(version>0),content_digest text NOT NULL CHECK(content_digest ~ '^sha256:[0-9a-f]{64}$'),
      config jsonb NOT NULL CHECK(jsonb_typeof(config)='object'),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      UNIQUE(project_id,name,version),UNIQUE(project_id,id),
      CHECK(config ? 'project_id' AND config->>'project_id'=project_id::text),
      CHECK(config ? 'name' AND config->>'name'=name));
      CREATE INDEX model_profiles_page ON model_profiles(project_id,created_at,id);
      CREATE TRIGGER immutable_model_profile BEFORE UPDATE OR DELETE ON model_profiles
      FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();""")


def downgrade() -> None:
    raise RuntimeError("Immutable history removal requires separately reviewed migration")
