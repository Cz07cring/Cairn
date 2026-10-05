"""Immutable artifact catalog. Only trusted internal ingestion, no public upload."""

from alembic import op

revision = "0005_artifacts"
down_revision = "0004_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE artifacts (
      id uuid PRIMARY KEY,project_id uuid NOT NULL REFERENCES projects(id),
      digest text NOT NULL CHECK(digest ~ '^sha256:[0-9a-f]{64}$'),
      size_bytes bigint NOT NULL CHECK(size_bytes>=0 AND size_bytes<=9007199254740991),
      mime text NOT NULL,representation text NOT NULL CHECK(representation IN ('RAW','REDACTED','TRUNCATED')),
      derived_from_artifact_id uuid,producer_identity text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      UNIQUE(project_id,id),
      FOREIGN KEY(project_id,derived_from_artifact_id) REFERENCES artifacts(project_id,id),
      CHECK((representation='RAW' AND derived_from_artifact_id IS NULL) OR
        (representation IN ('REDACTED','TRUNCATED') AND derived_from_artifact_id IS NOT NULL)));
      CREATE INDEX artifacts_project_digest ON artifacts(project_id,digest);
      CREATE TRIGGER immutable_artifact BEFORE UPDATE OR DELETE ON artifacts
      FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();""")


def downgrade() -> None:
    raise RuntimeError("Immutable artifact history requires reviewed migration")
