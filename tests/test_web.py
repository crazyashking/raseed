"""Tests for the dashboard: the aggregates, the HTML, and the server.

The aggregates get the most attention, because a spend figure that is quietly
wrong is worse than a page that fails to render. Two things are checked
relentlessly: that every bucket is on `occurred_on_local` (invariant 6) and that
soft-deleted rows never appear (invariant 2).

The HTML is checked for escaping, because every string on the page came from a
vision model reading an image, and an image is something a stranger can craft.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import threading
from collections.abc import Iterator
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest
from sqlalchemy.orm import Session, sessionmaker

from conftest import as_extraction_payload
from raseed.db import ledger
from raseed.db.engine import create_engine
from raseed.db.models import (
    SEED_CATEGORIES,
    Base,
    DateSource,
    ReconciliationOutcome,
    Source,
    Transaction,
    TransactionAdjustment,
    TransactionLineItem,
    User,
)
from raseed.db.seed import bootstrap
from raseed.extraction.providers.base import ProviderResult
from raseed.extraction.schemas import ExtractionResult
from raseed.identity import user_id_for
from raseed.money import money, rupees
from raseed.validation.reconcile import reconcile
from raseed.web import data, render
from raseed.web.server import Dashboard, handler_for

NOW = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
TODAY = NOW.date()

#: A fake bot token. Nothing talks to Telegram; this is only the HMAC key that
#: `initData` is signed and verified with, so any string works as long as both
#: sides use the same one.
BOT_TOKEN = "123456:test-token"

#: Derives `user_id`. Long enough to pass the config length floor.
SECRET = "test-secret-that-is-long-enough-to-pass"

#: The Telegram account whose dashboard the fixture seeds.
VISITOR_ID = 4242

#: Somebody else, used to prove one user cannot read another's receipts.
STRANGER_ID = 9999


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def store(
    session: Session,
    user: User,
    *,
    on: dt.date,
    name: str = "blinkit_001",
    **overrides: object,
) -> Transaction:
    """Put one real transaction in the ledger through the real writer."""
    extraction = ExtractionResult.model_validate(as_extraction_payload(name, **overrides))
    raw = ledger.record_extraction(
        session,
        user_id=user.id,
        result=ProviderResult(
            extraction=extraction,
            model_id="gemini-3.6-flash",
            prompt_version="v1",
            response_text=extraction.model_dump_json(),
            input_tokens=2000,
            output_tokens=2700,
            cost_micros_usd=23_151,
        ),
        source=Source.TELEGRAM_IMAGE,
        image_sha256=f"{name}-{on.isoformat()}-{len(overrides)}",
    )
    verdict = reconcile(extraction)
    return ledger.record_transaction(
        session,
        raw=raw,
        reconciliation=verdict,
        occurred_on_local=on,
        date_source=DateSource.RECEIPT_PRINTED,
    )


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("minor", "currency", "text"),
    [
        (21900, "INR", "₹219.00"),
        (0, "INR", "₹0.00"),
        (-5600, "INR", "-₹56.00"),
        (124050, "INR", "₹1,240.50"),
        (1230, "USD", "$12.30"),
        (1230, "XYZ", "XYZ 12.30"),
    ],
)
def test_money_formats(minor: int, currency: str, text: str) -> None:
    assert money(minor, currency) == text


def test_rupees_still_works_after_the_move() -> None:
    """`rupees` moved to `raseed.money`; the bot imports it from there now."""
    assert rupees(21900) == "₹219.00"


# ---------------------------------------------------------------------------
# Period bucketing (invariant 6)
# ---------------------------------------------------------------------------


def test_months_bucket_on_the_local_date_not_created_at(
    seeded: tuple[Session, User],
) -> None:
    """A July receipt photographed in August belongs to July. Invariant 6.

    Both rows are written now, so `created_at` is identical for both. Only
    `occurred_on_local` separates them, which is the whole point.
    """
    session, user = seeded
    store(session, user, on=dt.date(2026, 7, 31))
    store(session, user, on=dt.date(2026, 8, 1), grand_total_minor=50000)

    buckets = {
        b.label: b.total_minor
        for b in data.monthly_totals(session, user_id=user.id, months=3, today=TODAY)
    }
    assert buckets["Jul"] == 21900
    assert buckets["Aug"] == 50000


def test_empty_months_are_kept(seeded: tuple[Session, User]) -> None:
    """Dropping empty months would make the chart lie about time."""
    session, user = seeded
    store(session, user, on=dt.date(2026, 8, 1))
    buckets = data.monthly_totals(session, user_id=user.id, months=6, today=TODAY)
    assert len(buckets) == 6
    assert [b.label for b in buckets] == ["Mar", "Apr", "May", "Jun", "Jul", "Aug"]
    assert sum(1 for b in buckets if b.total_minor == 0) == 5


def test_the_chart_runs_oldest_to_newest(seeded: tuple[Session, User]) -> None:
    session, user = seeded
    buckets = data.monthly_totals(session, user_id=user.id, months=3, today=TODAY)
    assert [b.label for b in buckets] == ["Jun", "Jul", "Aug"]


@pytest.mark.parametrize(
    ("day", "start", "end"),
    [
        (dt.date(2026, 8, 8), dt.date(2026, 8, 1), dt.date(2026, 8, 31)),
        (dt.date(2026, 2, 14), dt.date(2026, 2, 1), dt.date(2026, 2, 28)),
        (dt.date(2024, 2, 14), dt.date(2024, 2, 1), dt.date(2024, 2, 29)),
        (dt.date(2026, 12, 31), dt.date(2026, 12, 1), dt.date(2026, 12, 31)),
    ],
)
def test_month_bounds(day: dt.date, start: dt.date, end: dt.date) -> None:
    assert data.month_bounds(day) == (start, end)


def test_december_rolls_into_january(seeded: tuple[Session, User]) -> None:
    """The month arithmetic has to survive a year boundary."""
    session, user = seeded
    store(session, user, on=dt.date(2025, 12, 20))
    buckets = data.monthly_totals(session, user_id=user.id, months=2, today=dt.date(2026, 1, 15))
    assert [b.label for b in buckets] == ["Dec", "Jan"]
    assert buckets[0].total_minor == 21900


# ---------------------------------------------------------------------------
# Soft deletes (invariant 2)
# ---------------------------------------------------------------------------


def test_a_soft_deleted_receipt_leaves_every_total(seeded: tuple[Session, User]) -> None:
    session, user = seeded
    keep = store(session, user, on=TODAY)
    drop = store(session, user, on=TODAY, grand_total_minor=99900)

    before = data.overview(session, user_id=user.id, today=TODAY)
    assert before.receipt_count == 2

    ledger.soft_delete_transaction(session, transaction=drop, when=NOW)

    after = data.overview(session, user_id=user.id, today=TODAY)
    assert after.receipt_count == 1
    assert after.total_minor == keep.grand_total_minor
    assert [r.id for r in data.recent(session, user_id=user.id)] == [keep.id]
    assert data.receipt(session, user_id=user.id, transaction_id=drop.id) is None


def test_a_soft_deleted_row_is_still_in_the_table(seeded: tuple[Session, User]) -> None:
    """Invariant 2: deletes are soft. The dashboard hides it, the ledger keeps it."""
    session, user = seeded
    dropped = store(session, user, on=TODAY)
    ledger.soft_delete_transaction(session, transaction=dropped, when=NOW)
    session.flush()
    assert session.get(Transaction, dropped.id) is not None


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


def test_the_month_over_month_delta(seeded: tuple[Session, User]) -> None:
    session, user = seeded
    store(session, user, on=dt.date(2026, 7, 10), grand_total_minor=20000)
    store(session, user, on=dt.date(2026, 8, 2), grand_total_minor=30000)

    overview = data.overview(session, user_id=user.id, today=TODAY)
    assert overview.total_minor == 30000
    assert overview.previous_total_minor == 20000
    assert overview.delta_minor == 10000
    assert overview.delta_pct == pytest.approx(50.0)


def test_no_previous_month_is_none_not_zero(seeded: tuple[Session, User]) -> None:
    """ "No data" and "no change" are different facts about money."""
    session, user = seeded
    store(session, user, on=TODAY)
    assert data.overview(session, user_id=user.id, today=TODAY).delta_pct is None


def test_an_empty_ledger_does_not_divide_by_zero(seeded: tuple[Session, User]) -> None:
    session, user = seeded
    overview = data.overview(session, user_id=user.id, today=TODAY)
    assert overview.total_minor == 0
    assert overview.receipt_count == 0
    assert overview.average_minor == 0
    assert overview.delta_pct is None


def test_api_spend_is_summed_from_raw_extractions(seeded: tuple[Session, User]) -> None:
    session, user = seeded
    store(session, user, on=TODAY)
    store(session, user, on=TODAY, grand_total_minor=30000)
    assert data.api_spend_micros(session, user_id=user.id) == 46_302


def test_api_spend_survives_a_soft_deleted_transaction(
    seeded: tuple[Session, User],
) -> None:
    """Invariant 5: `raw_extractions` is immutable. Deleting a receipt does not
    un-spend the money the extraction cost."""
    session, user = seeded
    txn = store(session, user, on=TODAY)
    ledger.soft_delete_transaction(session, transaction=txn, when=NOW)
    assert data.api_spend_micros(session, user_id=user.id) == 23_151


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


def test_everything_is_uncategorized_until_enrichment_exists(
    seeded: tuple[Session, User],
) -> None:
    """Commit 9 fills `category_id`. Until then the page says so."""
    session, user = seeded
    store(session, user, on=TODAY)
    start, end = data.month_bounds(TODAY)
    slices = data.category_totals(session, user_id=user.id, start=start, end=end)
    assert [s.name for s in slices] == [data.UNCATEGORIZED]
    assert slices[0].share == pytest.approx(1.0)


def test_category_slices_sum_to_the_basket_not_the_grand_total(
    seeded: tuple[Session, User],
) -> None:
    """Charges and taxes sit outside the line items, so they are outside the
    slices too. The page states this rather than showing a number that does not
    reconcile."""
    session, user = seeded
    txn = store(session, user, on=TODAY)
    start, end = data.month_bounds(TODAY)
    slices = data.category_totals(session, user_id=user.id, start=start, end=end)

    lines = session.scalars(
        TransactionLineItem.__table__.select().with_only_columns(
            TransactionLineItem.line_total_minor
        )
    ).all()
    assert sum(s.total_minor for s in slices) == sum(lines)
    assert sum(s.total_minor for s in slices) != txn.grand_total_minor


def test_categories_respect_the_period(seeded: tuple[Session, User]) -> None:
    session, user = seeded
    store(session, user, on=dt.date(2026, 7, 1))
    start, end = data.month_bounds(TODAY)
    assert data.category_totals(session, user_id=user.id, start=start, end=end) == []


# ---------------------------------------------------------------------------
# Receipt detail
# ---------------------------------------------------------------------------


def test_a_receipt_carries_its_lines_and_adjustments(
    seeded: tuple[Session, User],
) -> None:
    session, user = seeded
    txn = store(session, user, on=TODAY)
    found = data.receipt(session, user_id=user.id, transaction_id=txn.id)

    assert found is not None
    assert found.row.grand_total_minor == 21900
    assert len(found.lines) > 0
    assert found.line_subtotal_minor == sum(line.line_total_minor for line in found.lines)
    assert [line.position for line in found.lines] == sorted(line.position for line in found.lines)


def test_another_users_receipt_is_not_returned(seeded: tuple[Session, User]) -> None:
    """The user_id filter is a security boundary, not an optimisation."""
    session, user = seeded
    txn = store(session, user, on=TODAY)
    assert data.receipt(session, user_id="someone-else", transaction_id=txn.id) is None


def test_an_unknown_receipt_id_returns_none(seeded: tuple[Session, User]) -> None:
    session, user = seeded
    assert data.receipt(session, user_id=user.id, transaction_id="not-a-real-id") is None


def test_saved_is_mrp_minus_paid(seeded: tuple[Session, User]) -> None:
    """Brief 24.3, on the detail page."""
    session, user = seeded
    txn = store(session, user, on=TODAY)
    found = data.receipt(session, user_id=user.id, transaction_id=txn.id)
    assert found is not None
    for line in found.lines:
        if line.mrp_minor is None:
            assert line.saved_minor is None
        else:
            assert line.saved_minor == line.mrp_minor - line.line_total_minor


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------


def test_a_class_2_row_is_flagged(seeded: tuple[Session, User]) -> None:
    """The live Zepto receipt's shape: a gap the gate could not close."""
    session, user = seeded
    off = store(session, user, on=TODAY, grand_total_minor=31900)
    assert off.reconciliation_outcome is ReconciliationOutcome.CLASS_2

    flagged = data.flagged(session, user_id=user.id)
    assert [r.id for r in flagged] == [off.id]
    assert flagged[0].unaccounted_adjustment_minor != 0
    assert data.overview(session, user_id=user.id, today=TODAY).flagged_count == 1


