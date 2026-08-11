# Raseed: Decisions Log

One entry per architectural call, preflight answer, or place where reality
contradicted the brief. Newest section last within each heading. Every entry is
dated.

---

## Preflight (section 21.3, "Claude Code does these five")

All five checked live on **2026-08-08** via web search / first-party docs. None
answered from training data.

### 1. Gemini model ID and price

**Checked 2026-08-08**, source: `ai.google.dev/gemini-api/docs/pricing` (first
party). Paid-tier rates per 1M tokens:

| Model ID | Input | Output |
|---|---|---|
| `gemini-3.6-flash` | $1.50 | $7.50 |
| `gemini-3.5-flash` | $1.50 | $9.00 |
| `gemini-3.5-flash-lite` | $0.30 | $2.50 |
| `gemini-3.1-flash-lite` | $0.25 (text/image/video), $0.50 (audio) | $1.50 |
| `gemini-2.5-flash` | $0.30 (text/image/video), $1.00 (audio) | $2.50 |
| `gemini-2.5-flash-lite` | $0.10 (text/image/video), $0.30 (audio) | $0.40 |

**The brief is out of date here.** Section 4.2 names "Gemini 3 Flash" at
$0.50/$3. There is no GA `gemini-3-flash` on the pricing page at that rate; the
$0.50/$3 figure belongs to `gemini-3-flash-preview`. The current GA Flash is
`gemini-3.6-flash` at $1.50/$7.50, which is 3x the input and 2.5x the output the
brief budgeted.

**Status: DECIDED 2026-08-08.** Ashrit chose **`gemini-3.6-flash`**, written into
`.env.example` as `GEMINI_MODEL`. At the measured image token counts this is
about $0.0079 per receipt, roughly $0.47/month at 60 receipts, and $0.32 for a
full 32-receipt eval sweep. The 3x input premium over the brief's assumed rate is
real but immaterial at personal volume. Reversible with one `.env` line if eval
shows Flash-Lite is good enough. Section 4.2 of the brief is stale on this point
and should be corrected when the brief is next revised.

Also confirmed on the same page: free-tier content **is** used to improve
Google's products; paid tier is **not**. Section 4.2's "do not use the free tier
for real receipts" rule stands, re-verified rather than assumed.

### 2. Anthropic image token formula

**Checked 2026-08-08**, source: `platform.claude.com/docs/en/build-with-claude/vision`.

**The brief's `width x height / 750` is wrong for current models.** The current
formula is patch-based:

```
visual_tokens = ceil(width / 28) * ceil(height / 28)
```

Each patch is a 28x28 pixel block. Two resolution tiers, applied automatically
with no beta header:

| Tier | Models | Max long edge | Max visual tokens |
|---|---|---|---|
| High-resolution | Claude 4.7 and later | 2576 px | 4784 |
| Standard | All others | 1568 px | 1568 |

Images over either limit are downscaled preserving aspect ratio, which caps
token cost. The brief's "1568px downscale" claim (section 20, confidence 95) is
correct only for the standard tier.

Impact is limited to cost-tracking code, exactly as the brief predicted. Claude
is a fallback provider only (section 4.2), so this blocks nothing.

### 3. `python-telegram-bot` current version

**Checked 2026-08-08**, source: PyPI JSON API.

- Latest: **22.8**
- `requires_python`: `>=3.10`

The brief says "v21+", which is stale but not wrong in spirit. Pin `22.8` exactly
per section 23.2 rule 4. Migration notes to be read before the commit 7 Telegram
handler, not before commit 3.

### 4. Qwen3-VL in the Ollama library

**Checked 2026-08-08**, source: `ollama.com/library` and `ollama.com/blog/qwen3-vl`.

**Yes, it is there.** Tags: `qwen3-vl:2b`, `qwen3-vl:4b`, `qwen3-vl:8b`, and
`qwen3-vl:235b-cloud`. All support 256K context natively.

The brief's section 4.3 marked one-command Ollama install as unverified. It is
now verified for the local tags. The 8B tag is the one that matters: it is the
size section 4.3 targets for 12-16 GB VRAM, and it is a local tag rather than a
`-cloud` tag, so `ollama pull qwen3-vl:8b` runs on Ashrit's box.

Note the 235B variant is cloud-only. Ignore it.

### 5. Structured output in the chosen provider's SDK

**Checked 2026-08-08**, source: `googleapis/python-genai` docs + PyPI.

- Package: `google-genai`, latest **2.17.0**, `requires_python >=3.10`
- Pydantic v2 models are accepted directly as `response_schema` inside
  `GenerateContentConfig`, alongside `response_mime_type="application/json"`
- The SDK extracts the schema via `model_json_schema()` and auto-sets
  `property_ordering` to preserve Pydantic field declaration order
- `response.parsed` returns a validated model instance, but **only** when
  `response_schema` was a Pydantic model
- Both the Gemini Developer API and Vertex AI support it

**The parameter name has not changed.** It is still `response_schema`.

The auto-`property_ordering` behaviour is load-bearing for us: section 21.2
requires `is_receipt` and `receipt_confidence` to be generated before any line
item, and section 8 says field order is effectively prompt order. Declaration
order in the Pydantic model is therefore the mechanism that enforces 21.2. This
is now a tested property of the schema, not a hope.

---

## Environment

### 2026-08-08: Python 3.14.6 is the only runtime on this machine

`py --list` shows exactly one interpreter: 3.14.6. There is no 3.12 or 3.13.

The brief requires 3.12+, so 3.14 satisfies it. Flagging anyway because 3.14 is
newer than every version the allowlisted packages were pinned against in the
brief, and a missing wheel shows up as a build-from-source failure rather than a
clean error. Will verify at venv creation time in commit 1.

### 2026-08-08: OS is Windows 11, and it matters in three places

Asked and answered proactively:

1. **`tools/render_receipts.py` is unaffected.** It uses `pathlib` throughout and
   resolves paths from `__file__`, so it is portable. Verified by importing it
   and calling `validate()` alone.
2. **Path separators in tests.** Any fixture-loading code must use `pathlib`, not
   string concatenation with `/`. Cheap to get right, annoying to retrofit.
3. **`playwright install chromium`** downloads a Windows Chromium build. Not
   needed this session (no re-rendering), and per section 23.4 it gets run
   deliberately and on its own, never as a side effect.

Nothing else in the stack is OS-sensitive. SQLite, Pydantic, ruff and mypy behave
identically.

---

## Repository

### 2026-08-08: Layout established, move verified without re-rendering

Files moved into the section 11 structure. `git init` run (the repo was not
previously under version control).

Verified by importing `tools/render_receipts.py` and calling `validate()` alone,
per instruction. `main()` was not called and no images were rendered.

`jinja2` and `playwright` are not installed and were not installed to do this.
Both imports were stubbed in `sys.modules` before import, so the real `validate()`
ran against the real `variations.json` with zero new dependencies. This respects
the section 23 rule that nothing is installed as a side effect of another task.

Result: **32 records, all balance.** Path constants resolve correctly:

```
ROOT     C:\Users\ashri\Desktop\Raseed
TEMPLATE C:\Users\ashri\Desktop\Raseed\templates\blinkit.html   exists
RECORDS  C:\Users\ashri\Desktop\Raseed\variations.json          exists
OUT      C:\Users\ashri\Desktop\Raseed\data\eval                exists
```

Line-item counts per record confirm the dataset matches its description:
records 19, 20, 21 have 30 items each (the section 3.11 tiling stress case) and
record 32 has zero (the `skip_reconciliation` case).

### 2026-08-08: Most of data/eval/ does not exist

Searched the entire user profile, not just the repo. Present: `blinkit_022.png`,
`blinkit_026.png`, `blinkit_000.expected.json`. Absent: the other 30 PNGs, all 32
`*.expected.json`, and `docs/RASEED_TEST_DATA_PROMPTS.md`.

Blocks commits 2 and 3 as specified. See Blockers in `PLANNER.md`.

### 2026-08-08: Brief sections 3.10 and 3.11 are referenced but missing

`docs/RASEED_PROJECT_BRIEF.md` cites section 3.10 (PDF text layer vs vision input
router) four times: the stack table, the repo structure, the confidence audit,
and preflight item 1 of Ashrit's list. Section 3.11 (tiling) is cited in
conversation but appears in the brief only as a deferred item in section 22.

Neither section body exists. Section 3 jumps from 3.9 to section 4.

Not a blocker for commits 1-3. It is a blocker for commit 5 (`router.py`), which
has no spec to build against. Flagged rather than invented: per instruction, a
gap in the brief is a conversation, not a silent design decision.

---

### 2026-08-08: The expected JSON is provably derivable from variations.json

Claim under test: every missing `blinkit_NNN.expected.json` can be reconstructed
exactly, without rendering anything.

**Mechanism** (`tools/render_receipts.py:85-95`): `render()` enumerates the
records with `start=1`, sets `stem = f"{PREFIX}_{i:03d}"`, renders the image from
`template.render(**rec)`, and writes the sidecar as
`json.dumps(rec, indent=2, ensure_ascii=False)`. The image and the JSON come from
the same `rec` in the same loop iteration. The JSON is a serialization of the
render input, not a description of the render output. That is the property
section 19.1 was designed around: correct by construction.

Therefore `blinkit_NNN.expected.json == json.dumps(variations[NNN-1], indent=2,
ensure_ascii=False)`.

**Independent verification.** The two surviving PNGs were checked against the
records the mapping predicts. Both match on every field, including the 1-based
offset:

`blinkit_022.png` vs `variations.json[21]`

| Rendered | Record |
|---|---|
| Act II Sour Cream & Cheese Popcorn 50g, 50 g x 1, ~~₹37~~ ₹35 | `mrp_minor 3700`, `line_total_minor 3500` |
| Fresho Green Cucumber 500g, ~~₹26~~ ₹22 | `2600` / `2200` |
| Banana 3 pcs, ~~₹22~~ ₹19 | `2200` / `1900` |
| Moi Soi White Rice Paper (22cm) 100g, ~~₹190~~ ₹99 | `19000` / `9900` |
| Orange Carrot 500g, ~~₹24~~ ₹20 | `2400` / `2000` |
| MRP ₹299, Product discount -₹104, Item total ₹195 | derived, not stored |
| Handling +₹11, Delivery FREE | `charges` 1100 and 0 |
| Bill total ₹206 | `grand_total_minor 20600` |

