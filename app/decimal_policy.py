"""Business precision, exact integer encoding and legacy conversion."""
from decimal import Decimal, ROUND_HALF_UP, localcontext
import re

from app.conventions import parse_decimal

MAX_UNITS = 2**63 - 1
THREE_PLACES = frozenset({'BHD', 'IQD', 'JOD', 'KWD', 'LYD', 'OMR', 'TND'})
ZERO_PLACES = frozenset({'BIF', 'CLP', 'DJF', 'GNF', 'ISK', 'JPY', 'KMF', 'KRW', 'PYG', 'RWF', 'UGX', 'UYI', 'VND', 'VUV', 'XAF', 'XOF', 'XPF'})

# table: field -> (stored places, legacy Numeric precision, legacy scale, currency field)
DECIMAL_FIELDS = {
    'portfolios': {'annual_spending_amount': (3,24,4,'annual_spending_currency_code'), 'annual_inflation_decimal': (8,10,8,None)},
    'accounts': {k:(8,10,8,None) for k in ('portfolio_share_decimal','present_access_decimal')},
    'relationship_rules': {k:(3,28,12,'threshold_currency_code') for k in ('threshold_amount','warning_buffer_amount')},
    'allocation_targets': {k:(8,10,8,None) for k in ('minimum_decimal','maximum_decimal')},
    'instrument_classifications': {'weight_decimal': (8,10,8,None)},
    'retirement_assumptions': {**{k+'_return_decimal':(8,10,8,None) for k in ('equity','income','liquidity','alternatives')}, 'terminal_legacy_target_amount':(3,28,12,'terminal_legacy_target_currency_code')},
    'retirement_plans': {
        **{k:(3,28,12,'currency_code') for k in ('core_amount','flexible_amount','annual_savings_amount')},
        'terminal_legacy_target_amount':(3,28,12,'terminal_legacy_target_currency_code'),
        **{k:(8,10,8,None) for k in ('core_inflation_decimal','flexible_inflation_decimal','equity_return_decimal','income_return_decimal','liquidity_return_decimal','alternatives_return_decimal','lower_rate_decimal','upper_rate_decimal','lower_multiplier_decimal','middle_multiplier_decimal','upper_multiplier_decimal')},
    },
    'retirement_income': {'annual_amount':(3,28,12,'currency_code'), 'inflation_decimal':(8,10,8,None)},
    'fixed_deposits': {'annual_rate_decimal':(8,18,12,None), 'expected_maturity_proceeds_amount':(3,28,12,'currency_code')},
    'postings': {'quantity_delta':(6,28,12,None), 'unit_price':(6,28,12,None), 'cash_amount_delta':(3,28,12,'currency_code')},
    'cash_balance_checkpoints': {k:(3,28,12,'currency_code') for k in ('confirmed_balance_amount','prior_calculated_balance_amount','correction_amount')},
    'prices': {'price_amount':(6,28,12,None)},
    'valuation_observations': {'native_value_amount':(3,28,12,'currency_code')},
    'fx_rates': {'quote_per_base_amount':(6,28,12,None)},
}


def currency_places(currency):
    code = (currency or '').upper()
    if code in {'CLF', 'UYW'}:
        raise ValueError('This currency requires four decimal places and is not supported for monetary entries.')
    return 3 if code in THREE_PLACES else 0 if code in ZERO_PLACES else 2


def rounded(value, places):
    value = parse_decimal(value)
    with localcontext() as ctx:
        ctx.prec = max(28, len(value.as_tuple().digits), value.adjusted() + places + 2)
        return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def money(value, currency):
    return rounded(value, currency_places(currency))


def exact_units(value, places):
    value = parse_decimal(value)
    # Check magnitude before quantizing or formatting potentially huge exponents.
    if abs(value) > Decimal(MAX_UNITS).scaleb(-places):
        raise ValueError('This value exceeds the supported amount limit.')
    if value != rounded(value, places):
        raise ValueError(f'Use at most {places} decimal places; the value has not been rounded.')
    return int(value.scaleb(places))


def require_precision(value, places, error, field):
    try:
        exact_units(value, places)
    except (ValueError, TypeError) as exc:
        raise error(field, str(exc)) from exc
    return value