def test_a_balanced_row_is_not_flagged(seeded: tuple[Session, User]) -> None:
    session, user = seeded
    store(session, user, on=TODAY)
    assert data.flagged(session, user_id=user.id) == []


# ---------------------------------------------------------------------------
# Rendering, and escaping in particular
# ---------------------------------------------------------------------------


def render_index(session: Session, user: User, *, today: dt.date = TODAY) -> str:
    start, end = data.month_bounds(today)
    return render.dashboard(
        overview=data.overview(session, user_id=user.id, today=today),
        buckets=data.monthly_totals(session, user_id=user.id, months=6, today=today),
        slices=data.category_totals(session, user_id=user.id, start=start, end=end),
        rows=data.recent(session, user_id=user.id),
        generated_at=NOW,
    )


def test_the_page_is_a_complete_document(seeded: tuple[Session, User]) -> None:
    session, user = seeded
    store(session, user, on=TODAY)
    html = render_index(session, user)
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")
    assert "<title>Raseed</title>" in html


def test_the_page_is_self_contained(seeded: tuple[Session, User]) -> None:
    """It has to render over a tunnel on a phone with no CDN."""
    session, user = seeded
    store(session, user, on=TODAY)
    html = render_index(session, user)
    assert "<script" not in html.lower()
    for scheme in ("http://", "https://"):
        assert scheme not in html


