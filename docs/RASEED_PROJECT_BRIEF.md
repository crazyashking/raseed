# Raseed: Project Brief v1.3

Receipt in, structured spend data out. Telegram bot first, dashboard second, app later.

Updated 2026-08-08. Decisions marked **[DECIDED]**, reversals marked **[REVISED]**.
Four review passes plus one real receipt validated. Ready to hand to Claude Code.
Companion file: `RASEED_TEST_DATA_PROMPTS.md`.

---

## Verification key

- `[SOURCED]` = verified against a live source this session, with source and confidence
- `[MEMORY]` = not verified this session
- `[MY READ]` = my opinion or inference, no number attached

---

## 1. End goal

A personal spend ledger fed by receipt images and PDFs, returning line-item-level
structured data, categorized, queryable by any time window.

The differentiator is line-item granularity. You will know you spent ₹340 on tomatoes
across 6 orders in July, and not only that groceries totalled ₹4,200.

### Scope ladder

| Phase | What it does | Ship criteria |
|---|---|---|
| v0 | Telegram bot. Image, PDF or text in. Parse, confirm, write to SQLite. `/export`. Runs on your PC. | You use it 2 weeks without hand-editing the DB. |
| v1 | Hinglish lexicon, dedupe, reconciliation gate, `/month` `/week` `/undo` `/recent`, CSV export. | 30 receipts through the eval harness, accuracy above your bar. |
| v2 | Web dashboard, read-only. Robinhood-style tab nav. Deployed to Railway. | You stop using `/month` because the dashboard is better. |
| v3 | Currency switching with live FX, item canonicalization, price-over-time, WhatsApp adapter. | Open ended. |
| v4 | Native app, if the dashboard proves the habit stuck. | Not planned in detail. |

Note that v0 and v1 have **no dashboard**. All summaries come back through Telegram. The
dashboard is v2, and building it early is the most likely way to burn the project while the
data underneath is still wrong.

Everything runs local through v1. Hosting decision gets made at v2.

---

## 2. Dashboard behaviour **[DECIDED]**

Storage is an append-only ledger. No resets anywhere in the data layer.

**Default view on open:** current calendar month. If there is no spend yet this month,
it renders zeros with an empty state, and does not fall back to last month's numbers.

**Navigation:** back arrow steps through previous months indefinitely. All history stays
queryable. Weekly, quarterly, yearly and custom ranges are the same query with different
bounds.

**Top-level nav:** horizontal tab bar in the Robinhood pattern. Each tab is a top-level
spend domain.

```
[ All ]  [ Groceries ]  [ Food & Dining ]  [ Drinks ]  [ Entertainment ]  [ Uncategorized ]
```

Five domains plus All, matching the Robinhood nav density you pointed at. Tabs are driven by
the category table, so this grows from your data rather than from my guesses. See 3.8.

Tapping a tab filters everything below it to that domain. Tapping into a domain shows
its sub-categories, then individual items. Three levels: domain, category, item.

Implementation notes for later:
- Tabs are driven by the category table, so adding a domain does not require a code change.
- Tabs are horizontally scrollable, since the list will grow past screen width.
- The `All` tab is the default landing state.
- A domain with zero spend in the selected period is greyed out rather than hidden, so
  the tab order stays stable month to month.

---

## 3. Architecture decisions

### 3.1 Two-stage pipeline **[DECIDED]**

**Stage 1, extraction.** Vision model reads the image. Output is verbatim and literal.
Item name exactly as printed, amount as printed, zero interpretation.

**Stage 2, enrichment.** Categorization, normalization, canonicalization. Runs on the
Stage 1 JSON and never touches the image.

When the taxonomy changes in month 4, Stage 2 re-runs over stored extractions for free.

### 3.2 Raw extractions are immutable

Table `raw_extractions` stores image hash, model ID, prompt version, raw JSON response,
timestamp, token counts, cost. Never updated, never deleted. Every other table derives
from it.

### 3.3 The reconciliation gate

**What it is:** a pure function that runs after extraction and before anything is stored.

```
sum(line_items) + taxes + fees + delivery + tip - discounts  ==  stated_grand_total
```

`[SOURCED]` Blinkit's terms list nine chargeable line types that can appear on one order:
delivery, handling, convenience, platform, small cart, high demand surge, rain surge, print
and gift charges, confidence 95, blinkit.com/terms.

`[SOURCED]` Zepto publicly waived handling, surge and rain fees, confidence 70, single
secondary source from November 2025 and quick-commerce fee policy changes often. Do not
assume Zepto receipts are fee-free. Check one. Vision models miss those lines or double-count discounts fairly often. When that
happens the individual items look plausible, so nothing obviously flags as broken, and
the bad data lands in your ledger quietly.

The gate catches it because the arithmetic stops working. It costs one function call
and no API tokens.

**Failure policy [DECIDED: reject and ask for a better photo], with one addition.**

Straight rejection has a failure mode worth designing around. Some receipts will never
balance no matter how good the photo is:

- Partial refunds applied after the order
- Membership or wallet discounts applied at payment and absent from the itemized list
- Blinkit rounding the grand total to the nearest rupee
- Packaging or surge charges printed outside the fee block
- Tips added after the receipt was generated

On those, a hard reject produces a loop. You send a perfectly clear photo, the bot
rejects it, you send it again, and the receipt never enters the ledger. That happens on
exactly the receipts most worth capturing.

So the gate splits the failure into two classes:

**Class 1, extraction failed.** Model returned nulls, low confidence, unreadable regions,
missing grand total. This is a photo problem. Reject, ask for a clearer image, store
nothing. This is the default path.

**Class 2, extraction looks complete but the arithmetic is off by X.** All fields
populated, item list plausible, totals just do not add up. Bot shows the mismatch and the
amount, then offers:

- Retake photo (goes back to Class 1 handling)
- Log the gap as `unaccounted_adjustment` and store balanced

`[MY READ]` The `unaccounted_adjustment` field is the important part. It keeps the ledger
arithmetically honest, keeps the receipt in your data, and gives you a queryable signal.
If that field is populated on 30% of Blinkit receipts, your prompt has a specific bug and
you now know where to look.

Tolerance: see section 18.4. Default `RECONCILIATION_TOLERANCE_MINOR = 100` (₹1), not 1 paisa,
because Indian GST rounding makes a tight tolerance fail on correct receipts.

### 3.4 Human-in-the-loop confirm

Bot replies with a parsed summary plus an inline keyboard: Confirm / Edit / Discard.
Nothing commits silently in v0.

Later, auto-confirm becomes available for receipts that pass reconciliation and come from
a merchant format seen 20+ times.

### 3.5 Money handling **[DECIDED, simplified]**

**v0 through v2 is INR only.** All amounts stored as `amount_minor` INTEGER in paise.
No floats anywhere.

Two columns stay in the schema from day one even though they are unused:

- `currency` CHAR(3), hardcoded `'INR'` for now
- `fx_rate_to_base` DECIMAL, NULL for now

They cost nothing while empty and they mean multi-currency is a feature addition rather
than a migration.

**The v3 currency design**, so the schema does not fight it later:

- Every transaction stores its native currency and native amount, permanently
- A user setting picks a display currency
- Backend converts at read time using a rate table, populated daily from an FX API
- Historical transactions convert at the rate for their own date, so past months do not
  shift when today's rate moves
- The `fx_rates` table is just `(date, from_ccy, to_ccy, rate)`, populated by a daily job

That last point matters. If you convert at today's rate for everything, your March
totals change every time you open the app.

### 3.6 Idempotency

Dedupe on two levels:
1. SHA-256 of raw image bytes
2. Natural key `(merchant, order_id, grand_total, order_date)`

On a dupe hit, reply "already logged on <date>".

### 3.7 Item canonicalization

"Amul Taaza Toned Milk 500ml" and "AMUL TAAZA 500ML" are one product. Collapsing them is
what unlocks price-over-time tracking.

v0/v1: store `raw_name` plus `normalized_slug`, with quantity and unit pulled into their
own columns.

v2+: embedding similarity against a canonical item table, with a review queue for
low-confidence matches.

Do not solve this in v0. Just preserve `raw_name` so it stays solvable.

---

### 3.8 Category taxonomy **[CORRECTED: I overbuilt this]**

**What happened, plainly.** You named three domains in your first message: Groceries, Food &
Dining, Drinks. You later gestured at movies. In v0.1 I added Household and Personal Care
without being asked. When you said "every receipt from day one" I expanded that to twelve
top-level domains with roughly forty sub-categories, none of it validated against a single
real receipt.

That was wrong on two counts. It is unvalidated invention, and it contradicts the reference
you gave me: you described Robinhood's top nav as roughly four tabs (individual, predictions,
agentic, custodial). I have not independently verified Robinhood's current UI, but your own
reference point was four, not twelve. Twelve
horizontally-scrolling tabs is not that pattern.

