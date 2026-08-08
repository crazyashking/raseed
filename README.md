# Raseed

रसीद, *receipt*. A personal spend ledger fed by photographs of receipts, over
Telegram.

Send the bot a picture of a bill. It reads the line items, reconciles the
arithmetic against the printed total, asks you to confirm, and writes an
append-only ledger row. Then it deletes the image.

Status: early. See `docs/PLANNER.md` for what is built and what is next.

## How it works

Two stages, deliberately separate:

1. **Extraction** reads the image and returns exactly what is printed, into a
   Pydantic contract. It does not interpret, categorize, or tidy anything.
2. **Enrichment** categorizes and normalizes from that text alone. It never sees
   the image.

Between them sits the **reconciliation gate**: line totals plus charges plus
taxes minus order-level discounts must equal the printed grand total, within one
rupee. A receipt that does not balance is not stored silently.

Money is integer paise throughout. No floats, anywhere.

## Not stored, by design

No address, no phone number, no name, no card digits. Those fields do not exist
in any schema, so there is nothing to leak. Receipt images are deleted once you
confirm the row.

## Layout

```
docs/    the brief, the planner, the decision log
src/     the package
tests/   fixtures and the eval harness
tools/   receipt rendering and lockfile generation
data/    receipts and eval images, gitignored, never committed
```

## Setup

Python 3.12 or newer.

```
py -3 -m venv .venv
.venv\Scripts\python -m pip install --require-hashes -r requirements.txt
copy .env.example .env
```

Then fill in `.env`. It is gitignored. You need a bot token from
[@BotFather](https://t.me/BotFather), your **numeric** Telegram user ID from
[@userinfobot](https://t.me/userinfobot), and a paid-tier Gemini API key.

`TELEGRAM_ALLOWED_USER_IDS` is a whitelist and an empty one admits nobody, so
the bot refuses to start until it is set.

Run the migrations, then the bot:

```
.venv\Scripts\python -m alembic upgrade head
.venv\Scripts\python run.py
```

The checks:

```
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m mypy src tests
.venv\Scripts\python -m pip_audit
```

Dependencies come from a fixed allowlist and are hash-pinned in
`requirements.txt`. Adding one is a deliberate decision, recorded in
`docs/DECISIONS.md`.

## License

MIT. See `LICENSE`.