def test_an_empty_ledger_renders(seeded: tuple[Session, User]) -> None:
    session, user = seeded
    html = render_index(session, user)
    assert "Nothing logged yet" in html


def test_extracted_text_is_escaped(seeded: tuple[Session, User]) -> None:
    """Every string on this page came from a model reading a stranger's image."""
    session, user = seeded
    txn = store(session, user, on=TODAY)

    hostile = '<script>alert("xss")</script>'
    item = session.scalars(
        TransactionLineItem.__table__.select().with_only_columns(TransactionLineItem.id).limit(1)
    ).first()
    row = session.get(TransactionLineItem, item)
    assert row is not None
    row.raw_name = hostile
    session.flush()

    found = data.receipt(session, user_id=user.id, transaction_id=txn.id)
    assert found is not None
    html = render.receipt_page(found, generated_at=NOW)

    assert hostile not in html
    assert "&lt;script&gt;" in html


def test_an_adjustment_label_is_escaped(seeded: tuple[Session, User]) -> None:
    session, user = seeded
    txn = store(session, user, on=TODAY)
    adjustment = session.scalars(
        TransactionAdjustment.__table__.select()
        .with_only_columns(TransactionAdjustment.id)
        .limit(1)
    ).first()
    row = session.get(TransactionAdjustment, adjustment)
    assert row is not None
    row.label = "<img src=x onerror=alert(1)>"
    session.flush()

    found = data.receipt(session, user_id=user.id, transaction_id=txn.id)
    assert found is not None
    html = render.receipt_page(found, generated_at=NOW)
    assert "<img src=x" not in html
    assert "&lt;img" in html