**It is also the single cheapest thing in this plan to get wrong later.** Because of the
two-stage split in 3.1, Stage 2 re-runs over stored extractions for free. Changing the
taxonomy in month three costs one script execution. Designing it upfront from zero data is
backwards.

### The corrected approach: start minimal, let it grow from data

Starting taxonomy, which is only what you actually named:

```
Groceries
Food & Dining
Drinks
Entertainment
Uncategorized
```

No sub-categories at launch. Item-level detail is already preserved in `raw_name`, so you
lose nothing by not pre-classifying it.

**How new categories get created:** same feedback loop as the lexicon in 4.5. Anything Stage 2
cannot place lands in `Uncategorized`. After 30 to 50 receipts you look at what accumulated
there. If fifteen fuel transactions are sitting in the bucket, Transport becomes a real
category, and you re-run Stage 2 to backfill it. If two are sitting there, it stays
uncategorized and costs you nothing.

The same applies to sub-categories. Split Groceries into Vegetables, Dairy and the rest only
once you have enough grocery volume that the flat view stops being useful.

`[MY READ]` This is strictly better than my twelve-domain version, and not only because it is
honest about the missing data. A taxonomy grown from your actual spending will match how you
think about your money. One I invented would match how I imagine an Indian grocery shopper
spends, which is a guess.

### What does stay from the earlier version

These are structural rather than invented, and they hold regardless of how many categories
exist:

- Category IDs are stable slugs, display names mutable. Never key off the display name.
- `Uncategorized` always exists and is never a failure state.
- `channel` is stored separately: `quick_commerce`, `delivery`, `dine_in`, `retail`,
  `online`, `service`. Independent of category.
- `merchant` is its own table with country and currency.
- Three display levels remain available (domain, category, item) even while only two are
  populated.

### Reconciliation shapes still differ by receipt type

This part was not invented. `[SOURCED]` Blinkit's own terms of service list the charges that
can appear on an order: delivery charges, handling charges, convenience charges, platform
charges, small cart charges, high demand surge charges, rain surge charge, print charges and
gift charges, confidence 95, first-party source (blinkit.com/terms).

That is nine possible fee lines on one grocery receipt, which is the concrete justification
for the reconciliation gate in 3.3.

| Receipt type | Shape |
|---|---|
| Quick-commerce grocery | Many items, up to nine possible fee lines, coupons. The hard case. |
| Fuel | One line item, no discounts. Trivially reconciles. |
| Marketplace (Amazon, Flipkart) | Shipping plus per-line tax rather than a single tax line |
| Restaurant | Service charge, GST, optional tip |
| Utility bill | No line items at all. Reconciliation must skip, not fail. |

The last row still matters: the gate needs a `skip_reconciliation` path for zero-line-item
receipts, or every bill fails Class 1 and gets rejected forever.

---

### 3.9 Manual text entry **[DECIDED: include]**

`/add 400 chai` or plain text like "spent 400 on chai at the office".

This is a separate ingestion path, not a variant of the image path:

- Skips Stage 1 entirely. Goes to a small text-parse call, then straight to Stage 2.
- Has no reconciliation gate. There is nothing to reconcile against, so `reconciled` is
  NULL rather than true.
- Still goes through the confirm keyboard, since the parse can misread the amount.
- Costs roughly nothing. A text parse is maybe 200 input tokens.

Add a `source` enum on every transaction: `receipt_image`, `receipt_pdf`, `manual_text`.
That column tells you later which parts of your ledger are backed by a document and which
are backed by memory, and those deserve different trust when you are reconciling against a
bank statement.

`[MY READ]` This is the feature that decides whether the habit sticks. Photographing a
receipt is a deliberate act. Typing "180 auto" takes four seconds and captures the cash
spending that would otherwise never enter the ledger at all.

---

## 4. Model selection **[DECIDED: Gemini 3 Flash primary]**

### 4.1 Clearing up the earlier table

The v0.1 pricing table was a comparison across providers, and Sonnet 5 was one row in it.
Nothing in this project needs Sonnet-tier capability. Receipt extraction is a
well-constrained, schema-bounded task, and the cheap tiers handle it.

### 4.2 Cloud options

`[SOURCED]` all rates checked 2026-08-08, confidence 95:

| Model | Input $/MTok | Output $/MTok | Est. per receipt | Notes |
|---|---|---|---|---|
| **Gemini 3 Flash** | $0.50 | $3 | ~$0.003 | Your pick. Good OCR, cheap, wide availability. |
| Gemini 3.5 Flash-Lite | $0.30 | $2.50 | ~$0.002 | Cheaper, worth benchmarking against Flash |
| GPT-5.6 Luna | $0.20 | $1.20 | ~$0.001 | Cheapest listed. Vision support unconfirmed, verify before relying on it. |
| Claude Haiku 4.5 | $1 | $5 | ~$0.005 | Fallback / second opinion on disputed receipts |

Per-receipt figures: confidence 85, since token count varies with your screenshot
dimensions and receipt length.

`[SOURCED]` GPT-5.6 Luna dropped to $0.20/$1.20 on July 30, 2026, confidence 90. Whether
Luna accepts image input is not confirmed in what I found, so treat it as a candidate
to test rather than a plan.

**Do not use the Gemini free tier for real receipts.** `[SOURCED]` free-tier Gemini prompts
may be used to improve Google's products, paid tier is excluded from training,
confidence 90. Your receipts have your address and phone on them. Free tier is fine for
synthetic test fixtures.

### 4.3 Open-weight local option

You asked whether an open model can compete. Short answer: for OCR and document
extraction, yes.

`[SOURCED]` Qwen3-VL scores approximately 896 on OCRBench, leading open-weight models on
OCR-specific benchmarks, ahead of InternVL3 at approximately 820, confidence 80. The
number comes from aggregator sites rather than the primary leaderboard, so treat it as
directional.

`[SOURCED]` Qwen2.5-VL 7B and MiniCPM-V 2.6 (8B) both fit on a single 12 GB GPU at 4-bit
quantization and are described as strong at OCR and invoice extraction, confidence 85.

**Your hardware: 12 to 16 GB VRAM [DECIDED].** That places you comfortably in 7B to 8B
territory, with headroom to run at 8-bit rather than 4-bit, which is worth taking since
quantization hurts OCR more than it hurts chat.

| Model | Size | Fits your box | Notes |
|---|---|---|---|
| **Qwen3-VL 7B** | 7B | Yes, 8-bit comfortably | Start here. `[MY READ]` Ollama availability unverified, check `ollama.com/library` before assuming a one-command install. vLLM works regardless. |
| MiniCPM-V 2.6 | 8B | Yes, 4-bit to 8-bit | Alternative to benchmark against |
| Qwen3-VL 32B | 32B | No, needs ~24 GB at 4-bit | Out of reach |
| PaddleOCR-VL | <1.3B | Trivially | OCR only, no reasoning. Would need a text LLM after it. |

### 4.4 Hinglish, and why the 32B concern was wrong

**Correction to v0.2.** I flagged the 32B model as important for "Hindi item names,"
assuming Devanagari script. Indian quick-commerce receipts are almost entirely romanized
Hindi (Hinglish, sometimes called Latin Hindi or Roman Hindi). "Bhindi 500g." "Toor Dal
1kg." "Dahi 400g."

That changes the problem meaningfully, because it splits cleanly across the two stages:

**Stage 1, extraction: easy.** These are Latin characters. Any competent 7B VLM reads
"Bhindi 500g" as reliably as it reads "Okra 500g". Script complexity was never the issue.
The 32B model buys you nothing here.

**Stage 2, categorization: this is where the real work is.** Mapping `Bhindi` to
Vegetables, `Dahi` to Dairy, `Atta` to Staples, `Besan` to Staples requires the model to
know the vocabulary. That is a text-only task, and it is cheap on any decent API model.

`[MY READ]` Net effect: 7B local for Stage 1 is very likely fine, and worth benchmarking
seriously. The Hinglish burden moves entirely to Stage 2, where it costs almost nothing.

### 4.5 The Hinglish lexicon

Most of Stage 2 should not touch a model at all.

Indian grocery vocabulary is a bounded list rather than an open one, which is what makes a
lookup table viable at all. `[MY READ]` I do not have a defensible number for how many terms
cover what share of your orders, and the figure I gave in v0.2 (300 to 400) was invented.
Delete it from your thinking. The correct answer comes from the `lexicon_misses` table after
a month of real receipts.

The lexicon lives in `enrichment/lexicon/hinglish.yaml` and gets checked before any API call.
Seed it with whatever you know, let the miss log tell you the rest.

