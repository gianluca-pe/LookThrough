"""Keep existing rules and permit explicitly selected full-budget plans."""

import sqlalchemy as sa
from alembic import op

revision = "a4b8d0e2f6c9"
down_revision = "f3a7c9d1e5b8"
branch_labels = None
depends_on = None

RULE_FIELDS = ("lower_rate_decimal", "upper_rate_decimal", "lower_multiplier_decimal",
               "middle_multiplier_decimal", "upper_multiplier_decimal")


def _preserve_income_while_rebuilding(change):
    """Rebuild the referenced SQLite parent with FK enforcement left on.

    SQLite needs a table copy to relax NOT NULL. Copy income into a temporary
    table without foreign keys, rebuild its parent, then restore its original
    schema, rows and indexes in the same explicit transaction.
    """
    bind = op.get_bind()
    if not bind.connection.driver_connection.in_transaction:
        bind.exec_driver_sql("BEGIN")
    metadata = sa.MetaData()
    income = sa.Table("retirement_income", metadata, autoload_with=bind)
    bind.exec_driver_sql("CREATE TEMP TABLE retirement_income_migration_copy AS SELECT * FROM retirement_income")
    income.drop(bind)
    change()
    income.create(bind)
    columns = ", ".join(column.name for column in income.columns)
    bind.exec_driver_sql(f"INSERT INTO retirement_income ({columns}) SELECT {columns} FROM retirement_income_migration_copy")
    bind.exec_driver_sql("DROP TABLE retirement_income_migration_copy")
    if bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("Foreign-key verification failed during retirement migration.")


def upgrade():
    def change():
        op.add_column("retirement_plans", sa.Column("spending_policy", sa.String(16),
                                                   nullable=False, server_default="guardrails"))
        with op.batch_alter_table("retirement_plans") as batch:
            batch.alter_column("spending_policy", existing_type=sa.String(16), server_default=None)
            for field in RULE_FIELDS:
                batch.alter_column(field, existing_type=sa.Numeric(10, 8), nullable=True)
            batch.create_check_constraint("plan_spending_policy", "spending_policy IN ('full_budget', 'guardrails')")
            batch.create_check_constraint("plan_rules_complete",
                "(spending_policy = 'full_budget' AND lower_rate_decimal IS NULL "
                "AND upper_rate_decimal IS NULL AND lower_multiplier_decimal IS NULL "
                "AND middle_multiplier_decimal IS NULL AND upper_multiplier_decimal IS NULL) "
                "OR (lower_rate_decimal IS NOT NULL AND upper_rate_decimal IS NOT NULL "
                "AND lower_multiplier_decimal IS NOT NULL AND middle_multiplier_decimal IS NOT NULL "
                "AND upper_multiplier_decimal IS NOT NULL)")
    _preserve_income_while_rebuilding(change)


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM retirement_plans WHERE spending_policy = 'full_budget' LIMIT 1")).first():
        raise RuntimeError("Cannot downgrade a full-budget plan without changing its meaning. Restore a prior backup instead.")
    def change():
        with op.batch_alter_table("retirement_plans") as batch:
            batch.drop_constraint("plan_rules_complete", type_="check")
            batch.drop_constraint("plan_spending_policy", type_="check")
            batch.drop_column("spending_policy")
            for field in RULE_FIELDS:
                batch.alter_column(field, existing_type=sa.Numeric(10, 8), nullable=False)
    _preserve_income_while_rebuilding(change)