def test_the_gap_is_shown_not_hidden(seeded: tuple[Session, User]) -> None:
    session, user = seeded
    txn = store(session, user, on=TODAY, grand_total_minor=31900)
    found = data.receipt(session, user_id=user.id, transaction_id=txn.id)
    assert found is not None
    html = render.receipt_page(found, generated_at=NOW)
    assert "arithmetic is off" in html
    assert "subtracted twice" in html


def test_a_guessed_date_says_so(seeded: tuple[Session, User]) -> None:
    """Brief 24.4: never guess a date without recording that you guessed."""
    session, user = seeded
    txn = store(session, user, on=TODAY)
    txn.date_source = DateSource.MESSAGE_TIMESTAMP
    session.flush()
    html = render_index(session, user)
    assert "date from the message, not the receipt" in html


def test_the_tabs_come_from_the_category_table(seeded: tuple[Session, User]) -> None:
    """Brief section 2: tabs grow from the data, not from a hardcoded list."""
    session, user = seeded
    store(session, user, on=TODAY)
    start, end = data.month_bounds(TODAY)
    entries = data.tabs(session, user_id=user.id, start=start, end=end)

    assert entries[0].slug is None, "All comes first and is the default landing state"
    assert entries[0].name == "All"
    slugs = [e.slug for e in entries[1:]]
    assert slugs == [slug for slug, _ in SEED_CATEGORIES]


