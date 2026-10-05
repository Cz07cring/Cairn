"""EvidenceValidityDecision + TrustPropagationJob；扩展 project_trust_states。"""

from alembic import op

revision = "0033_trust_invalidation"
down_revision = "0032_obligation_supersede"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE project_trust_states
        ADD COLUMN IF NOT EXISTS decision_ids uuid[] NOT NULL DEFAULT '{}',
        ADD COLUMN IF NOT EXISTS propagation_job_id uuid;

      CREATE TABLE evidence_validity_decisions (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        evidence_id uuid NOT NULL,
        decision text NOT NULL CHECK(decision = 'INVALID'),
        reason_code text NOT NULL CHECK(char_length(reason_code) BETWEEN 1 AND 200),
        authority_identity text NOT NULL CHECK(char_length(authority_identity) BETWEEN 1 AND 10000),
        proof_artifact_ids uuid[] NOT NULL DEFAULT '{}',
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id, id),
        UNIQUE(project_id, evidence_id),
        FOREIGN KEY (project_id, evidence_id)
          REFERENCES evidence_envelopes(project_id, id)
      );
      CREATE INDEX evidence_validity_decisions_project_page
        ON evidence_validity_decisions(project_id, created_at, id);
      CREATE TRIGGER immutable_evidence_validity_decision
        BEFORE UPDATE OR DELETE ON evidence_validity_decisions
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();

      CREATE TABLE trust_propagation_jobs (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        decision_ids uuid[] NOT NULL CHECK(cardinality(decision_ids) >= 1),
        status text NOT NULL CHECK(status IN (
          'PENDING','RUNNING','COMPLETE','BLOCKED')),
        cursor text,
        affected_skill_ids uuid[] NOT NULL DEFAULT '{}',
        affected_goal_ids uuid[] NOT NULL DEFAULT '{}',
        affected_release_ids uuid[] NOT NULL DEFAULT '{}',
        deadline_at timestamptz NOT NULL,
        reason_code text CHECK(reason_code IS NULL OR char_length(reason_code) BETWEEN 1 AND 200),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id, id)
      );
      CREATE INDEX trust_propagation_jobs_project_page
        ON trust_propagation_jobs(project_id, created_at, id);

      ALTER TABLE project_trust_states
        ADD CONSTRAINT project_trust_states_propagation_job_fk
        FOREIGN KEY (project_id, propagation_job_id)
        REFERENCES trust_propagation_jobs(project_id, id);
    """)


def downgrade() -> None:
    raise RuntimeError("信任失效表不可就地回滚")
