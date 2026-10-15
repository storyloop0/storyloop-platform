"""Durable immutable claims for ordinary scene generation."""

import sqlalchemy as sa
from alembic import op

revision = "0019_generated_scene_generation_markers"
down_revision = "0018_turn_recovery_contexts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generated_scene_generation_markers",
        sa.Column("game_id", sa.Text, sa.ForeignKey("games.game_id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("turn_id", sa.Text, primary_key=True),
        sa.Column("marker_id", sa.Text, nullable=False),
        sa.Column("canonical_json", sa.Text, nullable=False),
        sa.Column("checksum", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("generated_scene_generation_markers")