```yaml
bhindi:    { canonical: okra,          category: vegetables }
baingan:   { canonical: eggplant,      category: vegetables }
lauki:     { canonical: bottle_gourd,  category: vegetables }
methi:     { canonical: fenugreek,     category: vegetables }
dhaniya:   { canonical: coriander,     category: vegetables }
dahi:      { canonical: yogurt,        category: dairy_eggs }
paneer:    { canonical: paneer,        category: dairy_eggs }
atta:      { canonical: wheat_flour,   category: staples }
maida:     { canonical: refined_flour, category: staples }
besan:     { canonical: gram_flour,    category: staples }
poha:      { canonical: flattened_rice,category: staples }
sooji:     { canonical: semolina,      category: staples }
toor_dal:  { canonical: pigeon_pea,    category: staples }
moong_dal: { canonical: mung_bean,     category: staples }
```

Two design notes:

**Fuzzy match, never exact.** Spelling is inconsistent across merchants and across orders:
bhindi/bhendi, dahi/dhai, paneer/panner, jeera/zeera, dhaniya/dhania. Use `rapidfuzz` with
a similarity threshold rather than dictionary lookup.

**Log every miss.** Unmatched terms go to a `lexicon_misses` table. Once a week you look at
that table, add the real ones to the YAML, and the lexicon grows from your actual spending
instead of from guesswork. The LLM fallback handles misses in the meantime.

`[MY READ]` This is also the most portfolio-legible piece of the project. A curated,
tested, fuzzy-matched Hinglish grocery lexicon with a miss-logging feedback loop is a
concrete artifact that reads as engineering judgment rather than API plumbing.

Licensing note: `[SOURCED]` Qwen3-VL ships under the Tongyi Qianwen license with
commercial-use restrictions, confidence 80. Apache-2.0 alternatives exist (Pixtral) at
lower quality. Matters only if this ever becomes a product.

**What I need from you:** GPU and VRAM on the PC. That single number decides whether
local is viable at all, and which tier you can run.

`[MY READ]` The plan I would follow: build against Gemini 3 Flash first because it removes
a variable while you are still getting the pipeline right. Once the golden test set exists,
swap in Qwen3-VL locally and score both on the same 30 receipts. Then you have a real
answer instead of a guess, and switching is a config change because the provider sits
behind an interface.

### 4.6 On cost

At 60 receipts a month, Gemini 3 Flash costs roughly $0.19. Confidence 85.

The point of saying the LLM cost is a rounding error: the API bill is small enough that it
should not influence any architectural decision. Do not optimize prompts for token count,
do not batch to save money, do not pick a worse model to save 2 cents. Pick on accuracy.
Hosting, at $5 to $8 a month once you deploy, will be 20x the API bill.

---

## 5. Hosting **[DECIDED: local first]**

v0 and v1 run on your PC with long-polling. Zero cost, zero deploy complexity, no webhook
or TLS setup. SQLite file on disk.

When you get to deployment, here is the comparison. `[SOURCED]` checked 2026-08-08 from
aggregator comparisons rather than first-party pricing pages, confidence 85:

| | Railway | Fly.io |
|---|---|---|
| Entry cost | $5/mo Hobby, includes $5 usage credits | No plan fee, smallest machine ~$2/mo |
| Billing | Per-second metering on top of plan fee | Per-second per machine, plus volumes, IPs, egress separately |
| Predictability | Good. Small projects land at the $5 floor. | Poor. Multiple separate meters. |
| Deploy flow | Git push, near-zero config | Dockerfile, `fly.toml`, more control |
| Learning curve | Low | Moderate |

**Railway.** For a single always-on Python process with a small DB, the $5 covers it,
the deploy is a git push, and you will not spend an evening reading billing docs. Fly is
better if you later want multi-region or scale-to-zero, and neither applies here.

`[MY READ]` One caveat worth knowing: Railway has no hard spending cap on the Hobby plan
beyond the credit, so set a usage alert when you sign up.

Database: SQLite through v1. Write Postgres-compatible SQL so the move is trivial.
Supabase free tier pauses projects after 7 days of inactivity `[SOURCED]` confidence 90,
which is a bad fit for a project you might not touch for a week.

---

## 6. Messaging layer **[DECIDED: Telegram]**

Telegram Bot API is free, BotFather setup takes two minutes, inline keyboards are native,
and there is no business verification.

`[SOURCED]` Telegram `getFile` caps bot downloads at 20 MB, confidence 95. Fine for
screenshots and PDFs. Only a constraint if you ever want multi-page scans.

WhatsApp stays on the table for later. `[SOURCED]` service messages inside the 24-hour
customer service window are free, confidence 90, so the cost is not the blocker. The
blocker is Meta business verification and a BSP relationship for a personal tool.

The bot layer sits behind a `MessagingAdapter` interface. The core pipeline receives an
image and a user ID, and has no knowledge of which messenger sent it. Adding WhatsApp
later means writing a second adapter class. That costs about 30 minutes now.

---

## 7. Privacy and PII redaction

Receipts contain delivery address, phone number, name, sometimes partial card digits.

### 7.1 Baseline, non-negotiable

1. `.gitignore` gets `data/`, `*.db`, `*.jpg`, `*.png`, `*.pdf` before the first commit
2. Real receipts never enter the repo. Fixtures are synthetic or heavily redacted.
3. Paid API tier only for real receipts, per section 4.2
4. Every table carries a `user_id` column from commit one, even while there is exactly one
   user. Costs nothing now, and it is the migration you do not want to do later.

### 7.2 On the image-editing idea

You asked about running an image-editing model up front to erase PII from the picture
before it goes downstream. Reasonable instinct, wrong tool. Three problems:

**It repaints pixels.** Generative editing regenerates the region it is editing and often
some margin around it. Inpainting over an address block sitting three lines above the item
list can alter digits in the amounts. On a financial ledger that is silent corruption, and
you would have no way to detect it because the output still looks like a valid receipt.

**It is non-deterministic.** You cannot write a test that asserts it worked. Every other
component in this pipeline is testable.

**It costs more than the actual work.** `[SOURCED]` Gemini image output is billed at $30
per million tokens, and an image up to 1024x1024 consumes 1290 tokens, which works out to
roughly $0.039 per image, confidence 90. That is about 13x the cost of the entire
extraction step, spent on a stage that adds risk instead of removing it.

### 7.3 What to do instead

Three layers, cheapest first. The first two get you most of the way for free.

**Layer 1: never extract PII.** The extraction schema has no fields for address, phone,
name, or card digits. Schema-constrained output means the model returns only what the
schema allows, so PII never enters the database at all. Free, deterministic, and it is
already part of the design.

**Layer 2: delete the image after extraction [DECIDED].** Keep the SHA-256 hash for dedupe
and drop the file. No stored image means no stored image PII.

Layer 3 (deterministic box redaction over stored images) is therefore **not being built**.
It only existed to protect retained images, and there are none. Noted here so the decision
is not relitigated later: it remains available if retention policy ever changes.

### 7.4 Safeguards, since deletion is irreversible

Deleting the image means Stage 1 can never be re-run for that receipt. Five rules make that
safe:

1. **Delete after confirm, never before.** The file survives until you have seen the parsed
   summary and pressed Confirm. Order of operations: extract, reconcile, show summary, wait
   for confirm, write to DB, then unlink.
2. **Raw extraction JSON is immutable and kept forever.** Stage 2 re-runs against it
   indefinitely, so taxonomy changes, lexicon growth and categorization fixes all still work
   retroactively. Only the pixels are gone.
3. **Reconciliation must pass before deletion.** A rejected receipt keeps its image, so you
   can retry without re-uploading.
4. **Store `line_item_count` and `grand_total` explicitly.** Truncation is the failure mode
   to fear: a 40-item receipt where the model returns 12. Reconciliation catches it because
   the sum will not match, and storing the count gives you a second check plus a queryable
   signal across your whole history.
5. **Retention is a config value.** `IMAGE_RETENTION_DAYS`, default `0` (delete on confirm).
   A cleanup job on startup removes anything past the window.

`[MY READ]` Set retention to 7 for the first two weeks. You will change the extraction
prompt several times in that period, and having recent images on hand makes that iteration
much faster. Drop it to 0 once the prompt stabilizes. It costs nothing to build and the
default is already the private one.

### 7.5 When it goes multi-user

The load-bearing work is:

- Row-level isolation on every query, enforced at the query layer rather than the
  application layer
- A working delete path, so a user can remove all their data and it actually goes
- Rate limiting per user, since your API key pays for their receipts
- A per-user Hinglish lexicon layer, since regional vocabulary differs (Gujarati, Bengali
  and Tamil grocery terms will not be in a Hindi-seeded list)

None of that needs building now. All of it is easier if `user_id` exists from commit one.

