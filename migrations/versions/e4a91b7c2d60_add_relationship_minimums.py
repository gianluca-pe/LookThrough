"""Add simple institution relationship minimums.

Revision ID: e4a91b7c2d60
Revises: d83a74c1e6b0
Create Date: 2026-08-28
"""

import sqlalchemy as sa
from alembic import op


revision = "e4a91b7c2d60"
down_revision = "d83a74c1e6b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Additive and empty: account eligibility already exists and keeps its exact
    # meaning. No minimum is inferred for an institution during upgrade.
    op.create_table(
        "relationship_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("institution_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column(
            "threshold_amount", sa.Numeric(precision=28, scale=12), nullable=False
        ),
        sa.Column(
            "threshold_currency_code", sa.String(length=3), nullable=False
        ),
        sa.Column(
            "warning_buffer_amount",
            sa.Numeric(precision=28, scale=12),
            nullable=True,
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default="1"
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "threshold_amount > 0", name="relationship_threshold_positive"
        ),
        sa.CheckConstraint(
            "warning_buffer_amount IS NULL OR warning_buffer_amount >= 0",
            name="relationship_warning_buffer_nonnegative",
        ),
        sa.ForeignKeyConstraint(["institution_id"], ["institutions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_relationship_rules_institution_id",
        "relationship_rules",
        ["institution_id"],
        unique=False,
    )
    op.create_index(
        "uq_active_relationship_rule_institution",
        "relationship_rules",
        ["institution_id"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_active_relationship_rule_institution",
        table_name="relationship_rules",
    )
    op.drop_index(
        "ix_relationship_rules_institution_id",
        table_name="relationship_rules",
    )
    op.drop_table("relationship_rules")