`blinkit_026.png` vs `variations.json[25]`, the order-level coupon case: three
items, MRP ₹480, product discount -₹56, item total ₹424, handling +₹10, platform
+₹5, FLAT100 Promo -₹100, bill total ₹339 against `grand_total_minor 33900`. The
coupon renders below the charges block, exactly as section 24.6 specifies, and it
is the only entry in `discounts`, confirming the 24.3 rule holds in the data.

Two records, two different indices, zero mismatches. The mapping is confirmed.

**Conclusion: nothing is needed from Ashrit to restore the expected JSON.** The
images remain a separate question (see Blockers in `PLANNER.md`); they are not
required until commit 6.

### 2026-08-08: LICENSE is MIT

Chosen by Ashrit. Section 18.7 offered MIT or AGPL. MIT it is.

---

### 2026-08-08: Gemini model is `gemini-3.6-flash`

Chosen by Ashrit. The full costing that led here is preserved under "Resolved
decisions" below, including the caveat that the tile arithmetic still needs
confirming with `count_tokens` at commit 6 before any paid call.

### 2026-08-08: Dependencies installed, pinned, hash-locked, audited

Fifteen direct packages installed into `.venv`, every one of them a name on the
section 23.1 allowlist. Nothing outside it. Resolved on CPython 3.14.6 with zero
source builds, which was the risk worth checking: 3.14 is newer than anything the
brief's allowlist was written against, and a missing wheel would have shown up as
a compiler error rather than a clean failure. It did not.

Direct versions: `alembic==1.19.1`, `google-genai==2.17.0`, `pdfplumber==0.11.10`,
`pydantic==2.13.4`, `pypdfium2==5.12.1`, `python-dotenv==1.2.2`,
`python-telegram-bot==22.8`, `PyYAML==6.0.3`, `rapidfuzz==3.14.5`,
`SQLAlchemy==2.0.51`, `tenacity==9.1.4`, plus dev `mypy==2.3.0`,
`pip-audit==2.10.1`, `pytest==9.1.1`, `ruff==0.16.2`. 71 packages with transitives.

`pip-audit`: **no known vulnerabilities**, all 71.

Three consequences worth recording:

1. **Lockfile format is `requirements.txt` with `--hash` lines, not `uv.lock`.**
   `uv` is not installed on this machine and is not on the allowlist. Section 23.2
   rule 4 permits either. Generated by `tools/mklock.py` from a
   `pip install --dry-run --report` resolve, and verified to install cleanly under
   `--require-hashes`.
2. **The lockfile is Windows/CPython-3.14 specific.** Nine of the pinned packages
   ship per-platform wheels (`pydantic-core`, `pypdfium2`, `cryptography`,
   `greenlet`, `pillow`, `RapidFuzz`, `cffi`, `MarkupSafe`, `librt`). Deploying to
   Railway on Linux means regenerating there, or extending the file with the extra
   per-platform hashes. Written into the lockfile header so it cannot be a
   surprise later.
3. **No editable install.** `pip install -e .` would drag `setuptools` into the
   venv as a side effect, which invariant 12 does not permit quietly. Instead
   `pyproject.toml` sets pytest's `pythonpath = ["src"]` and mypy's
   `mypy_path = "src"`. Same import behaviour, one fewer package.

`Jinja2` and `playwright` are allowlisted but deliberately **not** installed. They
belong to `tools/render_receipts.py`, and section 23.4 makes downloading a browser
binary an explicit act rather than a side effect of setup. They are recorded as a
comment in `pyproject.toml` and get pinned when that install actually happens.

### 2026-08-08: `tools/render_receipts.py` is excluded from the formatter

`ruff check` passes on it. `ruff format` wants to join one line continuation.
Ashrit's instruction was to move the file without editing its contents, so the
formatter is excluded from that one path rather than the file being reformatted.

### 2026-08-08: What the extraction schema does and does not carry

Section 17 lists commit 2 as "the full Pydantic contract **including quantity/unit
and refund modelling**". Two other sections of the brief place both of those
elsewhere, and following section 17 literally would contradict them:

- **Quantity and unit parsing is Stage 2.** Section 16.7 is explicit: parse
  deterministically with a regex table over `raw_name`, and "this belongs in Stage
  2". Stage 1 is verbatim transcription. So `LineItem` carries `quantity_text`
  exactly as printed ("500 ml x 2") and nothing normalized. `quantity`, `unit`,
  `unit_normalized`, `pack_count` and `normalized_slug` belong in the enrichment
  schema at commit 9.
- **Refunds are not an extraction concept.** Section 16.8 models a refund as its
  own transaction with a negative `amount_minor` and a `related_transaction_id`.
  That is a ledger shape, so it lands in `db/models.py` at commit 4. A refund is
  never a field on the receipt being read.

Resolution: commit 2 builds the extraction contract only, which is also exactly
what Ashrit's STEP 6 asked for. Nothing is dropped, only placed where the brief
itself puts it. Flagged to Ashrit rather than resolved silently.

### 2026-08-08: Added `printed_product_discount_minor` to the extraction schema

Section 24.3 specifies a cross-check: `sum(mrp_minor) - sum(line_total_minor)`
should equal **the printed product discount**. There was no field to hold the
printed value, so the check had nothing to compare against and could not be
implemented as written.

Added as a nullable field with a description that explicitly separates it from
`discounts[]`, so invariant 13 is not weakened: it is a cross-check input, never a
reconciliation term. All 32 eval fixtures leave it null, because the renderer
computes that line rather than storing it, so the cross-check degrades cleanly to
the per-line and aggregate MRP invariants when it is absent.

This is an addition required to implement the brief, not a change to a decision in
it. Raised with Ashrit in the commit 3 report.

### 2026-08-08: `grand_total_minor` is the last field, and that is a real trade-off

Fields are declared in receipt reading order, top to bottom, so the model
transcribes items, then charges, then the total. The alternative is putting the
total first so it anchors before the items are read.

Reading order was chosen because it is what a literal transcriber does, and
because the field description says to COPY the printed total and never compute it.
But the risk is genuine: if the model derives the total from its own item sum, the
reconciliation gate becomes tautological and catches nothing. That is not
detectable from a schema test. It has to be measured at commit 6 by feeding
deliberately corrupted receipts and checking that the gate still fires. Noted here
so it is not forgotten.

### 2026-08-08: Test fixtures committed, and why these three

`blinkit_001`, `blinkit_026` and `blinkit_032` are copied into `tests/fixtures/`
so the suite runs on a fresh clone with `data/` absent. All three are synthetic
with no PII. They were picked to cover distinct shapes rather than at random:

| Fixture | Shape it covers |
|---|---|
| `blinkit_001` | Normal receipt, one line with a null `mrp_minor`, a zero-value "Delivery charges" line |
| `blinkit_026` | Order-level coupon in `discounts[]`, every line has an MRP |
| `blinkit_032` | Zero line items, the `skip_reconciliation` case from section 3.8 |

A test asserts those three properties of the fixture set itself, so a later edit
cannot quietly narrow the coverage.

### 2026-08-08: The gate returns four outcomes, not a boolean

`BALANCED`, `SKIPPED`, `CLASS_1`, `CLASS_2`. `accepted` is true for the first
two. A boolean would have collapsed "this photo is unreadable" together with
"these numbers are 5 rupees out", and section 3.3 exists specifically because
those need different handling: one asks for a retake, the other offers to log the
gap. The verdict object also carries every number used to reach it, so the bot can
show the user the actual mismatch rather than a generic failure.

`unaccounted_adjustment_minor` is a property that returns non-None only on
`CLASS_2`. That makes it structurally impossible to record an adjustment on a
receipt that balanced.

### 2026-08-08: Class 1 is checked before skip, and that ordering matters

A receipt with zero line items goes down the `skip_reconciliation` path (section
3.8). But an extraction with zero line items, zero charges, zero taxes, zero
discounts and a grand total is not a utility bill: it is a failed read that
happened to catch one number. Skipping it would store a total with nothing behind
it, silently.

So the Class 1 checks run first, and "a total was read but nothing else was" is
one of them. `SKIPPED` is only reached when *something* was read and only the
itemisation is absent. `blinkit_032` is the real case: zero line items, one
convenience charge, and it skips correctly.

### 2026-08-08: No default confidence floor

Section 3.3 names low confidence as a Class 1 symptom but never fixes a number.
`reconcile` therefore takes `min_confidence` as an optional keyword that defaults
to off, rather than baking a guessed threshold into the gate. The bot layer
supplies it at commit 7 once real receipts show where the line actually sits.
This is deliberate under-specification, not an oversight.

### 2026-08-08: `DEFAULT_TOLERANCE_MINOR` lives in `reconcile.py`

It was briefly defined in `config.py`. The constant belongs with the gate that
uses it, so `reconcile.py` owns it and `config.py` re-exports it as
`DEFAULT_RECONCILIATION_TOLERANCE_MINOR`. One definition, no drift.

### 2026-08-08: Eval sweep result, all 32 records

`tools/sweep_eval.py` run against `data/eval/`. It is a committed tool rather
than a test, because the suite must not depend on a gitignored directory.

| | |
|---|---|
| Records swept | 32 |
| `BALANCED` | 31, every one at delta exactly 0 |
| `SKIPPED` | 1 (`blinkit_032`, zero line items) |
| MRP cross-check `OK` | 31 |
| MRP cross-check `NOT_APPLICABLE` | 1 (`blinkit_032`, no line carries an MRP) |
| MRP `MISMATCH` | 0 |
| Total product savings implied across the set | 374,200 paise (₹3,742) |

Every delta is exactly zero, not merely inside the ±100 paise tolerance, which is
the stronger result: the tolerance is not doing any work here and is reserved for
the GST rounding it was added for.

`blinkit_000`, the redacted real receipt, was swept separately and excluded from
the count. It also reconciles at delta 0 and passes the MRP cross-check.

### 2026-08-08: Seven tables, and why `transaction_adjustments` exists

`users`, `merchants`, `categories`, `raw_extractions`, `transactions`,
`transaction_line_items`, `transaction_adjustments`.

