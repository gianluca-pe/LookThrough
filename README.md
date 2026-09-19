# LookThrough

A local, single-user portfolio monitor and retirement planner. Enter portfolio
facts manually, inspect consolidated values and allocation, and explore retirement
under explicit assumptions. The application runs on your computer; it is not a
broker connection or a hosted service.

**Version: 1.0.0-beta.2.** A beta release for local, single-user portfolio monitoring
and retirement planning.
See [release notes](RELEASE-NOTES.md) for verification and limitations.

## What it does

- Records accounts, investments, trades, dividends, cash confirmations, dated prices
  and statement values; preserves reversals and correction evidence.
- Consolidates values across currencies, with visible missing/stale source data,
  account ownership shares and withdrawal-access restrictions.
- Compares allocation with entered targets and assesses Now/Bridge reserves against
  three and seven years of Core spending in the selected date's purchasing power.
- Solves deterministic retirement affordability: steady real Flexible spending
  alongside Core spending, savings, income and a final-age capital goal.
- Compares current and alternative allocations across 1,000 repeatable USD Monte
  Carlo paths, with streamed progress, printable reports and table equivalents.
- Offers named local databases, explicit backup-before-upgrade and JSON backup/restore.

Portfolio values and classifications are manual. Settings can explicitly download
Banca d'Italia public FX reference rates, review them and save them locally. No
portfolio details are sent with that request. Ordinary use works offline; manual FX
entry remains available.

## Install and run

Python 3.12+ is required. macOS with Python 3.14.7 is the verified environment for
this baseline; other platforms/Python versions have not been newly certified.
From this folder:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -c requirements-verified.txt
./run.sh
```

`requirements.txt` expresses supported dependency ranges. The constraints file
pins the versions used for this release, including test dependencies. Recreate the
virtual environment after moving the project; do not copy someone else's .venv.
The launcher runs Waitress through `.venv/bin/python -m waitress`, avoiding an
obsolete interpreter path in a generated `waitress-serve` script.

Open **http://127.0.0.1:5001/**. Waitress listens only on IPv4 loopback; localhost
also works. Other Host values are rejected. Normal output is a startup line;
errors and warnings remain visible. Press Control-C to stop.

On macOS, double-click **LookThrough.command** to start the server and open the
chooser in your default browser. Keep its Terminal window open. You can make a
Finder alias to this launcher. The shell launchers need executable permission if
an archive or web download removes it (`chmod +x run.sh LookThrough.command`).
Only one installation should listen on port 5001: the Finder launcher reuses an
already running server at that address. Stop another installation before testing
or switching to this one.

## Local data and recovery

The first launch creates an empty instance/ folder; no personal database is supplied.
Create a new database in the chooser. Each file contains one portfolio. Selection
is intentional even when there is only one file, and each tab retains its dataset.
Restarting the server expires old selections and forms.

The chooser discovers .sqlite3, .sqlite and .db files directly inside instance/.
Linked files and subfolders are excluded. Opening a compatible database does not
migrate it; older recognized files offer a review and explicit upgrade. The upgrade
keeps a recovery copy, verifies a disposable staged database and then publishes it.
A failure preserves the original. Newer, unrelated or corrupt files remain untouched.

Current schema: `e8f2a6b0c4d7`. JSON backup format: **11**; restore accepts **1–11**.
Settings restore validates a disposable database, previews the change and requires
confirmation before atomic replacement. Recovery copies and JSON exports serve
different purposes. Keep backups separate from the code; a Git tag is not a data backup.

For a transfer from an existing installation, stop its server and retain a verified
SQLite backup before copying a database into this folder. Choose one working copy
and one daily-use launcher. Do not share the same database between installations or
continue editing both copies. A later schema upgrade may prevent use by older code;
use the preserved recovery copy if returning to that version.

## Precision and planning limits

Money uses currency minor units; units, prices and entered FX use up to six decimal
places. Excess precision is rejected, calculated settlements round half up, and
intermediate financial calculations retain Decimal precision. Legacy upgrades keep
recovery copies and before/after conversion records.

Deterministic planning currently uses spending buckets. Monte Carlo uses proportional
withdrawals and annual rebalancing, but still starts from the classified retirement
sources. Bucket-free planning is a future idea, not an existing mode. Monte Carlo
uses illustrative USD risk assumptions, fixed inflation and unmodelled trading costs
and withdrawal taxes. These are conditional calculations, not recommendations or
performance measurements. See [current behavior](CURRENT-BEHAVIOR.md).

## Development and feedback

Read [AGENTS.md](AGENTS.md) and [DEVELOPMENT.md](DEVELOPMENT.md). Run:

```sh
.venv/bin/python -m pytest -q
```

Tests and demo fixtures use disposable data. Browser checks and reproducibility
instructions are in DEVELOPMENT.md. [ROADMAP.md](ROADMAP.md) contains unsequenced
possibilities. When reporting a problem, use synthetic examples and remove personal
values, account identifiers, database paths and screenshots of private records.

## Publication and licensing

LookThrough is licensed under the [BSD 3-Clause License](LICENSE), using the project
attribution "LookThrough contributors". Bundled libraries retain their separate
notices in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