def test_an_empty_tab_stays_in_place(seeded: tuple[Session, User]) -> None:
    """Greyed out rather than hidden, so the nav does not reshuffle monthly."""
    session, user = seeded
    store(session, user, on=TODAY)
    start, end = data.month_bounds(TODAY)
    entries = data.tabs(session, user_id=user.id, start=start, end=end)

    empty = [e for e in entries if e.empty]
    assert empty, "this fixture has categories with no spend"
    assert all(e.slug in {slug for slug, _ in SEED_CATEGORIES} for e in empty)


def test_a_category_tab_totals_line_items_not_grand_totals(
    seeded: tuple[Session, User],
) -> None:
    """The bug this guards against would inflate spend past what was paid.

    A receipt's grand total includes delivery, tax and order-level discounts.
    Those belong to no category, so totalling grand totals under a category
    filter reports more money than actually left the account.
    """
    session, user = seeded
    store(session, user, on=TODAY)

    rows = data.recent(session, user_id=user.id, category_slug="uncategorized")
    assert rows, "the fixture's line items are uncategorized"

    for row in rows:
        assert row.category_minor is not None
        assert row.amount_minor == row.category_minor
        assert row.amount_minor <= row.grand_total_minor

    scoped = data.overview(session, user_id=user.id, today=TODAY, category_slug="uncategorized")
    unscoped = data.overview(session, user_id=user.id, today=TODAY)
    assert scoped.total_minor <= unscoped.total_minor


def test_an_unknown_tab_falls_back_to_all(seeded: tuple[Session, User]) -> None:
    """A bad slug must not 404 and must not leak which slugs are real."""
    session, user = seeded
    store(session, user, on=TODAY)
    rows = data.recent(session, user_id=user.id, category_slug="no-such-category")
    assert rows == []


def _overview(**kwargs: object) -> data.Overview:
    defaults: dict[str, object] = {
        "period_label": "August 2026",
        "total_minor": 100_00,
        "receipt_count": 1,
        "previous_total_minor": 0,
        "average_minor": 100_00,
        "flagged_count": 0,
        "api_spend_micros": 0,
    }
    defaults.update(kwargs)
    return data.Overview(**defaults)  # type: ignore[arg-type]


def test_a_long_category_list_folds_into_other() -> None:
    """Past the limit the list stops ranking and starts being a wall."""
    slices = [
        data.Slice(name=f"Category {index}", total_minor=(20 - index) * 100, share=0.05)
        for index in range(12)
    ]
    html = render.dashboard(
        overview=_overview(),
        buckets=[data.Bucket("Aug", 100_00, 1)],
        slices=slices,
        rows=[],
        generated_at=NOW,
    )
    kept = render.CATEGORY_LIMIT
    assert "Category 0" in html
    assert f"Category {kept - 1}" in html
    # The tail is summarised, not silently dropped: the count says how many.
    assert f"Other ({len(slices) - kept})" in html
    assert f"Category {kept}" not in html


def test_a_month_with_no_spend_is_drawn_as_empty_not_as_small() -> None:
    """A one-pixel bar reads as a small amount. Zero is not a small amount."""
    buckets = [
        data.Bucket("Jul", 0, 0),
        data.Bucket("Aug", 500_00, 3),
    ]
    html = render.dashboard(
        overview=_overview(),
        buckets=buckets,
        slices=[],
        rows=[],
        generated_at=NOW,
    )
    assert "Jul: nothing recorded" in html
    assert "Aug: ₹500.00, 3 receipts" in html


def test_the_shell_carries_its_refusal_in_markup_not_in_script() -> None:
    """The old boot script assigned to `document.body.textContent` on failure.

    That threw the stylesheet away with the rest of the document and left a bare
    sentence on a blank page, which is what a desktop browser always got. The
    refusal is now real markup that is merely unhidden.
    """
    body = render.shell()
    # The specific thing that broke: assigning to the body's text threw away the
    # stylesheet with the rest of the document. Writing text into a single
    # element is fine and is the safe way to place an untrusted string.
    assert "body.textContent" not in body
    assert "document.write" not in body, "replacing the document loses its listeners"
    head, _, tail = body.partition("<script>")
    assert "Open this from the Raseed bot in Telegram." in head
    assert "Open this from the Raseed bot in Telegram." not in tail


