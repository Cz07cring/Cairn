"""VerificationRun + Kernel Assessment：不可变登记表。"""

from alembic import op

revision = "0028_verification_runs"
down_revision = "0027_index_memory"
branch_labels = None
depends_on = None

_DIGEST = r"^sha256:[0-9a-f]{64}$"


def upgrade() -> None:
    op.execute(f"""
      CREATE TABLE verification_runs (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        producer_activity_id uuid NOT NULL,
        producer_attempt_id uuid NOT NULL,
        subject_type text NOT NULL CHECK(subject_type IN ('CANDIDATE','SKILL_VERSION')),
        subject_id uuid NOT NULL,
        subject_digest text NOT NULL CHECK(subject_digest ~ '{_DIGEST}'),
        verification_profile_id uuid NOT NULL,
        verifier_digest text NOT NULL CHECK(verifier_digest ~ '{_DIGEST}'),
        audit_round int NOT NULL CHECK(audit_round >= 1),
        layer text NOT NULL CHECK(layer IN (
          'MECHANICAL','SEMANTIC','ADVERSARIAL','GLOBAL')),
        input_digest text NOT NULL CHECK(input_digest ~ '{_DIGEST}'),
        environment_digest text NOT NULL CHECK(environment_digest ~ '{_DIGEST}'),
        receipt_ids uuid[] NOT NULL,
        observations jsonb NOT NULL,
        content_digest text NOT NULL CHECK(content_digest ~ '{_DIGEST}'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id, id),
        UNIQUE(
          producer_activity_id, producer_attempt_id, audit_round,
          verification_profile_id, layer),
        FOREIGN KEY (project_id, producer_activity_id)
          REFERENCES activities(project_id, id),
        FOREIGN KEY (project_id, producer_attempt_id)
          REFERENCES activity_attempts(project_id, id),
        FOREIGN KEY (project_id, verification_profile_id)
          REFERENCES verification_profiles(project_id, id),
        CHECK(cardinality(receipt_ids) >= 1)
      );
      CREATE INDEX verification_runs_project_page
        ON verification_runs(project_id, created_at, id);
      CREATE TRIGGER immutable_verification_run
        BEFORE UPDATE OR DELETE ON verification_runs
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();

      CREATE TABLE verification_assessments (
        id uuid PRIMARY KEY,
        run_id uuid NOT NULL UNIQUE REFERENCES verification_runs(id),
        project_id uuid NOT NULL REFERENCES projects(id),
        subject_type text NOT NULL CHECK(subject_type IN ('CANDIDATE','SKILL_VERSION')),
        subject_id uuid NOT NULL,
        subject_digest text NOT NULL CHECK(subject_digest ~ '{_DIGEST}'),
        verification_profile_id uuid NOT NULL,
        layer text NOT NULL CHECK(layer IN (
          'MECHANICAL','SEMANTIC','ADVERSARIAL','GLOBAL')),
        audit_round int NOT NULL CHECK(audit_round >= 1),
        trust_revision bigint NOT NULL CHECK(trust_revision > 0),
        evaluator_digest text NOT NULL CHECK(evaluator_digest ~ '{_DIGEST}'),
        criterion_results jsonb NOT NULL,
        verdict text NOT NULL CHECK(verdict IN ('PASS','INSUFFICIENT','FAIL')),
        content_digest text NOT NULL CHECK(content_digest ~ '{_DIGEST}'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id, id),
        FOREIGN KEY (project_id, verification_profile_id)
          REFERENCES verification_profiles(project_id, id)
      );
      CREATE INDEX verification_assessments_project_page
        ON verification_assessments(project_id, created_at, id);
      CREATE TRIGGER immutable_verification_assessment
        BEFORE UPDATE OR DELETE ON verification_assessments
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();
    """)


def downgrade() -> None:
    raise RuntimeError("VerificationRun/Assessment 证据表不可就地回滚")
