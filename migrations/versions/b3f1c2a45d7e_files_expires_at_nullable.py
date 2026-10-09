"""files.expires_at 允许为空（长期有效）

``expires_at`` 为 ``NULL`` 表示该文件长期有效，只有管理员能设置。

Revision ID: b3f1c2a45d7e
Revises: e97a949478ec
Create Date: 2026-10-09 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3f1c2a45d7e"
down_revision: str | None = "e97a949478ec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite 不支持 ALTER COLUMN，batch_alter_table 会重建整张表。
    # recreate="always" 是为了让 SQLite 也走重建路径，别指望原生 ALTER。
    with op.batch_alter_table("files", schema=None, recreate="always") as batch_op:
        batch_op.alter_column(
            "expires_at",
            existing_type=sa.DateTime(),
            nullable=True,
        )


def downgrade() -> None:
    # 回滚前必须先把长期有效的记录填上一个时间，否则 NOT NULL 约束会失败
    op.execute("UPDATE files SET expires_at = created_at WHERE expires_at IS NULL")
    with op.batch_alter_table("files", schema=None, recreate="always") as batch_op:
        batch_op.alter_column(
            "expires_at",
            existing_type=sa.DateTime(),
            nullable=False,
        )
