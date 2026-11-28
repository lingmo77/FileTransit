"""匿名上传：系统账号承载匿名文件，并为按 IP 统计匿名上传次数加索引。

匿名上传的文件挂在一个不能登录的系统账号下（``users.is_system``），这样文件表、
清理、下载等现有逻辑都不用改。用户名带 ``@``，注册校验不允许这个字符，不会和真实用户撞名。
"""
import sqlalchemy as sa
from alembic import op

revision = "b5e1d7c3a9f4"
down_revision = "a6b9e2037c51"
branch_labels = None
depends_on = None

ANONYMOUS_USERNAME = "@anonymous"


# users 被多张表外键引用，PostgreSQL 无法 DROP 重建，只有 SQLite 需要重建
def _recreate() -> str:
    return "always" if op.get_context().dialect.name == "sqlite" else "auto"


def upgrade():
    # server_default 只用来给已有的行补值，补完就摘掉，默认值由 ORM 负责（与 can_permanent 相同）
    op.add_column("users", sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table("users", recreate=_recreate()) as batch:
        batch.alter_column("is_system", server_default=None)
    postgresql = op.get_context().dialect.name == "postgresql"
    # 时间一律存不带时区的 UTC；离线生成初始化脚本时也要能渲染成 SQL，所以直接写字面量
    now = "(now() AT TIME ZONE 'utc')" if postgresql else "CURRENT_TIMESTAMP"
    true, false = ("true", "false") if postgresql else ("1", "0")
    # 密码哈希 "!" 不是合法的 bcrypt 值，任何口令都校验不过；登录、找回密码等入口另外按 is_system 拒绝
    op.execute(
        "INSERT INTO users (username, email, password_hash, is_admin, is_active, email_verified, "
        "must_change_password, can_permanent, used_files, used_bytes, created_at, is_system) VALUES "
        f"('{ANONYMOUS_USERNAME}', 'anonymous@system.invalid', '!', {false}, {true}, {true}, "
        f"{false}, {false}, 0, 0, {now}, {true})"
    )
    op.create_index("ix_logs_action_ip_created", "activity_logs", ["action", "ip_address", "created_at"])


def downgrade():
    op.drop_index("ix_logs_action_ip_created", table_name="activity_logs")
    op.execute(f"DELETE FROM users WHERE username = '{ANONYMOUS_USERNAME}'")
    with op.batch_alter_table("users", recreate=_recreate()) as batch:
        batch.drop_column("is_system")