def test_every_page_offers_the_boot_script_something_to_swap() -> None:
    """Navigation replaces `#content` in place, so every page must carry one.

    Without this the boot script has nothing to put on screen and the tap looks
    like it did nothing at all.
    """
    pages = {
        "dashboard": render.dashboard(
            overview=_overview(),
            buckets=[data.Bucket("Aug", 100_00, 1)],
            slices=[],
            rows=[],
            generated_at=NOW,
            tabs=[data.Tab(slug=None, name="All", total_minor=100_00)],
        ),
        "not_found": render.not_found(),
    }
    for name, html in pages.items():
        assert 'id="content"' in html, f"{name} has nothing for the boot script to swap"
        assert 'id="chrome"' in html, f"{name} is missing its persistent chrome"


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------


@pytest.fixture
def live(tmp_path: Path) -> Iterator[str]:
    """A real HTTP server on a real loopback socket, on an ephemeral port.

    File-backed rather than in-memory, because the handler runs on a different
    thread than the test and an in-memory SQLite database belongs to exactly one
    connection. That is a property of the test harness, not of the server: in
    production the bot and the dashboard share one file for the same reason.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    with factory() as session:
        user = bootstrap(session, user_id_for(VISITOR_ID, secret=SECRET))
        store(session, user, on=TODAY)
        session.commit()

    dashboard = Dashboard(
        session_factory=factory,
        clock=lambda: NOW,
        bot_token=BOT_TOKEN,
        user_id_secret=SECRET,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(dashboard))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        engine.dispose()


def signed_init_data(
    telegram_id: int = VISITOR_ID, *, token: str = BOT_TOKEN, auth_date: int | None = None
) -> str:
    """A Telegram `initData` blob signed the way Telegram signs one.

    Built here rather than pasted from a real session, so the tests exercise the
    real verification path and nothing has to be stubbed out.
    """
    fields = {
        "auth_date": str(auth_date if auth_date is not None else int(NOW.timestamp())),
        "query_id": "AAF",
        "user": json.dumps({"id": telegram_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret_key, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def authorised(url: str, init_data: str | None = None) -> Request:
    return Request(url, headers={"Authorization": f"tma {init_data or signed_init_data()}"})


def test_health_answers(live: str) -> None:
    with urlopen(f"{live}/health") as response:
        assert response.status == HTTPStatus.OK
        assert response.read() == b"ok"


def test_the_shell_carries_no_ledger_data(live: str) -> None:
    """`/` is the only unauthenticated page, so it must contain nothing."""
    with urlopen(f"{live}/") as response:
        body = response.read().decode("utf-8")
    assert response.status == HTTPStatus.OK
    assert "Raseed" in body
    assert "₹" not in body
    assert "219" not in body


def test_the_dashboard_renders_for_a_signed_visitor(live: str) -> None:
    with urlopen(authorised(f"{live}/app")) as response:
        body = response.read().decode("utf-8")
    assert response.status == HTTPStatus.OK
    assert "₹219.00" in body


def test_the_dashboard_refuses_writes(live: str) -> None:
    """Read only by construction. A POST is refused, not quietly 404'd."""
    with pytest.raises(HTTPError) as caught:
        urlopen(Request(f"{live}/", data=b"{}", method="POST"))
    assert caught.value.code == HTTPStatus.METHOD_NOT_ALLOWED


def test_security_headers_are_set(live: str) -> None:
    with urlopen(f"{live}/") as response:
        headers = dict(response.headers)
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "private" in headers["Cache-Control"]

    policy = headers["Content-Security-Policy"]
    assert "default-src 'none'" in policy
    # Named ancestors rather than DENY: a Mini App lives in Telegram's frame,
    # and only Telegram's.
    assert "frame-ancestors https://web.telegram.org https://telegram.org" in policy
    assert "form-action 'none'" in policy


