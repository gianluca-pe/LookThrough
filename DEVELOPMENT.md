# Development

## Current architecture

Python, Flask, server-rendered Jinja and SQLite. SQLAlchemy 2.x manages source
records; Alembic/Flask-Migrate manages schema upgrades. Waitress serves the app on
127.0.0.1:5001. Progressive JavaScript improves interactions; Chart.js is bundled
locally. There is no separate frontend build or required network call during use.

The stack is the starting point, not a permanent veto on future design. Propose a
change when it solves an agreed problem; account for local deployment, data
compatibility, accessibility and maintenance cost. Do not add infrastructure merely
because it might be useful later.

- `app/__init__.py`: factories for the chooser or an explicitly configured dataset.
- `app/databases.py`: intentional selection, signed dataset links, per-file apps
  and idle eviction. Each app/engine stays bound to its file.
- `app/models/`: authoritative schemas and exact scaled-integer types.
- `app/services/`: valuation, posting, cash, classification, FX, backup and planning.
  Routes pass explicit portfolio/date inputs and handle forms/navigation.
- `app/templates/` and `app/static/`: presentation of service results; no alternate
  financial calculation path in JavaScript.
- `migrations/`: the complete revision chain. Preserve revision identifiers and
  compatibility code so existing databases can upgrade safely.
- `tests/`: numerical/route/UI regression tests and synthetic browser fixtures.

Persisted compatibility identifiers (including `core-flexible-0049`, Monte Carlo
model/draw versions and Alembic revisions) are data-format keys. Do not rename them
as documentation cleanup; changing them requires a compatibility plan.

Keep related source writes atomic. Reversals preserve posted history. Database
upgrade and restore validate staged copies before publication. Do not open the
user's database as part of test setup or a routine schema check.

## Install and verify

Use the README setup commands with `requirements-verified.txt` for the pinned
baseline. Python 3.14.7/macOS is verified; broader support needs its own checks.
The repository is self-contained; no globally installed application package is required.

```sh
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
```

Pytest discovers the ordinary suite. Additional retained compatibility experiments:

```sh
.venv/bin/python -m pytest -q tests/decimal_storage_analysis.py tests/foundations_review_probes.py
```

These are explicit probes, not another mandatory full suite for every small edit.
Choose checks that cover the changed behavior. Schema work also needs historical
migration/backup coverage; frontend work needs a real browser where behavior or
layout matters. Use disposable databases and exact Decimal expectations.

Prefer browser E2E checks for complex behavior. Keep an isolated test only when it
detects a material failure the browser checks cannot observe, such as an exact
calculation, atomic rollback or historical migration. Define its failure cases
before implementation. For each E2E run, retain a repeatable verification record:
the synthetic fixture and commands, assertion output, and any useful screenshots or
exports. Keep generated artifacts outside the repository and exclude private data.

## Browser fixtures

The existing scripts use Node.js with built-in fetch/WebSocket (Node 22+), and
Google Chrome at its standard macOS application path. They are local verification
helpers, not a claim of cross-platform browser automation. They create temporary
Chrome profiles and synthetic SQLite files, and save synthetic screenshots under
/tmp. Run the fixture and its browser check in separate terminals, then stop the
fixture with Control-C. Do not run checks sharing a debug port simultaneously.

Examples:

```sh
.venv/bin/python tests/databases_demo.py
node tests/databases_browser.cjs
```

```sh
.venv/bin/python tests/retirement_demo.py --state funded --port 5190
node tests/monte_carlo_browser.cjs
```

Other fixture/check pairs cover affordability, scenarios, funding, FX, ledger and
backup safety, and decimal upgrades. Read each script's first lines for its port
and fixture. Browser fixtures may use Flask's development server; normal user
launch uses Waitress. Release verification must also exercise Waitress streaming
and the actual shell launcher.

## Presentation and changed behavior

Each page should explain the question, result and effect of changing an input.
Distinguish recorded facts, model assumptions and optional rules. Show dates,
coverage and missing values. Keep technical explanations in disclosures and state
money basis (nominal or purchasing power) explicitly. Larger values are not
inherently better; show gaps against the entered objective.

Preserve labels, keyboard operation, visible focus, linked errors, first-error
focus and no-JavaScript recovery. Charts need matching tables. Check narrow screens
and reduced motion when presentation changes. Render tests alone do not establish
usability. Prefer the existing reusable components when they fit the task.

When changing a current rule, explain its before/after behavior and migration needs,
update CURRENT-BEHAVIOR.md and test the new outcome. The current tests document
behavior; they are not a reason to reject an explicitly accepted product change.
Numerical/data-integrity protections must remain meaningful through that change.

## Local release procedure

Use a clean checkout without private files. Review all files to be committed,
license notices and generic fixture data; run relevant verification and record
actual limitations in RELEASE-NOTES.md. Commit and tag an accepted release without
rewriting an existing tag. Public upload and any external network behavior require
explicit authorization. Never package .git history, .venv, instance/, backups or
exports into a source-only distribution by copying the entire working folder.
