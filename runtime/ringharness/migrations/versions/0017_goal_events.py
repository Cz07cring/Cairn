"""Goal 事件流：只增 seq，供 ReadModel snapshot/events。"""

from alembic import op

revision = "0017_goal_events"
down_revision = "0016_finalization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE goal_events (
        goal_id uuid NOT NULL REFERENCES goals(id),
        seq bigint NOT NULL CHECK(seq > 0),
        event_id uuid NOT NULL,
        project_id uuid NOT NULL REFERENCES projects(id),
        type text NOT NULL,
        entity_id uuid NOT NULL,
        entity_state_revision bigint CHECK(entity_state_revision IS NULL OR entity_state_revision > 0),
        occurred_at timestamptz NOT NULL,
        payload jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(goal_id, seq),
        UNIQUE(event_id),
        UNIQUE(project_id, event_id)
      );
      CREATE INDEX goal_events_project_goal ON goal_events(project_id, goal_id, seq);
      CREATE TRIGGER goal_events_immutable BEFORE UPDATE OR DELETE ON goal_events
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();
    """)


def downgrade() -> None:
    raise RuntimeError("Immutable event history requires reviewed migration")