---

## 8. Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12+ | Best library support for this |
| Bot framework | `python-telegram-bot` v21+ or `aiogram` v3 | PTB has better docs, aiogram is more idiomatic async |
| Schema | Pydantic v2 | The extraction schema is the contract |
| Vision | Gemini 3 Flash, behind a provider interface | Swap to local Qwen3-VL after benchmarking |
| PDF text | `pdfplumber` | Text-layer detection and extraction, section 3.10 |
| PDF render | `pypdfium2` | Fallback rasterization for image-only PDFs |
| Fuzzy match | `rapidfuzz` | Hinglish lexicon lookup, section 4.5 |
| Timezones | `zoneinfo` (stdlib) | Merchant-local date bucketing, section 16.2 |
| Retries | `tenacity` | API backoff, section 16.5 |
| DB | SQLite (v0/v1), Postgres later | Postgres-compatible SQL from the start |
| Migrations | Alembic | From commit one |
| Dashboard (v2) | FastAPI + React | Better portfolio value than Streamlit. Streamlit is faster if you only want the data. |
| Tests | pytest, fixture-based mock mode | Same pattern as your Job Application Bot |
| Lint | ruff, mypy, pre-commit | |

Deliberately absent: Pillow (no image editing), Playwright (no synthetic generation),
LangChain (structured output does not need it).

### Structured output

Use the provider's native schema-constrained output rather than asking for JSON in the
prompt. `[SOURCED]` constrained decoding guarantees schema compliance where prompt-based
JSON does not, confidence 85.

Schema design rules worth following:
- Reasoning fields before answer fields. Models generate left to right, so field order is
  effectively prompt order.
- Optional fields stay optional. Forcing a required field when the data is absent from the
  receipt invites hallucination.
- One schema per task. Extraction and categorization get separate schemas.

Note: local models via Ollama support structured output through a JSON schema parameter,
though enforcement is weaker than the hosted providers. Something to measure when you
benchmark.

---

## 9. Evaluation **[REVISED: electronic receipts only]**

### Correction from v0.3

I built the earlier eval plan around photographed paper receipts: creases, glare, angled
shots, a "deliberately bad photo" test case. That was wrong for your actual input.

Your inputs are **electronic receipts**: in-app screenshots and exported PDFs from Blinkit,
Zepto, Zomato, Amazon. Those are pixel-perfect, consistently laid out, and identical across
every order from the same merchant.

Three consequences:

1. **No image preprocessing.** Deskew, glare handling, contrast normalization, adaptive
   thresholding: none of it applies. That whole category of work is gone.
2. **Format variance within a merchant is near zero.** Once Blinkit parses correctly, it
   keeps parsing correctly until the app redesigns. Expect materially higher accuracy than
   a paper-receipt pipeline would get.
3. **The Class 1 rejection path gets rare.** It still belongs in the code, since a user can
   send a cropped or partial screenshot, but it stops being the common failure.

Photos of paper receipts still work as an input, and they are a legitimate later feature.
They are just not the case to design around.

### Test data

You are sourcing your own test receipts. Nothing here depends on me generating them.

One request on how they get stored: put them in `data/eval/` as files, not only through the
Telegram flow. Sending them through the bot once is a smoke test. An eval harness needs to
loop over the same 30 receipts on every prompt change, which requires them sitting in a
folder with their expected JSON beside them.

```
data/eval/
├── blinkit_001.png
├── blinkit_001.expected.json
├── zepto_004.pdf
├── zepto_004.expected.json
```

`data/` is gitignored, so this stays local. The README reports the accuracy number, never
the data.

### The one metric

Field-level accuracy across merchant, date, grand total, line item count, and per-item
name / quantity / amount. One number, tracked across prompt versions and across models.

---

## 10. Time and skills

`[MY READ]` estimates, no confidence scores since these are not facts. Revised upward from
v0.2, because scope grew: full receipt coverage, manual text entry, the input router,
`/export`, and the section 16 items.

| Phase | Estimate | New skills |
|---|---|---|
| v0 | 25 to 35 hours | Telegram bot API, structured output schemas, Pydantic v2, pdfplumber |
| v1 | 20 to 25 hours | Alembic, eval harness design, rapidfuzz, timezone-correct date handling |
| v2 | 20 to 30 hours | FastAPI, frontend framework, chart library |

v0 plus v1 is roughly six to eight weeks of evenings at a realistic pace. That is the honest
number, and it is larger than the v0.2 estimate because the plan got more complete rather
than because anything went wrong.

You already have Python, SQLite, LLM API work, Telegram approval flows from the LinkedIn
agent, and mock-mode testing from the Job Application Bot. The new pieces are structured
output enforcement, the input router, and the eval harness.

---

## 11. Repo structure

```
raseed/
├── README.md
├── CLAUDE.md               # invariants for Claude Code, see 18.1
├── LICENSE
├── pyproject.toml
├── .env.example
├── .gitignore              # data/, *.db, *.jpg, *.png, *.pdf on day one
├── alembic/
├── src/raseed/
│   ├── adapters/           # base.py, telegram.py, (whatsapp.py later)
│   ├── ingestion/
│   │   └── router.py       # PDF text layer vs vision vs plain text, section 3.10
│   ├── extraction/
│   │   ├── schemas.py      # Pydantic contract
│   │   ├── prompts/        # versioned: v1.md, v2.md
│   │   └── providers/      # base.py, gemini.py, ollama.py, anthropic.py
│   ├── enrichment/
│   │   ├── lexicon/
│   │   │   └── hinglish.yaml
│   │   ├── rules.py        # deterministic match, fuzzy lookup, unit parsing
│   │   └── categorize.py   # LLM fallback for lexicon misses
│   ├── validation/
│   │   └── reconcile.py    # the gate, including skip_reconciliation
│   ├── db/
│   │   ├── models.py
│   │   ├── queries.py      # all period logic lives here
│   │   └── backup.py       # /export and the nightly dump, section 16.1
│   └── config.py
├── tests/
│   ├── fixtures/           # hand-written JSON, no receipt images
│   └── eval/               # scoring harness (data itself lives in data/eval/, gitignored)
└── docs/
    └── DECISIONS.md        # one entry per architectural call
```

---

## 12. Naming

**Raseed** (रसीद, receipt in Hindi/Urdu). Short, pronounceable in both markets, and the
meaning is the product.

Alternatives: Parchi (पर्ची, slip). Ledgerloop (safer, less memorable). Kharcha has
adjacent collision risk since `khorcha-pati` already exists on GitHub as a Telegram
expense bot.

Domain, npm, and GitHub availability unchecked. Verify before committing.

---

## 13. Commit order

Superseded by section 17.

---

## 14. Open questions

**Answered:**
- Currency: INR only, columns reserved for later
- Reset behaviour: append-only ledger, current month as default view, Robinhood-style tabs
- Model: Gemini 3 Flash primary, Qwen3-VL 7B benchmarked against it after the golden set exists
- Hardware: 12 to 16 GB VRAM, so 7B/8B local is viable and 32B is not
- Hosting: local first, Railway when deployed
- Messenger: Telegram
- Reconciliation: reject by default, with the Class 1 / Class 2 split in section 3.3
- Multi-user: later, so `user_id` goes in every table from commit one
- PII: schema exclusion plus image deletion on confirm, no stored images
- Scope: every receipt from day one, with measurement narrowed to top merchants
- Manual text entry: included, as a separate ingestion path
- Eval: electronic receipts only, sourced by Ashrit, stored in `data/eval/` with expected JSON
- Input router: PDF text layer bypasses vision entirely

**Still open:**
Nothing structural. Merchant priority resolves itself from the receipts once they are
flowing, and the schema is merchant-agnostic by design.

---

## 15. What is blocking the start

Nothing. Commits 1 through 5 in section 17 need no API key, no receipts and no model
decision. That is pure logic with no external dependencies, and getting the contract and the
validators right before anything touches an API is what keeps this from turning into the
thing you said you did not want.

Your receipt collection runs in parallel and blocks nothing until commit 6.

---

## 16. Gaps found on full review

Everything above came out of the back-and-forth. These are things neither of us raised, found
by reading the whole plan cold. Ordered by how much they hurt if missed.

### 16.1 Backup and durability **[most important gap]**

Your ledger will be a single SQLite file on one machine. Images are deleted after confirm, so
there is no way to rebuild it. A disk failure, a bad migration, or an accidental `rm` and the
entire spend history is gone permanently.

Minimum viable protection:

- A `/export` command that dumps the full ledger to JSON and sends it back through Telegram.
  Telegram becomes an offsite backup for free, and you already trust it with the receipts.
- A nightly job writing `data/backups/raseed_YYYY-MM-DD.json`, keeping the last 30.
- Run Alembic migrations against a copy first, never the live file.

