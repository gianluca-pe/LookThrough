"""Add cash balance checkpoints.

Revision ID: 6da3f1c8b972
Revises: 4bf251d2fc42
Create Date: 2026-08-04
"""

from alembic import op
import sqlalchemy as sa


revision = "6da3f1c8b972"
down_revision = "4bf251d2fc42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cash_balance_checkpoints",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column(
            "confirmed_balance_amount",
            sa.Numeric(precision=28, scale=12),
            nullable=False,
        ),
        sa.Column(
            "prior_calculated_balance_amount",
            sa.Numeric(precision=28, scale=12),
            nullable=True,
        ),
        sa.Column(
            "correction_amount",
            sa.Numeric(precision=28, scale=12),
            nullable=True,
        ),
        sa.Column("source_note", sa.Text(), nullable=True),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_cash_balance_checkpoints_account_id_accounts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cash_balance_checkpoints")),
    )
    with op.batch_alter_table("cash_balance_checkpoints", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_cash_balance_checkpoints_account_id"),
            ["account_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_cash_balance_checkpoints_effective_date"),
            ["effective_date"],
            unique=False,
        )
    op.create_index(
        "uq_active_cash_checkpoint_account_currency_date",
        "cash_balance_checkpoints",
        ["account_id", "currency_code", "effective_date"],
        unique=True,
        sqlite_where=sa.text("superseded_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_active_cash_checkpoint_account_currency_date",
        table_name="cash_balance_checkpoints",
    )
    with op.batch_alter_table("cash_balance_checkpoints", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_cash_balance_checkpoints_effective_date"))
        batch_op.drop_index(batch_op.f("ix_cash_balance_checkpoints_account_id"))
    op.drop_table("cash_balance_checkpoints")
