"""R1b：Goal 模式、attempt 钉扎与已发布 Plan 的不可变来源关系。"""

from alembic import op

revision = "0044_plan_input_binding"
down_revision = "0043_plan_inputs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE goals ADD COLUMN plan_input_mode text NOT NULL DEFAULT 'OPTIONAL'
        CHECK(plan_input_mode IN ('OPTIONAL','REQUIRED'));
      ALTER TABLE activity_attempts
        ADD COLUMN plan_input_id uuid,
        ADD COLUMN plan_input_digest text
          CHECK(plan_input_digest IS NULL OR plan_input_digest ~ '^sha256:[0-9a-f]{64}$');
      ALTER TABLE activity_attempts ADD CONSTRAINT attempt_plan_input_pair
        CHECK((plan_input_id IS NULL) = (plan_input_digest IS NULL));
      ALTER TABLE activity_attempts ADD CONSTRAINT attempt_plan_input_fk
        FOREIGN KEY(project_id,plan_input_id) REFERENCES plan_inputs(project_id,id);
      CREATE OR REPLACE FUNCTION reject_attempt_plan_input_change() RETURNS trigger AS $$
      BEGIN
        IF NEW.plan_input_id IS DISTINCT FROM OLD.plan_input_id OR
           NEW.plan_input_digest IS DISTINCT FROM OLD.plan_input_digest THEN
          RAISE EXCEPTION 'attempt PlanInput binding is immutable';
        END IF;
        RETURN NEW;
      END;
      $$ LANGUAGE plpgsql;
      CREATE TRIGGER attempt_plan_input_immutable BEFORE UPDATE ON activity_attempts
        FOR EACH ROW EXECUTE FUNCTION reject_attempt_plan_input_change();
      ALTER TABLE plans
        ADD COLUMN source_plan_input_id uuid,
        ADD COLUMN source_plan_input_digest text
          CHECK(source_plan_input_digest IS NULL OR source_plan_input_digest ~ '^sha256:[0-9a-f]{64}$');
      ALTER TABLE plans ADD CONSTRAINT published_plan_source_pair
        CHECK((source_plan_input_id IS NULL) = (source_plan_input_digest IS NULL));
      ALTER TABLE plans ADD CONSTRAINT published_plan_source_fk
        FOREIGN KEY(project_id,source_plan_input_id) REFERENCES plan_inputs(project_id,id);
      CREATE OR REPLACE FUNCTION reject_published_plan_source_change() RETURNS trigger AS $$
      BEGIN
        IF TG_OP='DELETE' THEN
          IF OLD.status='PUBLISHED' THEN
            RAISE EXCEPTION 'published Plan source is immutable';
          END IF;
          RETURN OLD;
        END IF;
        IF OLD.status='PUBLISHED' AND
          (NEW.source_plan_input_id IS DISTINCT FROM OLD.source_plan_input_id OR
           NEW.source_plan_input_digest IS DISTINCT FROM OLD.source_plan_input_digest) THEN
          RAISE EXCEPTION 'published Plan source is immutable';
        END IF;
        RETURN NEW;
      END;
      $$ LANGUAGE plpgsql;
      CREATE TRIGGER published_plan_source_immutable BEFORE UPDATE OR DELETE ON plans
        FOR EACH ROW EXECUTE FUNCTION reject_published_plan_source_change();
    """)


def downgrade() -> None:
    raise RuntimeError("PlanInput 来源关系不可就地回滚")
