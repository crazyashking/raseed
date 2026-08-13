# Raseed

रसीद, *receipt*. A personal spend ledger fed by photographs of receipts, over
Telegram.

Send the bot a picture of a bill. It reads the line items, reconciles the
arithmetic against the printed total, asks you to confirm, and writes an
append-only ledger row. Then it deletes the image.

Send several pictures at once and it works out whether they are pages of one
long receipt or several separate ones, in a single reading. If nothing on the
receipt says when it was, it asks before saving, so a bill photographed a week
late lands in the week it was paid.

Status: early. `docs/DECISIONS.md` records every architectural call and why it
was made, including the ones that turned out wrong.

## How it works

Two stages, deliberately separate:

1. **Extraction** reads the image and returns exactly what is printed, into a
   Pydantic contract. It does not interpret, categorize, or tidy anything.
2. **Enrichment** categorizes and normalizes from that text alone. It never sees
   the image.

Between them sits the **reconciliation gate**: line totals plus charges plus
taxes minus order-level discounts must equal the printed grand total, within one
rupee. A receipt that does not balance is not stored silently.

Money is integer minor units throughout, paise for rupees and cents for dollars.
No floats, anywhere.

The currency is read off the receipt rather than assumed, and nothing is ever
converted. A dollar bill is stored in dollars and shown in dollars, and the
dashboard gives each currency its own section. Converting would need a rate, a
rate needs a date to be read on, and two defensible dates give two different
numbers.

## Not stored, by design

No address, no phone number, no name, no card digits. Those fields do not exist
in any schema, so there is nothing to leak. Receipt images are deleted once you
confirm the row.

## Layout

```
docs/            the brief, the decision log, proposals
src/             the package
alembic/         the migrations, which own the schema
tests/           fixtures and the suite
tools/           operational scripts: eval, enrichment backfill,
                 dashboard preview, row reassignment, lockfile generation
templates/       the HTML that renders the synthetic eval receipts
variations.json  the inputs those receipts are rendered from
data/            receipts and eval images, gitignored, never committed
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
.venv\Scripts\python -m ruff format .
.venv\Scripts\python -m mypy src tests
.venv\Scripts\python -m pip_audit
```

Dependencies come from a fixed allowlist and are hash-pinned in
`requirements.txt`. Adding one is a deliberate decision, recorded in
`docs/DECISIONS.md`.

## Deployment

`requirements.txt` pins hashes for Windows wheels, so pip rejects it on Linux.
`requirements-linux-aarch64.txt` is the runtime set for ARM Linux: the nine
packages the application imports plus their dependencies, without the test and
lint tooling.

```
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-linux-aarch64.txt
DATABASE_URL=... .venv/bin/python -m alembic upgrade head
```

`alembic/env.py` reads `DATABASE_URL` from the real environment and does not
read `.env`, so pass it explicitly when running migrations by hand. Without it
alembic silently falls back to a relative `raseed.db` and creates an empty one.

The dashboard listens on `127.0.0.1:8770` and that is not configurable. In front
of it, a reverse proxy terminates TLS and forwards to loopback; Telegram will
not open a Mini App over plain HTTP, so `DASHBOARD_PUBLIC_URL` must be an
`https://` address that resolves publicly.

## License

MIT. See `LICENSE`.
