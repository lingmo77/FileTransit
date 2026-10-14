"""Add resumable upload sessions without altering existing users or files."""
import sqlalchemy as sa
from alembic import op

revision = "d42e8f6b9a10"
down_revision = "c8e5a71f4b90"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "upload_sessions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("offset", sa.BigInteger(), nullable=False),
        sa.Column("expires_hours", sa.Integer(), nullable=False),
        sa.Column("is_public", sa.Boolean(), nullable=False),
        sa.Column("stored_path", sa.String(255), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("file_id", sa.Integer(), sa.ForeignKey("files.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    for field in ("user_id", "state", "expires_at"):
        op.create_index(f"ix_upload_sessions_{field}", "upload_sessions", [field])


def downgrade():
    op.drop_table("upload_sessions")
