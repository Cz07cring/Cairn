"""reference_edges 主键纳入 source_digest，允许同源多合同修订登记边。"""

from alembic import op

revision = "0023_reference_edges_digest_pk"
down_revision = "0022_validate_skill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 不可变表：禁止 DELETE 旧边；合同修订需按新 digest 追加，故 PK 必须含 source_digest。
    op.execute("""
      ALTER TABLE reference_edges DROP CONSTRAINT reference_edges_pkey;
      ALTER TABLE reference_edges ADD PRIMARY KEY (
        project_id, source_type, source_id, source_digest, target_type, target_id
      );
    """)


def downgrade() -> None:
    raise RuntimeError("reference_edges 修订边历史不可就地回滚主键")
