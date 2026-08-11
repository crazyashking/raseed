# Proposal: 24/7 hosting, a backend database, and open signup

Status: **partly built as of 2026-08-11.** The hosting half is done and running:
Oracle Always Free ARM, SQLite on the instance, dynamic DNS, nginx and certbot.
The open-signup half is not built, and the allowlist is still in force. See the
2026-08-11 entry in `docs/DECISIONS.md` for what was actually deployed, including
where it departs from the recommendations below.

Raised 2026-08-10, in response to: host it so the PC can be off, let friends use
it, keep it free beyond the Gemini spend, and drop the allowlist so it can spread
by word of mouth.

Free-tier terms move constantly. Every figure below was checked on 2026-08-10 and
should be re-checked before anyone acts on it.

---

## 1. The finding that matters most

**Open signup is not free, and the current caps do not make it free.**

The daily caps bound a *day*, not a *month*. `GLOBAL_DAILY_COST_LIMIT_USD=2.00`
means the worst case is $2 every day, which is **about $60 a month**, billed to
one card. Today that is theoretical because `TELEGRAM_ALLOWED_USER_IDS` admits
three people. Remove the allowlist and it becomes the actual exposure: anyone who
finds the bot can spend the budget, and a bored stranger with a folder of images
can burn the whole day's cap in a few minutes.

Two distinct problems, and they need different fixes:

| Problem | Who it hurts | Fix |
|---|---|---|
| Sustained spend has no monthly ceiling | Ashrit's card | A global **monthly** cap, new |
| One user can drain the global daily cap | Ashrit's friends, who then see "try tomorrow" | Per-user quotas, tighter for strangers |

Note that `is_receipt` is checked *after* the model call, so a junk image still
costs money. Invariant 11 protects the database, not the bill. Rate limiting is
the only thing that protects the bill.

**This is the one place the request as stated cannot be met literally.** Open
signup and "no more than we already spend" are in tension. The plan below bounds
the damage to a number you choose, rather than pretending the tension is not
there.

---

## 2. Hosting

### Recommendation: an Oracle Cloud Always Free VM

Free indefinitely, always on, no cold start, and it has a **real disk**, which is
what makes the rest of this cheap.

Checked 2026-08-10:

- Two `VM.Standard.E2.1.Micro` x86 instances (1/8 OCPU, 1 GB RAM each), or
  `VM.Standard.A1.Flex` ARM at **2 OCPU and 12 GB**.
- 200 GB block storage total, plus five volume backups.
- A credit card is required to open the account. It is not charged unless you
  explicitly upgrade.

Three caveats, all real:

1. **The ARM allowance was halved on 2026-06-15**, from 4 OCPU / 24 GB to 2 OCPU
   / 12 GB for Always Free tenancies. Oracle said instances over the new limit
   would be **terminated on or after 2026-08-18**, which is eight days out. Build
   within 2 OCPU / 12 GB and this never applies to us. 2 OCPU is roughly ten
   times what this workload needs.
2. **Idle instances may be reclaimed.** Oracle's criteria are a 7-day window with
   95th-percentile CPU under 20% *and* network under 20% (memory too, on ARM). A
   receipt bot is exactly that idle. The standard mitigation is to convert the
   account to Pay As You Go while using only Always Free resources: the bill stays
   $0 and reclamation no longer applies. Set a budget alert at $1 either way.
3. **You run the box.** Systemd unit, unattended upgrades, an SSH key. Once.

### Why not the managed platforms

| Platform | Verdict |
|---|---|
| **Fly.io** | Free tier is gone in 2026. New accounts get a trial measured in VM hours. |
| **Render** free | Spins down after **15 minutes** of no inbound traffic; cold start is **about a minute**. Its free Postgres also **expires after 30 days**. A minute of dead air after tapping Dashboard reads as broken. |
| **Koyeb** free | One instance, 512 MB, but scales to zero after 1 hour and **scale-to-zero cannot be disabled**. Same cold-start problem. |

Long polling makes cold starts worse: the process must be alive to receive
anything at all. Switching to webhooks would let a scale-to-zero host work, at
the cost of a code change plus a cold start on the first receipt of each session.
Not worth it when an always-on VM is free.