`[MY READ]` The `/export` command is 20 lines and it is the single highest value-per-line
thing in this entire plan. Build it in the first week, not later.

### 16.2 Date bucketing and timezone

You are in Arizona (MST, no DST). Your India orders are timestamped IST. That is a 12.5 hour
gap, which means a Blinkit order at 11pm IST on March 31 is 10:30am MST on March 31, but an
order at 2am IST on April 1 is 1:30pm MST on March 31.

Without a rule, receipts land in the wrong month and your totals silently drift.

**The rule: bucket by the date printed on the receipt, in the merchant's local timezone.**
That is the date you would see in the Blinkit app, so it is the date you will expect. Store
three things:

- `occurred_at_utc` for ordering and dedupe
- `occurred_on_local` DATE, the merchant-local calendar date, which is what period queries
  use
- `merchant_tz`, on the merchant record

All monthly, weekly and quarterly bucketing runs off `occurred_on_local`. Never off UTC and
never off your own timezone.

### 16.3 Undo and edit

You will confirm a receipt and then notice the category is wrong, or that you confirmed a
duplicate. There is currently no path back.

Needs:
- `/undo` reverses the most recent transaction
- `/recent` lists the last 10 with IDs
- Tapping a transaction offers Recategorize or Delete
- Deletes are soft (`deleted_at`), never hard, so the append-only property survives

### 16.4 Concurrent confirms

If you send three receipts in quick succession there are three pending confirmations at once.
A single global "pending receipt" variable produces the classic bug where confirming the
third one commits the first.

Key pending state by `(chat_id, message_id)` and carry that ID in the inline keyboard's
callback data. Set a TTL, and expire pending confirms after 24 hours.

### 16.5 API failure handling

Gemini will occasionally rate limit, time out, or return a 5xx. The receipt must not be lost.

- Retry with exponential backoff, three attempts
- The image file survives on disk until extraction succeeds and the user confirms, per the
  deletion rules in 7.4
- On final failure, tell the user plainly and keep the image queued for retry
- A `pending_extractions` table, so a bot restart does not drop in-flight work

### 16.6 Cost cap

A retry loop or a bad handler could burn API credits unattended. Set `DAILY_COST_LIMIT_USD`
in config, track spend against the token counts you are already storing in
`raw_extractions`, and hard stop with a Telegram message when it trips. At your expected
volume the limit should sit around $1/day, which is roughly 300 receipts and about 100x
normal usage.

### 16.7 Quantity and unit parsing

"Bhindi 500g", "Toor Dal 1kg", "Eggs 6 pcs", "Milk 1 L". The schema needs these split out or
price-per-unit comparison never works:

```
raw_name        "Amul Taaza Toned Milk 500ml"
normalized_slug "amul-taaza-toned-milk"
quantity        500
unit            "ml"
unit_normalized "ml"     # g/kg → g, ml/L → ml, pcs stays pcs
pack_count      1        # for "2 x 500ml" style entries
```

Parse deterministically with a regex table over the raw name. This belongs in Stage 2, and it
is a prerequisite for the item canonicalization in 3.7.

### 16.8 Refunds and negative amounts

Quick-commerce partial refunds are common: an item is out of stock and the amount comes back
after the order. The schema needs to handle it.

Model refunds as their own transaction with a negative `amount_minor` and a
`related_transaction_id` pointing at the original. Do not edit the original transaction,
since that breaks the append-only ledger and loses the fact that a refund occurred. Category
totals then net out correctly with no special-case logic.

### 16.9 Drift detection

Blinkit redesigns its receipt layout, accuracy silently drops, and you find out three months
later when your grocery totals look wrong.

You already store `prompt_version` and `model_version` on every extraction, which is the hard
part. Add:

- A weekly job that re-runs the eval set and logs the accuracy number
- An alert if reconciliation failure rate for any single merchant exceeds a threshold over a
  rolling 30 receipts

The second one is the real canary, since it fires on live data without needing a labelled set.

### 16.10 Prompt injection via receipt content

An item name on a receipt could contain instruction-shaped text. Risk is low because output
is schema-constrained and there are no tools attached to the extraction call, so the worst
case is a garbage field rather than an action.

Two cheap mitigations: keep the extraction call tool-free, and treat everything inside the
image as data in the system prompt. One line each, worth having.

---

## 17. Revised commit order

1. Package skeleton, `pyproject.toml`, `.gitignore`, ruff config
2. `schemas.py`, the full Pydantic contract. Quantity/unit parsing belongs to Stage 2
   per 16.7 and lands in commit 9; a refund is its own ledger row per 16.8 and lands in
   commit 4. Corrected 2026-08-09: this line previously claimed commit 2 covered both,
   which contradicted 16.7 and 16.8. The code always followed 16.7 and 16.8.
3. `reconcile.py` and tests, including the `skip_reconciliation` path
4. `db/models.py` and the first Alembic migration, with `user_id`, `occurred_on_local`,
   `merchant_tz`, `source`, `deleted_at` present from the start
5. `router.py`, the PDF text-layer versus vision decision, testable without an API key
6. Extraction provider interface plus the Gemini implementation
7. Telegram handler, confirm flow with per-message state
8. `/export`, before anything else user-facing
9. Enrichment: the Hinglish lexicon, fuzzy matching, miss logging
10. Summary commands and CSV export

Commits 1 through 5 need no API key, no receipts and no model decision. That is the work
available immediately.

---

## 18. Second review pass

Second full read of the conversation and the document. Eight more items, plus three stale
sections already corrected in place (scope ladder, stack table, repo structure).

### 18.1 CLAUDE.md in the repo **[most important of this pass]**

You are taking this to Claude Code. Over a long session it will suggest things that quietly
violate the invariants in this document: storing money as a float because it is convenient,
updating a transaction in place instead of appending, adding a `customer_address` field
because the receipt has one, merging extraction and categorization into one call to save a
request.

A `CLAUDE.md` at the repo root prevents that. It is short and it is all constraints:

```markdown
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
10. The bot never instructs the user to change how they send a receipt. One
    exception, added 2026-08-11: when it receives a file type it cannot read at
    all, it may name the types it can. Nothing is in flight to re-send in that
    case, and the alternative is silence.
11. is_receipt is checked before any line item is parsed. A false means store nothing.
12. No package is installed that is not on the allowlist in the brief. Ask first, always.
13. line_total_minor is always the amount PAID. discounts[] holds order-level discounts
    only. A product discount already inside the line price is never repeated there.
```

`[MY READ]` Ten lines, written once, and it is the difference between coming back in three
weeks to a codebase that still matches this plan and one that has drifted. Write it in
commit 1.

### 18.2 Non-INR receipts in v0

v0 is INR-only, but nothing stops an Instacart or DoorDash receipt from arriving. The rule:

- Extract the currency from the receipt as printed
- Store the transaction with its true `currency` and native `amount_minor`
- **Exclude non-INR transactions from all totals and dashboard views in v0**, with a note in
  the confirm message saying it was logged but not counted

Storing and excluding beats rejecting, because the data is captured for when v3 adds FX and
you do not lose those months.

### 18.3 Unknown merchants

