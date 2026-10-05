"""持久 memories 表：GET 列表与内部 PROPOSED 写入。"""

from alembic import op

revision = "0025_memories"
down_revision = "0024_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE memories (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        activity_id uuid NOT NULL,
        kind text NOT NULL CHECK(kind IN (
          'fact','decision','failure','hypothesis','question')),
        statement text NOT NULL CHECK(char_length(statement) BETWEEN 1 AND 10000),
        source_evidence_ids uuid[] NOT NULL,
        confidence_bp int NOT NULL CHECK(confidence_bp BETWEEN 0 AND 10000),
        status text NOT NULL CHECK(status IN ('PROPOSED','VERIFIED','SUPERSEDED')),
        valid_from timestamptz NOT NULL,
        supersedes_id uuid,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id, id),
        FOREIGN KEY (project_id, activity_id) REFERENCES activities(project_id, id),
        FOREIGN KEY (project_id, supersedes_id) REFERENCES memories(project_id, id)
      );
      CREATE INDEX memories_project_page
        ON memories(project_id, created_at DESC, id);
    """)


def downgrade() -> None:
    raise RuntimeError("memories 证据表不可就地回滚")