---

## 3. The database

**Recommendation: keep SQLite. Change nothing.**

The reason SQLite looked like a problem is that container platforms have
ephemeral filesystems. A VM does not. On a real disk, SQLite is not a compromise
for a three-to-twenty user ledger, it is the correct tool, and it means:

- no changes to `db/models.py`
- no Alembic rewrite
- no new dependency, so no invariant 12 conversation
- no risk of a type change quietly breaking invariant 1's integer paise

What it does require:

- **WAL mode (D11), now mandatory rather than optional.** Multiple people writing
  while dashboards read is exactly the contention D11 describes. Currently in
  rollback-journal mode, where a read can block a write.
- **Backups**, which do not exist today. A nightly copy plus upload to free object
  storage, or Litestream for continuous replication. Litestream is a binary, not
  a Python package, so invariant 12's allowlist does not apply, by the same
  reasoning already recorded for `cloudflared`.

If a managed database is wanted anyway, **Neon** is the pick: 0.5 GB free, scales
compute to zero but stays reachable, unlike Supabase which **pauses a free project
after 7 days idle**. Turso is 5 GB free but is libSQL, not Postgres, and swapping
the driver buys nothing here. Either way it is a real migration for no benefit
this project can currently use. Recommend against, for now.

---

## 4. A stable public address

The quick-tunnel dance (new hostname on every restart, update `.env` *and*
BotFather) disappears once the process stops restarting. It still needs a name.

| Option | Cost | Notes |
|---|---|---|
| **Dynamic DNS + Caddy** (recommended) | free | Stable subdomain, Let's Encrypt certificate issued and renewed automatically. Opens 443 on the VM. Built with nginx and certbot instead, because both ship in Ubuntu's archive and Caddy would have added a third-party repository. |
| **Tailscale Funnel** | free | Stable `*.ts.net` name, valid certificate, no domain and no open port. Bandwidth is throttled and not configurable. |
| **Cloudflare named tunnel** | ~$10/yr | Needs a domain in a Cloudflare zone. Not free. Rules itself out. |

Opening 443 changes the posture: today the listener is loopback-only and the
tunnel dials out. With Caddy in front, keep the Python listener on `127.0.0.1`
and let Caddy be the only thing bound publicly. Every route past `/` still demands
signed `initData`, so authentication does not weaken. Tailscale Funnel preserves
the dial-out posture if that is preferred.

Either way, **the address becomes permanent**, so `set_chat_menu_button` at
startup gets written once and the BotFather step is never repeated.

---

## 5. Replacing the allowlist

Goal: a link Ashrit can send anyone, with spend bounded by a number he picks.

**Proposed shape:**

1. **Deep-link invite codes.** `t.me/My_Raseed_bot?start=<code>` is a URL that
   forwards over WhatsApp, which is what word of mouth actually looks like. Codes
   carry a max use count and can be revoked. Better than fully open, because a
   scraper that finds the bot has no code.
2. **A trial quota for anyone without a code.** First N receipts (10 is a
   reasonable start) work with no approval. Enough to decide whether they like it,
   too few to be worth abusing.
3. **Approval in-chat, not in `.env`.** An admin command lifts a user to the full
   per-user daily cap. No file edit, no restart, no numeric ID collection.
4. **A global monthly cap** above the daily one. Reached, the bot declines
   politely until the month rolls. This is the number that actually bounds the
   card.
5. **A per-user hourly rate limit**, so a script cannot drain a day's budget in a
   burst.

Invariant 3 survives intact: approval state keys off the derived `user_id`
(`HMAC(USER_ID_SECRET, "telegram:<id>")`), so nothing new about a person gets
stored. No name, no username, no Telegram ID.

**Suggested numbers, all yours to set:**

| Setting | Suggested | Meaning |
|---|---|---|
| Global monthly cap | $5.00 | ~200 receipts a month across everyone. Hard ceiling on the card. |
| Global daily cap | $2.00 (unchanged) | Burst protection. |
| Per-user daily cap | $1.00 (unchanged) | ~40 receipts. |
| Trial quota | 10 receipts | Before approval is needed. |
| Per-user hourly | 15 receipts | Anti-burst. |

---

## 6. Work required

