"""Immutable project policy versions; no execution permission granted by storage alone."""

from alembic import op

revision = "0002_policies"
down_revision = "0001_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE policies (
        id uuid PRIMARY KEY,project_id uuid NOT NULL REFERENCES projects(id),name text NOT NULL,
        version bigint NOT NULL CHECK(version>0),content_digest text NOT NULL CHECK(content_digest ~ '^sha256:[0-9a-f]{64}$'),
        config jsonb NOT NULL CHECK(jsonb_typeof(config)='object'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id,name,version),UNIQUE(project_id,id),
        CHECK(config->>'project_id'=project_id::text),CHECK(config->>'name'=name));
        CREATE INDEX policies_page ON policies(project_id,created_at,id);
        CREATE FUNCTION reject_immutable_config_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'immutable configuration'; END $$;
        CREATE TRIGGER immutable_policy BEFORE UPDATE OR DELETE ON policies
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();""")


def downgrade() -> None:
    raise RuntimeError("Immutable history removal requires a separately reviewed migration")
