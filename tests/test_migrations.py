"""Migration-baseline tests against the disposable SQLite database."""

from flask import Flask
from sqlalchemy import inspect, text

from app.extensions import db


def test_migration_baseline_upgrades_disposable_database(
    unmigrated_app: Flask,
) -> None:
    runner = unmigrated_app.test_cli_runner()
    baseline_result = runner.invoke(args=["db", "upgrade", "040c3a312c85"])

    assert baseline_result.exit_code == 0, baseline_result.output

    with unmigrated_app.app_context():
        assert "alembic_version" in inspect(db.engine).get_table_names()
        baseline_revision = db.session.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()

    assert baseline_revision == "040c3a312c85"

    head_result = runner.invoke(args=["db", "upgrade"])

    assert head_result.exit_code == 0, head_result.output

    with unmigrated_app.app_context():
        table_names = set(inspect(db.engine).get_table_names())
        account_columns = {
            column["name"] for column in inspect(db.engine).get_columns("accounts")
        }
        instrument_columns = {
            column["name"] for column in inspect(db.engine).get_columns("instruments")
        }
        portfolio_columns = {
            column["name"] for column in inspect(db.engine).get_columns("portfolios")
        }
        fixed_deposit_columns = {
            column["name"]
            for column in inspect(db.engine).get_columns("fixed_deposits")
        }
        head_revision = db.session.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()

    assert {
        "portfolios",
        "institutions",
        "accounts",
        "instruments",
        "position_registrations",
        "transactions",
        "postings",
        "prices",
        "valuation_observations",
        "fx_rates",
        "cash_balance_checkpoints",
            "instrument_classifications",
            "fixed_deposits",
            "relationship_rules",
            "allocation_targets",
            "portfolio_snapshots",
            "retirement_assumptions",
            "retirement_scenarios",
    } <= table_names
    assert "reference" in account_columns
    assert "cash_settlement_account_id" in account_columns
    assert "annual_spending_currency_code" in portfolio_columns
    assert "annual_inflation_decimal" in portfolio_columns
    assert {
        "fund_base_currency_code",
        "hedging_status",
        "fire_bucket_code",
        "capital_certainty_code",
        "equity_sensitivity_code",
        "liquidity_profile_code",
        "duration_band_code",
        "credit_band_code",
        "currency_treatment_code",
    } <= instrument_columns
    assert "expected_maturity_proceeds_amount" in fixed_deposit_columns
    assert head_revision == "e8f2a6b0c4d7"


def test_relationship_migration_preserves_account_eligibility(
    unmigrated_app: Flask,
) -> None:
    runner = unmigrated_app.test_cli_runner()
    prior_result = runner.invoke(args=["db", "upgrade", "d83a74c1e6b0"])
    assert prior_result.exit_code == 0, prior_result.output

    with unmigrated_app.app_context():
        db.session.execute(text(
            "INSERT INTO portfolios "
            "(id, name, reporting_currency_code, annual_spending_amount, "
            "annual_spending_currency_code, price_stale_days, fx_stale_days, "
            "statement_value_stale_days, created_at, updated_at) VALUES "
            "(1, 'Portfolio', 'EUR', 48000, 'EUR', 14, 7, 45, "
            "'2026-08-28 10:00:00', '2026-08-28 10:00:00')"
        ))
        db.session.execute(text(
            "INSERT INTO institutions "
            "(id, portfolio_id, name, created_at, updated_at) VALUES "
            "(1, 1, 'Bank', '2026-08-28 10:00:00', '2026-08-28 10:00:00')"
        ))
        db.session.execute(text(
            "INSERT INTO accounts "
            "(id, portfolio_id, institution_id, name, account_type, "
            "default_currency_code, is_multicurrency, cash_tracking_mode, "
            "portfolio_share_decimal, present_access_decimal, "
            "relationship_eligible, is_active, created_at, updated_at) VALUES "
            "(1, 1, 1, 'Excluded account', 'cash', 'EUR', 0, "
            "'separate_cash', 1, 1, 0, 1, "
            "'2026-08-28 10:00:00', '2026-08-28 10:00:00')"
        ))
        db.session.commit()

    head_result = runner.invoke(args=["db", "upgrade"])
    assert head_result.exit_code == 0, head_result.output
    with unmigrated_app.app_context():
        eligible = db.session.execute(
            text("SELECT relationship_eligible FROM accounts WHERE id = 1")
        ).scalar_one()
        rule_count = db.session.execute(
            text("SELECT COUNT(*) FROM relationship_rules")
        ).scalar_one()
        head = db.session.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    assert eligible == 0
    assert rule_count == 0
    assert head == "e8f2a6b0c4d7"


