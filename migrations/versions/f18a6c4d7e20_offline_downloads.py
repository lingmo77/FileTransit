"""Add offline jobs without changing existing users, files or legacy upload tables."""
import sqlalchemy as sa
from alembic import op

revision = "f18a6c4d7e20"
down_revision = "d42e8f6b9a10"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "offline_tasks",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_key", sa.String(64)),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("source_encrypted", sa.Text(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("info_hash", sa.String(40)),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("desired_state", sa.String(16), nullable=False),
        sa.Column("is_public", sa.Boolean(), nullable=False),
        sa.Column("expires_hours", sa.Integer(), nullable=False),
        sa.Column("reserved_files", sa.Integer(), nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("completed_bytes", sa.BigInteger(), nullable=False),
        sa.Column("download_speed", sa.BigInteger(), nullable=False),
        sa.Column("upload_speed", sa.BigInteger(), nullable=False),
        sa.Column("connections", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(512)),
        sa.Column("imported_at", sa.DateTime()),
        sa.Column("seed_started_at", sa.DateTime()),
        sa.Column("cleanup_pending", sa.Boolean(), nullable=False),
        sa.Column("removed_at", sa.DateTime()),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "request_key", name="uq_offline_task_request"),
    )
    op.create_index("ix_offline_tasks_user_created", "offline_tasks", ["user_id", "created_at"])
    op.create_index("ix_offline_tasks_state", "offline_tasks", ["state"])
    op.create_index("ix_offline_tasks_info_hash", "offline_tasks", ["info_hash"])
    op.create_table(
        "offline_task_transfers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("offline_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("gid", sa.String(16), nullable=False, unique=True),
        sa.Column("parent_gid", sa.String(16)),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_offline_transfers_task", "offline_task_transfers", ["task_id"])
    op.create_table(
        "offline_task_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("offline_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_index", sa.Integer(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("completed_bytes", sa.BigInteger(), nullable=False),
        sa.Column("stored_path", sa.String(255)),
        sa.Column("public_id", sa.String(32), unique=True),
        sa.Column("file_id", sa.Integer(), sa.ForeignKey("files.id", ondelete="SET NULL")),
        sa.Column("imported_at", sa.DateTime()),
        sa.UniqueConstraint("task_id", "file_index", name="uq_offline_file_index"),
    )
    op.create_index("ix_offline_task_files_file_id", "offline_task_files", ["file_id"])


def downgrade():
    op.drop_table("offline_task_files")
    op.drop_table("offline_task_transfers")
    op.drop_table("offline_tasks")
