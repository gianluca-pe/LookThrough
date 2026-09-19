# LookThrough 1.0.0-beta.2

Beta release dated 19 September 2026 for local, single-user portfolio monitoring
and retirement planning.

## Included

Manual portfolio records; dated values and FX; cash confirmations and auditable
activity/reversals; consolidated holdings/allocation; bucket reserve assessment;
deterministic Core/Flexible affordability; 1,000-path Monte Carlo comparisons;
named local databases; explicit upgrades and validated backup/restore.

The baseline includes exact financial storage, concurrent-write and backup-snapshot
safeguards, local Host validation, a quiet Waitress launcher and valid editable
Monte Carlo allocation defaults. Schema head is `e8f2a6b0c4d7`; backup format is 11.

## Source distribution

The source distribution includes application code, the complete migration chain,
current documentation and synthetic test fixtures. Private databases, backups,
exports and virtual environments are excluded.

LookThrough uses the BSD 3-Clause license with attribution to "LookThrough
contributors". Bundled dependency licenses remain separate. Release archives must
include LICENSE and THIRD-PARTY-NOTICES.md.

## Documentation maintenance

Comments and docstrings describe current behavior without external design-document
citations or development anecdotes. Setup prerequisites, quantity-entry fallback,
chart loading and display precision are documented against the implementation.
Application logic, rendered UI content, migration identifiers and stored formats
are unchanged.

Verification on 19 September 2026: **1,005 tests passed in 77.14 seconds**, plus
**17 precision/compatibility probes**. Python syntax-tree and comment-stripped
template/CSS/JavaScript comparisons confirmed no executable or presentation changes.
JavaScript syntax, local documentation links and diff whitespace checks passed.
Browser interaction checks were not repeated for these documentation-only edits.

## Launcher correction

The launcher starts Waitress with `.venv/bin/python -m waitress`. This avoids a
startup failure when the generated `waitress-serve` script retains an obsolete
interpreter path after a project-folder move. Binding remains 127.0.0.1:5001;
portfolio data and schema are unchanged.

Verification: **72 launcher, application-factory and local-Host tests passed**.
The new regression reproduced the failure before the correction. A disposable
application copy also passed actual launcher startup from another working directory
with spaces in its path, health/chooser/static requests, Host rejection and clean
interrupt shutdown. Dependency consistency and shell syntax checks passed.

## Verification

Verification on 19 September 2026 used an independent fresh virtual environment,
with exact dependency versions matching requirements-verified.txt and imports
resolved entirely within this repository. All data was disposable and synthetic.

- Ordinary regression: **1,005 passed in 80.87 seconds**.
- Explicit precision/compatibility probes: **17 passed**.
- Waitress chooser browser checks: creation, prior-schema upgrade, dataset switching,
  prefixed Overview/retirement/Monte Carlo entry, keyboard, linked-error focus,
  no-JavaScript recovery and 390px layout passed.
- Waitress Monte Carlo browser checks: streamed progress, a complete 1,000-path run,
  chart/table agreement, keyboard age/path selection, connection recovery,
  no-JavaScript comparison and reduced-motion/narrow-layout checks passed.
- Waitress backup browser checks: invalid upload, linked errors/focus, retry, preview,
  required confirmation, keyboard restore, no JavaScript and exact table round trip
  passed.
- Dependency consistency, shell syntax, documentation links and source-file review
  were checked. The full migration/backup chain is retained.

The actual run.sh launcher also passed in a disposable app copy: startup from
another working directory, loopback binding, health/chooser/static files, CSRF and
Host rejection, database creation/redirect, quiet request/migration logging, preserved
server warnings/application errors, no browser tracebacks and clean Control-C shutdown.

## Limits

- Verified runtime: macOS, Python 3.14.7 and requirements-verified.txt. Python 3.12+
  is declared; additional platforms/versions need separate verification.
- Current deterministic planning is bucket-based. Optional bucket-free planning
  and a consolidated portfolio factsheet remain future ideas.
- Monte Carlo is an illustrative USD model with fixed inflation and simplified
  costs/taxes/asset behavior. See CURRENT-BEHAVIOR.md and the in-app assumptions.
- FX downloads are manual public reference data with offline fallback; other
  portfolio data is manual. No broker feed, telemetry or cloud service is included.
- Source-ready local software is not an installer, hosted product or public release.