def test_planning_migration_preserves_legacy_classification_rows(
    unmigrated_app: Flask,
) -> None:
    runner = unmigrated_app.test_cli_runner()
    prior = runner.invoke(args=["db", "upgrade", "f7b2c9d4e6a1"])
    assert prior.exit_code == 0, prior.output
    with unmigrated_app.app_context():
        db.session.execute(text(
            "INSERT INTO portfolios "
            "(id, name, reporting_currency_code, annual_spending_amount, "
            "annual_spending_currency_code, price_stale_days, fx_stale_days, "
            "statement_value_stale_days, created_at, updated_at) VALUES "
            "(1, 'Example portfolio', 'EUR', 48000, 'EUR', 14, 7, 45, "
            "'2026-08-29 10:00:00', '2026-08-29 10:00:00')"
        ))
        db.session.execute(text(
            "INSERT INTO instruments "
            "(id, portfolio_id, name, instrument_type, valuation_currency_code, "
            "fire_bucket_code, is_active, created_at, updated_at) VALUES "
            "(1, 1, 'Legacy equity', 'fund', 'EUR', 'next', 1, "
            "'2026-08-29 10:00:00', '2026-08-29 10:00:00')"
        ))
        db.session.execute(text(
            "INSERT INTO instrument_classifications "
            "(id, instrument_id, economic_role_code, weight_decimal, "
            "effective_date, created_at, updated_at) VALUES "
            "(1, 1, 'opportunistic', 1, '2026-08-01', "
            "'2026-08-29 10:00:00', '2026-08-29 10:00:00')"
        ))
        db.session.commit()

    result = runner.invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output
    with unmigrated_app.app_context():
        row = db.session.execute(text(
            "SELECT economic_role_code, weight_decimal FROM "
            "instrument_classifications WHERE id = 1"
        )).one()
        assert row.economic_role_code == "opportunistic"
        assert row.weight_decimal == 100000000
        assert "allocation_targets" in inspect(db.engine).get_table_names()


def test_funding_migration_preserves_portfolio_and_adds_no_assumption(
    unmigrated_app: Flask,
) -> None:
    runner = unmigrated_app.test_cli_runner()
    prior = runner.invoke(args=["db", "upgrade", "a2d4e6f8b0c1"])
    assert prior.exit_code == 0, prior.output
    with unmigrated_app.app_context():
        db.session.execute(text(
            "INSERT INTO portfolios "
            "(id, name, reporting_currency_code, annual_spending_amount, "
            "annual_spending_currency_code, price_stale_days, fx_stale_days, "
            "statement_value_stale_days, created_at, updated_at) VALUES "
            "(1, 'Example portfolio', 'USD', 30000, 'USD', 14, 7, 45, "
            "'2026-08-29 10:00:00', '2026-08-29 10:00:00')"
        ))
        db.session.commit()

    result = runner.invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output
    with unmigrated_app.app_context():
        row = db.session.execute(text(
            "SELECT name, annual_spending_amount, annual_inflation_decimal "
            "FROM portfolios WHERE id = 1"
        )).one()
        head = db.session.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()

    assert row.name == "Example portfolio"
    assert row.annual_spending_amount == 30000000
    assert row.annual_inflation_decimal is None
    assert head == "e8f2a6b0c4d7"


def test_fd_expected_proceeds_migration_preserves_existing_terms(
    unmigrated_app: Flask,
) -> None:
    runner = unmigrated_app.test_cli_runner()
    prior_result = runner.invoke(args=["db", "upgrade", "e4a91b7c2d60"])
    assert prior_result.exit_code == 0, prior_result.output

    with unmigrated_app.app_context():
        # The terms table may be empty on upgrade; that is the important
        # non-inventing path for a populated database too.
        before = db.session.execute(
            text("SELECT COUNT(*) FROM fixed_deposits")
        ).scalar_one()
    result = runner.invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output
    with unmigrated_app.app_context():
        columns = {
            row["name"] for row in inspect(db.engine).get_columns("fixed_deposits")
        }
        after = db.session.execute(
            text("SELECT COUNT(*) FROM fixed_deposits")
        ).scalar_one()
    assert before == after == 0
    assert "expected_maturity_proceeds_amount" in columns