The last one is the only addition beyond what the brief names directly. Charges,
taxes and order-level discounts have to be queryable, otherwise the ledger cannot
reproduce its own grand total without reparsing
`raw_extractions.response_json`, and both `/export` (section 16.1) and every
summary would depend on a text blob. One table with a `kind` enum rather than
three near-identical tables. A test asserts that the stored parts still satisfy
the reconciliation equation for every fixture.

### 2026-08-08: The `users` table holds no Telegram ID

`users` is `id`, `created_at`, `deleted_at`, and nothing else. Access control
lives in `TELEGRAM_ALLOWED_USER_IDS` in the environment, so no external account
identifier ever lands in the database. A Telegram numeric ID is a pseudonymous
handle for a real person, and invariant 3 is easier to keep absolutely than
approximately.

When multi-user arrives, the Telegram-to-user mapping becomes its own table and
this one does not change. `user_id` is already on every table, which was the
expensive part.

### 2026-08-08: Money columns on `transactions` are deliberately unconstrained

The extraction schema forbids negative amounts, because a printed receipt cannot
show one. The ledger must allow them, because section 16.8 models a refund as its
own row with a negative `grand_total_minor` and a `related_transaction_id`
pointing at the original. The original is never edited, so the append-only
property survives and category totals net out with no special-case logic.

`CHECK` constraints are therefore applied only where they are unambiguously
right: `mrp_minor >= 0`, `amount_minor >= 0` on adjustments, and currency being
three uppercase characters.

### 2026-08-08: Cost is stored in integer micro-dollars

A receipt costs about $0.0079. In cents that rounds to zero, and a float would
violate invariant 1. `raw_extractions.cost_micros_usd` is an integer count of
millionths of a dollar, so a receipt is 7,900 and the daily cost cap in section
16.6 stays exact.

### 2026-08-08: Primary keys are UUID4 text, not autoincrementing integers

Row IDs travel in Telegram inline-keyboard callback data. Sequential integers
would let one user guess another user's row IDs the moment this stops being
single-user, and the confirm flow in 16.4 keys pending state by ID.

### 2026-08-08: SQLite foreign keys are enforced explicitly

SQLite ignores foreign keys unless `PRAGMA foreign_keys=ON` is issued per
connection. Without it, a refund pointing at a transaction that does not exist
would insert happily and the chain in 16.8 would rot silently. `db/engine.py`
attaches the pragma on connect, along with `journal_mode=WAL` for the durability
reason in 16.1. A test asserts the pragma is actually live rather than assuming
it.

### 2026-08-08: `alembic.ini` carries no connection string

`sqlalchemy.url` is left blank and `alembic/env.py` reads `DATABASE_URL` from the
environment. No database path is committed, and the live ledger cannot be
targeted by accident from a checked-in file. `env.py` deliberately does not use
`raseed.config.Settings`, which would demand a bot token and an API key that a
migration has no use for.

`render_as_batch=True` is set because SQLite cannot `ALTER` most things in place;
without it any future column change silently fails to generate.

### 2026-08-08: Categories are seeded at bootstrap, not in the migration

A category needs a `user_id`, and no user exists at migration time. So
`db/seed.py` runs on first connect instead and is idempotent. It never renames an
existing category, because `display_name` is mutable by design and only the slug
is stable.

### 2026-08-08: A test asserts the migration matches the models

`test_the_migration_matches_the_models` runs `alembic upgrade head` against a
temporary SQLite file and compares the resulting tables and columns to
`Base.metadata`. A hand-edited migration that drifts from `models.py` is a silent
data bug, and this is the cheapest possible guard against it.

### 2026-08-08: Section 3.10 drafted and accepted, image path only

Ashrit ruled out the PDF text-layer path: most PDFs will be scans or exported
images anyway, so the router is designed around the image path and everything
else is made to look like one. Full text at
`docs/proposals/3.10-ingestion-router.md`. He confirmed `MAX_PDF_PAGES = 5` and
accepted the other four open questions as drafted.

Consequence: **`pdfplumber` is installed and unused in v0.** It stays on the
allowlist so the branch can be added later without a dependency conversation.

### 2026-08-08: The first boundary drawn on an invariant

Invariant 9 says "no image editing, upscaling, or enhancement anywhere in the
pipeline", and it is written absolutely. Rasterizing a PDF page creates pixels
that did not exist as pixels, and decoding HEIC produces different bytes for the
same picture. Neither is editing, but the invariant does not say so.

**Accepted rule: decoding and rendering are permitted, altering is not.** A
transform is allowed only if it is deterministic, lossless with respect to
content, and produces a visually identical result on every run. Rasterizing at
fixed DPI and transcoding a container qualify. Sharpening, straightening,
denoising and upscaling do not.

The text of invariant 9 in `CLAUDE.md` is unchanged. This records where its edge
sits, which is a different thing from changing it.

### 2026-08-08: Gemini schema conversion verified, not assumed

The whole value of section 21.2 rests on `is_receipt` being generated before any
line item. That only holds if the field order survives into the provider's
constrained decoder.

Checked against the installed `google-genai` 2.17.0 before writing the provider:
`ExtractionResult` converts cleanly, `additional_properties: false` survives, the
nullable unions become `nullable: true` rather than failing, and the SDK emits
`property_ordering` with `is_receipt` first. This was the largest unverified
assumption in the design and it holds.

### 2026-08-08: Cost is priced from a dated table, and an unknown model refuses

`extraction/pricing.py` carries `RATES_CHECKED_ON = 2026-08-08` alongside the
rates. Pricing a model with no entry raises `UnknownModelError` rather than
returning zero, because a silently wrong cost defeats the daily cap in 16.6.
Cost arithmetic rounds up, so the recorded figure is never lower than the real
one and the cap trips early rather than late.

### 2026-08-08: Prompts are versioned files, never edited in place

`extraction/prompts/v1.md`, loaded by version, with the version stored on every
`raw_extractions` row. A released version is never edited: a new one is added.
Old rows reference `v1` and that reference has to keep meaning what it meant, or
the audit trail is fiction. A test asserts the prompt still states the four rules
that matter, so a rewrite cannot quietly drop one.

### 2026-08-08: The error taxonomy splits on retryability, not on severity

`ProviderTransientError` (429, 5xx, timeouts) is retried three times with
exponential backoff per 16.5. `ProviderResponseError` is not, because retrying an
identical request against a temperature-zero decoder gets the same answer and
spends the rate limit. `ProviderBlockedError` is separate again, because the user
needs a different message and no amount of retrying helps.

### 2026-08-08: Nothing in the test suite spends money

The Gemini client is injected, so all 55 provider tests run against a fake with
no key and no network. The only thing that can spend is
`tools/eval_extraction.py`, which is never run by pytest, refuses to start
without `GEMINI_API_KEY`, and has a `--dry-run` mode that calls the free
`count_tokens` endpoint and prints the projected cost before anything is
authorised.

### 2026-08-08: First live call, and a correction to the commit 6 message

**The offline schema check was necessary but not sufficient, and I said otherwise.**
Commit 6 claimed the contract "converts cleanly, so it holds". Conversion held.
API acceptance did not. The first live call failed with a 400:

```
Unknown name "additional_properties" at 'generation_config.response_schema'
```

`extra="forbid"` on the Pydantic models becomes `additional_properties` in the
converted schema, and the Gemini API rejects that field outright. It converts
without complaint locally, so nothing short of a real request could find it.

Four shapes were probed against the live API rather than guessed at:

| Shape | Result |
|---|---|
| `response_schema=ExtractionResult` | **400**, rejects `additional_properties` |
| `response_schema=` converted Schema, field stripped | works |
| `response_json_schema=model_json_schema()` | works |
| `response_json_schema=` stripped | works |

**Chosen: build the wire schema through the public `JSONSchema` to `Schema`
conversion, then pin `property_ordering` on every object explicitly.** The
`response_json_schema` path also works and is simpler, but it relies on the API
honouring dictionary key order, which is not a documented guarantee.
`property_ordering` is the documented mechanism, and section 21.2 is too
load-bearing to rest on luck. The private `_transformers.t_schema` was avoided.

Dropping `additional_properties` from the wire schema weakens nothing.
Strictness belongs at validation, and `ExtractionResult` still rejects unknown
fields when the response is parsed. A test asserts both properties of the wire
schema so an SDK change fails loudly.

### 2026-08-08: Measured token counts, and the preflight was wrong in both directions

Measured with the free `count_tokens` endpoint, decomposed by counting the
prompt alone and subtracting:

| | Measured | Preflight guess | Error |
|---|---|---|---|
| Prompt `v1` alone | 860 tokens | ~700 | close |
| `blinkit_022` image (1600x1982) | 1,110 tokens | 2,322 | **109% too high** |
| `blinkit_026` image (1600x1624) | 1,089 tokens | 2,322 | **113% too high** |

So the tile formula `ceil(w/768) * ceil(h/768) * 258` roughly doubles the real
figure. This is the "verify before trusting these numbers" caveat from the
preflight costing, now discharged.

Output tokens went the other way. The estimate was ~450; the real calls used
roughly 1,280 including thinking. Net effect on cost:

| | Estimated | Measured |
|---|---|---|
| Per 5-item receipt | $0.0079 | **$0.0113** |
| 60 receipts/month | $0.47 | **$0.68** |
| Full 32-receipt sweep | $0.32 | **$0.36** |

Still a rounding error at personal volume, so section 4.6's conclusion survives,
but the number in the brief is now measured rather than modelled. Thinking
tokens are the dominant term, which is worth knowing before any prompt change.

### 2026-08-08: First extraction results

Two receipts, the only two PNGs currently present. `gemini-3.6-flash`,
prompt `v1`, temperature 0.

| | |
|---|---|
| Exact field match | 2/2 |
| Grand total correct | 2/2 |
| Accepted by the gate | 2/2 |
| Passed the MRP cross-check | 2/2 |
| Total spend | $0.0227 |

Every field matched, including the struck-through prices, which is the section
24.5 failure mode. Separately, a text-only probe with a non-receipt input
returned `is_receipt: false` with a sensible rejection reason, so the invariant
11 safeguard works on a live model and not just in tests.

**D2: no evidence of a derived total in this run.** No receipt had wrong items
while still balancing. This is weak evidence on two samples, and it is not
settled until the other 30 images exist. Re-check on every prompt change.

### Which Gemini model, given the brief's price is stale (resolved: `gemini-3.6-flash`)

Rates are **per million tokens**, not per image. Costing the actual eval set:

Gemini tiles images into 768x768 blocks at 258 tokens each. Our renders are
1600 px wide (800 CSS px at `device_scale_factor=2`), so a 5-item receipt at
1600x1982 is 9 tiles = **2,322 tokens**, and a 30-item receipt at roughly
1600x7200 is 30 tiles = **7,740 tokens**. Assuming ~700 input tokens of prompt
plus schema, ~450 output tokens for 5 items and ~2,200 for 30:

| Model | Per 5-item receipt | Per 30-item receipt | 60/month | Full 32-receipt eval sweep |
|---|---|---|---|---|
| `gemini-3.6-flash` ($1.50/$7.50) | $0.0079 | $0.0292 | $0.47 | $0.32 |
| `gemini-3.5-flash-lite` ($0.30/$2.50) | $0.0020 | $0.0080 | $0.12 | $0.08 |
| `gemini-3.1-flash-lite` ($0.25/$1.50) | $0.0014 | $0.0054 | $0.09 | $0.06 |

So a receipt costs well under one paisa on any of them, and the brief's section
4.6 conclusion (cost is a rounding error, pick on accuracy) survives intact.

The one place the 4x gap becomes visible is **prompt iteration**: a full eval
sweep is $0.32 on Flash versus $0.08 on Flash-Lite, so fifty sweeps during
development is $16 versus $4. Real, but not decision-shaping.

Note `gemini-3.1-flash-lite` undercuts `gemini-3.5-flash-lite` on both axes
despite the lower version number. If Flash-Lite wins the benchmark, check 3.1
before defaulting to 3.5.

Options, ranked:

1. **`gemini-3.6-flash`** ($1.50/$7.50). Current GA Flash. Section 4.5's stated
   plan is to build against the strongest option first "because it removes a
   variable while you are still getting the pipeline right", then benchmark. The
   specific risk it buys down is section 24.5's named failure mode: misreading
   the struck-through MRP as the paid price. If a weaker model does that, it
   looks like a prompt bug and costs hours.
2. **`gemini-3.5-flash-lite`** ($0.30/$2.50) or **`gemini-3.1-flash-lite`**
   ($0.25/$1.50). Section 4.2 already flagged Flash-Lite as worth benchmarking.
   Unproven on strikethrough receipt OCR.
3. **`gemini-3-flash-preview`** ($0.50/$3). Matches the brief's numbers, but it
   is a preview endpoint. Preview endpoints get deprecated on short notice, which
   is a bad foundation for a ledger.

Recommendation: **option 1 as the `.env.example` default**, with Flash-Lite
benchmarked against it on the 32-receipt set as the first thing after extraction
exists. Both sweeps together cost about $0.40, which buys a measured answer
instead of a guess. The model sits behind the provider interface (section 4.5)
and lives in `.env`, so switching is a one-line change with no code edit.

**Verify before trusting these numbers.** The tile arithmetic uses Google's
simplified formula; their docs also give a "crop unit" formula that yields a
different tile count, and the output-token figures are my estimates. The
`google-genai` SDK exposes `count_tokens`, which is free, so the exact input
count gets measured at commit 6 before a single paid call is made.

**Resolved 2026-08-08:** Ashrit chose option 1. `GEMINI_MODEL=gemini-3.6-flash`
is in `.env.example`.

### 2026-08-08: `tzdata` installed, an allowlist gap found by reality

Seventeen flow tests failed with `ZoneInfoNotFoundError` on a zone name that is
certainly valid. The cause is not a typo: **Windows ships no IANA timezone
database.** `zoneinfo.TZPATH` was empty, `available_timezones()` returned zero
entries, and `ZoneInfo("UTC")` itself raised.

Section 23.1 lists `zoneinfo` as stdlib requiring no install. That is true on
Linux and macOS and false on Windows, where the stdlib module exists but has
nothing to read. Invariant 6 buckets every period query on the merchant-local
calendar date, so this is not optional.

`tzdata` is not on the allowlist, so per invariant 12 work stopped and Ashrit
was asked before anything was installed. **He approved it.** `tzdata==2026.3` is
now in `pyproject.toml` and hash-locked; 598 zones resolve.

`src/raseed/timezones.py` wraps `ZoneInfo` so the failure is legible if this
recurs on a fresh machine: it distinguishes "no database at all" from "no such
zone" and names the fix. Treat this as an addition to section 23.1 rather than a
deviation from it.

### 2026-08-08: The flow is transport-agnostic and Telegram is a thin shell

Every decision about a receipt lives in `adapters/flow.py`, which knows nothing
about Telegram. `adapters/telegram.py` translates results into messages and
buttons and does nothing else.

The reason is testability, not future-proofing. `test_flow.py` reaches every
branch including the daily cap, expiry, Class 1, Class 2 and the duplicate paths
with no bot token, no network and no API key. Brief section 2 also names
WhatsApp as a later transport, which this shape allows, but that is a side
effect.

### 2026-08-08: The order of checks in `submit_image` is the design

Cheapest and most protective first:

1. Daily cost cap, before anything is downloaded or paid for (16.6)
2. Dedupe on the image hash, which is free (3.6 as revised by 24.4)
3. Extraction, the only step that spends money
4. `is_receipt`, before a single line item is read (invariant 11)
5. The reconciliation gate
6. The user, who confirms before anything commits (3.4)

Putting the cap first means a runaway loop costs nothing. Putting the hash check
second means the common accidental resend never reaches the model.

### 2026-08-08: Pending state is keyed by `(chat_id, message_id)` and lives in memory

Brief 16.4 names the bug: three receipts in flight, one global "pending"
variable, and confirming the third commits the first. The key travels in the
inline keyboard's `callback_data`, which Telegram caps at 64 bytes. The encoding
is `action|chat_id|message_id`; a test asserts the worst realistic case (a
supergroup ID and a nine-digit message ID) fits, and oversized data raises rather
than truncating, because a silently truncated key would confirm the wrong
receipt.

The store is in memory. A restart loses pending confirmations, which is
acceptable because nothing has been committed yet. It is **not** acceptable to
leak the images those entries pointed at, so `ImageStore.sweep` runs at startup
and `expire_stale` deletes on TTL. Invariant 7 does not stop applying because
the process died.

### 2026-08-08: A Class 2 receipt has no Confirm button at all

Rather than showing Confirm and refusing it, the keyboard for an unreconciled
Class 2 offers only "Log the gap" and "Discard". A button that exists and does
nothing teaches the user to distrust the buttons. Once the gap is accepted the
normal keyboard returns and the difference is recorded as an
`unaccounted_adjustment`, per brief 3.3.

### 2026-08-08: The whitelist fails closed, and a stranger gets silence

`TELEGRAM_ALLOWED_USER_IDS` is a whitelist of numeric IDs and **an empty
whitelist admits nobody.** A misconfigured `.env` therefore locks the owner out
rather than opening the ledger to anyone who finds the bot.

An unlisted sender gets no reply at all, not even a refusal, because any reply
confirms the bot exists to whoever is probing it. `__main__` refuses to start
with an empty whitelist, so failing closed does not silently look like a broken
bot.

### 2026-08-08: The daily cap is parsed through `Decimal`, never `float`

`DAILY_COST_LIMIT_USD` is written by a human in dollars and held as integer
micro-dollars, because it is compared against summed `cost_micros_usd`. A value
finer than a micro-dollar is **refused, not rounded**: rounding a budget is a
decision, and it is not the config parser's to make. Invariant 1 governs the
ledger, but a budget that drifts by a rounding error is no better than a float
rupee.

### 2026-08-08: `python -m raseed` does not create tables

The entrypoint wires config, engine, provider, flow and bot together and runs
polling. It seeds categories via `db/seed.py`, which is idempotent, but it never
calls `create_all`. Alembic owns the schema. A `create_all` that quietly
disagreed with the migration history would be a data bug that only surfaced at
the first migration, which is exactly the wrong time.

### 2026-08-08: First live Telegram run, two real receipts, and four findings

The bot ran end to end for the first time. Two real receipts (a Blinkit-style
and a Zepto order), both extracted, both confirmed, both written to the ledger.
The transport, the confirm flow, the gate, the append-only writes and the image
deletion all worked. Four things came out of it that the synthetic eval could
not have found.

**1. The date parser rejected the format the model actually emits. Fixed.**

`order_datetime_local` on receipt 2 came back as `2026-07-30T12:44:00`. Every
entry in `DATE_FORMATS` uses a space separator, so ISO 8601 with `T` parsed to
`None`, the flow fell back to the message timestamp, and a **30 July receipt was
dated 9 August**. Invariant 6 buckets period queries on exactly this value, so
that receipt would have landed in the wrong month's total.

The schema asks for the date "exactly as printed, do not reformat". The model
normalised it anyway, which is unsurprising for a field named
`order_datetime_local`. `parse_printed_date` now tries
`datetime.fromisoformat` before the printed-format list. Tests carry the exact
live string.

This is the class of bug only a real run finds: 45 flow tests passed against
fixtures whose dates happened to be written the way the parser expected.

**2. A ₹100 discount was counted twice, and the gate caught it. Unresolved.**

Receipt 2 has a `ZEPINDCC100 Offer Applied` coupon of ₹100 in `discounts[]`, and
its line totals sum to **exactly** the ₹627.00 grand total. So the discount is
already inside the line prices and is also being subtracted again, which is
precisely what invariant 13 forbids: *"A product discount already inside the
line price is never repeated there."*

The gate did its job. It returned Class 2 with a ₹100 gap rather than storing a
silent inconsistency. But the stored `unaccounted_adjustment_minor = 10000` is
misleading: nothing is unaccounted for, the model double-counted.

Two readings, and they need different fixes:

- The receipt prints post-discount effective line prices, the model copied them
  correctly, and then wrongly also listed the order-level coupon. A prompt fix.
- The model computed the distribution itself. The oddly precise line totals
  (₹94.87, ₹50.89, ₹115.57, ₹70.73, ₹46.54) are consistent with a proportional
  split, which is what a derived value looks like. A much more serious problem,
  and a cousin of D2.

**Not guessable from here.** The image was deleted on confirm, per invariant 7.
Tracked as D6.

**3. Invariant 7 has a real cost, now observed rather than theorised.**