Ordered. Nothing starts before the working tree is committed.

**Blocking the move:**

- **D3, now blocking rather than deferred.** `requirements.txt` is hash-locked for
  Windows and CPython 3.14 only; nine packages ship per-platform wheels. Must be
  regenerated on the target with `tools/mklock.py`. This was always going to be
  the first Linux deploy's problem, and this is that deploy.
- **Carry `USER_ID_SECRET` across verbatim.** If it changes, every user becomes a
  new user with an empty ledger and the old rows are permanently unreachable,
  because nothing records who they belonged to. Back up `raseed.db` before
  anything moves.
- **D10, now mandatory.** `flow.expire_stale` still has no production caller, so
  abandoned images accumulate forever. On your own PC that is untidy. On a server
  taking strangers' uploads it is a disk that fills and an invariant 7 violation
  that grows.
- **D11, WAL**, per section 3.

**New code:**

- Global monthly cap, trial quota, hourly rate limit, invite codes, admin approve.
- `set_chat_menu_button` at startup.
- A systemd unit, so the bot restarts on boot and on crash.
- Backups.

**Not required, and worth saying so:** no database migration, no Postgres, no
webhook rewrite, no new Python dependency.

---

## 7. Can it be $0, including the API?

Asked directly by Ashrit on 2026-08-10. The honest answer is that hosting reaches
$0, and the API does not, unless a documented decision is reversed.

**Confidence: high.** Verified 2026-08-10 against current Google terms.

A free Gemini tier does exist and is generous on volume. Google uses free-tier
content "to provide, improve, and develop Google products and services", and
human reviewers may see it. Paid-tier prompts and responses are excluded from
that. The exception is the EEA, Switzerland and the UK, where paid-service terms
cover the free tiers too, which does not help a user in the US or India.

That is exactly the trade brief section 4.2 already weighed and rejected, on the
grounds that these are real receipts. So:

- **Hosting, tunnel, TLS, database, backups: genuinely $0.**
- **Extraction: $0.0246 per receipt, and the only way to zero it is to send other
  people's grocery receipts to a tier that trains on them and shows them to human
  reviewers.**

Reversing 4.2 is Ashrit's call and would need a dated `DECISIONS.md` entry. The
recommendation is to keep the paid tier and let the monthly cap do the bounding.
At $5.00 a month that is roughly 200 receipts across everybody, which is well
past "a couple of friends trying it".

Worth noting for later: output tokens are 87% of the bill (measured, see
`DECISIONS.md`), so trimming the extraction schema is the lever that actually
lowers cost. That is an optimization and not a path to zero.

---

## 8. Multiple currencies

Asked by Ashrit on 2026-08-10: can a friend in the States see dollars while
others see rupees?

**Confidence: high.** This section is from reading the code, not from the web.

### What already works

- `transactions.currency` is a real stored column, ISO 4217, with CHECK
  constraints for uppercase and length 3 (`db/models.py:414`, `:391-392`).
- The extraction schema reads the currency off the receipt itself
  (`extraction/schemas.py:183`), so a US receipt comes back `USD` with correct
  integer cents. Invariant 1 was written for this and holds today.
- `money()` already carries symbols for INR, USD, EUR and GBP and falls back to
  printing the code for anything else (`money.py:17`, `:32-36`).

So the storage layer needs no changes at all.

### What is broken today

A US friend's receipts would store correctly and then display wrongly, because
the dashboard was written when there was one user and one currency.

1. **Aggregates sum across currencies with no grouping.** `data.py:345`, `:386`,
   `:404` and `:409` add `amount_minor` over every row. Mix 500 USD cents with
   500 INR paise and the monthly total reads 1000 of nothing.
2. **Most display defaults to INR.** Of roughly eighteen `money()` call sites in
   `render.py`, only three pass `row.currency` (`:540`, `:551`, `:700`). The
   hero total, the average, the chart, the category breakdown, the peak label and
   every line in the receipt detail table print a rupee sign regardless of what
   was actually stored.
3. **Number compaction is India-specific.** `render.py:301-306` formats with
   lakh and crore, so a US user sees "1.2L" where they expect "120k".
