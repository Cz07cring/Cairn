"""持久 checkpoints 表：宿主 POST 采纳后可供 GET 列表。"""

from alembic import op

revision = "0024_checkpoints"
down_revision = "0023_reference_edges_digest_pk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE checkpoints (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        activity_id uuid NOT NULL,
        attempt_id uuid NOT NULL,
        kind text NOT NULL CHECK(kind IN (
          'PLAN','EXECUTE','AUDIT','INTEGRATE','RECONCILE','FINALIZE',
          'PROBE_MODEL','VALIDATE_SKILL','INDEX_MEMORY','EXPORT_EVIDENCE')),
        context_digest text NOT NULL CHECK(context_digest ~ '^sha256:[0-9a-f]{64}$'),
        completed_step_ids uuid[] NOT NULL,
        next_step_id uuid,
        candidate_manifest_id uuid,
        workspace_manifest_digest text CHECK(
          workspace_manifest_digest IS NULL
          OR workspace_manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
        session_ref text,
        artifact_ids uuid[] NOT NULL,
        effect_ids uuid[] NOT NULL,
        content_digest text NOT NULL CHECK(content_digest ~ '^sha256:[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id, id),
        UNIQUE(content_digest),
        FOREIGN KEY (project_id, activity_id) REFERENCES activities(project_id, id),
        FOREIGN KEY (project_id, attempt_id) REFERENCES activity_attempts(project_id, id)
      );
      CREATE INDEX checkpoints_activity_page
        ON checkpoints(activity_id, created_at, id);
    """)


def downgrade() -> None:
    raise RuntimeError("checkpoints 证据表不可就地回滚")
