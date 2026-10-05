"""Profiles reference a trusted deployment registry; there is no public verifier registration."""

from alembic import op

revision = "0004_verification"
down_revision = "0003_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE verifier_definitions (
      project_id uuid NOT NULL REFERENCES projects(id),ref text NOT NULL,content_digest text NOT NULL,
      content jsonb NOT NULL,approval_record_digest text NOT NULL,
      PRIMARY KEY(project_id,ref,content_digest),
      CHECK(content_digest ~ '^sha256:[0-9a-f]{64}$'),CHECK(approval_record_digest ~ '^sha256:[0-9a-f]{64}$'),
      CHECK(content ? 'project_id' AND content->>'project_id'=project_id::text));
      CREATE TRIGGER immutable_verifier BEFORE UPDATE OR DELETE ON verifier_definitions
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();
      CREATE TABLE verification_profiles (
       id uuid PRIMARY KEY,project_id uuid NOT NULL REFERENCES projects(id),name text NOT NULL,
       version bigint NOT NULL CHECK(version>0),content_digest text NOT NULL CHECK(content_digest ~ '^sha256:[0-9a-f]{64}$'),
       config jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
       verifier_ref text GENERATED ALWAYS AS (config->>'verifier_ref') STORED NOT NULL,
       verifier_digest text GENERATED ALWAYS AS (config->>'verifier_digest') STORED NOT NULL,
       UNIQUE(project_id,name,version),UNIQUE(project_id,id),
       FOREIGN KEY(project_id,verifier_ref,verifier_digest) REFERENCES verifier_definitions(project_id,ref,content_digest),
       CHECK(config ? 'project_id' AND config->>'project_id'=project_id::text),CHECK(config ? 'name' AND config->>'name'=name));
      CREATE INDEX verification_profiles_page ON verification_profiles(project_id,created_at,id);
      CREATE TRIGGER immutable_profile BEFORE UPDATE OR DELETE ON verification_profiles
       FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();
      CREATE TABLE reference_edges (
       project_id uuid NOT NULL REFERENCES projects(id),source_type text NOT NULL,source_id text NOT NULL,source_digest text NOT NULL,
       target_type text NOT NULL,target_id text NOT NULL,target_digest text NOT NULL,
       PRIMARY KEY(project_id,source_type,source_id,target_type,target_id),
       CHECK(source_digest ~ '^sha256:[0-9a-f]{64}$'),CHECK(target_digest ~ '^sha256:[0-9a-f]{64}$'));
      CREATE TRIGGER immutable_reference_edge BEFORE UPDATE OR DELETE ON reference_edges
       FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();""")


def downgrade() -> None:
    raise RuntimeError("Immutable evidence/config history requires reviewed migration")