4. **`money()` assumes two decimal places** (`divmod(abs(minor), 100)`). Fine for
   INR, USD, EUR and GBP. Wrong for JPY (zero) and KWD (three). Not a problem
   until someone sends such a receipt, and worth a guard.

### Two jobs, and only one is advisable

**Job A: each user sees their own receipts in their own currency.** Thread
currency through the aggregates, group totals by currency, fix the defaulted call
sites, make compaction follow the currency. No exchange rates, no network call,
no new dependency, fully deterministic, no invariant touched. **This is the
recommendation, and it is what Ashrit's question actually asks for.**

**Job B: convert between currencies**, so a mixed ledger shows one combined
total, or a user picks a display currency. This needs an exchange-rate source,
which means a network dependency and an availability failure mode. It also needs
a decision that has no obviously right answer: a March receipt converted at
today's rate and the same receipt converted at March's rate are different
numbers, and both are defensible. And the converted figure is inherently
approximate, which sits badly beside invariant 1's exact integer paise.

**Recommend deferring Job B.** If a combined number is ever wanted, it should be
displayed as a clearly labelled estimate alongside the exact per-currency totals,
never as the headline figure.

### The mixed-ledger case

Job A leaves one open question: what should a *single user* with both INR and USD
receipts see? The proposal is one section per currency, each exact, with no
combined total. Nobody is misled and nothing is invented.

---

## 9. Oracle runbook

**Confidence: medium-high on the steps, high on the region rule.** The console
UI changes wording between visits, so treat labels as approximate and the
sequence as correct.

1. **Sign up** at `oracle.com/cloud/free`. A card is required for identity
   verification. It is not charged unless the account is explicitly upgraded.
2. **Pick the home region at signup, carefully.** Always Free resources run only
   in the home region, and it **cannot be changed after the tenancy is
   provisioned**. The only remedy is deleting the account and starting over.
   - Friends mostly in India: `ap-mumbai-1`.
   - Otherwise: `us-ashburn-1`.
   - The real difference is tens of milliseconds on a dashboard load. Capacity
     matters more than latency here.
3. **Create the instance.** Ubuntu LTS. Try `VM.Standard.A1.Flex` at **2 OCPU /
   12 GB**, staying inside the post-2026-06-15 limit. If Always Free ARM capacity
   is unavailable, which is common in popular regions, take
   `VM.Standard.E2.1.Micro` instead: 1 GB RAM is comfortable for this workload,
   which is a Python process and a stdlib HTTP server. **Confidence on the
   capacity claim: medium**, it is widely reported and varies by region and day.
4. **Save the SSH private key** at creation. It is shown once.
5. **Open 443** in the subnet security list, and in `iptables`/`ufw` on the
   instance. Oracle's Ubuntu images ship with iptables rules that block traffic
   even after the cloud-side rule is added, which is the classic hour-long
   confusion.
6. **Convert to Pay As You Go** once it is running, staying entirely inside
   Always Free limits. The bill stays $0 and idle reclamation stops applying. Set
   a budget alert at $1 regardless.
7. **Deploy:** clone, build the venv, regenerate `requirements.txt` on the box
   (D3), copy `.env` across with `USER_ID_SECRET` **unchanged**, copy
   `raseed.db` across, `alembic upgrade head`, systemd unit, Caddy in front.

---

## 10. Decisions taken, 2026-08-10

| Question | Ashrit's answer |
|---|---|
| Oracle account with a card | **Yes**, will create. Runbook in section 9. |
| Monthly ceiling | **$5.00.** Also asked whether $0 is possible: see section 7. |
| Invite codes or fully open | **Invite codes for now**, fully open later. |
| Region | No preference stated. See section 9 step 2. |
| Currencies | Raised as a new requirement. See section 8. |

### Still open

1. **Where are the friends?** It decides the home region, and the region is
   permanent. This is the only answer needed before signup.
2. **Does section 7 change anything?** The recommendation is to keep the paid
   Gemini tier and accept about $1.50 a month for a couple of friends. Reversing
   brief 4.2 to reach $0 is available and is not advised.
3. **Job A now, or after the move?** Currency display is independent of hosting.
   Doing it first means the US friend's first impression is correct. Doing it
   after means the move lands sooner.
