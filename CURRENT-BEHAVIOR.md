# Current behavior and calculation reference

This describes the v1.0.0-beta.2 baseline. It is a reference for understanding and
changing the application, not a permanent product specification. New features can
revise these choices with clear behavior, data migration and verification.

## Portfolio sources

One portfolio per SQLite database; several local files can be deliberately selected.
Transaction-tracked positions use quantities and dated prices. Statement-valued
positions use dated value observations, without invented units or cash events.
A position is never counted in both modes. Fully sold positions remain in history.

Separate cash uses the latest eligible end-of-day confirmation plus later cash
effects. A same-date confirmation includes that day's activities. Confirmations
create no postings; corrections are not income, spending or performance. Accounts
whose cash is included in an aggregate value have no separate cash sleeve. Custody
and a linked cash-settlement account remain distinct; each effect is counted once.

Native currency amounts remain authoritative. Apply ownership share and withdrawal
access separately. Restricted included capital still contributes to net worth;
access affects available withdrawals. Source values expose their effective dates,
valuation mode, completeness and freshness. Missing means unknown, never zero.

Buys, sells, dividends and deposit events save related rows atomically. Posted
activities are reversed rather than rewritten. Corrections to dated prices/values,
FX and cash retain explicit confirmation/audit behavior. Source value changes alone
are not measured investment returns. Frozen snapshots/comparisons are audit records,
not editable holdings or sources for subsequent portfolio facts.

## Exact quantities and storage

All financial services use Decimal. Source columns use checked signed 64-bit scaled
integers: three storage decimals for money, six for units/prices/entered FX, eight
for dimensionless fractions. Currency validation restricts money to its minor unit
(normally two; supported zero/three-place currencies differ). Four-place monetary
currencies remain unsupported. Percent inputs accept up to six decimal places.

Excess input precision and overflow are errors. Calculated settlements use half-up
rounding; intermediate conversions/projections retain Decimal precision. Portfolio
quantities are replayed with exact values, rather than SQLite floating-point SUM.

Schema head `e8f2a6b0c4d7` includes decimal-conversion evidence. Older data upgrades
round according to these rules in a staged copy, retain before/after records and
validate source history before replacement. Original immutable snapshots and FX
provider evidence remain unchanged. Lossy schema downgrade is refused; a preserved
pre-upgrade SQLite copy is the recovery path. JSON format 11 includes conversion
records and accepts formats 1–11 through the validated restore workflow.

## FX

Reporting currency is a view. Saved dated EUR reference sets provide coherent
inverses and cross rates; only rates from the same eligible set are combined.
Prior individual-pair records remain available before the first saved reference
set. A missing leg never silently inherits an older date. Effective dates and
staleness remain visible.

Settings can manually request Banca d'Italia's public JSON exchange-rate table.
The request carries no portfolio contents or requested holding identifiers. Review
before saving; repeated identical data is a no-op. A changed same-date edition
requires explicit replacement and retains the previous edition. Historical dates
are not polled. Manual entry/correction and offline use remain supported. Large
manual changes require acknowledgement. Public indicative FX is not a trade quote.
Provider format/address changes can require application maintenance.

## Allocation and buckets

Economic roles are Equity, Income, Liquidity and Alternatives. Dated instrument
weights total 100% subject to the existing validation tolerance. Cash is Liquidity.
Role allocation differs from a spending bucket; one does not imply the other.
Instrument-wide metadata omitted from current forms is retained in stored records.

Current buckets are Now, Bridge, Growth and Projects, one per instrument. Projects
contribute to net worth/allocation but are excluded from retirement funding. Legacy
stored role/bucket mappings are implemented in the classification service; changing
labels must not reinterpret those records accidentally.

Allocation targets are user-entered. Target comparisons and chart bands are withheld
when included values are missing or unclassified. Unclassified role and bucket
amounts are summed directly from holdings lacking those classifications; tiny
Decimal remainders from differently grouped FX totals do not count as missing
classification. This calculation fix requires no stored-data migration.

Overview's ten-year reserve assessment compares
Now with three years of Core and Bridge with seven years of Core, held constant in
the selected date's purchasing power (zero real return). Without an adopted plan,
the unsplit annual spending budget is identified explicitly. Negative sourced cash
adds to Now's requirement. This reserve check is not a full retirement projection.

Cash is assigned once through the Now/Bridge waterfall. Account access and deposit
maturity independently govern eligibility. Current bucket placement is shown before
cross-bucket applications; the sequential waterfall determines total coverage,
Growth reliance and shortfalls. Show uncapped surpluses/gaps and distinct unused
reserves. Percentage charts use the same positive eligible starting capital as the
reserve service, including Growth. Unknown/zero denominators withhold percentages.

