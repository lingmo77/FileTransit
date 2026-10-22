"""Persist a verified payload checkpoint independently of Aria2's session cache."""
import sqlalchemy as sa
from alembic import op

revision = "a6b9e2037c51"
down_revision = "f18a6c4d7e20"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("offline_tasks", sa.Column("payload_ready", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("offline_tasks", "payload_ready")
