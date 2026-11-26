"""users.can_permanent：管理员可指定用户允许永久保存

``can_permanent`` 为真表示该用户可以把自己上传的文件设为长期有效
（``expires_at = NULL``）。管理员不依赖这一列，天然允许。

Revision ID: c8e5a71f4b90
Revises: b3f1c2a45d7e
Create Date: 2026-10-09 16:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8e5a71f4b90"
down_revision: str | None = "b3f1c2a45d7e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# users 被多张表外键引用，PostgreSQL 无法 DROP 重建，只有 SQLite 需要重建
def _recreate() -> str:
    return "always" if op.get_context().dialect.name == "sqlite" else "auto"


def upgrade() -> None:
    # server_default 是给**已存在**的行补值用的：NOT NULL 列没有默认值就加不上去。
    # 补完就摘掉，让默认值只由 ORM 侧负责，避免以后改模型时两边打架。
    op.add_column(
        "users",
        sa.Column("can_permanent", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    with op.batch_alter_table("users", schema=None, recreate=_recreate()) as batch_op:
        batch_op.alter_column("can_permanent", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None, recreate=_recreate()) as batch_op:
        batch_op.drop_column("can_permanent")
