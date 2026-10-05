"""Skill 版本与 SkillSet；配置内容不可变，仅允许受控状态字段变更。"""

from alembic import op

revision = "0006_skills"
down_revision = "0005_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE skill_versions (
        id uuid PRIMARY KEY,
        skill_id uuid NOT NULL,
        project_id uuid NOT NULL REFERENCES projects(id),
        name text NOT NULL,
        version bigint NOT NULL CHECK(version>0),
        content_digest text NOT NULL CHECK(content_digest ~ '^sha256:[0-9a-f]{64}$'),
        config jsonb NOT NULL,
        status text NOT NULL CHECK(status IN ('CANDIDATE','VALIDATING','ACTIVE','REVOKED','REJECTED')),
        audit_id uuid,
        revocation_reason text,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id,name,version),
        UNIQUE(project_id,id),
        CHECK(config->>'project_id'=project_id::text),
        CHECK(config->>'name'=name),
        CHECK((status='REVOKED')=(revocation_reason IS NOT NULL)),
        CHECK(status <> 'ACTIVE' OR audit_id IS NOT NULL)
      );
      CREATE INDEX skill_versions_page ON skill_versions(project_id,created_at,id);
      CREATE INDEX skill_versions_skill ON skill_versions(project_id,skill_id,version);
      CREATE FUNCTION reject_skill_content_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.skill_id IS DISTINCT FROM OLD.skill_id
           OR NEW.project_id IS DISTINCT FROM OLD.project_id
           OR NEW.name IS DISTINCT FROM OLD.name
           OR NEW.version IS DISTINCT FROM OLD.version
           OR NEW.content_digest IS DISTINCT FROM OLD.content_digest
           OR NEW.config IS DISTINCT FROM OLD.config THEN
          RAISE EXCEPTION 'immutable skill content';
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER skill_content_immutable BEFORE UPDATE ON skill_versions
        FOR EACH ROW EXECUTE FUNCTION reject_skill_content_mutation();
      CREATE TRIGGER skill_no_delete BEFORE DELETE ON skill_versions
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();

      CREATE TABLE skill_validation_records (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        subject_skill_version_id uuid NOT NULL,
        subject_digest text NOT NULL CHECK(subject_digest ~ '^sha256:[0-9a-f]{64}$'),
        verification_profile_id uuid NOT NULL,
        content_digest text NOT NULL CHECK(content_digest ~ '^sha256:[0-9a-f]{64}$'),
        content jsonb NOT NULL,
        verdict text NOT NULL CHECK(verdict IN ('PASS','INSUFFICIENT','FAIL')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id,id),
        FOREIGN KEY(project_id,subject_skill_version_id)
          REFERENCES skill_versions(project_id,id),
        FOREIGN KEY(project_id,verification_profile_id)
          REFERENCES verification_profiles(project_id,id),
        CHECK(content->>'project_id'=project_id::text),
        CHECK(content->>'subject_skill_version_id'=subject_skill_version_id::text),
        CHECK(content->>'subject_digest'=subject_digest),
        CHECK(content->>'verification_profile_id'=verification_profile_id::text),
        CHECK(content->>'verdict'=verdict)
      );
      CREATE INDEX skill_validations_page ON skill_validation_records(project_id,subject_skill_version_id,created_at,id);
      CREATE TRIGGER immutable_skill_validation BEFORE UPDATE OR DELETE ON skill_validation_records
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();

      ALTER TABLE skill_versions
        ADD CONSTRAINT skill_versions_audit_fk
        FOREIGN KEY(project_id,audit_id) REFERENCES skill_validation_records(project_id,id);

      CREATE TABLE skill_sets (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects(id),
        name text NOT NULL,
        version bigint NOT NULL CHECK(version>0),
        content_digest text NOT NULL CHECK(content_digest ~ '^sha256:[0-9a-f]{64}$'),
        config jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(project_id,name,version),
        UNIQUE(project_id,id),
        CHECK(config->>'project_id'=project_id::text),
        CHECK(config->>'name'=name)
      );
      CREATE INDEX skill_sets_page ON skill_sets(project_id,created_at,id);
      CREATE TRIGGER immutable_skill_set BEFORE UPDATE OR DELETE ON skill_sets
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_config_change();
    """)


def downgrade() -> None:
    raise RuntimeError("技能历史不可就地回滚，需单独评审迁移")
