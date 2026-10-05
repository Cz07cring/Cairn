"""ReleaseValidityRecord：发布后证据失效的追加事实（不改 ReleaseManifest）。"""

from alembic import op

revision = "0034_release_validity_records"
down_revision = "0033_trust_invalidation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE release_validity_records (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        release_manifest_id uuid NOT NULL,
        status text NOT NULL CHECK(status = 'INVALIDATED'),
        decision_ids uuid[] NOT NULL CHECK(cardinality(decision_ids) >= 1),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id, id),
        FOREIGN KEY (project_id, release_manifest_id)
          REFERENCES release_manifests(project_id, id)
      );
      CREATE INDEX release_validity_records_release_page
        ON release_validity_records(release_manifest_id, created_at, id);
      CREATE TRIGGER immutable_release_validity_record
        BEFORE UPDATE OR DELETE ON release_validity_records
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();
    """)


def downgrade() -> None:
    raise RuntimeError("release_validity_records 不可就地回滚")
