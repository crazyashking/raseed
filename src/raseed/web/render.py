"""HTML for the dashboard, generated as text.

No template engine. Jinja2 is on the allowlist but is not installed (it belongs
to `tools/render_receipts.py`), and pulling it in for four pages would be a
dependency bought with nothing. `html.escape` on every interpolation does the
one job a template engine would be doing for us here.

**Every value that came from a model or a receipt is escaped.** A merchant name,
a line item, a rejection reason: all of it is text a vision model produced from
an image a stranger could have crafted. Brief 21.4 treats extracted text as
untrusted input to the prompt; it is equally untrusted as input to a page.

The CSS is inline and the page is self-contained. It has to work over a tunnel
on a phone with no CDN, and a dashboard that needs the network to render is a
dashboard that fails exactly when you are standing in a shop wondering what you
spent.
"""

from __future__ import annotations

import datetime as dt
from html import escape

from raseed.db.models import AdjustmentKind, DateSource
from raseed.money import money
from raseed.web import data

#: Bar chart height in pixels. Small enough for a phone, tall enough to read.
CHART_HEIGHT: int = 120

STYLE: str = """
:root {
  --bg: #f6f6f4; --card: #ffffff; --ink: #16150f; --muted: #6c6a5f;
  --line: #e2e0d8; --accent: #1f6f4a; --warn: #a8571b; --warn-bg: #fdf3e7;
  --bar: #cfd8d1; --bar-live: #1f6f4a;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #14150f; --card: #1d1e17; --ink: #f0efe6; --muted: #9d9b8e;
    --line: #2e2f26; --accent: #6ec295; --warn: #e0a35f; --warn-bg: #2a2116;
    --bar: #33352a; --bar-live: #6ec295;
  }
}
:root[data-theme="dark"] {
  --bg: #14150f; --card: #1d1e17; --ink: #f0efe6; --muted: #9d9b8e;
  --line: #2e2f26; --accent: #6ec295; --warn: #e0a35f; --warn-bg: #2a2116;
  --bar: #33352a; --bar-live: #6ec295;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  padding: 16px; max-width: 780px; margin-inline: auto;
}
a { color: inherit; }
header { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
h1 { font-size: 17px; margin: 0; letter-spacing: .02em; }
h2 { font-size: 13px; margin: 28px 0 10px; color: var(--muted);
     text-transform: uppercase; letter-spacing: .09em; font-weight: 600; }
.period { color: var(--muted); font-size: 13px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px; }
.hero { margin-top: 14px; }
.total { font-size: 34px; font-weight: 650; letter-spacing: -.02em;
         font-variant-numeric: tabular-nums; }
.delta { font-size: 13px; color: var(--muted); margin-top: 2px; }
.up { color: var(--warn); } .down { color: var(--accent); }
.facts { display: flex; flex-wrap: wrap; gap: 18px; margin-top: 14px;
         padding-top: 14px; border-top: 1px solid var(--line); }
.fact .k { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .07em; }
.fact .v { font-size: 15px; font-variant-numeric: tabular-nums; }
.chart { display: flex; align-items: flex-end; gap: 8px; height: CHARTHEIGHTpx; }
.col { flex: 1; display: flex; flex-direction: column; justify-content: flex-end; height: 100%; }
.bar { background: var(--bar); border-radius: 4px 4px 0 0; min-height: 2px; }
.bar.live { background: var(--bar-live); }
.tick { text-align: center; font-size: 11px; color: var(--muted); margin-top: 6px; }
.tick b { display: block; color: var(--ink); font-weight: 500;
          font-variant-numeric: tabular-nums; }
table { width: 100%; border-collapse: collapse; }
td, th { padding: 10px 0; border-bottom: 1px solid var(--line); text-align: left;
         vertical-align: top; }
th { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .07em;
     font-weight: 600; }
tr:last-child td { border-bottom: 0; }
.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.sub { color: var(--muted); font-size: 12px; }
.tag { display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 999px;
       background: var(--warn-bg); color: var(--warn); white-space: nowrap; }
.note { background: var(--warn-bg); border: 1px solid var(--line); border-left: 3px solid var(--warn);
        border-radius: 8px; padding: 12px 14px; font-size: 13px; color: var(--ink); }
.share { height: 6px; border-radius: 3px; background: var(--bar); overflow: hidden; margin-top: 5px; }
.share i { display: block; height: 100%; background: var(--bar-live); }
.empty { color: var(--muted); padding: 22px 0; text-align: center; }
footer { margin-top: 34px; padding-top: 14px; border-top: 1px solid var(--line);
         color: var(--muted); font-size: 12px;
         display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.overflow { overflow-x: auto; }
""".replace("CHARTHEIGHT", str(CHART_HEIGHT))


