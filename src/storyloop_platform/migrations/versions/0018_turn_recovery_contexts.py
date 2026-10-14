"""Freeze immutable ordinary player turn recovery inputs."""

import sqlalchemy as sa
from alembic import op

revision = "0018_turn_recovery_contexts"
down_revision = "0017_generated_scene_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "turn_recovery_contexts",
        sa.Column("game_id", sa.Text, sa.ForeignKey("games.game_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("request_id", sa.Text, primary_key=True),
        sa.Column("player_id", sa.Text, sa.ForeignKey("player_accounts.player_id"), nullable=False),
        sa.Column("turn_id", sa.Text, nullable=False),
        sa.Column("input_hash", sa.Text, nullable=False),
        sa.Column("generation_identity", sa.Text, nullable=False),
        sa.Column("created_order", sa.BigInteger, nullable=False),
        sa.Column("canonical_json", sa.Text, nullable=False),
        sa.Column("checksum", sa.Text, nullable=False),
        sa.UniqueConstraint("game_id", "created_order", name="uq_turn_recovery_context_order"),
    )


def downgrade() -> None:
    op.drop_table("turn_recovery_contexts")