## Deterministic retirement

The planner solves a steady annual real Flexible allowance alongside entered Core
spending, returns, inflation, working-year net savings, dated retirement income,
retirement age, final age and a real-money legacy goal. Calculations identify Core
shortfalls and unreachable legacy goals. Withdrawal amounts are before unmodelled
withdrawal taxes; planned income's contribution is shown when applicable.

Included non-Project capital remains modelled while restricted. Account access and
fixed-deposit maturity independently control withdrawal availability. Withdrawals
follow Cash → Now → Bridge → Growth, preserving source identity and proportional
mixed-role sales. Deposits follow the existing principal/quoted-terms model, not a
general accrual engine. Positive working savings enter at year-end in the starting
retirement mix and stop at retirement; working living costs are assumed salary-funded.

Update projection is exploration. Save as my plan explicitly adopts the calculated
Core + Flexible budget. Shared spending views use that adopted plan; the ten-year
reserve card intentionally uses only Core. Simulated income, spending changes and
sales never post ledger entries. Optional spending rules and named frozen return-path
comparisons remain read-only experiments unless an explicit adoption action applies.

Actual-spending charts show nominal annual payments and a purchasing-power reference;
capital charts use today's money with nominal equivalents. The legacy target has
both bases at the final age. Printable reports expose corresponding annual tables.

## Monte Carlo

The USD experiment starts from a reviewed deterministic projection and tests its
Flexible allowance or a lower amount, including zero. It compares current and one
alternative allocation across 1,000 shared repeatable paths. Proportional withdrawals
and annual rebalancing replace bucket ordering in this experiment. Projects and real
access/maturity limits remain; source preparation still expects bucket classification.
Bucket-free portfolio planning is not an implemented mode.

Risk model v2 uses user-entered nominal compound growth, fixed inflation, annual
independent correlated normal log-return shocks and no upper-return clipping.
Equity annual-return volatility is 16.3%, converted to log volatility consistently
with entered growth. Income and Alternatives use illustrative 6% and 16% log-return
volatility; Liquidity has zero simulated volatility. Equity/Income shock correlation
is +0.35; gold shocks have zero correlation with either, not a guaranteed hedge.

Exposure proxies are global equities, intermediate investment-grade bonds, cash/
deposits and gold. Deposits are mapped to Liquidity for the experiment and receive
its entered fixed return, including before maturity; recorded classifications stay
intact. The preset equity annual-return volatility of 16.3% is an illustrative
broad-market value; holding it constant throughout the user's horizon is this
application's modelling choice. Growth rates are user-entered; bond and gold
volatility settings are illustrative.

Preset risk parameters and any prepopulated example inputs are supplied to help
users explore and test the software's calculation logic. They are not recommended
investment assumptions, personalized investment advice, or recommendations to buy,
sell or hold any asset or adopt any allocation. Simulation results are conditional
illustrations, not forecasts or guarantees of future performance, and should not
be relied on alone for investment or retirement decisions. Users should assess
the suitability of all assumptions for their circumstances and seek qualified
professional advice where needed.

At the annual boundary, available assets pay net spending proportionally; remaining
available assets rebalance before returns. Locked holdings cannot be traded: deficits
are calculated against the total including locked holdings, and available money fills
them proportionally when the target is infeasible. Working savings enter at year-end.
The shared annual engine and exact tests define the detailed source/date boundaries.

Results separate full-lifestyle funding, Core shortfalls and the final-age goal;
percentile charts are distributions, not individual paths. Printable tables and paired
paths allow inspection. Persistent regimes, heavy tails, changing correlations,
separate FX paths, deposit default, variable inflation, withdrawal taxes and trading
costs are unmodelled. Results are conditional scenarios, not guarantees.

Signed links validate model version and current source evidence. A bounded four-run
process-local cache speeds repeated inspection; source changes require fresh review.
There is no persistent run archive. Model and draw versions identify repeatable
calculations; stored policy versions and schema revisions govern data compatibility.

## Local boundary

Waitress binds only to 127.0.0.1; Host validation accepts localhost/127.0.0.1 with a
valid port before database discovery or access. CSRF applies to writes. There is no
multi-user authentication, public server, background worker, telemetry or cloud sync.
Do not infer that local safety checks make the app suitable for external hosting.
The shell launcher invokes Waitress as a module using the repository-local virtual
environment's Python, so a stale console-script interpreter path does not block startup.