The model will return merchant names that are not in your merchant table, and it will spell
the same merchant differently across receipts ("Blinkit", "BLINKIT", "Blink Commerce
Private Limited" on the GST invoice).

Rule: fuzzy match the extracted name against the merchant table first. On a miss above the
threshold, auto-create the merchant with `verified = false` and route it into a
`/merchants` review command where you can merge duplicates. Never block a receipt on an
unknown merchant.

### 18.4 Reconciliation tolerance is too tight

Section 3.3 specifies 1 paisa. That is wrong for Indian receipts.

Indian GST is split into CGST and SGST, each rounded independently, and most merchants round
the grand total to the nearest rupee. A tolerance of 1 paisa will fail on receipts that are
perfectly correct.

Set `RECONCILIATION_TOLERANCE_MINOR = 100` (₹1) as the default, make it configurable, and
store the actual delta on the transaction so you can tighten it later once you have data on
what the real drift looks like.

### 18.5 Categorization confidence

Stage 2 needs to record how it decided:

- `category_source`: `lexicon_exact`, `lexicon_fuzzy`, `llm`, `manual`
- `category_confidence`: float for fuzzy and LLM paths, null for exact
- Anything below threshold goes to `uncategorized` rather than getting a guess

That column tells you where categorization is weak, and it means a future lexicon
improvement can re-run only the low-confidence rows instead of everything.

### 18.6 Onboarding commands

Small but the bot is unusable without them: `/start`, `/help`, `/settings`. `/help` should
list every command and state plainly that any image, PDF or plain text message works with no
special format.

### 18.7 LICENSE

The repo goes public. Pick a license in commit 1 rather than after the first star. MIT if you
want maximum reuse, AGPL if you care that a hosted clone contributes back.

### 18.8 README plan

For the portfolio angle, the README needs to lead with the measured accuracy number and the
architecture diagram, not with a feature list. The things worth showing: the two-stage
pipeline, the reconciliation gate, the input router, and the Hinglish lexicon with its
miss-logging loop. Those four are what read as engineering judgment.

---

## 19. Third review pass

### 19.1 AI-generated test receipts have a trap in them

You are generating 30 sample receipts by giving one real receipt to another AI and asking for
variations. That works for the receipts themselves.

The trap is the **expected JSON**. If you also let a model produce the expected output, you
are no longer measuring extraction accuracy. You are measuring agreement between two models,
and they can agree on the same wrong answer.

Two ways through:

- **Generate from structure, not from an image.** Have the AI output the structured data
  first (items, quantities, prices, fees, total), then render a receipt from that data. The
  expected JSON is then the input rather than a second guess, and it is correct by
  construction.
- **Or hand-verify.** If the AI produces the image first, you check the expected JSON
  yourself. Slower, and still fine for 30.

The first option is better and it is close to free.

Second, smaller point: AI-generated receipts will not reproduce Blinkit's exact fee
structure, its layout quirks, or how it renders a rain surge line. They validate that your
pipeline works. They do not validate that it works on Blinkit. Keep at least a handful of
genuinely real receipts in the eval set once you have them.

### 19.2 Corrections made in this pass

| Item | What changed |
|---|---|
| Category taxonomy | Twelve invented domains cut to the five you actually named, plus a growth loop. Section 3.8. |
| Hinglish lexicon size | The "300 to 400 terms" figure was invented. Removed. |
| Blinkit fee claim | Was asserted from memory, now sourced from Blinkit's own terms. Confidence 95. |
| Dashboard tabs | Now matches the Robinhood nav density you referenced |

---

## 20. Confidence audit

Per-claim, so you can see exactly which parts of this document are load-bearing and which are
guesses wearing a confident tone.

### High confidence, sourced this session

| Claim | Score | Basis |
|---|---|---|
| Blinkit fee line types | 95 | blinkit.com/terms, first-party |
| Claude / Gemini / OpenAI token rates | 95 | Provider pricing pages, checked 2026-08-08 |
| Anthropic image token formula and 1568px downscale | 95 | Anthropic vision docs |
| Telegram `getFile` 20 MB download cap | 95 | core.telegram.org, first-party |
| Gemini bounding box format, 0-1000 normalized | 95 | Google docs |
| Sonnet 5 intro pricing ends Aug 31, 2026 | 95 | Anthropic pricing docs |
| Gemini free tier used for product improvement | 90 | Multiple provider-adjacent sources |
| Supabase free tier pauses after 7 days | 90 | Multiple sources |
| Gemini image output ~$0.039 per 1024px image | 90 | Google pricing page |

### Medium confidence

| Claim | Score | Why not higher |
|---|---|---|
| Per-receipt cost of ~$0.003 on Gemini 3 Flash | 85 | Arithmetic on sourced rates, but token count varies with your receipt size |
| Railway / Fly / Render pricing | 85 | Aggregator comparisons, not first-party pricing pages. Verify before paying. |
| Telegram compresses photos to 1280px at ~87% JPEG | 85 | Secondary source. Verify with one real upload. |
| Qwen3-VL 7B fits 12 GB at 4-bit | 85 | Secondary sources, and my 8-bit extrapolation is my own |
| Constrained decoding beats prompt-based JSON | 85 | Well established, but no single authoritative benchmark cited |
| Qwen3-VL ~896 OCRBench | 80 | Aggregator sites, not the primary leaderboard |
| Qwen3-VL Tongyi Qianwen license restrictions | 80 | Secondary source. Read the actual license before commercial use. |

### Low confidence, and you should treat these as assumptions rather than facts

| Claim | Score | Why |
|---|---|---|
| Indian GST rounding makes 1 paisa tolerance fail | 70 | The CGST/SGST split is factual. The rounding-to-rupee behaviour is my inference from how Indian receipts generally print. Verify on your first ten receipts and set the tolerance from data. |
| Quick-commerce receipts carry a usable order ID | 70 | Very likely, unverified. Dedupe design depends on it, so check one receipt before building the natural key. |
| Indian quick-commerce apps export text-layer PDFs | 65 | This determines whether the cheap accurate path in 3.10 gets used at all. Open one PDF from each merchant and check. Highest-value verification on this list. |
| Qwen3-VL 7B matches Gemini 3 Flash on Hinglish receipts | 55 | Completely untested. The whole local-model option rests on it. |
| Time estimates (v0 at 25-35 hours) | 50 | Software estimates are unreliable and mine are not special. Treat as an order of magnitude. |

### Overall document confidence: **82**

Breaking that down honestly:

- **Architecture and data model: 92.** The append-only ledger, two-stage split, immutable raw
  extractions, integer money, reconciliation gate, input router and timezone handling are all
  standard, well-understood patterns. Nothing here is novel or risky.
- **Cost and feasibility: 88.** Rates are sourced. The project is clearly buildable by one
  person at your skill level. Nothing in it requires capability that does not exist.
- **Alignment with your stated end goal: 90.** Line-item granularity, quick-commerce
  receipts, a monthly-default dashboard with full history, Telegram first and a broader app
  later. All of it traces back to something you said.
- **Empirical claims about Indian receipt formats: 65.** Everything specific to how Blinkit,
  Zepto and Zomato actually print a receipt is inference, with the exception of the fee list.
  This is where the document is weakest and it is entirely fixable by opening five receipts.
- **Time and effort: 50.** Guesses.

The 82 is dragged down almost entirely by the fourth and fifth rows, and the fourth resolves
itself the moment you have receipts in hand.

### What I got wrong across this conversation, for the record

1. Designed around photographed paper receipts when your input is electronic
2. Invented a twelve-domain taxonomy from three you named
3. Fabricated the "300 to 400 Hinglish terms" figure
4. Set the reconciliation tolerance at 1 paisa, which would reject correct receipts
5. Recommended a UX that told users to change how they send files
6. Flagged the 32B model as necessary for Hindi when the script is Latin
7. Asserted Blinkit's fee structure from memory before verifying it

Five of the seven were caught by you, not by me. That is worth knowing about how to use this
document: it is a good starting structure and it is not an authority. Check anything in it
that touches the real world.

---

## 21. Fourth review pass: hallucination audit

Read specifically for things I asserted that may not exist or may not be buildable.

### 21.1 Corrections made in this pass

| Claim | Problem | Fix |
|---|---|---|
| "Blinkit and Zepto receipts carry handling fees, rain fees, small-cart fees" | Blinkit is now sourced at 95. Zepto publicly waived handling, surge and rain fees per a Nov 2025 report. I had lumped them together from memory. | Split. Blinkit sourced, Zepto flagged at 70 with a note to check one receipt. |
| "Robinhood's top nav carries four or five tabs" | I stated this as fact about Robinhood. I was actually repeating your description back to you. | Attributed to you, marked unverified. |
| "Qwen3-VL 7B, one-command Ollama install" | I do not know whether Qwen3-VL is in the Ollama library. Qwen VL models have had inconsistent Ollama support historically. | Marked unverified, check ollama.com/library. vLLM works regardless. |
| Model ID strings | I never wrote exact API model strings, which is correct, but the doc could be read as implying I know them. | See 21.3. |

### 21.2 New gap: the bot must be able to say "this is not a receipt"

You will eventually send it a screenshot that is not a receipt. A chat, a meme, a photo taken
by accident. A vision model given a receipt schema and a non-receipt image will not refuse. It
will fill the schema with invented values, and because they are invented they will often
balance, so the reconciliation gate will pass them.

That is the worst failure mode available: silently fabricated spending in your ledger, with no
image kept to audit against.

Fix, and it belongs in the schema rather than the prompt:

```
is_receipt: bool
receipt_confidence: float
rejection_reason: str | None
```

Reasoning fields come before answer fields (section 8), so `is_receipt` is evaluated before
any line item is generated. If it comes back false, the bot replies plainly and stores
nothing. Add two or three non-receipt images to the eval set and assert they are all rejected.

`[MY READ]` This is the most important thing found in this pass. Everything else here is
tidying.

### 21.3 Preflight checklist **[instructions for Claude Code]**

Run this before writing any code. Everything below moves faster than a document can track, and
all of it was last checked on 2026-08-08.

**Claude Code does these five. Do not skip any. Do not assume the answer from training data.**

1. **Current Gemini model ID and price.** Web-search the live Gemini API pricing page. Get the
   exact model string (not the marketing name) and the current input/output rates. The brief
   names "Gemini 3 Flash" at $0.50/$3, but Gemini 3.6 Flash launched 2026-07-21 at $1.50/$7.50
   and 3.5 Flash-Lite sits at $0.30/$2.50. Confirm which is the right price-performance point
   today and write the chosen model string into `.env.example`.
2. **Anthropic image token formula, if the Claude fallback provider is being built.** The brief
   uses `width x height / 750`. Newer Anthropic docs also describe a patch-based formula
   (28x28 pixel visual tokens). Check which applies to the current model tier. The cost
   conclusion survives either way, so this only matters for the cost-tracking code.
3. **Current `python-telegram-bot` major version.** The brief says v21+, correct as of a May
   2026 knowledge cutoff. Check PyPI for the current major and read its migration notes before
   pinning.
4. **Whether Qwen3-VL is available in the Ollama library.** Check `ollama.com/library`. If it
   is not there, the local path uses vLLM instead, which changes the setup instructions but
   nothing architectural. Do not claim a one-command install without confirming it.
5. **Pydantic v2 structured-output support in the chosen provider's current SDK.** Confirm the
   provider still accepts a JSON schema for constrained decoding and that the parameter name
   has not changed.

Record all five answers in `docs/DECISIONS.md` with the date checked, so the next session does
not repeat the work.

**Separately, Ashrit does these five.** They need real receipts and no amount of searching
substitutes:

1. Open one PDF invoice from Blinkit, Zepto and Zomato. Does text select, or is it an image?
   This decides whether the cheap accurate path in 3.10 gets used at all.
2. Does a Blinkit receipt carry an order ID? The dedupe natural key depends on it.
3. Is the printed grand total rounded to the rupee or to the paisa? This sets
   `RECONCILIATION_TOLERANCE_MINOR`.
4. Do Zepto receipts still show zero fees, or have the waived charges returned?
5. Does the receipt print a timezone or only a local time? Affects section 16.2.

### 21.4 Verified as real and implementable

Checked deliberately, since a plan full of libraries that do not exist is worse than no plan:

- `pdfplumber`, `pypdfium2`, `rapidfuzz`, `tenacity`, `jinja2`, `playwright`, `alembic`,
  `pydantic` v2, `ruff`, `mypy`, `pytest`: all real, all actively maintained, all doing what
  the doc says they do.
- `zoneinfo` is stdlib from Python 3.9.
- Telegram's `PhotoSize` array is ordered smallest to largest, so `message.photo[-1]` is the
  largest available. Correct.
- Ollama supports JSON-schema-constrained output via its format parameter. Correct.
- CGST/SGST splitting on intra-state Indian transactions, IGST on inter-state. Correct.
- Every Hinglish lexicon mapping in section 4.5 is accurate: bhindi/okra, baingan/eggplant,
  lauki/bottle gourd, methi/fenugreek, dhaniya/coriander, dahi/yogurt, atta/wheat flour,
  maida/refined flour, besan/gram flour, poha/flattened rice, sooji/semolina, toor dal/pigeon
  pea, moong dal/mung bean.
- Nothing in the architecture requires a capability that does not exist. There is no step in
  this plan that a competent Python developer could not build.

### 21.5 The remaining honest weakness

Everything specific to how a Blinkit, Zepto or Zomato receipt actually looks is inference,
with the fee list as the one exception. That does not make the plan wrong. It makes the plan
untested, and it stays untested until you open five receipts.

---

## 22. Final state

**Open questions: none.**

**Companion file:** `RASEED_TEST_DATA_PROMPTS.md`, the Gemini prompts for generating test
receipts with correct-by-construction expected JSON.

**Verify before or during commit 1** (ten minutes, and it lifts the document's weakest
section from 65 to something real):
1. Open one PDF invoice from Blinkit, Zepto and Zomato. Does text select, or is it an image?
2. Does a Blinkit receipt carry an order ID?
3. Is the printed grand total rounded to the rupee, or to the paisa?
4. Do Zepto receipts still show zero fees?
5. Current Gemini model ID string and price.

**Decided and recorded:** currency, reset behaviour, dashboard nav, taxonomy, model, hardware,
hosting, messenger, reconciliation policy and tolerance, multi-user posture, PII handling,
scope, manual entry, eval approach, input routing, image UX, backup, timezone, undo,
concurrency, retries, cost cap, units, refunds, drift detection, non-receipt rejection.

**Deliberately deferred, reason recorded:** WhatsApp adapter (setup friction), tiling (build
only if measured), item canonicalization (v2+), FX conversion (v3), native app (v4), box
redaction (no stored images), sub-categories (no data yet).

**Order of operations for the first session:**
1. Run the Claude Code preflight in section 21.3, record answers in `docs/DECISIONS.md`
2. Read section 23 before installing anything
3. Commit 1: package skeleton, `CLAUDE.md`, `LICENSE`, `.gitignore`, `.env.example`

No API key, no receipts and no model decision are required to reach the end of commit 1.

**Document confidence: 82.** Per-claim breakdown in section 20. Unchanged by this pass, since
the corrections were to individual claims rather than to the structure.


---

## 23. Supply chain security **[MANDATORY, read before any install]**

**This section is an instruction, not a suggestion. Nothing gets installed on Ashrit's machine
without passing these checks first.**

The realistic threat here is not a sophisticated attack. It is typosquatting: a package named
`pdfplumbr` or `rapid-fuzz` or `python-telegram-bot-api` that installs cleanly, does what you
expect, and also does something else. `pip install` runs arbitrary code at install time, so a
wrong character in a package name is code execution on the machine.

### 23.1 The allowlist

These are the only packages this project needs. Canonical PyPI names, verified spelling:

| Import name | PyPI name | Purpose |
|---|---|---|
| `telegram` | `python-telegram-bot` | Bot framework |
| `pydantic` | `pydantic` | Schema contract |
| `pdfplumber` | `pdfplumber` | PDF text layer |
| `pypdfium2` | `pypdfium2` | PDF rasterization fallback |
| `rapidfuzz` | `rapidfuzz` | Lexicon fuzzy matching |
| `tenacity` | `tenacity` | Retry with backoff |
| `alembic` | `alembic` | Migrations |
| `sqlalchemy` | `SQLAlchemy` | ORM |
| `yaml` | `PyYAML` | Lexicon file |
| `dotenv` | `python-dotenv` | Config |
| `google.genai` | `google-genai` | Gemini SDK |
| `jinja2` | `Jinja2` | Receipt template rendering |
| `playwright` | `playwright` | Headless render for test data |
| `pytest` | `pytest` | Tests |
| `ruff` | `ruff` | Lint |
| `mypy` | `mypy` | Types |
| `pip_audit` | `pip-audit` | Vulnerability scan |

Note the traps in that list. `python-telegram-bot` imports as `telegram`. `PyYAML` imports as
`yaml`. `python-dotenv` imports as `dotenv`. Getting these backwards is exactly how people end
up installing a squatted package.

`zoneinfo`, `sqlite3`, `hashlib`, `json` and `pathlib` are stdlib. Never install them.

### 23.2 Rules for Claude Code

1. **Install nothing outside the table in 23.1 without asking Ashrit first.** State the package
   name, what it does, and why an allowlisted package cannot do the job.
2. **Never run a `pip install` command copied from a blog, README, Stack Overflow answer or
   model output without checking the name against PyPI first.**
3. **Everything installs inside a project virtualenv.** Never `sudo pip`, never `--user`, never
   system Python.
4. **Pin exact versions in `pyproject.toml`.** No unbounded `>=`. A lockfile (`uv.lock` or
   `requirements.txt` with hashes) gets committed.
5. **Run `pip-audit` after every dependency change** and before every commit that touches
   `pyproject.toml`. It goes in pre-commit and in CI.
6. **Nothing is installed silently as a side effect of another task.** If a dependency is
   needed mid-session, stop and say so.

### 23.3 Vetting procedure for anything new

Before installing a package not on the allowlist:

```bash
# 1. Inspect without installing. Resolves and reports, downloads nothing.
pip install --dry-run <package>

# 2. Pull the artifact without executing anything, then look inside it.
pip download --no-deps --no-binary :all: <package> -d /tmp/vet
```

Then check, by hand:

- **The PyPI project page.** Does the Homepage link go to a real repository? Is there a
  maintained GitHub with issues, releases and more than one contributor?
- **Release history.** A package with one release from last week that claims to be a mature
  library is a red flag. So is a long-dormant project that suddenly published a release from a
  new maintainer.
- **Spelling, character by character, against the official documentation.** Not against
  memory, and not against what a model wrote.
- **Whether it is doing something at install time.** Look for `setup.py` running network
  calls, subprocess, or base64 blobs. A pure-Python library should not need any of that.
- **`pip-audit` on the resolved set** before it goes anywhere near the project environment.

### 23.4 Non-package hygiene

- The Gemini API key lives in `.env`, which is gitignored from commit 1. It is never printed to
  logs, never committed, never pasted into a chat window.
- `playwright install chromium` downloads a browser binary. That is expected and it comes from
  Microsoft's official distribution. Run it once, deliberately, not as part of some other task.
- No `curl | bash` for anything, ever.
- Set a spend limit in the Google Cloud console alongside the `DAILY_COST_LIMIT_USD` in section
  16.6. Application-level caps fail open if the application is what broke.
- Receipt images and the database never leave the machine except through the `/export` command
  Ashrit runs deliberately.

`[MY READ]` The single highest-value item on this page is rule 2 in 23.2. Every package this
project needs is already listed and verified. If a `pip install` command appears for something
that is not on that list, that is the moment to stop and check rather than the moment to run it.


---

## 24. First real receipt: findings **[2026-08-08]**

One real Blinkit-format screenshot, run through the step 1 prompt. The gate caught a genuine
modelling error on the first try, and the receipt exposed four schema gaps that no amount of
planning would have found.

### 24.1 The receipt

| Item | MRP | Paid |
|---|---|---|
| Act II Sour Cream & Cheese Popcorn 50g | 37 | 35 |
| Green Cucumber 500g | 26 | 22 |
| Mr. Makhana Pudina Party Flavoured Makhana 21g | 50 | 50 |
| Banana 3 pcs | 22 | 19 |
| Moi Soi White Rice Paper 22cm 100g | 190 | 99 |
| Orange Carrot 500g | 24 | 20 |
| **Sum** | **349** | **245** |

Bill details as printed: MRP ₹349, Product discount -₹104, Item total ₹245, Handling charge
+₹11, Delivery charges FREE, Bill total ₹256.

**The receipt is internally consistent.** 349 - 104 = 245, and 245 + 11 = 256.

### 24.2 The error, and why it matters

Gemini stored `line_total_minor` as the **paid** price (24500 total) and separately recorded
the ₹104 product discount in `discounts`. The discount was then subtracted twice:

```
24500 + 1100 + 0 - 10400 = 15200   ✗  reported total was 25600
```

The extraction was not wrong about any individual number. Every value it read was correct.
It was wrong about **what the numbers mean relative to each other**, which is exactly the class
of error a human reviewer skims past and the gate catches for free.

`[MY READ]` This is the strongest possible validation of section 3.3. The gate earned its place
on receipt number one, before a single line of the project has been written.

### 24.3 Schema correction: line-level versus order-level discounts

Two modellings both reconcile correctly:

```
A, MRP basis:   34900 + 1100 - 10400 = 25600  ✓
B, paid basis:  24500 + 1100 -     0 = 25600  ✓
```

**Adopt B, and capture the MRP anyway.** Per line item:

```
mrp_minor         3700     # struck-through price, nullable
line_total_minor  3500     # what was actually paid, always present
```

Then the rule, which goes in `CLAUDE.md`:

> `line_total_minor` is always the amount actually paid. The `discounts` array holds
> **order-level** discounts only: coupons, promo codes, wallet credits, cashback applied at
> checkout. A product discount already reflected in the line price is never repeated in
> `discounts`.

Reconciliation becomes:

```
sum(line_total_minor) + sum(charges) + sum(taxes) - sum(order_level_discounts) == grand_total
```

Keeping `mrp_minor` is worth it independently. It gives you "you saved ₹104 this order" and
"₹2,340 saved this month" for free, which is a nicer dashboard number than anything else in
the plan.

Cross-check to add: `sum(mrp_minor) - sum(line_total_minor)` should equal the printed product
discount. If it does not, a line price was misread. That is a second independent validator at
zero cost.

### 24.4 Gap: no order ID, no date, no merchant name on this screen

The natural screenshot a user takes is the item list plus bill details. It contains **none of**
an order ID, an order date, a timestamp, or the merchant's name.

That breaks three things in the brief as written.

**Dedupe (3.6).** The natural key `(merchant, order_id, grand_total, order_date)` is
unavailable. Revised:
- Primary: SHA-256 of the image bytes, which still works
- Secondary: `(merchant, grand_total_minor, received_date)` as a *soft* match. On a hit, ask
  "looks like a duplicate of the ₹256 order from yesterday, log anyway?" rather than silently
  rejecting. Two genuinely identical orders on the same day are possible.

**Date bucketing (16.2).** No printed date means `occurred_on_local` has to be inferred.
Revised: default to the Telegram message date in the user's timezone, and store
`date_source` as `receipt_printed` or `message_timestamp`. Anything on the second value is
editable from the confirm keyboard. Never silently guess without recording that you guessed.

Built 2026-08-12, with a third value. Confirm asks "when was this?" whenever nothing could
be read off the receipt, offering today, yesterday, and a month grid for backdating. The
answer stores `date_source` as `user_supplied`, because a date a person chose is neither
printed nor guessed and the dashboard flags a guess. See the decisions log.

**Merchant.** Not printed anywhere on this screen. Inferring it from UI styling is
unreliable and I would not build on it. Revised: `merchant` is nullable. On extraction, if
merchant is null, the confirm keyboard offers a quick-pick of previously seen merchants plus
"other". One tap, no typing, and it populates the merchant table from real use.

`[MY READ]` This is the finding I would not have reached without a real receipt, and it is
the reason the preflight list in 21.3 exists.

### 24.5 Smaller findings

**"FREE" is a charge value.** Delivery charges printed as `FREE`, not `0`. Charge amounts can
be non-numeric: FREE, Waived, `--`, `N/A`. Map all of them to 0. Gemini handled this
correctly, which is worth noting.

**"MRP" and "Item total" are subtotal lines, not charges.** An extractor that treats every
row in the bill block as a charge will add ₹349 and ₹245 to the total. The prompt must name
the subtotal labels explicitly as lines to skip.

**No tax lines at all.** Confirms `taxes: []` is a normal state for quick-commerce, not a
parse failure.

**Hinglish sits inside brand names, not as standalone items.** "Mr. Makhana Pudina Party
Flavoured Makhana" carries two Hinglish tokens (makhana, pudina) inside an English product
name. Section 4.5 assumed whole-name matching. Revised: the lexicon matches on **tokens
within** a name, not the full string. Tokenize, then fuzzy-match each token, then take the
strongest category signal.

**Strikethrough confusion is a named failure mode.** Every discounted line prints the MRP
immediately before the paid price. A vision model reading ₹37 instead of ₹35 is an easy and
plausible error. The `mrp_minor` field plus the 24.3 cross-check catches it. Add three
strikethrough-heavy receipts to the eval set specifically.

### 24.6 Template review

Gemini's HTML template was structurally good and had four problems, three of which would have
quietly corrupted the test set rather than failing loudly.

| Problem | Effect if unfixed |
|---|---|
| Derived MRP as `item_total + sum(discounts)` | Under the corrected schema `discounts` is empty for a product-discount-only receipt, so MRP would render equal to Item total and the discount rows would vanish from every synthetic receipt |
| Never rendered `mrp_minor`, so no strikethrough | Zero of the 30 test images would exercise the strikethrough case, which is the single most likely OCR error on this receipt format |
| Printed merchant, order ID and date in a header | The real screen has none of these. Synthetic receipts would be easier than reality and the accuracy number would be inflated |
| Formatted all money as `%.2f` | Real receipt prints ₹35, not ₹35.00. Every synthetic image would look subtly wrong |

Corrected template is at `templates/blinkit.html`. Changes:

- MRP total derived from `line_items` via `mrp_minor`, falling back to `line_total_minor` when
  null. Never from `discounts`.
- Strikethrough MRP renders beside the paid price when the two differ.
- Metadata header defaults off, behind a `show_metadata` flag for when you want that variant.
- `rs()` macro prints whole rupees when there are no paise.
- Order-level discounts render **below** charges, since they apply after item total. Product
  discounts stay above it as a derived summary row.
- MRP and Product discount rows only appear when a product discount actually exists.

Verified by rendering your real receipt through it:

```
MRP                  ₹349
Product discount     -₹104
Item total           ₹245
Handling charge      +₹11
Delivery charges     FREE
Bill total           ₹256
5 strikethrough spans, reconciles True, mrp cross-check True
```

That reproduces the source receipt exactly. A coupon variant was also checked and renders the
coupon below the charges block with the total adjusting correctly.

### 24.7 Updated prompt

`RASEED_TEST_DATA_PROMPTS.md` step 1 has been corrected to state the line-level versus
order-level discount rule up front, list the subtotal labels to skip, and require `mrp_minor`.
Re-run step 1 with the updated prompt before generating variations, or your 30 test records
will encode the same double-count.