def page(title: str, body: str) -> str:
    """Wrap body content in a complete, self-contained document."""
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>"
        f"<style>{STYLE}</style>"
        "</head><body>"
        f"{body}"
        "</body></html>"
    )


def _delta(overview: data.Overview) -> str:
    pct = overview.delta_pct
    if pct is None:
        return '<div class="delta">No spend recorded last month.</div>'
    arrow = "▲" if overview.delta_minor > 0 else "▼"
    css = "up" if overview.delta_minor > 0 else "down"
    return (
        f'<div class="delta"><span class="{css}">{arrow} {abs(pct):.0f}%</span> '
        f"vs last month ({money(overview.previous_total_minor)})</div>"
    )


def _hero(overview: data.Overview) -> str:
    flagged = (
        f'<div class="fact"><div class="k">Needs a look</div>'
        f'<div class="v">{overview.flagged_count}</div></div>'
        if overview.flagged_count
        else ""
    )
    api = overview.api_spend_micros / 1_000_000
    return (
        f'<div class="card hero">'
        f'<div class="total">{escape(money(overview.total_minor))}</div>'
        f"{_delta(overview)}"
        f'<div class="facts">'
        f'<div class="fact"><div class="k">Receipts</div>'
        f'<div class="v">{overview.receipt_count}</div></div>'
        f'<div class="fact"><div class="k">Average</div>'
        f'<div class="v">{escape(money(overview.average_minor))}</div></div>'
        f"{flagged}"
        f'<div class="fact"><div class="k">API cost, all time</div>'
        f'<div class="v">${api:.2f}</div></div>'
        f"</div></div>"
    )


def _chart(buckets: list[data.Bucket]) -> str:
    if not any(b.total_minor for b in buckets):
        return '<div class="card"><div class="empty">Nothing to chart yet.</div></div>'
    peak = max(b.total_minor for b in buckets) or 1
    columns = []
    for index, bucket in enumerate(buckets):
        height = max(2, round(bucket.total_minor / peak * CHART_HEIGHT))
        live = " live" if index == len(buckets) - 1 else ""
        amount = f"{bucket.total_minor // 100:,}" if bucket.total_minor else "0"
        columns.append(
            f'<div class="col"><div class="bar{live}" style="height:{height}px"></div>'
            f'<div class="tick"><b>{amount}</b>{escape(bucket.label)}</div></div>'
        )
    return f'<div class="card"><div class="chart">{"".join(columns)}</div></div>'


def _categories(slices: list[data.Slice], subtotal_note: bool) -> str:
    if not slices:
        return '<div class="card"><div class="empty">No line items in this period.</div></div>'

    rows = []
    for item in slices:
        rows.append(
            f"<tr><td>{escape(item.name)}"
            f'<div class="share"><i style="width:{item.share * 100:.1f}%"></i></div></td>'
            f'<td class="num">{escape(money(item.total_minor))}'
            f'<div class="sub">{item.share * 100:.0f}%</div></td></tr>'
        )

    note = ""
    if subtotal_note:
        note = (
            '<div class="note" style="margin-top:12px">'
            "Everything is <b>Uncategorized</b> because enrichment is not built yet "
            "(commit 9). These slices sum to the basket, not to the grand total: "
            "charges, taxes and order-level discounts sit outside the line items."
            "</div>"
        )
    return f'<div class="card"><table>{"".join(rows)}</table></div>{note}'


def _row(row: data.Row) -> str:
    flag = (
        f'<span class="tag">gap {escape(money(row.unaccounted_adjustment_minor))}</span>'
        if row.flagged
        else ""
    )
    guessed = (
        '<div class="sub">date from the message, not the receipt</div>'
        if row.date_source == DateSource.MESSAGE_TIMESTAMP.value
        else ""
    )
    items = f"{row.item_count} item{'s' if row.item_count != 1 else ''}"
    return (
        f'<tr><td><a href="/receipt/{escape(row.id)}">'
        f"{row.occurred_on_local.strftime('%d %b %Y')}</a> {flag}"
        f'<div class="sub">{items}</div>{guessed}</td>'
        f'<td class="num">{escape(money(row.grand_total_minor, row.currency))}</td></tr>'
    )


def dashboard(
    *,
    overview: data.Overview,
    buckets: list[data.Bucket],
    slices: list[data.Slice],
    rows: list[data.Row],
    generated_at: dt.datetime,
) -> str:
    """The whole dashboard, as one self-contained page."""
    if rows:
        table = (
            '<div class="card"><table><thead><tr><th>Receipt</th>'
            f'<th class="num">Total</th></tr></thead><tbody>'
            f"{''.join(_row(r) for r in rows)}</tbody></table></div>"
        )
    else:
        table = (
            '<div class="card"><div class="empty">'
            "Nothing logged yet. Send the bot a receipt.</div></div>"
        )

    all_uncategorized = bool(slices) and all(s.name == data.UNCATEGORIZED for s in slices)

    body = (
        "<header><h1>Raseed</h1>"
        f'<div class="period">{escape(overview.period_label)}</div></header>'
        f"{_hero(overview)}"
        "<h2>Last six months</h2>"
        f"{_chart(buckets)}"
        "<h2>Where it went</h2>"
        f"{_categories(slices, all_uncategorized)}"
        "<h2>Receipts</h2>"
        f"{table}"
        "<footer><span>Read only. Nothing here can change the ledger.</span>"
        f"<span>{generated_at.strftime('%d %b %Y, %H:%M')}</span></footer>"
    )
    return page("Raseed", body)


