"""ModelInvocation 登记暴露给模型的工具名（kind-aware dispatch）。

Revision ID: 0037_model_exposed_tools
Revises: 0036_trust_inv_stop
"""

from __future__ import annotations

from alembic import op

revision = "0037_model_exposed_tools"
down_revision = "0036_trust_inv_stop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE model_invocations
          ADD COLUMN exposed_tools text[] NOT NULL DEFAULT '{}';
        """
    )


def downgrade() -> None:
    raise RuntimeError("exposed_tools 列不可就地回滚，需单独评审迁移")
