"""Overview and setup Review presentation from the shared portfolio summary."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select



OVERVIEW_CHART_SCRIPTS = (
    '<script src="/static/js/vendor/chart.umd.min.js" defer>',
    '<script src="/static/js/charts.js" defer>',
)


def assert_no_inline_script(body: str, extra_scripts: tuple[str, ...] = ()) -> None:
    """Only the deferred enhancement file (plus a page's declared vendored
    extras) may appear; no inline or behaviour-critical JavaScript
   ."""
    scripts = [re.sub(r"\?v=\d+", "", tag) for tag in re.findall(r"<script[^>]*>", body)]
    assert scripts == ['<script src="/static/js/enhance.js" defer>', *extra_scripts]

from app.extensions import db
from app.models import (
    Account,
    FxRate,
    Institution,
    Instrument,
    InstrumentClassification,
    Portfolio,
    PositionRegistration,
    Price,
    Transaction,
    Posting,
    ValuationObservation,
)
from app.services.cash import CashConfirmationCommand, confirm_cash


def _portfolio() -> Portfolio:
    portfolio = Portfolio(
        name="Personal portfolio", reporting_currency_code="EUR",
        annual_spending_amount=Decimal("48000"),
        annual_inflation_decimal=Decimal("0.03"),
        default_as_of_date=date(2026, 8, 2),
    )
    db.session.add(portfolio)
    db.session.commit()
    return portfolio


def _account(portfolio: Portfolio) -> Account:
    institution = Institution(portfolio_id=portfolio.id, name="Broker")
    db.session.add(institution)
    db.session.flush()
    account = Account(
        portfolio_id=portfolio.id, institution_id=institution.id,
        name="Brokerage", account_type="brokerage",
        default_currency_code="EUR", is_multicurrency=False,
        cash_tracking_mode="separate_cash",
        portfolio_share_decimal=Decimal("1"), present_access_decimal=Decimal("1"),
        relationship_eligible=True, is_active=True,
    )
    db.session.add(account)
    db.session.commit()
    return account


def _confirm(
    portfolio: Portfolio,
    account: Account,
    amount: str,
    *,
    currency: str | None = None,
    on: date = date(2026, 8, 1),
) -> None:
    confirm_cash(
        CashConfirmationCommand(
            portfolio_id=portfolio.id,
            account_id=account.id,
            currency_code=currency or account.default_currency_code,
            effective_date=on,
            confirmed_balance_amount=Decimal(amount),
        )
    )


def _statement_position(portfolio, account, *, name="Endowment", value=None, value_date=date(2026, 8, 1)):
    instrument = Instrument(
        portfolio_id=portfolio.id, name=name, instrument_type="other",
        valuation_currency_code="EUR", is_active=True,
    )
    db.session.add(instrument)
    db.session.flush()
    registration = PositionRegistration(
        account_id=account.id, instrument_id=instrument.id,
        tracking_mode="statement_valued", opening_date=date(2026, 8, 1),
    )
    db.session.add(registration)
    db.session.flush()
    if value is not None:
        db.session.add(ValuationObservation(
            position_registration_id=registration.id, effective_date=value_date,
            native_value_amount=Decimal(value), currency_code="EUR",
        ))
    db.session.commit()
    return registration


def _tracked_position(portfolio, account, *, name="Global Fund", quantity="1000", price=None, price_date=date(2026, 8, 1), currency="EUR"):
    instrument = Instrument(
        portfolio_id=portfolio.id, name=name, instrument_type="fund",
        valuation_currency_code=currency, is_active=True,
    )
    db.session.add(instrument)
    db.session.flush()
    registration = PositionRegistration(
        account_id=account.id, instrument_id=instrument.id,
        tracking_mode="transaction_tracked", opening_date=date(2026, 8, 1),
    )
    db.session.add(registration)
    db.session.flush()
    transaction = Transaction(
        portfolio_id=portfolio.id, transaction_type="opening_balance",
        effective_date=date(2026, 8, 1), status="posted",
    )
    db.session.add(transaction)
    db.session.flush()
    db.session.add(Posting(
        transaction_id=transaction.id, account_id=account.id,
        posting_kind="instrument", instrument_id=instrument.id,
        currency_code=currency, quantity_delta=Decimal(quantity),
    ))
    if price is not None:
        db.session.add(Price(
            instrument_id=instrument.id, effective_date=price_date,
            price_amount=Decimal(price), currency_code=currency,
        ))
    db.session.commit()
    return registration


@pytest.fixture
def valued_portfolio(app: Flask) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _statement_position(portfolio, account, value="12345.67")


# --- Root resumption ---



# --- Overview states ---

def test_overview_current_state(client: FlaskClient, valued_portfolio: None) -> None:
    body = client.get("/overview").get_data(as_text=True)
    assert "<h1>Overview</h1>" in body
    assert "12,345.67" in body
    assert "Values as of 2 Aug 2026 · reporting in EUR" in body
    assert "Included portfolio value" in body
    assert "All 1 positions valued and current." in body
    # The valued instrument is unclassified: an actionable classification
    # item links to its editor .
    attention = body.split('id="attention-heading"', 1)[1].split("</section>", 1)[0]
    assert "<strong>Classification</strong>" in attention
    assert "Allocation role and FIRE bucket are not set." in attention
    assert 'href="/instruments/1/classification">Classify →</a>' in attention
    # Reporting-currency position: the currency table shows the same figure.
    assert "Value by currency" in body
    # Subordinate gross subtotals reconcile to the gross total row;
    # untouched cash is never a known zero. With all factors at 100% the
    # bridge notes stay hidden and gross equals the headline.
    assert "Accessible now" not in body
    assert "stays outside the portfolio" not in body
    subtotals = body.split('class="data subtotals"', 1)[1].split("</table>", 1)[0]
    assert "Investments (gross)" in subtotals and "12,345.67" in subtotals
    assert "Cash (gross)" in subtotals and "none tracked" in subtotals
    assert "Gross tracked value" in subtotals


def test_overview_missing_state_never_zero(client: FlaskClient, app: Flask) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _statement_position(portfolio, account, value=None)
    body = client.get("/overview").get_data(as_text=True)
    assert '<div class="headline"><span class="money-missing">—</span></div>' in body
    assert "Missing" in body
    # No synthetic zero in the headline card (the funding card legitimately
    # shows the annual spending amount itself).
    card = body.split('id="total-heading"', 1)[1].split("</section>", 1)[0]
    assert "0.00" not in card
    # Missing item is actionable and links to the section that resolves it.
    assert "No eligible statement value" in body
    assert 'Update →' in body
    assert 'href="/values?registration=1#statements-heading"' in body


def test_overview_partial_is_labelled_known_subtotal(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _statement_position(portfolio, account, name="Valued one", value="10000")
        _tracked_position(portfolio, account, name="Unpriced one", price=None)
    body = client.get("/overview").get_data(as_text=True)
    assert "Known included portfolio value" in body
    assert "Known subtotal" in body
    assert "10,000.00" in body
    assert "Partial" in body
    # Not presented as complete.
    assert "valued and current" not in body


def test_overview_stale_distinct_from_missing(client: FlaskClient, app: Flask) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        # 45-day statement staleness: value of 1 Jun vs as-of 2 Aug is stale.
        _statement_position(portfolio, account, value="5000", value_date=date(2026, 6, 1))
    body = client.get("/overview").get_data(as_text=True)
    assert "Stale" in body
    assert "5,000.00" in body  # stale values still count, with a badge
    # Stale stays distinct from missing on the valued surfaces; the funding
    # card legitimately reports untouched cash as Partial there.
    card = body.split('id="total-heading"', 1)[1].split("</section>", 1)[0]
    assert "Missing" not in card
    attention = body.split('class="attention-list"', 1)[1].split("</ul>", 1)[0]
    assert "Missing" not in attention
    # A single stale component is named and its Update action targets the
    # exact prefilled form.
    attention = body.split('class="attention-list"', 1)[1].split("</ul>", 1)[0]
    assert "Stale statement value" in attention
    assert 'href="/values?registration=1#statements-heading">Update →</a>' in attention


# --- Source-aware update links  ---

def test_missing_price_attention_opens_prices_section(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _tracked_position(portfolio, account, price=None)
    body = client.get("/overview").get_data(as_text=True)
    attention = body.split('class="attention-list"', 1)[1]
    assert 'href="/values?instrument=1#prices-heading">Update →</a>' in attention


def test_missing_fx_attention_opens_fx_section(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        # USD price exists but no USD→EUR FX rate: only conversion is missing.
        _tracked_position(portfolio, account, name="US Fund", price="50", currency="USD")
    body = client.get("/overview").get_data(as_text=True)
    assert "No eligible FX path" in body
    attention = body.split('class="attention-list"', 1)[1]
    assert 'href="/values?base=USD&amp;quote=EUR#fx-heading">Update →</a>' in attention
    assert_no_inline_script(body, OVERVIEW_CHART_SCRIPTS)


def test_review_missing_attention_uses_same_targeted_links(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _statement_position(portfolio, account, value=None)
    body = client.get("/setup/review").get_data(as_text=True)
    attention = body.split('class="attention-list"', 1)[1]
    assert 'href="/setup/values?registration=1#statements-heading">Update →</a>' in attention


# --- Specific stale components  ---

def test_stale_price_attention_names_component_and_targets_form(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _tracked_position(portfolio, account, price="10", price_date=date(2026, 6, 1))
    body = client.get("/overview").get_data(as_text=True)
    attention = body.split('class="attention-list"', 1)[1].split("</ul>", 1)[0]
    assert "Stale price" in attention
    assert "Stale price and" not in attention
    assert 'href="/values?instrument=1#prices-heading">Update →</a>' in attention


def _shared_stale_fund() -> None:
    """One instrument held in two accounts with a stale instrument-wide price
    (one shared action, every affected account named)."""

    portfolio = _portfolio()
    first_account = _account(portfolio)
    registration = _tracked_position(
        portfolio,
        first_account,
        name="Shared Fund",
        price="10",
        price_date=date(2026, 6, 1),
    )
    second_account = Account(
        portfolio_id=portfolio.id,
        institution_id=first_account.institution_id,
        name="Second brokerage",
        account_type="brokerage",
        default_currency_code="EUR",
        is_multicurrency=False,
        cash_tracking_mode="separate_cash",
        portfolio_share_decimal=Decimal("1"),
        present_access_decimal=Decimal("1"),
        relationship_eligible=True,
        is_active=True,
    )
    db.session.add(second_account)
    db.session.flush()
    second_registration = PositionRegistration(
        account_id=second_account.id,
        instrument_id=registration.instrument_id,
        tracking_mode="transaction_tracked",
        opening_date=date(2026, 8, 1),
    )
    opening = Transaction(
        portfolio_id=portfolio.id,
        transaction_type="opening_balance",
        effective_date=date(2026, 8, 1),
        status="posted",
    )
    db.session.add_all([second_registration, opening])
    db.session.flush()
    db.session.add(
        Posting(
            transaction_id=opening.id,
            account_id=second_account.id,
            posting_kind="instrument",
            instrument_id=registration.instrument_id,
            currency_code="EUR",
            quantity_delta=Decimal("100"),
        )
    )
    db.session.commit()


def test_shared_stale_price_is_one_action_with_all_affected_accounts(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        _shared_stale_fund()

    body = client.get("/overview").get_data(as_text=True)
    attention = body.split('class="attention-list"', 1)[1].split("</ul>", 1)[0]
    assert attention.count("Stale price") == 1
    assert attention.count("/values?instrument=1#prices-heading") == 1
    assert "affects Brokerage, Second brokerage" in " ".join(attention.split())


def test_review_shared_stale_price_names_the_same_affected_accounts(
    client: FlaskClient, app: Flask
) -> None:
    # The guided Review attention list cannot drift from Overview.
    with app.app_context():
        _shared_stale_fund()

    body = client.get("/setup/review").get_data(as_text=True)
    attention = body.split('class="attention-list"', 1)[1].split("</ul>", 1)[0]
    assert attention.count("Stale price") == 1
    assert "affects Brokerage, Second brokerage" in " ".join(attention.split())
    assert 'href="/setup/values?instrument=1#prices-heading">Update →</a>' in attention


def test_stale_fx_attention_names_component_and_targets_pair(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _tracked_position(portfolio, account, name="US Fund", price="50", currency="USD")
        db.session.add(FxRate(
            effective_date=date(2026, 6, 1), base_currency_code="USD",
            quote_currency_code="EUR", quote_per_base_amount=Decimal("0.9"),
        ))
        db.session.commit()
    body = client.get("/overview").get_data(as_text=True)
    attention = body.split('class="attention-list"', 1)[1].split("</ul>", 1)[0]
    assert "Stale FX" in attention
    assert 'href="/values?base=USD&amp;quote=EUR#fx-heading">Update →</a>' in attention


def test_stale_price_and_fx_names_both_and_keeps_general_destination(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _tracked_position(portfolio, account, name="US Fund", price="50",
                          price_date=date(2026, 6, 1), currency="USD")
        db.session.add(FxRate(
            effective_date=date(2026, 6, 1), base_currency_code="USD",
            quote_currency_code="EUR", quote_per_base_amount=Decimal("0.9"),
        ))
        db.session.commit()
    body = client.get("/overview").get_data(as_text=True)
    attention = body.split('class="attention-list"', 1)[1].split("</ul>", 1)[0]
    assert "Stale price and FX" in attention
    # Two stale components keep the general Values destination: one link must
    # not imply that only one fix is needed.
    assert 'href="/values">Update →</a>' in attention
    assert "?instrument=" not in attention
    assert "?base=" not in attention


def test_review_stale_attention_uses_same_specific_labels(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _statement_position(portfolio, account, value="5000", value_date=date(2026, 6, 1))
    body = client.get("/setup/review").get_data(as_text=True)
    attention = body.split('class="attention-list"', 1)[1].split("</ul>", 1)[0]
    assert "Stale statement value" in attention
    assert 'href="/setup/values?registration=1#statements-heading">Update →</a>' in attention



# --- Review ---


def test_overview_review_holdings_consistent(client: FlaskClient, valued_portfolio: None) -> None:
    for path in ("/overview", "/setup/review", "/holdings"):
        assert "12,345.67" in client.get(path).get_data(as_text=True)



# --- Cash-aware three-total presentation  ---

def _usd_account(portfolio: Portfolio) -> Account:
    institution = Institution(portfolio_id=portfolio.id, name="US Bank")
    db.session.add(institution)
    db.session.flush()
    account = Account(
        portfolio_id=portfolio.id, institution_id=institution.id,
        name="Dollar account", account_type="brokerage",
        default_currency_code="USD", is_multicurrency=False,
        cash_tracking_mode="separate_cash",
        portfolio_share_decimal=Decimal("1"), present_access_decimal=Decimal("1"),
        relationship_eligible=True, is_active=True,
    )
    db.session.add(account)
    db.session.commit()
    return account


def _subtotals_block(body: str) -> str:
    return body.split('class="data subtotals"', 1)[1].split("</table>", 1)[0]


def _cash_row(body: str) -> str:
    return _subtotals_block(body).split("<th scope=\"row\">Cash (gross)</th>", 1)[1].split("</tr>", 1)[0]


def test_overview_combines_investment_and_cash_subtotals(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _statement_position(portfolio, account, value="10000")
        _confirm(portfolio, account, "2000")
    body = client.get("/overview").get_data(as_text=True)
    assert "Included portfolio value" in body
    assert "12,000.00" in body
    assert "All 1 positions and 1 cash balances valued and current." in body
    subtotals = _subtotals_block(body)
    assert "Investments (gross)" in subtotals and "10,000.00" in subtotals
    assert "Cash (gross)" in subtotals and "2,000.00" in subtotals
    # By-currency table is the whole portfolio: EUR investments plus cash.
    native = body.split('id="currency-totals-heading"', 1)[1]
    assert "12,000.00" in native


def test_overview_institution_and_currency_charts_match_exact_tables(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        euro_account = _account(portfolio)
        _statement_position(portfolio, euro_account, value="10000")
        dollar_account = _usd_account(portfolio)
        _confirm(portfolio, dollar_account, "5000", currency="USD")
        db.session.add(
            FxRate(
                effective_date=date(2026, 8, 1),
                base_currency_code="USD",
                quote_currency_code="EUR",
                quote_per_base_amount=Decimal("0.9"),
            )
        )
        db.session.commit()

    body = client.get("/overview").get_data(as_text=True)
    assert body.count('data-chart="value-breakdown"') == 2
    institution = body.split('id="institution-totals-heading"', 1)[1].split(
        "</section>", 1
    )[0]
    currency = body.split('id="currency-totals-heading"', 1)[1].split(
        "</section>", 1
    )[0]
    assert "Broker" in institution and "10,000.00" in institution
    assert "US Bank" in institution and "4,500.00" in institution
    assert "EUR" in currency and "10,000.00" in currency
    assert "USD" in currency and "5,000.00" in currency
    assert "4,500.00" in currency
    assert 'data-percentages="[69.0, 31.0]"' in currency
    assert "Share of known included value" in currency
    assert "69.0%" in currency and "31.0%" in currency
    assert "does not infer the underlying economic currency exposure" in currency
    assert "charts show only known reporting values" in institution.lower()
    assert_no_inline_script(body, OVERVIEW_CHART_SCRIPTS)


def test_overview_missing_cash_fx_known_subtotal_and_targeted_fx_link(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _statement_position(portfolio, account, value="10000")
        _confirm(portfolio, _usd_account(portfolio), "5000")
    body = client.get("/overview").get_data(as_text=True)
    # Unconverted cash is excluded from the labelled known subtotal, never zero.
    assert "Known included portfolio value" in body
    assert "10,000.00" in body
    cash_row = _cash_row(body)
    assert "—" in cash_row and "no reporting value yet" in cash_row
    assert "0.00" not in cash_row
    attention = body.split('class="attention-list"', 1)[1].split("</ul>", 1)[0]
    assert '<span class="badge badge-warning">Cash</span>' in attention
    assert "USD cash" in attention
    assert 'href="/values?base=USD&amp;quote=EUR#fx-heading">Update →</a>' in attention


def test_overview_stale_cash_fx_stays_included_and_identified(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _statement_position(portfolio, account, value="10000")
        _confirm(portfolio, _usd_account(portfolio), "5000")
        db.session.add(FxRate(
            effective_date=date(2026, 6, 1), base_currency_code="USD",
            quote_currency_code="EUR", quote_per_base_amount=Decimal("0.9"),
        ))
        db.session.commit()
    body = client.get("/overview").get_data(as_text=True)
    # Stale FX still counts: 10,000 + 5,000 × 0.9.
    assert "14,500.00" in body
    attention = body.split('class="attention-list"', 1)[1].split("</ul>", 1)[0]
    assert "Stale FX" in attention
    assert '<span class="badge badge-warning">Cash</span>' in attention
    assert "USD cash" in attention
    assert 'href="/values?base=USD&amp;quote=EUR#fx-heading">Update →</a>' in attention


def test_overview_confirmed_zero_is_a_real_cash_value(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _statement_position(portfolio, account, value="12345.67")
        _confirm(portfolio, account, "0")
    body = client.get("/overview").get_data(as_text=True)
    cash_row = _cash_row(body)
    assert "0.00" in cash_row and "1 cash balances valued" in cash_row


def test_overview_untouched_account_is_not_a_known_zero(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _statement_position(portfolio, account, value="12345.67")
    body = client.get("/overview").get_data(as_text=True)
    cash_row = _cash_row(body)
    assert "—" in cash_row and "none tracked" in cash_row
    assert "0.00" not in cash_row


def test_review_shows_same_three_totals(client: FlaskClient, app: Flask) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _account(portfolio)
        _statement_position(portfolio, account, value="10000")
        _confirm(portfolio, account, "2000")
    body = client.get("/setup/review").get_data(as_text=True)
    assert "Included portfolio value" in body
    assert "12,000.00" in body
    subtotals = _subtotals_block(body)
    assert "Investments (gross)" in subtotals and "10,000.00" in subtotals
    assert "Cash (gross)" in subtotals and "2,000.00" in subtotals
    native = body.split('id="review-native-heading"', 1)[1]
    assert "12,000.00" in native




# --- Share/access overlay presentation  ---

def _joint_account(portfolio, *, share="1", access="1", name="Joint account"):
    institution = Institution(portfolio_id=portfolio.id, name="Joint Bank")
    db.session.add(institution)
    db.session.flush()
    account = Account(
        portfolio_id=portfolio.id, institution_id=institution.id,
        name=name, account_type="brokerage",
        default_currency_code="EUR", is_multicurrency=False,
        cash_tracking_mode="separate_cash",
        portfolio_share_decimal=Decimal(share),
        present_access_decimal=Decimal(access),
        relationship_eligible=True, is_active=True,
    )
    db.session.add(account)
    db.session.commit()
    return account


def test_overview_partial_share_included_headline_and_sublines(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _joint_account(portfolio, share="0.5")
        _statement_position(portfolio, account, value="100000")
    body = client.get("/overview").get_data(as_text=True)
    card = body.split('id="total-heading"', 1)[1].split("</section>", 1)[0]
    # EUR 100,000 × 50% share × 100% access: included and accessible 50,000.
    assert "Included portfolio value" in card
    assert "50,000.00" in card
    assert "Gross tracked value" in card and "100,000.00" in card
    assert "Accessible now" in card
    # Gross investment subtotal stays explicitly labelled.
    assert "Investments (gross)" in card
    # The standing share fact is carried by the sublines above, not nagged
    # in Attention; the unclassified instrument is the only
    # actionable item .
    attention = body.split('id="attention-heading"', 1)[1].split("</section>", 1)[0]
    assert "Only part of this account is included" not in attention
    assert "<strong>Classification</strong>" in attention


def test_overview_zero_share_is_a_real_zero_not_missing(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _joint_account(portfolio, share="0")
        _statement_position(portfolio, account, value="10000")
    body = client.get("/overview").get_data(as_text=True)
    card = body.split('id="total-heading"', 1)[1].split("</section>", 1)[0]
    assert '<div class="headline">' in card and "0.00" in card  # exact zero
    assert "money-missing" not in card.split('class="headline', 1)[1].split("</div>", 1)[0]
    # The exclusion is stated by the gross/excluded subline; Attention stays
    # actionable-only.
    assert "10,000.00" in card and "stays outside the portfolio" in card
    attention = body.split('id="attention-heading"', 1)[1].split("</section>", 1)[0]
    assert "excluded from the portfolio value" not in attention


def test_overview_missing_value_has_no_synthetic_overlay(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _portfolio()
        account = _joint_account(portfolio, share="0.5")
        _statement_position(portfolio, account, value=None)
    body = client.get("/overview").get_data(as_text=True)
    assert '<div class="headline"><span class="money-missing">—</span></div>' in body
    assert "Missing" in body
    # No synthetic zero in the headline card (the funding card legitimately
    # shows the annual spending amount itself).
    card = body.split('id="total-heading"', 1)[1].split("</section>", 1)[0]
    assert "0.00" not in card


# --- Ten-year funding and reporting-currency view  ---

def _funding_portfolio(
    *, spending: str = "8000", spending_currency: str = "EUR",
    inflation: str | None = None,
) -> Portfolio:
    portfolio = Portfolio(
        name="Personal portfolio", reporting_currency_code="EUR",
        annual_spending_amount=Decimal(spending),
        annual_spending_currency_code=spending_currency,
        annual_inflation_decimal=(
            Decimal(inflation) if inflation is not None else None
        ),
        default_as_of_date=date(2026, 8, 2),
    )
    db.session.add(portfolio)
    db.session.commit()
    return portfolio


def _classified_position(
    portfolio, account, *, value: str, role: str | None,
    bucket: str | None = None, name="Classified fund",
):
    registration = _statement_position(portfolio, account, name=name, value=value)
    instrument = db.session.get(Instrument, registration.instrument_id)
    instrument.fire_bucket_code = bucket
    if role is not None:
        db.session.add(InstrumentClassification(
            instrument_id=registration.instrument_id, economic_role_code=role,
            weight_decimal=Decimal("1"), effective_date=date(2026, 8, 1),
        ))
    db.session.commit()
    return registration


def _funding_block(body: str) -> str:
    return body.split('id="funding-heading"', 1)[1].split("</section>", 1)[0]


def _funding_row(body: str, label: str) -> str:
    return _funding_block(body).split(
        f'<th scope="row">{label}', 1
    )[1].split("</tr>", 1)[0]


def test_reporting_view_override_revalues_and_preserves_as_of(
    client: FlaskClient, app: Flask
) -> None:
    # ccy=USD revalues portfolio and spending for the request only,
    # both pickers preserve each other's values, stored default untouched.
    with app.app_context():
        portfolio = _funding_portfolio(spending="48000")
        account = _account(portfolio)
        # The account's declared currency makes USD a relevant reporting choice.
        account.default_currency_code = "USD"
        account.is_multicurrency = True
        _classified_position(
            portfolio, account, value="100", role="equity", bucket="growth"
        )
        _confirm(portfolio, account, "1000", currency="EUR")
        _confirm(portfolio, account, "0", currency="USD")
        db.session.add(FxRate(
            effective_date=date(2026, 8, 1), base_currency_code="EUR",
            quote_currency_code="USD", quote_per_base_amount=Decimal("2"),
        ))
        db.session.commit()
        portfolio_id = portfolio.id
    body = client.get("/overview?as_of=2026-08-02&ccy=usd").get_data(as_text=True)
    assert "reporting in USD" in body
    card = body.split('id="total-heading"', 1)[1].split("</section>", 1)[0]
    assert "2,200.00" in card  # (EUR 100 position + EUR 1,000 cash) × 2
    # The picker shows the selected view and both forms carry each other.
    ccy_input = re.search(r'<select[^>]*id="ccy".*?</select>', body, re.S).group(0)
    assert 'value="USD" selected' in ccy_input
    assert '<input type="hidden" name="as_of" value="2026-08-02">' in body
    assert '<input type="hidden" name="ccy" value="USD">' in body
    # Annual spending is converted too, with its FX path disclosed.
    funding = _funding_block(body)
    assert "48,000.00" in funding and "96,000.00" in funding
    assert "1 EUR = 2 USD" in funding and "EUR → USD" in funding
    with app.app_context():
        assert db.session.get(Portfolio, portfolio_id).reporting_currency_code == "EUR"
    assert_no_inline_script(body, OVERVIEW_CHART_SCRIPTS)


def test_invalid_reporting_view_error_is_linked_announced_focused(
    client: FlaskClient, valued_portfolio: None
) -> None:
    # An invalid ccy falls back to the stored default; the error is
    # announced, links to its field, and focuses it without JavaScript.
    response = client.get("/overview?ccy=EU")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "reporting in EUR" in body
    error = re.search(
        r'<p class="field-error" id="ccy-error" role="alert">.*?</p>', body, re.S
    ).group(0)
    assert 'href="#ccy"' in error
    assert "three-letter currency" in error
    assert "saved default, EUR" in error
    ccy_input = re.search(r'<select[^>]*id="ccy"[^>]*>', body).group(0)
    assert 'aria-invalid="true"' in ccy_input
    assert 'aria-describedby="ccy-error ccy-hint"' in ccy_input
    assert "autofocus" in ccy_input


def test_funding_applies_accessible_cash_first_then_buckets(
    client: FlaskClient, app: Flask
) -> None:
    # Spending 8,000 at 0% inflation: Now needs 24,000, Bridge needs 56,000.
    # Cash 6,000 applies first, then the Bridge income fund early, then the
    # Growth equity fund with both dependencies named. Projects is excluded.
    with app.app_context():
        portfolio = _funding_portfolio(inflation="0")
        account = _account(portfolio)
        _confirm(portfolio, account, "6000")
        _classified_position(
            portfolio, account, value="20000", role="income",
            bucket="bridge", name="Bridge income",
        )
        _classified_position(
            portfolio, account, value="60000", role="equity",
            bucket="growth", name="Growth equity",
        )
        _classified_position(
            portfolio, account, value="20000", role="alternatives",
            bucket="projects", name="House project",
        )
    body = client.get("/overview").get_data(as_text=True)
    # The reserve assessment is the only funding card.
    assert "Defensive runway" not in body and "Spending runway" not in body
    funding = _funding_block(body)
    assert "Annual spending" in funding and "8,000.00" in funding
    assert "purchasing power" in funding and "zero real return" in funding
    assert "Annual spending stays constant" in funding
    from html import unescape
    import json
    chart_tag = re.search(r'<div[^>]*data-chart="bucket-reserves"[^>]*>', funding)[0]
    assert json.loads(unescape(re.search(r'data-actual="([^"]+)"', chart_tag)[1])) == ['7.0', '23.3']
    assert json.loads(unescape(re.search(r'data-required="([^"]+)"', chart_tag)[1])) == ['27.9', '65.1']
    assert json.loads(unescape(re.search(r'data-actual-amounts="([^"]+)"', chart_tag)[1])) == ['6000.00', '20000.00']
    assert json.loads(unescape(re.search(r'data-required-amounts="([^"]+)"', chart_tag)[1])) == ['24000.00', '56000.00']
    assert 'data-unit="percent"' in chart_tag and 'data-maximum="100"' in chart_tag
    assert '86,000.00' in funding  # Denominator excludes the Project holding.
    assert funding.index('data-chart="bucket-reserves"') < funding.index('id="funding-detail"')
    assert "Accessible cash is assigned once" in funding
    now = _funding_row(body, "Now — years 1–3")
    assert "24,000.00" in now and "Gap" in now
    assert "6,000.00" in now   # cash applied first
    assert "18,000.00" in now  # Now reserve gap, covered by early Bridge use
    bridge = _funding_row(body, "Bridge — years 4–10")
    assert "56,000.00" in bridge and "Gap" in bridge
    assert "54,000.00" in bridge  # Bridge reserve gap, covered by Growth
    # Growth remainder is what is left; Projects stays visibly excluded.
    assert "Unallocated capital" in funding and "6,000.00" in funding
    assert "Growth remaining" not in funding
    assert "do not cover both periods on their own" in funding
    assert "Left after year 10" not in funding
    assert "stay excluded" in funding and "20,000.00" in funding
    # The waterfall stays inspectable without JavaScript or recomputation.
    assert "How each period is funded" in funding
    assert "Accessible cash" in funding
    assert "Bridge (3–10 years) bucket" in funding
    assert "Growth (10+ years) bucket" in funding
    assert "Later bucket" in funding and "Equity-exposed" in funding
    assert "year 1" in funding  # constant real annual amounts


def test_reserve_card_works_without_a_nominal_inflation_assumption(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _funding_portfolio()  # no inflation assumption
        account = _account(portfolio)
        _confirm(portfolio, account, "6000")
        _classified_position(
            portfolio, account, value="20000", role="income", bucket="now"
        )
    body = client.get("/overview").get_data(as_text=True)
    funding = _funding_block(body)
    assert "Now — years 1–3" in funding
    assert '24,000.00' in _funding_row(body, 'Now — years 1–3')
    assert '56,000.00' in _funding_row(body, 'Bridge — years 4–10')
    assert 'zero real return' in funding
    assert 'Annual inflation assumption' not in body
    assert 'Set funding assumptions' not in funding


def test_unbucketed_holding_makes_funding_partial(client: FlaskClient, app: Flask) -> None:
    # A valued holding without a bucket cannot be a source: the known
    # arithmetic stays visible under an explicit Partial badge.
    with app.app_context():
        portfolio = _funding_portfolio(inflation="0")
        account = _account(portfolio)
        _confirm(portfolio, account, "6000")
        _classified_position(portfolio, account, value="20000", role=None)
    body = client.get("/overview").get_data(as_text=True)
    funding = _funding_block(body)
    assert "Partial" in funding
    assert "describe known sources only" in funding
    assert "coverage is incomplete" in funding
    assert "cover both periods without Growth withdrawals" not in funding
    now = _funding_row(body, "Now — years 1–3")
    assert "6,000.00" in now and "Gap" in now


def test_unsourced_cash_is_partial_confirmed_negative_is_deficit(
    client: FlaskClient, app: Flask
) -> None:
    with app.app_context():
        portfolio = _funding_portfolio(inflation="0")
        account = _account(portfolio)
        _classified_position(
            portfolio, account, value="60000", role="equity", bucket="growth"
        )
    body = client.get("/overview").get_data(as_text=True)
    # Untouched cash is unknown, never a synthetic zero.
    assert "Partial" in _funding_block(body)

    with app.app_context():
        portfolio = db.session.scalar(select(Portfolio))
        account = db.session.scalar(select(Account))
        _confirm(portfolio, account, "-50")
    body = client.get("/overview").get_data(as_text=True)
    funding = _funding_block(body)
    # A confirmed negative balance is real: it adds to the Now requirement.
    assert "negative by" in funding and "50.00" in funding
    now = _funding_row(body, "Now — years 1–3")
    assert "24,050.00" in now


def test_missing_and_stale_spending_fx_are_honest_funding_states(
    client: FlaskClient, app: Flask
) -> None:
    # Missing spending FX withholds period results; the attention item and
    # the card both link to the exact prefilled FX form.
    with app.app_context():
        portfolio = _funding_portfolio(
            spending="10000", spending_currency="USD", inflation="0"
        )
        account = _account(portfolio)
        _classified_position(
            portfolio, account, value="100", role="equity", bucket="growth"
        )
        _confirm(portfolio, account, "8000")
    body = client.get("/overview").get_data(as_text=True)
    funding = _funding_block(body)
    assert "Missing" in funding
    assert "Now — years 1–3" not in funding
    assert 'href="/values?base=USD&amp;quote=EUR#fx-heading">Update →</a>' in funding
    attention = body.split('class="attention-list"', 1)[1].split("</ul>", 1)[0]
    assert "Annual spending FX" in attention
    assert 'href="/values?base=USD&amp;quote=EUR#fx-heading">Update →</a>' in attention

    # A stale rate still computes, badged Stale with the reason named.
    with app.app_context():
        db.session.add(FxRate(
            effective_date=date(2026, 6, 1), base_currency_code="USD",
            quote_currency_code="EUR", quote_per_base_amount=Decimal("0.8"),
        ))
        db.session.commit()
    body = client.get("/overview").get_data(as_text=True)
    funding = _funding_block(body)
    assert "Stale" in funding
    assert "the spending FX rate is older than its freshness setting" in funding
    now = _funding_row(body, "Now — years 1–3")
    assert "24,000.00" in now  # 10,000 USD × 0.8 × 3 years at 0% inflation
    assert "8,000.00" in now and "Gap" in now