def test_fixed_deposit_migration_preserves_existing_positions_and_values(
    unmigrated_app: Flask,
) -> None:
    runner = unmigrated_app.test_cli_runner()
    prior_result = runner.invoke(args=["db", "upgrade", "b6f1d3a9e5c2"])
    assert prior_result.exit_code == 0, prior_result.output

    with unmigrated_app.app_context():
        db.session.execute(text(
            "INSERT INTO portfolios "
            "(id, name, reporting_currency_code, annual_spending_amount, "
            "annual_spending_currency_code, price_stale_days, fx_stale_days, "
            "statement_value_stale_days, created_at, updated_at) VALUES "
            "(1, 'Portfolio', 'USD', 48000, 'USD', 14, 7, 45, "
            "'2026-08-28 10:00:00', '2026-08-28 10:00:00')"
        ))
        db.session.execute(text(
            "INSERT INTO institutions "
            "(id, portfolio_id, name, created_at, updated_at) VALUES "
            "(1, 1, 'Bank', '2026-08-28 10:00:00', '2026-08-28 10:00:00')"
        ))
        db.session.execute(text(
            "INSERT INTO accounts "
            "(id, portfolio_id, institution_id, name, account_type, "
            "default_currency_code, is_multicurrency, cash_tracking_mode, "
            "portfolio_share_decimal, present_access_decimal, "
            "relationship_eligible, is_active, created_at, updated_at) VALUES "
            "(1, 1, 1, 'Fixed Deposits', 'deposit', 'USD', 0, "
            "'included_in_aggregate', 1, 1, 1, 1, "
            "'2026-08-28 10:00:00', '2026-08-28 10:00:00')"
        ))
        db.session.execute(text(
            "INSERT INTO instruments "
            "(id, portfolio_id, name, instrument_type, valuation_currency_code, "
            "is_active, created_at, updated_at) VALUES "
            "(1, 1, 'FD_USD_204', 'fixed_deposit', 'USD', 1, "
            "'2026-08-28 10:00:00', '2026-08-28 10:00:00')"
        ))
        db.session.execute(text(
            "INSERT INTO position_registrations "
            "(id, account_id, instrument_id, tracking_mode, opening_date, "
            "created_at, updated_at) VALUES "
            "(1, 1, 1, 'statement_valued', '2026-06-02', "
            "'2026-08-28 10:00:00', '2026-08-28 10:00:00')"
        ))
        db.session.execute(text(
            "INSERT INTO valuation_observations "
            "(id, position_registration_id, effective_date, native_value_amount, "
            "currency_code, created_at, updated_at) VALUES "
            "(1, 1, '2026-06-02', 85000, 'USD', "
            "'2026-08-28 10:00:00', '2026-08-28 10:00:00')"
        ))
        db.session.commit()

    head_result = runner.invoke(args=["db", "upgrade"])
    assert head_result.exit_code == 0, head_result.output

    with unmigrated_app.app_context():
        row = db.session.execute(text(
            "SELECT i.name, r.opening_date, v.native_value_amount "
            "FROM instruments i "
            "JOIN position_registrations r ON r.instrument_id = i.id "
            "JOIN valuation_observations v ON v.position_registration_id = r.id"
        )).one()
        assert row.name == "FD_USD_204"
        assert str(row.opening_date) == "2026-06-02"
        assert row.native_value_amount == 85000000
        assert db.session.execute(
            text("SELECT COUNT(*) FROM fixed_deposits")
        ).scalar_one() == 0


