"""ActivityAttempt / worker 登记与 claim、heartbeat。"""

from alembic import op

revision = "0009_claims"
down_revision = "0008_activities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE workers (
        id uuid PRIMARY KEY,
        subject text NOT NULL UNIQUE,
        allowed_kinds text[] NOT NULL,
        capabilities text[] NOT NULL DEFAULT '{}',
        cpu_millicores int NOT NULL CHECK(cpu_millicores>0),
        memory_bytes bigint NOT NULL CHECK(memory_bytes>0),
        disk_bytes bigint NOT NULL CHECK(disk_bytes>0),
        model_slots int NOT NULL CHECK(model_slots>=0),
        browser_slots int NOT NULL CHECK(browser_slots>=0),
        status text NOT NULL CHECK(status='ACTIVE'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
      );

      CREATE TABLE activity_attempts (
        id uuid PRIMARY KEY,
        activity_id uuid NOT NULL REFERENCES activities(id),
        project_id uuid NOT NULL REFERENCES projects(id),
        worker_id uuid NOT NULL REFERENCES workers(id),
        binding_digest text NOT NULL CHECK(binding_digest ~ '^sha256:[0-9a-f]{64}$'),
        fencing_epoch bigint NOT NULL CHECK(fencing_epoch>0),
        lease_expires_at timestamptz NOT NULL,
        renewal_seq bigint NOT NULL CHECK(renewal_seq>=0),
        status text NOT NULL CHECK(status IN (
          'ACTIVE','COMPLETED','FAILED','EXPIRED','CANCELLED')),
        context_digest text CHECK(context_digest IS NULL OR context_digest ~ '^sha256:[0-9a-f]{64}$'),
        model_snapshot jsonb,
        skill_versions jsonb NOT NULL DEFAULT '[]'::jsonb,
        started_at timestamptz NOT NULL,
        finished_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(activity_id,fencing_epoch),
        UNIQUE(project_id,id)
      );
      CREATE UNIQUE INDEX activity_attempts_one_active
        ON activity_attempts(activity_id) WHERE status='ACTIVE';
      CREATE INDEX activity_attempts_lease ON activity_attempts(lease_expires_at,id);

      CREATE TABLE resource_reservations (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        activity_id uuid NOT NULL,
        attempt_id uuid NOT NULL UNIQUE REFERENCES activity_attempts(id),
        worker_id uuid NOT NULL REFERENCES workers(id),
        resources jsonb NOT NULL,
        status text NOT NULL CHECK(status IN ('HELD','RELEASED','QUARANTINED')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
      );

      CREATE TABLE budget_reservations (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        budget_scope_id uuid NOT NULL,
        activity_id uuid NOT NULL,
        attempt_id uuid NOT NULL UNIQUE REFERENCES activity_attempts(id),
        status text NOT NULL CHECK(status IN ('HELD','RELEASED','QUARANTINED')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
      );

      CREATE INDEX activities_budget_schedule
        ON activities(budget_scope_id,status,wake_at,id);
    """)


def downgrade() -> None:
    raise RuntimeError("租约与 attempt 历史不可就地回滚，需单独评审迁移")
