"""Project API foundation only; runtime tables follow subsequent slices."""

from alembic import op

revision = "0001_projects"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE projects (
        id uuid PRIMARY KEY, name text NOT NULL CHECK(length(btrim(name)) BETWEEN 1 AND 200),
        repository_ref text NOT NULL CHECK(length(repository_ref) BETWEEN 1 AND 200),
        state_revision bigint NOT NULL DEFAULT 1 CHECK(state_revision>0),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp());
        CREATE INDEX projects_page ON projects(created_at,id);
        CREATE TABLE project_memberships (
          project_id uuid REFERENCES projects(id), subject text NOT NULL, PRIMARY KEY(project_id,subject));
        CREATE TABLE project_trust_states (
          project_id uuid PRIMARY KEY REFERENCES projects(id), trust_revision bigint NOT NULL DEFAULT 1 CHECK(trust_revision>0),
          status text NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','BLOCKED')));
        CREATE TABLE project_requests (
          scope text PRIMARY KEY, body_digest text NOT NULL, result jsonb NOT NULL,
          created_at timestamptz NOT NULL DEFAULT clock_timestamp());
        CREATE TABLE project_events (
          id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects(id), kind text NOT NULL,
          payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp());""")


def downgrade() -> None:
    raise RuntimeError("Destructive downgrade requires a separately reviewed data migration")