The finding above cannot be settled because the evidence is gone by design. This
is not an argument for changing invariant 7, which exists for good reasons and
stays as written. It is worth recording that "delete the image on confirm" and
"diagnose a suspicious extraction afterwards" are in genuine tension, and that
the resolution is to catch these before confirming, not after.

**4. Cost is 2.2x the synthetic measurement.**

| | Synthetic (2026-08-08) | Live, real receipts |
|---|---|---|
| Input tokens | 1,970 | 2,014 and 2,017 |
| Output tokens | 1,280 | 2,684 and 3,062 |
| Cost per receipt | $0.0113 | **$0.0246** |
| 60 receipts/month | $0.68 | **$1.48** |

Input was predicted almost exactly. Output was more than double, and output is
87% of the bill at $7.50 per 1M. Real receipts have more line items and the
model thinks longer about them.

Section 4.6's conclusion still holds at $1.48/month, but the `DAILY_COST_LIMIT_USD`
default of $1.00 is now about **40 receipts a day**, not the ~125 the comment in
`.env.example` claims. That comment is stale. D5 updated.

**One thing that worked exactly as designed:** `printed_product_discount_minor`
came back as 10400 on receipt 1, and the MRP totals minus the paid totals for
the five items carrying an MRP is 29900 - 19500 = 10400, to the paisa. The
brief 24.3 cross-check is validated on real data, not just on fixtures.

### 2026-08-08: The merchant quick-pick cannot bootstrap itself

`merchant_name` came back `None` on both live receipts, which is exactly the
situation brief 24.4 describes: an app screenshot rarely prints the merchant
name. That is why the confirm keyboard offers previously seen merchants instead.

But `known_merchants` only returns merchants already in the table, and nothing
else writes to that table. On a fresh ledger the quick-pick is empty, so the
first merchant can never be added, so it stays empty forever. Both live
transactions have `merchant_id = NULL` and the `merchants` table is empty.

This is a design gap in commit 7, not a bug in the code as specified. Tracked as
D7. It needs a decision from Ashrit about how a merchant first gets named, and
the options are not equivalent, so it is not being guessed at.

### 2026-08-08: The dashboard is a web app, and it ships loopback-only first

Ashrit asked for a web dashboard reached from a button in Telegram, Rocket Money
in shape if not in scope.

**Correction to what I wrote earlier today.** I said "there is no dashboard
anywhere in the brief". That is wrong. Section 8's stack table has a row for it:
*"Dashboard (v2) | FastAPI + React | Better portfolio value than Streamlit.
Streamlit is faster if you only want the data."* So the brief anticipated a
dashboard, named a preferred stack, and named an alternative. What is true is
narrower: the dashboard is absent from the **section 17 commit list**, so its
timing was a scope decision even though its existence was not.

**And I built neither of the two options the brief names.** Not FastAPI, not
Streamlit, but the standard library. The reason is invariant 12: neither is on
the section 23.1 allowlist, and React would add a whole toolchain besides. Four
read-only routes for one user did not justify that conversation. This is a real
deviation from section 8 and it is recorded as one rather than glossed over.

It is also cheap to reverse. `web/data.py` returns plain dataclasses and knows
nothing about HTTP, so a FastAPI or React front end would replace `render.py`
and `server.py` and keep every aggregate and every test.

**Loopback first, public second, and never public without authentication.** The
server binds `127.0.0.1` and `HOST` is deliberately not readable from the
environment: making the ledger reachable from the internet should require
editing code and thinking about it, not flipping a variable. Invariant 3 means
there is no name, address, phone or card number in there to leak, which
materially limits the damage, but it is still a record of what someone bought.

Phase 2 adds a public HTTPS address and authentication **in the same change**,
never one without the other.

### 2026-08-08: No web framework, and no new dependency

`http.server` from the standard library. A framework would be right for
sessions, forms, uploads and real concurrency; this is four read-only routes for
one user, and section 23.1 has no web framework on it. Adding FastAPI or Flask
would have meant an invariant 12 conversation to buy ergonomics we do not need.

The pages are hand-rendered strings with `html.escape` on every interpolation,
for the same reason: Jinja2 is allowlisted but uninstalled, and four pages do
not justify pulling it in.

**Every value on the page is escaped, and that is not paranoia.** Brief 21.4
treats extracted text as untrusted input to the prompt because a receipt image
is something a stranger can craft. The same text is equally untrusted as input
to a page. Two tests put a `<script>` tag through a line item and an adjustment
label and assert it comes out inert.

The page is fully self-contained: no scripts, no external stylesheet, no fonts,
no images. A test asserts no `http://` or `https://` appears anywhere in the
output. It has to render over a tunnel, on a phone, with no CDN.

### 2026-08-08: The dashboard is read-only by construction, not by convention

Nothing in `raseed/web/` imports `db.ledger`. There is no write path to get
wrong. `POST` returns 405 with an `Allow: GET` header rather than a 404, because
"this endpoint does not accept writes" and "this endpoint does not exist" are
different facts.

### 2026-08-08: `rupees` moved to `raseed.money`

The bot and the dashboard both format money, and money formatting duplicated in
two places drifts. A ledger whose Telegram total disagrees with its dashboard
total is worse than one with no dashboard. `money(minor, currency)` handles the
currency column that has existed since commit 4 and had no formatter.

### 2026-08-08: Two bugs the dashboard tests caught before the page shipped

- **The "this date was guessed" warning would never have appeared.**
  `Row.date_source` carries `DateSource.MESSAGE_TIMESTAMP.value`, which is
  `"message_timestamp"`, and the renderer compared it against the uppercase
  member name. Brief 24.4 requires that a guessed date is visibly a guess, so a
  silently-never-firing warning defeats the requirement entirely. Both live
  receipts have `MESSAGE_TIMESTAMP`, so this would have been wrong on 100% of
  real data.
- **The category slices do not sum to the grand total, on purpose.** Categories
  live on line items, so the slices cover the basket and exclude charges, taxes
  and order-level discounts. A test asserts the inequality rather than papering
  over it, and the page states it in words. Presenting a total that does not
  reconcile would undo the entire point of the gate.

### 2026-08-08: The merchant gap (D7) is closed by decision, not by code

Ashrit: *"we don't need the merchant's name honestly so you can ignore that
part."* D7 is dismissed. `merchant_id` stays nullable and unused, the quick-pick
stays as written, and the merchants table stays empty.

**Consequence, recorded so it can be revisited cheaply:** the dashboard lists
receipts as "09 Aug 2026, 6 items, ₹256.00" and not "Blinkit ₹256.00". If that
reads as a gap once there are fifty rows in the table, seeding a handful of
common merchants is a small change and nothing built since depends on their
absence.

### 2026-08-08: Two files contradicted each other, and it crashed the live bot

Found in the bot log, not by a test. Ashrit ran `/undo` on both receipts and
resent one. The confirm crashed with:

```
IntegrityError: UNIQUE constraint failed:
  transactions.user_id, transactions.image_sha256
```

Twice in four minutes, and he saw nothing either time.

**The contradiction.** `db/queries.find_by_image_hash` deliberately ignores
soft-deleted matches, and its docstring says why: *"A soft-deleted match does
not count, because the user deleted it on purpose and resending is how you undo
that."* But `uq_transactions_user_image` covered every row, deleted or not.
Those two statements cannot both hold. The dedupe check reported "not a
duplicate", extraction ran and **was billed**, and the INSERT then died.

Both shipped in commits 4 and 7 respectively, both were tested, and neither test
could have caught it: one tests the query, the other tests the constraint, and
the bug lives in the space between them.

**Resolved by making the index partial:** `WHERE deleted_at IS NULL`. Invariant
2 keeps soft-deleted rows forever, and keeping them must not make a deletion
permanent. Live rows still cannot collide, which a second test pins so the fix
cannot be over-applied. Migration `8f2c1a740e93`. SQLite and Postgres both
support partial indexes, which matters because brief section 8 puts Postgres on
the roadmap.

The constraint lost, not the query, because `/undo` has to be reversible. A
receipt you can delete but never re-add is a trap.

### 2026-08-08: No error handler, so a crash looked exactly like a dead bot

The same incident, second finding. python-telegram-bot logged `No error handlers
are registered` and the user got **silence**. He tapped Confirm and nothing came
back.

Silence is the worst answer available, because it is indistinguishable from the
process being down. `RaseedBot.on_error` is now registered and replies
"Something went wrong on my end and I did not save that one. Nothing was
changed."

Invariant 10 still applies when the bot is the thing that broke. "Something went
wrong on my end" is a statement about the bot. "Try sending it as a file" would
be an instruction to the user, and a test asserts the error text contains no
such phrase. The handler also swallows a failure to deliver its own message,
because an error handler that raises leaves PTB with nowhere to go.

### 2026-08-08: Two bot instances were polling the same token

The log filled with `Conflict: terminated by other getUpdates request`. Two
`run.py` processes were alive at once, which Telegram does not allow: only one
consumer may call `getUpdates` for a token, and the two kept evicting each
other. Both were stopped and one clean instance started.

Worth knowing rather than fixing in code for now: nothing prevents a second
instance from being launched. A pidfile or a single-instance lock is the obvious
guard, and it matters more once this is hosted rather than run by hand.

### 2026-08-08: Real receipts are English, and the lexicon was built for Hindi

Brief 4.5 rests on Indian grocery vocabulary being Hinglish. Measured against
the twelve real line items in the ledger, a Hinglish-keyed lexicon matched
**3 of 12**. Blinkit and Zepto print "Green Cucumber", "Fresh White Eggs",
"English Oven Milk Bread".

Two fixes, both measured:

- **Every term indexes its `canonical` English name as a lookup surface.** So
  `kheera` catches "Green Cucumber" without a second entry, and the Hinglish
  keys earn their keep on receipts that never say a Hindi word.
- **N-gram matching, widest first.** A third of the lexicon is multi-word
  (`moong dal`, `garam masala`, `shimla mirch`) and was unreachable by
  single-token matching: "Moong Dal" tokenizes to two tokens and neither is a
  surface. Longest wins, and a token consumed by a wide match is not offered to
  a narrow one.

Result: **12 of 12** on real line items, 7 of 7 on the spelling drift brief 4.5
names, zero false positives on a non-grocery control set.

### 2026-08-08: The fuzzy threshold is measured, and one rule beats one number

Scored `fuzz.ratio` over the drift pairs brief 4.5 names against grocery words
that must not collapse into each other:

| | |
|---|---|
| genuine drift, worst | zeera/jeera 80.0, bhendi/bhindi 83.3, panner/paneer 83.3 |
| false positive, worst | paneer/paper 72.7, namak/namkeen 66.7 |

78 sits in that gap. **`WRatio` was rejected**: it scores paneer against paper at
90, which would file a sheet of paper under dairy.

One false positive survived: `phone` scores 80 against `honey`, putting a phone
charger in groceries. Fixed with a rule rather than a number: **a fuzzy match
must agree on its first letter.** Transliteration drift happens in the middle of
a word, not at the start. Where the initial sound genuinely does change
(jeera/zeera, the case the brief names) that is a `variants` entry and matches
exactly.

Genuinely different words for the same thing (curd/dahi at 25, chhole/chana at
36) are not drift and no threshold reaches them. `variants` is the honest
mechanism for those.

### 2026-08-08: Brief 4.5's lexicon categories are from a retracted taxonomy

Section 4.5's example YAML uses `vegetables`, `dairy_eggs` and `staples`. Those
belong to the twelve-domain taxonomy that section 3.8 explicitly retracts
("[CORRECTED: I overbuilt this]") in favour of Groceries, Food & Dining, Drinks,
Entertainment, Uncategorized, with no sub-categories at launch.

3.8 corrects 4.5, so `category` uses the five real slugs. Nothing is lost: the
finer meaning moves to `canonical`, where it belongs. When there is enough
grocery volume to split Groceries (3.8 says wait for that), the canonical names
are already there and Stage 2 re-runs over stored extractions for free.

### 2026-08-08: `types-PyYAML` not installed, and the stub is not missed

mypy wants stubs for `yaml`. `types-PyYAML` is not on the section 23.1
allowlist, so invariant 12 makes it a conversation, and it buys almost nothing
here: `yaml.safe_load` returns `Any` whatever the stubs say, and the lexicon
loader validates every field with explicit isinstance checks before trusting it.
That runtime validation is what actually protects the lexicon. A mypy override
records the reasoning.

### 2026-08-08: `category_confidence` is stored in basis points, not as a float

Brief 18.5 asks for "float for fuzzy and LLM paths, null for exact". It does not
get a float. `test_no_floating_point_columns_anywhere` walks **every column of
every table**, not only the money ones, and carving an exception into that test
so it can hold a similarity score is a worse trade than storing the number
differently.

The column is `category_confidence_bp`: an integer in basis points, 0 to 10000,
with a CHECK constraint on the range. Four significant digits, which is more
precision than rapidfuzz's scores meaningfully carry, and 7826 reads as plainly
as 78.26. Null on the exact path, exactly as the brief says.

### 2026-08-08: "Nobody decided" is a null source, not a fifth enum member

Brief 18.5 names four sources: `lexicon_exact`, `lexicon_fuzzy`, `llm`,
`manual`. Every one of them is something that **decided**. An item the lexicon
did not know, with no model available or a model that failed, has not been
decided by any of them.

That state is `category_source IS NULL`, not a new enum value. It is also
exactly the query the backfill tool wants: `WHERE category_source IS NULL OR
category_confidence_bp < 8000` is the set of rows worth re-running after a
lexicon change. Adding a `NO_MATCH` member would have made that query longer and
the enum less honest.

A below-threshold model answer is different again: source is `llm`, the weak
confidence is stored, and the category is `uncategorized`. Brief 18.5 says below
threshold goes to uncategorized rather than getting a guess; it does not say to
forget that the model was asked.

### 2026-08-08: Stage 2's API spend is recorded in `raw_extractions`

Brief 16.6's daily cap is computed by summing `raw_extractions.cost_micros_usd`
over a rolling 24 hours. Stage 2's model fallback is a billed API call. Recorded
anywhere else, or nowhere, the cap silently under-counts the moment the fallback
starts firing, and a cap that under-counts is not a cap.

So Stage 2 calls are written to the same table, with a new `stage` column
(`extraction` | `categorization`, defaulting to `extraction` so every existing
row keeps meaning what it meant). Stage 1 rows are what a transaction derives
from; Stage 2 rows are billing and provenance only, and nothing has a foreign
key to one.

The alternative considered and rejected was a separate `api_calls` table. It
would have needed the cap query to sum two tables and stay in sync forever, for
no gain over one nullable-free enum column.

### 2026-08-08: Stage 2 runs on confirm, and can never cost the user a receipt

Three consequences, all deliberate:

- **On confirm, not on submit.** A receipt the user discards costs nothing to
  categorize.
- **A fallback failure is not an error.** The lexicon's answers already stand and
  the unmatched items are already `uncategorized`, so a rate-limited or broken
  Stage 2 produces a receipt that stores fine with some items uncategorized. It
  never produces a lost receipt. Brief 16.5 in spirit.
- **Over budget drops the model, not the receipt.** If the daily cap is gone by
  confirm time, the lexicon still runs and the fallback is skipped. Those rows
  are re-runnable later from `raw_extractions` for free.

### 2026-08-08: `tools/enrich.py` updates derived columns, and that reads on invariant 2

Invariant 2 says the ledger is append-only and corrections are new rows. The
backfill writes `category_id`, `category_source`, `category_confidence_bp` and
`normalized_slug` in place on existing line items.

