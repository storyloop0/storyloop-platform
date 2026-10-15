"""Durable, immutable generated scene snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0017_generated_scene_snapshots"
down_revision = "0016_model_execution_audits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generated_scene_snapshots",
        sa.Column("game_id", sa.Text, sa.ForeignKey("games.game_id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("turn_id", sa.Text, primary_key=True),
        sa.Column("snapshot_id", sa.Text, nullable=False),
        sa.Column("canonical_json", sa.Text, nullable=False),
        sa.Column("checksum", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("generated_scene_snapshots")