# ---------------------------------------------------------------------------
# Authentication (Telegram Mini App initData)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/app", "/receipt/anything", "/nope"])
def test_every_page_but_the_shell_needs_proof(live: str, path: str) -> None:
    """Including an unknown path. A stranger cannot map the routes by 404s."""
    with pytest.raises(HTTPError) as caught:
        urlopen(f"{live}{path}")
    assert caught.value.code == HTTPStatus.UNAUTHORIZED


def test_a_blob_signed_with_the_wrong_token_is_refused(live: str) -> None:
    """The signature is over the bot token. Anyone else's bot proves nothing."""
    forged = signed_init_data(token="999:someone-elses-bot")
    with pytest.raises(HTTPError) as caught:
        urlopen(authorised(f"{live}/app", forged))
    assert caught.value.code == HTTPStatus.UNAUTHORIZED


def test_a_tampered_blob_is_refused(live: str) -> None:
    """Changing the user ID after signing must not survive."""
    tampered = signed_init_data().replace("4242", "9999")
    with pytest.raises(HTTPError) as caught:
        urlopen(authorised(f"{live}/app", tampered))
    assert caught.value.code == HTTPStatus.UNAUTHORIZED


def test_a_stale_blob_is_refused(live: str) -> None:
    old = signed_init_data(auth_date=int(NOW.timestamp()) - 7200)
    with pytest.raises(HTTPError) as caught:
        urlopen(authorised(f"{live}/app", old))
    assert caught.value.code == HTTPStatus.UNAUTHORIZED


def test_the_wrong_scheme_is_refused(live: str) -> None:
    request = Request(f"{live}/app", headers={"Authorization": f"Bearer {signed_init_data()}"})
    with pytest.raises(HTTPError) as caught:
        urlopen(request)
    assert caught.value.code == HTTPStatus.UNAUTHORIZED


def test_the_refusal_says_nothing(live: str) -> None:
    """It must not confirm the ledger exists or say which check failed."""
    with pytest.raises(HTTPError) as caught:
        urlopen(f"{live}/app")
    body = caught.value.read().decode("utf-8")
    assert "Open this from the Raseed bot" in body
    assert "₹" not in body
    assert "signature" not in body.lower()


def test_health_needs_no_proof(live: str) -> None:
    """The tunnel and any uptime check need it, and it reveals nothing."""
    with urlopen(f"{live}/health") as response:
        assert response.read() == b"ok"


# ---------------------------------------------------------------------------
# One user cannot read another's ledger
# ---------------------------------------------------------------------------


def test_a_stranger_gets_their_own_empty_dashboard(live: str) -> None:
    """The bug this whole change exists to prevent.

    Before per-user IDs the dashboard rendered "the first user in the table",
    so the second person to open the link would have seen the owner's spending.
    """
    stranger = signed_init_data(STRANGER_ID)
    with urlopen(authorised(f"{live}/app", stranger)) as response:
        body = response.read().decode("utf-8")
    assert "₹219.00" not in body
    assert "₹0.00" in body


def test_a_stranger_cannot_open_someone_elses_receipt(live: str) -> None:
    """Guessing a transaction ID is not enough, because the query is scoped."""
    with urlopen(authorised(f"{live}/app")) as response:
        mine = response.read().decode("utf-8")
    transaction_id = mine.split('href="/receipt/', 1)[1].split('"', 1)[0]

    with pytest.raises(HTTPError) as caught:
        urlopen(authorised(f"{live}/receipt/{transaction_id}", signed_init_data(STRANGER_ID)))
    assert caught.value.code == HTTPStatus.NOT_FOUND


def test_the_same_account_always_lands_on_the_same_ledger() -> None:
    """Derived, not stored, so it has to be a pure function of the account."""
    assert user_id_for(VISITOR_ID, secret=SECRET) == user_id_for(VISITOR_ID, secret=SECRET)
    assert user_id_for(VISITOR_ID, secret=SECRET) != user_id_for(STRANGER_ID, secret=SECRET)


def test_a_different_secret_is_a_different_ledger() -> None:
    """The stated cost of not storing the identifier. See raseed/identity.py."""
    assert user_id_for(VISITOR_ID, secret=SECRET) != user_id_for(VISITOR_ID, secret="x" * 32)


def test_the_derived_id_does_not_contain_the_account_number() -> None:
    """The whole reason it is an HMAC and not a formatted string."""
    assert str(VISITOR_ID) not in user_id_for(VISITOR_ID, secret=SECRET)
