"""审计记录表。"""

from alembic import op

revision = "0015_audits"
down_revision = "0014_candidates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE audits (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        goal_id uuid NOT NULL REFERENCES goals(id),
        task_id uuid REFERENCES tasks(id),
        activity_id uuid NOT NULL REFERENCES activities(id),
        attempt_id uuid NOT NULL REFERENCES activity_attempts(id),
        subject_candidate_manifest_id uuid NOT NULL REFERENCES candidate_manifests(id),
        goal_contract_revision int NOT NULL,
        task_contract_revision int,
        verification_profile_id uuid NOT NULL,
        layer text NOT NULL,
        audit_round int NOT NULL CHECK(audit_round>=1),
        verifier_run_ids uuid[] NOT NULL DEFAULT '{}',
        verdict text NOT NULL CHECK(verdict IN ('PASS','INSUFFICIENT','FAIL')),
        criterion_results jsonb NOT NULL,
        evidence_ids uuid[] NOT NULL DEFAULT '{}',
        reason text NOT NULL,
        content_digest text NOT NULL CHECK(content_digest ~ '^sha256:[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id, id),
        UNIQUE(activity_id)
      );
      CREATE INDEX audits_goal ON audits(goal_id, id);
      CREATE INDEX audits_task ON audits(task_id, id);
      CREATE INDEX audits_candidate ON audits(subject_candidate_manifest_id, id);
      CREATE TRIGGER immutable_audit BEFORE UPDATE OR DELETE ON audits
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audits;")
