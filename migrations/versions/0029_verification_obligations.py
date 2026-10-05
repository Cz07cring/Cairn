"""VerificationObligation：验证动作义务登记（状态可结算，不可删除）。"""

from alembic import op

revision = "0029_verification_obligations"
down_revision = "0028_verification_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE verification_obligations (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        activity_id uuid NOT NULL,
        attempt_id uuid NOT NULL,
        subject_type text NOT NULL CHECK(subject_type IN ('CANDIDATE','SKILL_VERSION')),
        subject_id uuid NOT NULL,
        profile_id uuid NOT NULL,
        layer text NOT NULL CHECK(layer IN (
          'MECHANICAL','SEMANTIC','ADVERSARIAL','GLOBAL')),
        audit_round int NOT NULL CHECK(audit_round >= 1),
        effect_ids uuid[] NOT NULL DEFAULT '{}',
        invocation_ids uuid[] NOT NULL DEFAULT '{}',
        status text NOT NULL DEFAULT 'OPEN' CHECK(status IN (
          'OPEN','ASSESSED','QUARANTINED')),
        assessment_id uuid REFERENCES verification_assessments(id),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id, id),
        UNIQUE(
          activity_id, attempt_id, subject_id, profile_id, layer, audit_round),
        FOREIGN KEY (project_id, activity_id)
          REFERENCES activities(project_id, id),
        FOREIGN KEY (project_id, attempt_id)
          REFERENCES activity_attempts(project_id, id),
        FOREIGN KEY (project_id, profile_id)
          REFERENCES verification_profiles(project_id, id)
      );
      CREATE INDEX verification_obligations_subject_pending
        ON verification_obligations(subject_id, status);
      -- 允许 OPEN→ASSESSED/QUARANTINED 更新；禁止 DELETE（义务不可抹除）
      CREATE TRIGGER verification_obligation_no_delete
        BEFORE DELETE ON verification_obligations
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();
    """)


def downgrade() -> None:
    raise RuntimeError("VerificationObligation 表不可就地回滚")
