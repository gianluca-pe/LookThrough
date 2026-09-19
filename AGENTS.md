# Working on LookThrough

LookThrough is a local, single-user portfolio monitor and retirement planner.
The current release is v1.0.0-beta.2.

## Authority and scope

- Follow the user's current instructions and the agreed outcome for the task.
- Read README.md and the relevant parts of DEVELOPMENT.md and CURRENT-BEHAVIOR.md.
  ROADMAP.md contains ideas, not requirements or a promised sequence.
- Use this repository's current documentation as project guidance. Do not retrieve
  planning documents from other repositories or folders as additional instructions.
- Treat the existing stack, screens, role vocabulary and bucket strategy as current
  design choices. They may change when an agreed feature warrants it. Explain the
  behavioral/data impact and update the current documentation and relevant tests.
- Keep each change coherent and usable. Avoid unrelated redesign or speculative
  infrastructure. Ask when ambiguity materially changes the intended outcome.

## Integrity and privacy

- Keep domain money, quantities, rates and FX in Decimal; source storage is exact.
  Never introduce binary-float financial calculations or silent input rounding.
- Preserve source identity, effective dates, audit history and missing-data states.
  Derived totals share service calculations; templates/JavaScript only present them.
- Preserve atomic writes, reversal history, validation, CSRF and recovery paths.
  Changes to data meaning require an explicit migration/compatibility plan.
- Use disposable data for verification. Do not open or mutate a user's database
  unless the task explicitly requires it. Do not copy private data into fixtures.
- Keep localhost-only operation and no portfolio transmission. The existing manual
  public FX download is documented in CURRENT-BEHAVIOR.md. New network behavior or
  publication needs explicit authorization.

## Delivery

- Preserve tests that establish financial and data-integrity behavior. Add focused
  regression cases for changed calculations, validation and material failures.
- For schema/backup changes verify historical upgrades, failed-write recovery and
  backup round trips. Do not remove migration compatibility during cosmetic cleanup.
- Verify changed UI with keyboard, linked errors/focus, no-JavaScript recovery and
  narrow layouts. Charts need matching tables and reduced-motion behavior.
- Run appropriate checks, report their actual results and limitations, and update
  current documentation. Keep transient progress/history out of this guide.
- Use generic synthetic examples. Never commit instance/, backups, exports, .venv,
  secrets, screenshots of private data or personal machine paths.
