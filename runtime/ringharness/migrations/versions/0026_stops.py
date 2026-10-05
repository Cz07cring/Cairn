"""stops / stop_receipts：停止意图与可信观察（05§3.6 DEV03 最小切片）。"""

from alembic import op

revision = "0026_stops"
down_revision = "0025_memories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE stops (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        request_id uuid NOT NULL,
        -- 本切片：activation_id 与 attempt_id 同值（见 storage/stops 文档注释）
        activation_id uuid NOT NULL,
        activity_id uuid NOT NULL,
        attempt_id uuid NOT NULL,
        fencing_epoch bigint NOT NULL CHECK(fencing_epoch >= 0),
        reason text NOT NULL CHECK(reason IN (
          'PAUSE','CANCEL','LEASE_EXPIRED','FINALIZATION_RECOVERY','SHUTDOWN')),
        deadline_at timestamptz NOT NULL,
        status text NOT NULL CHECK(status IN (
          'REQUESTED','CONFIRMED','UNCONFIRMED')),
        state_revision bigint NOT NULL CHECK(state_revision > 0),
        receipt_ids uuid[] NOT NULL DEFAULT '{}',
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id, id),
        UNIQUE(activation_id, request_id),
        FOREIGN KEY (project_id, activity_id) REFERENCES activities(project_id, id),
        FOREIGN KEY (project_id, attempt_id) REFERENCES activity_attempts(project_id, id),
        CHECK(activation_id = attempt_id)
      );
      CREATE INDEX stops_activity ON stops(project_id, activity_id, created_at DESC, id);

      CREATE TABLE stop_receipts (
        receipt_id uuid PRIMARY KEY,
        stop_id uuid NOT NULL REFERENCES stops(id),
        project_id uuid NOT NULL REFERENCES projects(id),
        activation_id uuid NOT NULL,
        attempt_id uuid NOT NULL,
        resource_instance_id uuid NOT NULL,
        observed_at timestamptz NOT NULL,
        observation text NOT NULL CHECK(observation IN (
          'EXITED','ISOLATED','RUNNING','UNKNOWN')),
        compute_released boolean NOT NULL,
        write_capability_revoked boolean NOT NULL,
        proof_artifact_ids uuid[] NOT NULL DEFAULT '{}',
        disposition text NOT NULL CHECK(disposition IN (
          'APPLIED','PENDING_RECONCILIATION','DUPLICATE')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id, receipt_id),
        UNIQUE(stop_id, receipt_id)
      );
      CREATE INDEX stop_receipts_stop ON stop_receipts(stop_id, created_at, receipt_id);
    """)


def downgrade() -> None:
    raise RuntimeError("停止证明账本不可就地回滚")
