# Order service fixture — intentional idempotency defect for Ringharness E2E-0.

Local Python mini-service used as the **target repository** under development.
Ringharness itself is not modified by this fixture.

## Known defect

`create_order` ignores `idempotency_key` for duplicate detection. Sequential and
concurrent retries with the same key can create multiple orders and deduct
inventory more than once.

## Layout

- `tests/` — public tests visible to Executor (sequential idempotency).
- `tests_hidden/` — Auditor-only concurrent / anti-cheat tests; seed strips these
  from the executor worktree.
- `reference_fix/` — known-good `store.py` for fixture self-check only; not
  shipped into executor context as a hint beyond what Agent would discover.

## Fixed business input

See `FIXED_INPUT.json` and `scripts/seed_business_e2e.py`.