def test_account_reference_migration_preserves_existing_account(
    unmigrated_app: Flask,
) -> None:
    runner = unmigrated_app.test_cli_runner()
    prior_result = runner.invoke(args=["db", "upgrade", "6da3f1c8b972"])
    assert prior_result.exit_code == 0, prior_result.output

    with unmigrated_app.app_context():
        db.session.execute(text(
            "INSERT INTO portfolios "
            "(id, name, reporting_currency_code, annual_spending_amount, "
            "price_stale_days, fx_stale_days, statement_value_stale_days, "
            "created_at, updated_at) VALUES "
            "(1, 'Portfolio', 'EUR', 48000, 14, 7, 45, "
            "'2026-08-08 10:00:00', '2026-08-08 10:00:00')"
        ))
        db.session.execute(text(
            "INSERT INTO institutions "
            "(id, portfolio_id, name, created_at, updated_at) VALUES "
            "(1, 1, 'Bank', '2026-08-08 10:00:00', '2026-08-08 10:00:00')"
        ))
        db.session.execute(text(
            "INSERT INTO accounts "
            "(id, portfolio_id, institution_id, name, account_type, "
            "default_currency_code, is_multicurrency, cash_tracking_mode, "
            "portfolio_share_decimal, present_access_decimal, "
            "relationship_eligible, is_active, created_at, updated_at) VALUES "
            "(1, 1, 1, 'Existing account', 'cash', 'EUR', 0, "
            "'separate_cash', 1, 1, 1, 1, "
            "'2026-08-08 10:00:00', '2026-08-08 10:00:00')"
        ))
        db.session.commit()

    head_result = runner.invoke(args=["db", "upgrade"])
    assert head_result.exit_code == 0, head_result.output

    with unmigrated_app.app_context():
        row = db.session.execute(
            text("SELECT name, reference FROM accounts WHERE id = 1")
        ).one()
        assert row.name == "Existing account"
        assert row.reference is None


def test_cash_settlement_migration_preserves_existing_account(
    unmigrated_app: Flask,
) -> None:
    runner = unmigrated_app.test_cli_runner()
    prior_result = runner.invoke(args=["db", "upgrade", "a48d6c9e21f4"])
    assert prior_result.exit_code == 0, prior_result.output

    with unmigrated_app.app_context():
        db.session.execute(text(
            "INSERT INTO portfolios "
            "(id, name, reporting_currency_code, annual_spending_amount, "
            "price_stale_days, fx_stale_days, statement_value_stale_days, "
            "created_at, updated_at) VALUES "
            "(1, 'Portfolio', 'EUR', 48000, 14, 7, 45, "
            "'2026-08-08 10:00:00', '2026-08-08 10:00:00')"
        ))
        db.session.execute(text(
            "INSERT INTO institutions "
            "(id, portfolio_id, name, created_at, updated_at) VALUES "
            "(1, 1, 'Bank', '2026-08-08 10:00:00', '2026-08-08 10:00:00')"
        ))
        db.session.execute(text(
            "INSERT INTO accounts "
            "(id, portfolio_id, institution_id, name, reference, account_type, "
            "default_currency_code, is_multicurrency, cash_tracking_mode, "
            "portfolio_share_decimal, present_access_decimal, "
            "relationship_eligible, is_active, created_at, updated_at) VALUES "
            "(1, 1, 1, 'Existing brokerage', 'REF-7', 'brokerage', 'SGD', 0, "
            "'separate_cash', 1, 1, 1, 1, "
            "'2026-08-08 10:00:00', '2026-08-08 10:00:00')"
        ))
        db.session.execute(text(
            "INSERT INTO cash_balance_checkpoints "
            "(id, account_id, currency_code, effective_date, "
            "confirmed_balance_amount, prior_calculated_balance_amount, "
            "correction_amount, source_note, created_at) VALUES "
            "(1, 1, 'SGD', '2026-08-05', 1000, 0, 1000, "
            "'Existing child row', '2026-08-08 10:00:00')"
        ))
        db.session.commit()

    head_result = runner.invoke(args=["db", "upgrade"])
    assert head_result.exit_code == 0, head_result.output

    with unmigrated_app.app_context():
        row = db.session.execute(
            text(
                "SELECT name, reference, cash_settlement_account_id "
                "FROM accounts WHERE id = 1"
            )
        ).one()
        assert row.name == "Existing brokerage"
        assert row.reference == "REF-7"
        assert row.cash_settlement_account_id is None
        checkpoint_account_id = db.session.execute(
            text("SELECT account_id FROM cash_balance_checkpoints WHERE id = 1")
        ).scalar_one()
        assert checkpoint_account_id == 1


