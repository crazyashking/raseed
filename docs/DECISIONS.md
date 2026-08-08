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

---

## Resolved decisions

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
