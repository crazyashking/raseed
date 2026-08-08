# Raseed invariants

Violating any of these is a bug, regardless of how convenient it is.

1. Money is INTEGER minor units (paise) plus an ISO currency code. Never float.
2. The ledger is append-only. Corrections are new rows. Deletes are soft (deleted_at).
3. No PII fields in any schema: no address, phone, name, or card digits. Ever.
4. Extraction and categorization are separate calls against separate schemas.
5. raw_extractions is immutable. Never UPDATE, never DELETE.
6. Period queries bucket on occurred_on_local, never on UTC, never on server time.
7. Receipt images are deleted after confirm. Nothing depends on them persisting.
8. Every table has user_id, even while there is one user.
9. No image editing, upscaling, or enhancement anywhere in the pipeline.
10. The bot never instructs the user to change how they send a receipt.
11. is_receipt is checked before any line item is parsed. A false means store nothing.
12. No package is installed that is not on the allowlist in the brief. Ask first, always.
13. line_total_minor is always the amount PAID. discounts[] holds order-level discounts
    only. A product discount already inside the line price is never repeated there.

---

## Working agreements

These are process rules, not invariants. The list above is copied verbatim from
section 18.1 of `docs/RASEED_PROJECT_BRIEF.md` and does not change without Ashrit
saying so explicitly.

- The brief is `docs/RASEED_PROJECT_BRIEF.md`. Read it before designing anything.
  If it looks wrong, say so and wait for an answer. Do not silently deviate.
- `docs/PLANNER.md` is the living status. Update it when a commit lands, a blocker
  appears or clears, or the shape of the work changes.
- `docs/DECISIONS.md` gets one dated entry per architectural call, per preflight
  answer, and every time reality contradicts the brief.
- Documentation may be kept accurate without asking. Architectural decisions may
  not be changed without asking.
- Dependencies come from the allowlist in section 23.1 and nowhere else. Everything
  installs into the project virtualenv at `.venv`, pinned exactly, hash-locked in
  `requirements.txt`.
- `data/` is gitignored. Tests must never read from it. Test fixtures live in
  `tests/fixtures/` and contain no PII.
- `data/eval/blinkit_000.*` is a redacted real receipt. The image is never committed
  under any circumstance.

## Commands

```
.venv\Scripts\python -m pytest              # test suite
.venv\Scripts\python -m ruff check .        # lint
.venv\Scripts\python -m ruff format .       # format
.venv\Scripts\python -m mypy src tests      # types
.venv\Scripts\python -m pip_audit           # dependency vulnerabilities
```
