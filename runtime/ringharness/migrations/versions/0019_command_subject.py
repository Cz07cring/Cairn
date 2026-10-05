"""command_operations 记录发起主体，供无 project 列表只返自身命令。"""

from alembic import op

revision = "0019_command_subject"
down_revision = "0018_planning_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 历史行无法从 request_digest 反查主体；置空后仅新命令参与「自身」过滤。
    op.execute("""
      ALTER TABLE command_operations
        ADD COLUMN subject text;
      UPDATE command_operations SET subject='' WHERE subject IS NULL;
      ALTER TABLE command_operations
        ALTER COLUMN subject SET NOT NULL;
      CREATE INDEX commands_subject_page
        ON command_operations(subject, created_at, id);
    """)


def downgrade() -> None:
    raise RuntimeError("命令主体列不可就地回滚，需单独评审迁移")