def _adjustments(receipt: data.Receipt) -> str:
    if not receipt.adjustments:
        return ""
    rows = []
    for adjustment in receipt.adjustments:
        sign = "-" if adjustment.kind is AdjustmentKind.DISCOUNT else ""
        rows.append(
            f"<tr><td>{escape(adjustment.label)}"
            f'<div class="sub">{escape(adjustment.kind.value.lower())}</div></td>'
            f'<td class="num">{sign}{escape(money(adjustment.amount_minor))}</td></tr>'
        )
    return f'<h2>Charges and discounts</h2><div class="card"><table>{"".join(rows)}</table></div>'


def receipt_page(receipt: data.Receipt, *, generated_at: dt.datetime) -> str:
    """One receipt, every line, with the arithmetic shown."""
    row = receipt.row

    lines = []
    for line in receipt.lines:
        saved = line.saved_minor
        mrp = (
            f'<div class="sub">MRP {escape(money(line.mrp_minor or 0))}'
            f"{f', saved {escape(money(saved))}' if saved and saved > 0 else ''}</div>"
            if line.mrp_minor is not None
            else ""
        )
        quantity = (
            f'<div class="sub">{escape(line.quantity_text)}</div>' if line.quantity_text else ""
        )
        lines.append(
            f"<tr><td>{escape(line.raw_name)}{quantity}{mrp}</td>"
            f'<td class="num">{escape(money(line.line_total_minor))}</td></tr>'
        )

    charges = receipt.total_for(AdjustmentKind.CHARGE)
    taxes = receipt.total_for(AdjustmentKind.TAX)
    discounts = receipt.total_for(AdjustmentKind.DISCOUNT)
    computed = receipt.line_subtotal_minor + charges + taxes - discounts
    delta = computed - row.grand_total_minor

    maths = (
        f"<tr><td>Line items</td>"
        f'<td class="num">{escape(money(receipt.line_subtotal_minor))}</td></tr>'
        f'<tr><td>Charges</td><td class="num">{escape(money(charges))}</td></tr>'
        f'<tr><td>Taxes</td><td class="num">{escape(money(taxes))}</td></tr>'
        f'<tr><td>Discounts</td><td class="num">-{escape(money(discounts))}</td></tr>'
        f"<tr><td><b>Printed total</b></td>"
        f'<td class="num"><b>{escape(money(row.grand_total_minor))}</b></td></tr>'
    )

    gap = ""
    if delta:
        gap = (
            f'<div class="note" style="margin-top:12px">The arithmetic is off by '
            f"<b>{escape(money(abs(delta)))}</b>, logged as an unaccounted adjustment. "
            f"Either something was not printed, or a discount already inside the line "
            f"prices was subtracted twice.</div>"
        )

    guessed = (
        '<div class="sub">The receipt did not print a readable date, so this is the '
        "date the photo arrived.</div>"
        if row.date_source == DateSource.MESSAGE_TIMESTAMP.value
        else ""
    )
    order = f'<div class="sub">Order {escape(receipt.order_id)}</div>' if receipt.order_id else ""

    body = (
        f'<header><h1><a href="/">Raseed</a></h1>'
        f'<div class="period">{row.occurred_on_local.strftime("%d %b %Y")}</div></header>'
        f'<div class="card hero"><div class="total">'
        f"{escape(money(row.grand_total_minor, row.currency))}</div>"
        f"{order}{guessed}</div>"
        f'<h2>Items</h2><div class="card"><table>{"".join(lines)}</table></div>'
        f"{_adjustments(receipt)}"
        f'<h2>The arithmetic</h2><div class="card"><table>{maths}</table></div>{gap}'
        f'<footer><span><a href="/">Back</a></span>'
        f"<span>{generated_at.strftime('%d %b %Y, %H:%M')}</span></footer>"
    )
    return page("Receipt", body)


def not_found() -> str:
    return page(
        "Not found",
        '<header><h1><a href="/">Raseed</a></h1></header>'
        '<div class="card"><div class="empty">No such receipt.</div></div>',
    )


__all__ = ["CHART_HEIGHT", "STYLE", "dashboard", "not_found", "page", "receipt_page"]