def test_cash_settlement_migration_downgrade_and_reupgrade(
    unmigrated_app: Flask,
) -> None:
    runner = unmigrated_app.test_cli_runner()
    assert runner.invoke(args=["db", "upgrade", "d7e1f5a9b3c6"]).exit_code == 0

    downgrade = runner.invoke(args=["db", "downgrade", "a48d6c9e21f4"])
    assert downgrade.exit_code == 0, downgrade.output
    with unmigrated_app.app_context():
        columns = {
            column["name"] for column in inspect(db.engine).get_columns("accounts")
        }
        assert "cash_settlement_account_id" not in columns

    reupgrade = runner.invoke(args=["db", "upgrade"])
    assert reupgrade.exit_code == 0, reupgrade.output


def test_classification_migration_preserves_existing_instrument(
    unmigrated_app: Flask,
) -> None:
    runner = unmigrated_app.test_cli_runner()
    prior_result = runner.invoke(args=["db", "upgrade", "c921c6d74ef0"])
    assert prior_result.exit_code == 0, prior_result.output

    with unmigrated_app.app_context():
        db.session.execute(text(
            "INSERT INTO portfolios "
            "(id, name, reporting_currency_code, annual_spending_amount, "
            "price_stale_days, fx_stale_days, statement_value_stale_days, "
            "created_at, updated_at) VALUES "
            "(1, 'Portfolio', 'EUR', 48000, 14, 7, 45, "
            "'2026-08-09 10:00:00', '2026-08-09 10:00:00')"
        ))
        db.session.execute(text(
            "INSERT INTO instruments "
            "(id, portfolio_id, name, ticker_or_isin, instrument_type, "
            "valuation_currency_code, is_active, created_at, updated_at) VALUES "
            "(1, 1, 'Existing fund', 'FUND-1', 'fund', 'USD', 1, "
            "'2026-08-09 10:00:00', '2026-08-09 10:00:00')"
        ))
        db.session.commit()

    head_result = runner.invoke(args=["db", "upgrade"])
    assert head_result.exit_code == 0, head_result.output

    with unmigrated_app.app_context():
        row = db.session.execute(text(
            "SELECT name, ticker_or_isin, valuation_currency_code, "
            "fund_base_currency_code, fire_bucket_code "
            "FROM instruments WHERE id = 1"
        )).one()
        assert row.name == "Existing fund"
        assert row.ticker_or_isin == "FUND-1"
        assert row.valuation_currency_code == "USD"
        assert row.fund_base_currency_code is None
        assert row.fire_bucket_code is None
        assert db.session.execute(
            text("SELECT COUNT(*) FROM instrument_classifications")
        ).scalar_one() == 0


def test_spending_currency_migration_preserves_each_portfolios_meaning(
    unmigrated_app: Flask,
) -> None:
    runner = unmigrated_app.test_cli_runner()
    prior_result = runner.invoke(args=["db", "upgrade", "8e4c9b1d2a7f"])
    assert prior_result.exit_code == 0, prior_result.output

    with unmigrated_app.app_context():
        db.session.execute(text(
            "INSERT INTO portfolios "
            "(id, name, reporting_currency_code, annual_spending_amount, "
            "price_stale_days, fx_stale_days, statement_value_stale_days, "
            "created_at, updated_at) VALUES "
            "(1, 'Euro plan', 'EUR', 48000.1250, 14, 7, 45, "
            "'2026-08-09 10:00:00', '2026-08-09 10:00:00'), "
            "(2, 'Dollar plan', 'USD', 60000.5000, 14, 7, 45, "
            "'2026-08-09 10:00:00', '2026-08-09 10:00:00')"
        ))
        db.session.commit()

    head_result = runner.invoke(args=["db", "upgrade"])
    assert head_result.exit_code == 0, head_result.output

    with unmigrated_app.app_context():
        rows = db.session.execute(text(
            "SELECT name, reporting_currency_code, annual_spending_amount, "
            "annual_spending_currency_code FROM portfolios ORDER BY id"
        )).all()
        assert rows[0].name == "Euro plan"
        assert rows[0].annual_spending_amount == 48000130
        assert rows[0].annual_spending_currency_code == "EUR"
        assert rows[1].name == "Dollar plan"
        assert rows[1].annual_spending_amount == 60000500
        assert rows[1].annual_spending_currency_code == "USD"