The reading this rests on: those four columns are a **cache over
`raw_extractions`**, not facts. `raw_name`, `line_total_minor`, `mrp_minor` and
everything in `raw_extractions` are the facts, and none of them is ever touched.
The schema already anticipated this in commit 4 ("the normalized columns are
filled by Stage 2 and are null until then"), and brief 3.8 and 18.5 both
describe re-running Stage 2 over history as the payoff for keeping Stage 1
immutable, which is not possible if the derived columns can only be appended.

**Flagged for Ashrit rather than assumed.** The tool reports by default and
writes only on `--apply`, so nothing changes without an explicit choice. If the
correct reading is that a re-categorization must be a new row, that is a
schema change and a conversation, not an edit.

### 2026-08-08: A colour word loses a tie to the product beside it

`Orange Carrot` matched both `orange` and `carrot`. Both exact, both six
letters, so the tie went to whichever came first and a carrot was filed as an
orange. The category was right either way; `normalized_slug` was not, and that
is the column that makes `Bhindi 500g` and `Okra 500g` the same product later.

The blanket fix, "prefer the last matching token", breaks the case the matcher
was built around: `Mr. Makhana Pudina Party Flavoured Makhana` would resolve to
mint instead of fox nut. What actually distinguishes them is position relative
to the noun. Colours precede it ("Green Cucumber", "Fresh White Eggs"), flavours
follow it. So colours are demoted in a tie and nothing else changes. `Orange
1kg` still matches the fruit, because there is nothing else in the name to
prefer.

### 2026-08-08: Stage 2 gets its own provider protocol, prompt directory and model

Invariant 4 keeps extraction and categorization apart. One `Provider` interface
with an `extract` and a `categorize` method would make it natural for a future
implementation to answer both from a single call, which is the exact thing the
invariant forbids. So `CategorizationProvider` is its own Protocol, the prompts
live in `enrichment/prompts/` with their own versions (`categorize-v1`, prefixed
so it cannot be confused with Stage 1's `v1` in the same column), and the model
is a separate setting.

The default Stage 2 model is `gemini-3.5-flash-lite`, $0.30 in and $2.50 out per
million against Stage 1's $1.50 and $7.50. The binding constraint on that choice
was not preference: `cost_micros` refuses to price a model with no published
rate on file, and that refusal is what keeps the daily cap honest, so a newer
lite variant means verifying its price first rather than guessing it in code.

The taxonomy is baked into the wire schema as an enum, built per call because
the categories live in the database and grow. The decoder therefore cannot emit
a category that does not exist, so there is no "the model invented a category"
failure mode downstream, only an "out of range index" one.

### 2026-08-08: `normalized_slug` keeps the brand, `canonical_slug` is new

Brief 16.7 defines `normalized_slug` with a worked example:
`Amul Taaza Toned Milk 500ml` becomes `amul-taaza-toned-milk`. That is the
printed name with the quantity stripped, and it keeps the brand.

Stage 2 also produces a different answer: what the item **is**, from the
lexicon. `Bhindi 500g` and `Okra 500g` share no `normalized_slug` at all, and
brief 3.7's item canonicalization needs them to be one product.

I had initially filled `normalized_slug` with the lexicon canonical, which
silently redefined a column the brief specifies. Corrected: `normalized_slug`
means what 16.7 says, and `canonical_slug` is a new column holding the lexicon's
answer. Both are useful and neither can stand in for the other, so overloading
one would have thrown the brand away permanently.

### 2026-08-08: `quantity` is stored in `unit_normalized`, and packs stay separate

Brief 16.7's example (`500ml` to `quantity 500, unit ml, unit_normalized ml`)
does not disambiguate `1kg`. Storing `quantity 1` with `unit_normalized "g"`
would mean one gram, and every price-per-unit comparison against it would be
wrong by a factor of a thousand.

So the quantity is expressed in the normalized unit: `1kg` is `1000` with `unit`
"kg" and `unit_normalized` "g"; `1.5 L` is `1500` ml. Normalizing down to the
small unit is also what keeps the column an integer, which invariant 1's
reasoning covers as well as it covers money.

`pack_count` is never multiplied into `quantity`. `2 x 500ml` is
`pack_count 2, quantity 500`, exactly as the brief writes it, because a two-pack
and a one-litre bottle are different products at different prices. Null pack
count means the name said nothing, which is a different fact from "one".

Blinkit prints the trailing form, `500 ml x 2`, in a separate quantity column
rather than in the title. Both forms are parsed, and the receipt's own quantity
column is preferred over a size buried in a product name.

### 2026-08-08: user IDs are derived from the Telegram account, never stored

Multiple people need separate ledgers. Every table already carries `user_id`
(invariant 8), so the only missing piece was turning "Telegram user 123456789"
into one.

The obvious answer, a column mapping one to the other, is not available.
Invariant 3 bans PII fields in any schema, a test asserts `users` holds exactly
`id`, `created_at` and `deleted_at`, and another bans any column name containing
"telegram". A Telegram account ID identifies a person, and a public repo whose
schema advertises where that identifier lives is precisely what invariant 3
exists to prevent.

So the ID is derived:

    user_id = UUID(HMAC-SHA256(USER_ID_SECRET, "telegram:<id>")[:16])

Stable, so someone returns to their own ledger. One way, so a stolen database
cannot be turned back into a list of Telegram accounts. And there is no mapping
table, so there is nothing to leak.

**The price, stated plainly: `USER_ID_SECRET` can never change.** Change it and
every user is a new user with an empty ledger, and the old rows are unreachable
because nothing anywhere records whose they were. `tools/claim.py` exists for
exactly that, and it is the only reason the situation is recoverable at all.

The secret is namespaced (`telegram:`) so a second transport later derives a
different ID for the same number instead of silently colliding.

`tools/claim.py` moves rows between user IDs, which is an UPDATE against
`raw_extractions`, a table invariant 5 calls immutable. The reading taken:
invariant 5 protects the *content* of an extraction, what the model saw and
said. Which ledger a row belongs to is not content, and the alternative to
moving the rows is abandoning them. It is a one-shot repair tool, run by hand,
that backs the database up first and refuses to run without an explicit target.

### 2026-08-08: a global cost cap, because N per-user caps do not bound the bill

Brief 16.6's daily cap is per user. Two users each staying inside a $1 budget is
a $2 day, ten users is a $10 day, and the number that actually gets billed was
not being checked anywhere.

`GLOBAL_DAILY_COST_LIMIT_USD` defaults to $2.00 and is checked **before** the
per-user cap, since a total that is already blown is not made acceptable by an
individual who has been frugal. Both read the same rolling 24 hour window over
`raw_extractions.cost_micros_usd`, which is where every API call this project
makes gets priced.

Sized against measurement, not roundness: $0.0246 per receipt live, so $2.00 is
about 80 receipts a day across everybody, against an expected load of two
friends at 10 to 15 receipts each, once. It is a runaway guard, not a quota. A
loop or a spammer costs $2 and stops.

The refusal message names the cause honestly ("Raseed has hit its total reading
budget for today") and adds "Nothing is wrong on your side", because the user
who trips a global cap is usually not the user who filled it.

### 2026-08-08: the dashboard authenticates with Telegram's `initData`, in a header

The dashboard was loopback-only and therefore needed no authentication. Once a
Cloudflare Tunnel points a public HTTPS name at it, that stops being true on the
first request.

Telegram signs the `initData` blob it hands a Mini App with the bot token, so a
page opened from the bot can prove which account opened it: no password, no
session store, no OAuth round trip, no new dependency (stdlib `hmac`).
Verification is `hmac.compare_digest` over the sorted data-check-string, with a
one hour freshness window checked in both directions, since a blob dated in the
future is either a broken clock or someone minting one that never expires.

**Header, not cookie.** `Authorization: tma <initData>` on every request. A
cookie would mean CSRF reasoning, SameSite reasoning and a logout story, all for
a read-only app that displays what the viewer already owns. Nothing is stored
browser-side, so there is nothing to steal from it and nothing to expire.

`/` therefore serves a data-free shell: markup, style, and the only JavaScript
in this project, which reads `Telegram.WebApp.initData` and fetches the real page
with the header. An unauthenticated GET of the root reveals that Raseed exists
and nothing else.

`X-Frame-Options: DENY` had to go, because a Mini App **is** a frame. It is
replaced by a CSP `frame-ancestors` naming Telegram's two origins, which is
narrower than what it replaced rather than a relaxation.

Every unknown path answers 401 before it answers 404, so an unauthenticated
caller cannot map the routes.

`HOST` stays hardcoded to `127.0.0.1`. A Cloudflare Tunnel dials *out* from this
machine, so the listener never needs to be reachable from the network, and the
public name resolves to Cloudflare rather than to a home IP address.

### 2026-08-09: a confirm that fails must leave the button working

Reported live: the Confirm button "not working and returning an error message".
The ledger told the story. Two transactions were confirmed at 21:43 and 21:52
UTC, both soft-deleted by `/undo` at 23:26, and then three extractions at
23:26, 23:27 and 23:31 that produced no transaction at all. Three receipts read,
three receipts paid for, nothing saved.

Four separate defects, found by replaying those stored extractions:

**`confirm` consumed the pending entry before doing the work that can fail.**
`pop` came first, so any failure after it left a button on screen with nothing
behind it. The receipt could not be retried, only resent and paid for again. The
entry is now read, and removed only once the row is in the ledger.

**A transport failure escaped the provider error taxonomy entirely.** Both
Gemini providers translated `errors.APIError` and nothing else. Verified against
the SDK: an unreachable host raises `httpx.ConnectError`, for which
`isinstance(exc, errors.APIError)` is False. So a DNS blip, which the log shows
happening at 08:06 on 2026-08-09, arrived at the caller as a raw transport
exception, past every `except ProviderError` between here and there. On the
confirm path that took down a confirm; on the submit path it would have taken
down an extraction whose image was already saved.

That made a liar of `_apply_fallback`'s docstring, which promises that a failed
Stage 2 never costs the user the receipt. That promise is now unconditional:
it catches everything, because it must not depend on a vendor SDK being
disciplined about which exception type it raises.

**Confirming the same image twice was a crash rather than an answer.** Resending
is what a person does when a button looks broken, and each resend produces
another pending entry for the same bytes. The second confirm reached the partial
unique index and surfaced as "something went wrong on my end". It now checks for
an already-logged transaction before Stage 2 runs, so the answer is "already
logged on 09 Aug" and no money is spent reaching it.

**Nothing logged what a button press did.** A confirm that answered "no longer
waiting" wrote no line anywhere, which is why this had to be reconstructed from
the ledger rather than read from the log. One INFO line per press now records
the action and the resulting step, and neither is a receipt, an amount, or
anything about the sender.

The most likely thing actually tapped, incidentally, was none of those: the
receipts were submitted at 16:26-16:31 local and the bot was restarted at 18:04,
and the pending store is in memory. `EXPIRED_MESSAGE` now says so ("it either
timed out or I was restarted since. Nothing was saved") instead of the previous
"that one is no longer waiting", which was accurate and useless.

**Not fixed, and deliberately: the pending store is still in memory.** Persisting
it would make a restart survivable, and it is not a change to make quietly.
`raw_extractions` already holds everything needed to rebuild a pending receipt,
so it is cheap, but it also makes a confirm button valid indefinitely and brief
16.4 gives it a 24 hour life. That is Ashrit's call, not mine.

### 2026-08-09: a test that only passed in the morning

`test_the_global_cap_is_a_rolling_window` began failing on its own, with no code
change. `burn()` wrote spend rows with `server_default=func.now()`, the real
clock, while the flow compares them against a window derived from its injected
clock. The test asserted that a row falls outside a window two days after `NOW`,
which held only while the wall clock was behind `NOW + 1 day`. It passed for a
day and then stopped.

The rows are now stamped explicitly. Worth recording because the failure looked
exactly like a regression in the cost cap and was not one: a test that reads the
real clock while the code reads an injected one is a test that reports the date,
not the behaviour.

### 2026-08-09: the dashboard is previewed on disk, not unlocked on the server

Opening `http://127.0.0.1:8770` in a desktop browser correctly shows the "open
this from the bot" card and no data, because every route except `/` requires a
signed Telegram `initData`. That is the W2 security model working, but it also
means the page cannot be looked at at all until `cloudflared` is running, which
makes it impossible to judge or iterate on the design.

The obvious fix, an environment-gated dev bypass on loopback, was **rejected**.
It would put a permanent authentication hole in a repo that is going public, one
commit after the commit whose entire purpose was adding authentication, and the
interlock protecting it (refuse to start when `DASHBOARD_PUBLIC_URL` is set)
would be one careless `.env` edit away from being the only thing standing
between a public tunnel and an open ledger.

`tools/preview_dashboard.py` renders the same pages with the same functions
straight to disk instead. No server, so no authentication to bypass; nothing in
`src/` is imported by it and no route is added, so deleting the file leaves the
application byte-for-byte unchanged. Output goes under `data/`, which is
gitignored, because those pages contain a real ledger.

### 2026-08-09: the Mini App stopped replacing its own document

Reported live as "the 3rd category gives an error". Not reproducible in desktop
Chrome, which turned out to be the diagnosis rather than an obstacle to it.

The boot script navigated by calling `document.open(); document.write(html);
document.close()` and then re-running `wire()` to re-attach its click listener.
That is only correct where `document.open()` clears event listeners. Chrome
does, which is why every tab worked there and why a probe confirmed listeners do
not survive it. Embedded webviews do not all behave that way, and where they do
not, each navigation leaves another live listener behind: the first tap fires one
fetch, the second two, the third three, each racing to rewrite the whole
document. That is a bug whose symptom is "it breaks on the third one".

Navigation now parses the response with `DOMParser` and replaces a single
`#content` element. The listener is attached exactly once to a document that is
never torn down, which removes the failure by construction rather than by
counting. `DOMParser` also does not execute scripts, so the swap cannot run
anything, which suits a page built out of model-extracted strings. Verified with
a faked-but-correctly-signed `initData` against the live server: twelve
consecutive navigations, and exactly one fetch per click at every depth.

The same change is what makes motion possible. A document rewrite cannot be
animated; swapping one element can. The tab bar is now persistent chrome that
survives navigation, so the active pill transitions rather than being destroyed
and rebuilt, and a tap highlights optimistically before the network answers.
Cards stagger in, bars grow from the baseline they are measured against, and
ranked fills wipe from the left. All of it is switched off under
`prefers-reduced-motion`, which is a correctness requirement: for some people
this kind of movement causes nausea.

### 2026-08-09: brief section 17 item 2 corrected (D1)

Section 17 listed commit 2 as "the full Pydantic contract including quantity/unit
and refund modelling". That contradicted two other sections of the same
document: 16.7 puts quantity parsing in Stage 2, and 16.8 makes a refund its own
ledger row. Nothing in the code was wrong. Quantity parsing shipped in commit 9
where 16.7 says it belongs, and refunds landed in commit 4 as 16.8 requires; only
the section 17 line was wrong, and it had been wrong since the brief was written.

Corrected with Ashrit's approval on 2026-08-09, which is the rule: documentation
may be kept accurate without asking, but the brief is his document and a line
that looks wrong is a conversation, not an edit. Raised as D1 on 2026-08-08,
carried through four commits, closed here.

### 2026-08-09: pending receipts go on disk, keyed by HMAC

D9, closed. The pending store was a dict, so restarting the bot invalidated
every outstanding confirm button while the buttons stayed on screen. A receipt
already read and paid for could then only be resent and paid for again. Its
deadline was "before friends are invited", and inviting friends was the next
step.

**Almost nothing is stored.** `raw_extractions` already holds the model's
response verbatim and is immutable, and both the reconciliation gate and the MRP
cross-check are pure functions of it. So a `pending_receipts` row carries the
decisions a human made (an accepted gap, a merchant pick), a pointer to the
extraction, and the resolved date. Everything else is recomputed on read, for
free, from data that was already there.

**No Telegram identifier reaches the database.** The lookup key is
`HMAC(USER_ID_SECRET, "pending:<chat_id>:<message_id>")`, the same construction
`identity.py` uses for `user_id`. The real key always arrives in the callback
data, so hashing costs nothing at lookup time, and W3's property holds: a stolen
database still cannot be turned back into a list of accounts. A test asserts the
chat and message IDs appear nowhere in the table. `expire()` therefore returns
receipts carrying a placeholder key, which is honest about what was never
stored; `flow.expire_stale` only reads `image_path`, which is the one thing
expiry must act on under invariant 7.

The table is soft-deletable like every table except `raw_extractions`. A pending
receipt is not ledger data and a hard delete would be defensible, but invariant 2
is stated without exceptions, and one nullable column is a cheaper price than an
exception to an invariant.

**Two bugs the tests caught, both from the same root.** The in-memory store
handed out the very object it held, so `accept_gap` and `set_merchant` mutated
it in place and never wrote back. Against a persistent store that silently
discarded the user's answer. Both now call `put` explicitly. And the row's
`created_at` had to be stamped from the flow's injected clock rather than
`func.now()`: a row stamped by the database expires against the wall clock while
everything else reasons about the injected one, which is exactly the bug that
made a cost-cap test pass or fail depending on the time of day a day earlier.

### 2026-08-09: a category tab totals line items, not grand totals

Brief section 2 says tapping a tab filters everything below it. The tempting
implementation is to keep showing each receipt's grand total and just hide the
receipts that do not match. That would be wrong, and quietly: a grand total
includes delivery, taxes and order-level discounts, and none of those belong to
any category. A receipt with ₹100 of groceries, ₹40 of snacks and a ₹25 delivery
fee would report ₹165 under Groceries and ₹165 again under Snacks, so the tabs
would add up to more money than left the account.

Under a tab, every figure is therefore a sum of matching **line items**:
`Row.amount_minor` returns the category share, the hero, the chart and the
receipt list all use it, and each row still prints the receipt's own total
beside it so the smaller number does not look like a mistake. The hero says
"Line items only" rather than leaving the reader to work it out. On the All tab
nothing changes and grand totals are still the right figure.

Two smaller calls fell out of it. `uncategorized` matches both the seeded
category row and every line whose `category_id` is still NULL, because
`category_totals` already folds NULL into Uncategorized and the tab has to agree
with the number printed on the tab. And tab order is the `SEED_CATEGORIES` order
from brief section 2, not alphabetical and not `created_at`: the seed writes
every row in one flush, so they share a timestamp and ordering on it returns
whatever the database feels like.

### 2026-08-09: one hue for spend, and CSS-only tooltips

Spend is a single series, so the dashboard uses a single hue and no legend, and
rank is carried by bar length and order rather than by colour. The two steps were
run through the palette validator rather than picked by eye: `#1a7f52` on the
light surface and `#46a878` on the dark one clear the lightness band, the chroma
floor and 3:1 contrast against the surface each actually renders on. A
categorical palette was considered for the category breakdown and dropped: eight
hues would need a legend, would need to survive colour-vision checks pairwise,
and would repaint every time the category set changed.

The chart tooltips are pure CSS (`content: attr(data-tip)`). A test asserts that
no page carrying ledger data contains a `<script>` tag, and that is worth more
than a nicer tooltip: every string on those pages was produced by a vision model
reading an image a stranger could have crafted, so the page is built out of
untrusted input and should not also be executing anything.

### 2026-08-09: a pending store may not open its own connection

`DatabasePendingStore` shipped holding a `session_factory` and opening a fresh
session per call. Every test passed. The first real receipt after it came back
`Something went wrong on my end and I did not save that one`, and the log said
`sqlite3.OperationalError: database is locked`.

It was a deadlock, not contention. `submit_image` has already written
`raw_extractions` on the caller's session, so that connection holds SQLite's
single write lock for the rest of the request. `put` then asked a *second*
connection for the same lock, which only the caller's own commit could release,
and the caller was blocked waiting for `put` to return. It could never have
worked. Receipt ingest was dead for every photo from the moment D9 landed.

Every method on `PendingStore` now takes the caller's `session` and none of them
commit. That also makes the pending row atomic with the extraction it points at,
which it should always have been: a rollback used to leave a committed pending
row aimed at an extraction that no longer existed.

The reason the suite missed it is worth more than the fix. `shared_sessions`
builds SQLite with `StaticPool`, which hands every session **the same
connection**, precisely so a second store can observe what the first one wrote.
That also means no two sessions in any test can ever contend for the write lock,
so code that opens its own connection mid transaction reads as correct. The
fixture that made D9 testable is what made D9's bug invisible. There is now a
`file_sessions` fixture on a real database file, where separate sessions are
separate connections, and two tests that fail with the exact production error
when the old shape is restored.

~~Left alone deliberately: the database is still in rollback-journal mode, so a
dashboard read can briefly block a bot write.~~ **Wrong when it was written, and
corrected 2026-08-11.** `db/engine.py` has executed `PRAGMA journal_mode=WAL` on
every SQLite connection since commit 4, its module docstring says so, and the
live database reports `journal_mode = wal`. This paragraph is what D11 in the
planner was built on, and D11 was tracked for three days as work that did not
exist. See the entry for 2026-08-11.

---

### 2026-08-11: the pre-publish sweep, and what it turned up

Ashrit set the repo going public, on the condition that it carry no dead code,
no machine-written filler, and none of his personal information. The sweep ran
over all 94 tracked files and the whole of the git history.

Most of it came back clean: zero unreferenced symbols in `src/`, exactly one
comment in the package that is not explaining a decision (a section divider), no
keys, no tokens, no home directory paths, no email addresses, and eval data that
is synthetic groceries with nothing personal in it. Three things were not clean.

**A live Telegram identifier was being used as documentation.** `identity.py`,
`tools/claim.py` and this file all illustrated the derivation with the running
bot's real account ID, which is also the first half of its token. The secret
half was never committed, so nothing needed rotating, and a public repo still
should not name a running bot. Replaced with an obviously fake `123456789`
everywhere, then scrubbed from history before the first push, which is the only
moment that costs nothing.

Writing this entry put the real ID straight back into the file documenting its
removal, and the scrub caught it only because the scrub ran afterwards. The
write-up is part of the surface being swept.

**D11 never existed.** Traced to the paragraph above this entry, tracked for
three days as outstanding work, and twice repeated to Ashrit as a prerequisite
for the hosting move. WAL has been on since commit 4. A tracked claim about the
code is still only a claim, and one `grep` settles it.

**The bot told people it had been restarted.** `EXPIRED_MESSAGE` named a restart
as a cause of a dead confirm button. That was true until `DatabasePendingStore`
moved pending state to disk on 2026-08-09, and false for the two days after. A
test asserted the stale word, which is how wording outlives the behaviour it
described.

Three message changes followed. One of them is a policy.

`EXPIRED_MESSAGE` now names the two causes that remain: the 24 hour TTL from
brief 16.4, and a button that was already used.

The duplicate messages now say what someone resending a receipt wants to know,
which is that it landed, when it landed, that it was not counted twice, and
where to go and look at it. The submit path and the confirm path differ on money
because the difference is real: a duplicate caught at submit is caught before
extraction and costs nothing, and one caught at confirm has already paid for
Stage 1. Saying otherwise would be a lie told to make a message friendlier.

**The policy: a provider's own exception text never reaches a chat.** It used
to. `flow.py` interpolated `str(exc)` directly into the reply, and the Gemini
provider builds those strings out of raw SDK errors, so a URL, a request id or a
paragraph of JSON could land in front of whoever was using the bot. Three
allowlisted people made that survivable. Open signup, which is where this is
headed, does not. The exception now goes to the log in full, tagged with a
reference, and the person gets the cause plus that reference. The reference is
the image digest: already computed, stable, and revealing nothing once the image
itself is deleted.

**Second pass, and it found a stored column that was always wrong.**
`PendingReceipt.date_source` was never assigned in `submit_image`, so it held
its `RECEIPT_PRINTED` default on every row, including the ones where the date
had been guessed from the Telegram message. It was written to
`pending_receipts.date_source`, read back on rebuild, and consulted by nothing:
`confirm` re-derived the answer by parsing the extraction a second time, so the
ledger came out correct and the wrong value never surfaced. Both live pending
rows held the default, which is what confirmed it.

Its own docstring said it existed "so a rebuilt receipt still knows whether the
date was printed or guessed", which was false. A stored column that is always
the same wrong value is a trap for whoever reads it next, and brief 24.4's date
editing is unbuilt, so the next person to build it would have reached for
exactly this field. It is now set where `printed` is already known, `confirm`
reads it instead of re-parsing, and a test carries a guessed date across a real
database round trip.

Also fixed, and too small to have argued about: `average_minor` came from
`round(total / len(current))`. That reaches the correct answer for every amount
this ledger will ever hold, and it reaches it through a float, while invariant 1
says money is integer minor units and the README says "no floats, anywhere". Now
`money.average`, integer throughout, rounding half away from zero because
refunds are their own negative rows per brief 16.8.

---

## Still open

- **Brief sections 3.10 and 3.11 do not exist.** Referenced four times, never
  written. 3.10 defines the PDF-text-layer versus vision router and blocks
  commit 5. 3.11 defines tiling for the tall 30-item receipts. Not inventing
  either one.
- **Thirty of the 32 eval PNGs are absent**, along with `blinkit_000.png` (the
  redacted real receipt) and `docs/RASEED_TEST_DATA_PROMPTS.md`. Needed at
  commit 6, not before. Either Ashrit restores them or they get re-rendered,
  which triggers the `Jinja2` + `playwright` install above.