def row_currency(table, row, currency_field):
    return row.get(currency_field) or (row.get('reporting_currency_code') if table == 'portfolios' else None)


def normalize_legacy(table, row):
    """Return rounded Decimal fields and a before/after record; never guess inputs."""
    result = dict(row)
    changes = []
    for field, (places, _, _, currency_field) in DECIMAL_FIELDS.get(table, {}).items():
        value = row.get(field)
        if value is None:
            continue
        currency = row_currency(table, row, currency_field) if currency_field else None
        target = currency_places(currency) if currency_field else places
        after = rounded(value, target)
        exact_units(after, places)
        if value != 0 and after == 0 and (field in {'quantity_delta','unit_price','price_amount','quote_per_base_amount','weight_decimal'}):
            raise ValueError(f'{table} record {row["id"]}: rounding {field} would erase a nonzero value.')
        result[field] = after
        if after != value:
            changes.append(dict(source_table=table, source_record_id=row['id'], field_name=field,
                                before_value=format(value,'f'), after_value=format(after,'f'), currency_code=currency))
    return result, changes


def scaled_constraint(sql, fields):
    """Scale numeric literals in existing financial range constraints."""
    for field, (places, *_rest) in fields.items():
        sql = re.sub(r'\b'+field+r'(\s*(?:<=|>=|<>|!=|=|<|>)\s*)(-?\d+(?:\.\d+)?)(?![\w.])',
                     lambda m: field+m[1]+str(int(Decimal(m[2]).scaleb(places))), sql)
    return sql


def currency_check(field, currency_expression):
    three = ','.join(repr(code) for code in sorted(THREE_PLACES))
    zero = ','.join(repr(code) for code in sorted(ZERO_PLACES))
    divisor = f"CASE WHEN {currency_expression} IN ({three}) THEN 1 WHEN {currency_expression} IN ({zero}) THEN 1000 ELSE 10 END"
    return f"{field} IS NULL OR ({currency_expression} NOT IN ('CLF','UYW') AND {field} % ({divisor}) = 0)"


def validate_normalized_history(tables):
    """Reject normalization that invalidates source history; never repair by guessing."""
    totals = {}
    for row in tables['instrument_classifications']:
        key = (row['instrument_id'], row['effective_date'])
        totals[key] = totals.get(key, Decimal(0)) + row['weight_decimal']
    if any(abs(total - 1) > Decimal('0.000001') for total in totals.values()):
        raise ValueError('A classification set would be incomplete after conversion. Correct it before upgrading.')
    transactions = {r['id']:r for r in tables['transactions']}
    effects = sorted((r for r in tables['postings'] if r['posting_kind']=='instrument' and r['quantity_delta'] is not None
                      and transactions[r['transaction_id']]['status']=='posted'
                      and transactions[r['transaction_id']]['reverses_transaction_id'] is None),
                     key=lambda r:(transactions[r['transaction_id']]['effective_date'],r['transaction_id'],r['id']))
    balances = {}
    for row in effects:
        key=(row['account_id'],row['instrument_id'])
        balances[key]=balances.get(key,Decimal(0))+row['quantity_delta']
        if balances[key]<0:
            raise ValueError('A holding would become negative after conversion. Review its activity before upgrading.')
    grouped = {}
    for row in tables['postings']:
        grouped.setdefault(row['transaction_id'], []).append(row)
    for transaction_id, rows in grouped.items():
        transaction = transactions[transaction_id]
        if transaction['transaction_type'] not in {'buy', 'sell', 'dividend'}:
            continue
        if not any(row['posting_kind'] in {'clearing', 'income'} for row in rows):
            continue
        sums = {}
        for row in rows:
            if row['cash_amount_delta'] is not None:
                code = row['currency_code']
                sums[code] = sums.get(code, Decimal(0)) + row['cash_amount_delta']
        if any(sums.values()):
            raise ValueError('An activity would not balance after rounding. Review it before upgrading.')
    for row in tables['cash_balance_checkpoints']:
        if row['prior_calculated_balance_amount'] is not None and row['correction_amount'] is not None:
            if row['prior_calculated_balance_amount'] + row['correction_amount'] != row['confirmed_balance_amount']:
                raise ValueError('A cash correction would not reconcile after rounding. Review it before upgrading.')
