"""Render the dashboard to static files, so it can be looked at without Telegram.

The dashboard is only reachable through a Telegram Mini App: every route except
`/` demands a signed `initData` blob, and Telegram will not open a Mini App at
`127.0.0.1` or over plain HTTP. That is the right security posture and it is not
being softened here. It does mean that opening `http://127.0.0.1:8770` in a
desktop browser correctly shows the "open this from the bot" card and nothing
else, which is useless when the thing you want to do is look at the design.

So this renders the same pages, with the same functions, straight to disk. No
authentication is involved because no server is involved. Nothing here is
imported by the application, no route is added, and no bypass exists in the
shipped code: if this file were deleted the dashboard would be unchanged.

    .venv\\Scripts\\python tools\\preview_dashboard.py
    .venv\\Scripts\\python tools\\preview_dashboard.py --demo   # invented data

`--demo` fabricates a ledger in memory so the layout can be judged with six
months of history and a full category breakdown. It touches no database and
writes nothing but HTML. Without it, your real ledger is read **read-only**.

Links between the generated pages are rewritten to relative filenames, so the
category tabs and the receipt drill-down are both clickable from the filesystem
exactly as they are over the tunnel.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import random
import re
import sys
import webbrowser

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from raseed.config import Settings
from raseed.db.engine import create_engine
from raseed.db.models import (
    AdjustmentKind,
    ReconciliationOutcome,
    Transaction,
)
from raseed.timezones import zone
from raseed.web import data, render

#: Where the files land. Inside `data/`, which is gitignored, because these
#: pages contain a real ledger.
OUT: pathlib.Path = pathlib.Path("data/preview")


def _rewrite(html: str) -> str:
    """Point in-page links at the generated files instead of at server routes."""
    html = re.sub(r'href="/receipt/([^"]+)"', r'href="receipt-\1.html"', html)
    html = re.sub(r'href="/app\?tab=([^"]+)"', r'href="tab-\1.html"', html)
    html = html.replace('href="/app"', 'href="index.html"')
    return html.replace('href="/"', 'href="index.html"')


def _write(path: pathlib.Path, html: str) -> None:
    path.write_text(_rewrite(html), encoding="utf-8")
    print(f"  {path}")


def _from_database(out: pathlib.Path) -> int:
    settings = Settings.from_env()
    engine = create_engine(settings.database_url)
    sessions = sessionmaker(bind=engine)

    # "Today" has to be today *where the receipts were bought*, not where this
    # script is running. The ledger buckets on `occurred_on_local` (invariant 6),
    # so taking the month boundary off a machine clock in another timezone would
    # put the current month's receipts in the wrong bucket.
    now = dt.datetime.now(zone(settings.default_timezone))
    today = now.date()

    with sessions() as session:
        user_ids = list(session.scalars(select(Transaction.user_id).distinct()))
        if not user_ids:
            print("No transactions in the ledger. Try --demo to see the layout.")
            return 0
        if len(user_ids) > 1:
            print(f"note: {len(user_ids)} users in the ledger, previewing the first")
        user_id = user_ids[0]

        start, end = data.month_bounds(today)
        entries = data.tabs(session, user_id=user_id, start=start, end=end)

        # One file per tab, so the nav is clickable from the filesystem exactly
        # as it is over the tunnel.
        for entry in entries:
            slug = entry.slug
            slices = (
                data.top_items(session, user_id=user_id, start=start, end=end, category_slug=slug)
                if slug is not None
                else data.category_totals(session, user_id=user_id, start=start, end=end)
            )
            page = render.dashboard(
                overview=data.overview(session, user_id=user_id, today=today, category_slug=slug),
                buckets=data.monthly_totals(
                    session, user_id=user_id, months=6, today=today, category_slug=slug
                ),
                slices=slices,
                rows=data.recent(session, user_id=user_id, limit=50, category_slug=slug),
                generated_at=now,
                tabs=entries,
                active_tab=slug,
                active_tab_name=entry.name if slug is not None else None,
            )
            _write(out / ("index.html" if slug is None else f"tab-{slug}.html"), page)

        rows = data.recent(session, user_id=user_id, limit=50)
        for row in rows:
            found = data.receipt(session, user_id=user_id, transaction_id=row.id)
            if found is not None:
                _write(out / f"receipt-{row.id}.html", render.receipt_page(found, generated_at=now))
        return len(rows)


# ---------------------------------------------------------------------------
# Invented data, for judging the layout at a realistic size
# ---------------------------------------------------------------------------

_CATEGORIES = [
    ("Groceries", 41),
    ("Snacks and drinks", 18),
    ("Household", 13),
    ("Dairy and eggs", 11),
    ("Fruit and vegetables", 8),
    ("Personal care", 5),
    ("Baby care", 2),
    ("Stationery", 1),
    ("Pet supplies", 1),
]

#: (name, total_minor, times bought) for the filtered-tab preview.
_DEMO_ITEMS = (
    ("Haldiram's Moong Dal | Crispy Fried Lentil Snack", 24000, 3),
    ("Parle-G Gold Biscuit", 13400, 4),
    ("Fresh White Eggs", 11000, 2),
    ("Godrej Jersey Curd Tub", 8200, 1),
)
_ITEM_SUM = sum(total for _, total, _ in _DEMO_ITEMS)

_ITEMS = [
    ("Nandini Toned Fresh Milk | Pouch", "500 ml", 4800, 4140),
    ("Fresh White Eggs", "6 pieces", 14500, 11000),
    ("Haldiram's Moong Dal | Crispy Fried Lentil Snack", "200 g", 24000, 24000),
    ("Parle-G Gold Biscuit", "100 g", 16000, 13400),
    ("Godrej Jersey Curd Tub", "400 g", 11000, 8200),
    ("English Oven Milk Bread", "350 g", 6000, 5400),
    ("Tata Salt Iodised", "1 kg", 2800, 2800),
]


def _demo(out: pathlib.Path) -> int:
    rng = random.Random(7)
    now = dt.datetime.now(dt.UTC).astimezone()
    today = now.date()

    buckets = []
    cursor = today.replace(day=1)
    for index in range(6):
        empty = index == 4
        total = 0 if empty else rng.randint(180_00, 940_00)
        count = 0 if empty else rng.randint(2, 9)
        buckets.append(data.Bucket(cursor.strftime("%b"), total, count))
        cursor = data.previous_month(cursor).replace(day=1)
    buckets.reverse()

    month_total = buckets[-1].total_minor
    month_count = buckets[-1].count

    slices = []
    for name, weight in _CATEGORIES:
        slices.append(data.Slice(name, round(month_total * weight / 100), weight / 100))

    rows = []
    for index in range(month_count):
        flagged = index == 1
        rows.append(
            data.Row(
                id=f"demo-{index:03d}",
                occurred_on_local=today - dt.timedelta(days=index * 3),
                grand_total_minor=rng.randint(120_00, 900_00),
                currency="INR",
                outcome=(
                    ReconciliationOutcome.CLASS_2 if flagged else ReconciliationOutcome.BALANCED
                ),
                unaccounted_adjustment_minor=100_00 if flagged else 0,
                date_source="PRINTED_ON_RECEIPT" if index else "MESSAGE_TIMESTAMP",
                item_count=rng.randint(3, 12),
            )
        )

    overview = data.Overview(
        period_label=data.month_label(today),
        total_minor=month_total,
        receipt_count=month_count,
        previous_total_minor=buckets[-2].total_minor,
        average_minor=round(month_total / month_count) if month_count else 0,
        flagged_count=sum(1 for r in rows if r.flagged),
        api_spend_micros=int(len(rows) * 24_600),
    )

    entries = [data.Tab(slug=None, name="All", total_minor=month_total)]
    for name, weight in _CATEGORIES[:5]:
        slug = name.lower().replace(" & ", "-and-").replace(" ", "-")
        entries.append(
            data.Tab(slug=slug, name=name, total_minor=round(month_total * weight / 100))
        )
    entries.append(data.Tab(slug="entertainment", name="Entertainment", total_minor=0))

    _write(
        out / "index.html",
        render.dashboard(
            overview=overview,
            buckets=buckets,
            slices=slices,
            rows=rows,
            generated_at=now,
            tabs=entries,
        ),
    )

    # One filtered page, so the tab state can be judged as well as the nav.
    groceries = entries[1]
    _write(
        out / f"tab-{groceries.slug}.html",
        render.dashboard(
            overview=data.Overview(
                period_label=data.month_label(today),
                total_minor=groceries.total_minor,
                receipt_count=month_count,
                previous_total_minor=round(groceries.total_minor * 1.18),
                average_minor=round(groceries.total_minor / month_count) if month_count else 0,
                flagged_count=0,
                api_spend_micros=overview.api_spend_micros,
            ),
            buckets=[data.Bucket(b.label, round(b.total_minor * 0.41), b.count) for b in buckets],
            # Shares are a proportion of these items' own sum, which is what
            # `data.top_items` computes. Inventing them independently would put
            # a chart on screen whose bars do not add up.
            slices=[
                data.Slice(name, total, total / _ITEM_SUM, count)
                for name, total, count in _DEMO_ITEMS
            ],
            rows=[
                data.Row(
                    id=row.id,
                    occurred_on_local=row.occurred_on_local,
                    grand_total_minor=row.grand_total_minor,
                    currency=row.currency,
                    outcome=row.outcome,
                    unaccounted_adjustment_minor=row.unaccounted_adjustment_minor,
                    date_source=row.date_source,
                    item_count=row.item_count,
                    category_minor=round(row.grand_total_minor * 0.41),
                )
                for row in rows
            ],
            generated_at=now,
            tabs=entries,
            active_tab=groceries.slug,
            active_tab_name=groceries.name,
        ),
    )

    for row in rows:
        lines = [
            data.LineDetail(
                position=position,
                raw_name=name,
                quantity_text=quantity,
                mrp_minor=mrp,
                line_total_minor=paid,
                category=_CATEGORIES[position % len(_CATEGORIES)][0],
            )
            for position, (name, quantity, mrp, paid) in enumerate(_ITEMS[: row.item_count])
        ]
        adjustments = [
            data.AdjustmentDetail(AdjustmentKind.CHARGE, "Delivery Fee", 2500),
            data.AdjustmentDetail(AdjustmentKind.TAX, "GST", 1800),
        ]
        if row.flagged:
            adjustments.append(
                data.AdjustmentDetail(AdjustmentKind.DISCOUNT, "ZEPINDCC100 Offer Applied", 10000)
            )
        subtotal = sum(line.line_total_minor for line in lines)
        receipt = data.Receipt(
            row=data.Row(
                id=row.id,
                occurred_on_local=row.occurred_on_local,
                grand_total_minor=subtotal + 2500 + 1800 - (10000 if row.flagged else 0),
                currency="INR",
                outcome=row.outcome,
                unaccounted_adjustment_minor=row.unaccounted_adjustment_minor,
                date_source=row.date_source,
                item_count=len(lines),
            ),
            lines=lines,
            adjustments=adjustments,
            order_id=f"ZEP{rng.randint(10**9, 10**10 - 1)}",
            source="TELEGRAM_PHOTO",
        )
        _write(out / f"receipt-{row.id}.html", render.receipt_page(receipt, generated_at=now))

    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--demo",
        action="store_true",
        help="render invented data instead of the ledger, to judge the layout",
    )
    parser.add_argument("--out", type=pathlib.Path, default=OUT)
    parser.add_argument("--open", action="store_true", help="open the result in a browser")
    args = parser.parse_args()

    out: pathlib.Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    print(f"Rendering to {out.resolve()}")
    count = _demo(out) if args.demo else _from_database(out)
    if not count and not args.demo:
        return 0

    index = (out / "index.html").resolve()
    print(f"\n{count} receipt page{'s' if count != 1 else ''} plus the dashboard.")
    print(f"Open: {index.as_uri()}")
    if args.open:
        webbrowser.open(index.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
