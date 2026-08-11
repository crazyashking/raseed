"""HTML for the dashboard, generated as text.

No template engine. Jinja2 is on the allowlist but is not installed (it belongs
to `tools/render_receipts.py`), and pulling it in for a handful of pages would
be a dependency bought with nothing. `html.escape` on every interpolation does the
one job a template engine would be doing for us here.

**Every value that came from a model or a receipt is escaped.** A merchant name,
a line item, a rejection reason: all of it is text a vision model produced from
an image a stranger could have crafted. Brief 21.4 treats extracted text as
untrusted input to the prompt; it is equally untrusted as input to a page.

The CSS is inline and the page is self-contained. It has to work over a tunnel
on a phone with no CDN, and a dashboard that needs the network to render is a
dashboard that fails exactly when you are standing in a shop wondering what you
spent.

**There is no JavaScript on any page that carries ledger data.** The only script
in this module is the boot stub in `shell()`, which runs before any data exists
in the document. That is enforced by a test, and it is the reason the chart
tooltips below are pure CSS: a page built out of untrusted extracted strings is
a page that should not also be executing anything.

Colour follows the method in the data-viz skill. Spend is a single series, so it
is a single hue and needs no legend; rank is carried by bar length and order,
never by colour. The two series steps were run through the palette validator
rather than picked by eye: `#1a7f52` on the light surface and `#46a878` on the
dark one both pass the lightness band, the chroma floor and 3:1 contrast against
the surface they actually render on. Status colours stay reserved, and every one
of them ships with a word next to it, so nothing on this page means something by
colour alone.
"""

from __future__ import annotations

import datetime as dt
from html import escape

from raseed.db.models import AdjustmentKind, DateSource
from raseed.money import money
from raseed.web import data

#: Height of the plot area in pixels, excluding value labels and month ticks.
#: Small enough for a phone in a Telegram frame, tall enough to compare bars.
CHART_HEIGHT: int = 132

#: How many categories get their own bar before the tail is folded into "Other".
#: Past this the list stops being a ranking and starts being a wall.
CATEGORY_LIMIT: int = 7

STYLE: str = """
:root {
  color-scheme: light;
  --plane: #f7f7f5; --card: #ffffff;
  --ink: #12120f; --ink2: #56554f; --muted: #85837c;
  --line: #e6e5df; --rule: #c9c8c0;
  --series: #1a7f52; --track: #e7f1eb; --on-series: #ffffff;
  --good: #006300; --warn-ink: #8a4b12; --warn-bg: #fdf1e3; --warn-edge: #ec835a;
  --tip-bg: #12120f; --tip-ink: #ffffff;
  --shadow: 0 1px 2px rgba(11,11,11,.05), 0 1px 1px rgba(11,11,11,.04);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --plane: #101109; --card: #1b1c17;
    --ink: #f4f3ea; --ink2: #c3c2b7; --muted: #898781;
    --line: #2c2d26; --rule: #3a3b32;
    --series: #46a878; --track: #23322a; --on-series: #101109;
    --good: #0ca30c; --warn-ink: #e8a869; --warn-bg: #2a2015; --warn-edge: #ec835a;
    --tip-bg: #f4f3ea; --tip-ink: #12120f;
    --shadow: none;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --plane: #101109; --card: #1b1c17;
  --ink: #f4f3ea; --ink2: #c3c2b7; --muted: #898781;
  --line: #2c2d26; --rule: #3a3b32;
  --series: #46a878; --track: #23322a; --on-series: #101109;
  --good: #0ca30c; --warn-ink: #e8a869; --warn-bg: #2a2015; --warn-edge: #ec835a;
  --tip-bg: #f4f3ea; --tip-ink: #12120f;
  --shadow: none;
}

* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0 auto; padding: 20px 16px 40px; max-width: 720px;
  background: var(--plane); color: var(--ink);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
  -webkit-font-smoothing: antialiased;
}
a { color: inherit; text-decoration: none; }

/* ---- page furniture ---- */
.top { display: flex; align-items: center; justify-content: space-between;
       gap: 12px; margin-bottom: 16px; }
.brand { display: flex; align-items: center; gap: 9px; font-size: 16px;
         font-weight: 620; letter-spacing: -.01em; }
.dot { width: 9px; height: 9px; border-radius: 3px; background: var(--series);
       flex: none; }
.period { color: var(--muted); font-size: 13px; }
h2 { font-size: 11px; margin: 26px 0 10px; color: var(--muted);
     text-transform: uppercase; letter-spacing: .1em; font-weight: 650; }
.card { background: var(--card); border: 1px solid var(--line);
        border-radius: 14px; padding: 18px; box-shadow: var(--shadow); }

/* ---- top nav, brief section 2 ---- */
.tabs { display: flex; gap: 7px; overflow-x: auto; margin: 0 -16px 16px; padding: 2px 16px 8px;
        scrollbar-width: none; -webkit-overflow-scrolling: touch; }
.tabs::-webkit-scrollbar { display: none; }
.tab { flex: none; padding: 7px 14px; border-radius: 999px; font-size: 13px;
       border: 1px solid var(--line); background: var(--card); color: var(--ink2);
       white-space: nowrap; }
.tab.on { background: var(--series); border-color: var(--series); color: var(--on-series);
          font-weight: 600; }
/* Greyed, not hidden: the tab order has to stay put month to month. */
.tab.off { color: var(--muted); opacity: .55; }

/* ---- hero ---- */
.eyebrow { font-size: 12px; color: var(--muted); letter-spacing: .04em; }
.total { font-size: 40px; line-height: 1.1; font-weight: 660; letter-spacing: -.025em;
         margin-top: 4px; }
.delta { display: inline-flex; align-items: center; gap: 6px; margin-top: 8px;
         font-size: 13px; color: var(--ink2); }
.delta .arrow { font-size: 11px; }
.up { color: var(--warn-ink); } .down { color: var(--good); }
/* 132px is chosen so a phone gets a clean 2x2 and a desktop gets one row of
   four. Anything narrower orphans the last tile on its own row. */
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
         gap: 16px 18px; margin-top: 18px; padding-top: 16px;
         border-top: 1px solid var(--line); }
.stat .k { font-size: 10px; color: var(--muted); text-transform: uppercase;
           letter-spacing: .08em; font-weight: 600; }
.stat .v { font-size: 17px; margin-top: 3px; font-variant-numeric: tabular-nums; }
.stat .v.flag { color: var(--warn-ink); }

/* ---- bar chart: one series, one hue, rank carried by length ---- */
.plot { position: relative; }
.rules { position: absolute; left: 0; right: 0; top: 0; height: CHARTHEIGHTpx;
         pointer-events: none; }
.rules i { position: absolute; left: 0; right: 0; height: 1px; background: var(--line); }
.rules i.base { background: var(--rule); }
.cols { position: relative; display: flex; gap: 10px; align-items: flex-end;
        height: CHARTHEIGHTpx; }
.col { position: relative; flex: 1; height: 100%;
       display: flex; flex-direction: column; justify-content: flex-end; }
.cv { font-size: 11px; color: var(--muted); text-align: center; margin-bottom: 6px;
      font-variant-numeric: tabular-nums; white-space: nowrap; }
.col.now .cv { color: var(--ink); font-weight: 600; }
.bar { background: var(--series); border-radius: 4px 4px 0 0; }
.bar.nil { background: var(--track); border-radius: 4px; }
.xrow { display: flex; gap: 10px; margin-top: 9px; }
.xrow span { flex: 1; text-align: center; font-size: 11px; color: var(--muted); }
.xrow span.now { color: var(--ink); font-weight: 620; }
/* Hover layer, CSS only. See the module docstring for why there is no JS here. */
.col::after {
  content: attr(data-tip); position: absolute; bottom: 100%; left: 50%;
  transform: translate(-50%, -6px); background: var(--tip-bg); color: var(--tip-ink);
  font-size: 11px; line-height: 1.4; padding: 5px 9px; border-radius: 7px;
  white-space: nowrap; opacity: 0; visibility: hidden; transition: opacity .12s;
  z-index: 3; font-variant-numeric: tabular-nums;
}
.col:hover::after, .col:focus-within::after { opacity: 1; visibility: visible; }
/* The end columns anchor to their own edge instead of their centre, or the
   tooltip runs off the side of a phone screen. */
.col:first-child::after { left: 0; transform: translate(0, -6px); }
.col:last-child::after { left: auto; right: 0; transform: translate(0, -6px); }

/* ---- ranked category bars ---- */
.cat + .cat { margin-top: 15px; }
.cathead { display: flex; align-items: baseline; justify-content: space-between;
           gap: 12px; font-size: 14px; }
.catname { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.catval { font-variant-numeric: tabular-nums; white-space: nowrap; color: var(--ink2); }
.catval b { color: var(--ink); font-weight: 600; }
.catval em { font-style: normal; color: var(--muted); font-size: 12px; margin-left: 6px; }
.track { height: 7px; border-radius: 4px; background: var(--track); margin-top: 7px;
         overflow: hidden; }
.track i { display: block; height: 100%; background: var(--series); border-radius: 4px; }

/* ---- receipt list ---- */
.list { display: block; }
.item { display: flex; align-items: center; justify-content: space-between; gap: 14px;
        padding: 13px 4px; border-bottom: 1px solid var(--line); min-height: 56px; }
.item:last-child { border-bottom: 0; }
a.item:hover { background: var(--plane); border-radius: 8px; }
/* The left half stacks; without an explicit block the date and the item count
   render on one line and read as "09 Aug 20264 items". */
.item > span { display: block; min-width: 0; }
.when { font-size: 15px; }
.meta { display: block; font-size: 12px; color: var(--muted); margin-top: 3px; }
.amount { font-size: 16px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.chev { color: var(--muted); font-size: 13px; margin-left: 2px; }

/* ---- flags, notes, empties ---- */
.tag { display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 999px;
       background: var(--warn-bg); color: var(--warn-ink); white-space: nowrap;
       font-weight: 550; margin-left: 6px; }
.note { background: var(--warn-bg); border: 1px solid var(--line);
        border-left: 3px solid var(--warn-edge); border-radius: 10px;
        padding: 12px 14px; font-size: 13px; color: var(--ink); margin-top: 12px; }
.hint { font-size: 12px; color: var(--muted); margin-top: 12px; }
.empty { text-align: center; padding: 30px 10px; }
.empty .big { font-size: 15px; color: var(--ink); }
.empty .small { font-size: 13px; color: var(--muted); margin-top: 6px; }

/* ---- tables (receipt detail) ---- */
table { width: 100%; border-collapse: collapse; }
td, th { padding: 11px 0; border-bottom: 1px solid var(--line); text-align: left;
         vertical-align: top; }
th { font-size: 10px; color: var(--muted); text-transform: uppercase;
     letter-spacing: .08em; font-weight: 650; }
tr:last-child td { border-bottom: 0; }
.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
tr.sum td { border-top: 1px solid var(--rule); border-bottom: 0; font-weight: 620; }

footer { margin-top: 32px; padding-top: 16px; border-top: 1px solid var(--line);
         color: var(--muted); font-size: 12px;
         display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }

/* ======================================================================
   Motion.

   Every animation here is an entrance or a state change, never a loop and
   never anything that moves while being read. The whole block is switched
   off under `prefers-reduced-motion`, which is a correctness requirement
   and not a nicety: for some people this kind of movement causes nausea.
   ====================================================================== */
@keyframes rise { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: none; } }
@keyframes grow { from { transform: scaleY(0); } to { transform: scaleY(1); } }
@keyframes widen { from { transform: scaleX(0); } to { transform: scaleX(1); } }
@keyframes pulse { 0%, 100% { opacity: .45; } 50% { opacity: .9; } }

/* Content swaps on every tab change, so the sections stagger in. `both` holds
   the from-state before the delay elapses, or the cards flash at full opacity
   for a frame first. */
#content > * { animation: rise .34s cubic-bezier(.22,.61,.36,1) both; }
#content > *:nth-child(1) { animation-delay: 0ms; }
#content > *:nth-child(2) { animation-delay: 35ms; }
#content > *:nth-child(3) { animation-delay: 60ms; }
#content > *:nth-child(4) { animation-delay: 85ms; }
#content > *:nth-child(5) { animation-delay: 105ms; }
#content > *:nth-child(6) { animation-delay: 120ms; }
#content > *:nth-child(n+7) { animation-delay: 135ms; }
/* The outgoing half of the crossfade. The incoming content is already
   mid-animation underneath, so this is deliberately faster than the entrance. */
#content.leaving { opacity: 0; transform: translateY(-6px);
                   transition: opacity .16s ease-in, transform .16s ease-in; }

/* Bars grow out of the baseline they are measured from. */
.bar { transform-origin: bottom; animation: grow .55s cubic-bezier(.22,.61,.36,1) both;
       animation-delay: calc(var(--i, 0) * 55ms + 60ms); }
.track i { transform-origin: left; animation: widen .6s cubic-bezier(.22,.61,.36,1) both;
           animation-delay: calc(var(--i, 0) * 45ms + 90ms); }

/* Interactive feedback. A tab has to answer the finger before the network does. */
.tab { transition: background .22s ease, color .22s ease, border-color .22s ease,
                   transform .12s ease; }
.tab:active { transform: scale(.94); }
a.item { transition: background .18s ease, transform .12s ease; }
a.item:active { transform: scale(.99); }
.card { transition: border-color .2s ease; }

/* The first paint, before any data exists. */
.skeleton { height: 12px; border-radius: 6px; background: var(--track);
            animation: pulse 1.1s ease-in-out infinite; }
.skeleton + .skeleton { margin-top: 12px; }
.skeleton.wide { width: 62%; } .skeleton.narrow { width: 34%; height: 30px; }

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: .001ms !important; animation-delay: 0ms !important;
    animation-iteration-count: 1 !important; transition-duration: .001ms !important;
  }
}
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


def _compact(minor: int) -> str:
    """A chart-tick amount: short enough for six of them on a phone.

    Indian grouping, because that is what the ledger is in: a lakh is a lakh,
    not 0.1 million.
    """
    rupees = minor / 100
    if rupees >= 100_000:
        return f"{rupees / 100_000:.1f}L".replace(".0L", "L")
    if rupees >= 1_000:
        return f"{rupees / 1_000:.1f}k".replace(".0k", "k")
    return f"{rupees:,.0f}"


def _brand(period: str) -> str:
    return (
        '<div class="top"><div class="brand"><span class="dot"></span>Raseed</div>'
        f'<div class="period">{escape(period)}</div></div>'
    )


def _tab_href(slug: str | None) -> str:
    """Where a tab points. All is the bare route, so it is the default landing."""
    return "/app" if slug is None else f"/app?tab={escape(slug, quote=True)}"


def _tabs(entries: list[data.Tab], active: str | None) -> str:
    """The top nav. Brief section 2.

    Every tab is always present and always in the same order, including ones
    with nothing in them this month: a nav that reshuffles as spend moves around
    is a nav you cannot build muscle memory for.
    """
    if not entries:
        return ""
    pills = []
    for entry in entries:
        classes = "tab"
        if entry.slug == active:
            classes += " on"
        elif entry.empty:
            classes += " off"
        pills.append(
            f'<a class="{classes}" href="{_tab_href(entry.slug)}">{escape(entry.name)}</a>'
        )
    return f'<div class="tabs">{"".join(pills)}</div>'


def _delta(overview: data.Overview) -> str:
    """Change against last month, stated in words as well as colour."""
    pct = overview.delta_pct
    if pct is None:
        return '<div class="delta">Nothing recorded last month to compare against.</div>'
    if overview.delta_minor == 0:
        return '<div class="delta">Level with last month.</div>'
    up = overview.delta_minor > 0
    arrow = "&#9650;" if up else "&#9660;"
    css = "up" if up else "down"
    word = "more" if up else "less"
    return (
        f'<div class="delta"><span class="{css}"><span class="arrow">{arrow}</span> '
        f"{abs(pct):.0f}% {word}</span> than last month "
        f"({escape(money(overview.previous_total_minor))})</div>"
    )


def _hero(overview: data.Overview, *, scope: str | None = None) -> str:
    """The headline figures.

    `scope` names the active category. It is spelled out rather than left
    implicit, because under a tab this total is a sum of matching line items and
    excludes the delivery fees, taxes and order-level discounts that a grand
    total includes. Two different quantities must not share one presentation.
    """
    stats = [
        ("Receipts", str(overview.receipt_count), ""),
        ("Average", escape(money(overview.average_minor)), ""),
    ]
    if overview.flagged_count:
        stats.append(("Needs a look", str(overview.flagged_count), " flag"))
    stats.append(("API cost, all time", f"${overview.api_spend_micros / 1_000_000:.2f}", ""))

    tiles = "".join(
        f'<div class="stat"><div class="k">{escape(k)}</div><div class="v{cls}">{v}</div></div>'
        for k, v, cls in stats
    )
    if scope:
        eyebrow = f"{scope} in {escape(overview.period_label)}"
        note = (
            '<div class="hint">Line items only. Delivery, taxes and order-level '
            "discounts are not part of any category.</div>"
        )
    else:
        eyebrow = f"Spent in {escape(overview.period_label)}"
        note = ""

    return (
        f'<div class="card">'
        f'<div class="eyebrow">{eyebrow}</div>'
        f'<div class="total">{escape(money(overview.total_minor))}</div>'
        f"{_delta(overview)}"
        f'<div class="stats">{tiles}</div>{note}'
        f"</div>"
    )


def _items(slices: list[data.Slice], what: str) -> str:
    """The biggest line items inside the selected category.

    Brief section 2 drills domain, then category, then item. There are no
    sub-categories in v0 (brief 3.8), so a domain drills straight to its items.
    """
    if not slices:
        return (
            '<div class="card"><div class="empty">'
            f'<div class="big">Nothing under {what} this month.</div></div></div>'
        )

    bars = "".join(
        f'<div class="cat"><div class="cathead">'
        f'<span class="catname">{escape(item.name)}</span>'
        f'<span class="catval"><b>{escape(money(item.total_minor))}</b>'
        f"<em>{item.share * 100:.0f}%</em></span></div>"
        f'<div class="track"><i style="width:{max(item.share * 100, 1.5):.1f}%;'
        f'--i:{index}"></i></div>'
        f'<div class="meta">'
        f"{'bought once' if item.count <= 1 else f'bought {item.count} times'}</div>"
        f"</div>"
        for index, item in enumerate(slices)
    )
    return f'<div class="card">{bars}</div>'


def _chart(buckets: list[data.Bucket]) -> str:
    """Six months of spend as one series in one hue.

    Every month is drawn even when it is empty, because a missing bar would make
    the axis lie about time. A zero month gets a flat track and the tooltip says
    so, rather than a one-pixel sliver that reads as a small amount.
    """
    if not any(b.total_minor for b in buckets):
        return (
            '<div class="card"><div class="empty">'
            '<div class="big">No spend in the last six months.</div>'
            '<div class="small">The chart fills in as receipts land.</div>'
            "</div></div>"
        )

    peak = max(b.total_minor for b in buckets)
    last = len(buckets) - 1

    columns, ticks = [], []
    for index, bucket in enumerate(buckets):
        now = " now" if index == last else ""
        if bucket.total_minor:
            height = max(3, round(bucket.total_minor / peak * (CHART_HEIGHT - 22)))
            bar = f'<div class="bar" style="height:{height}px;--i:{index}"></div>'
            label = _compact(bucket.total_minor)
            receipts = f"{bucket.count} receipt{'s' if bucket.count != 1 else ''}"
            tip = f"{bucket.label}: {money(bucket.total_minor)}, {receipts}"
        else:
            bar = f'<div class="bar nil" style="height:3px;--i:{index}"></div>'
            label = "&mdash;"
            tip = f"{bucket.label}: nothing recorded"

        columns.append(
            f'<div class="col{now}" tabindex="0" data-tip="{escape(tip)}">'
            f'<div class="cv">{label}</div>{bar}</div>'
        )
        ticks.append(f'<span class="{now.strip()}">{escape(bucket.label)}</span>')

    rules = (
        '<div class="rules"><i style="top:0"></i><i style="top:50%"></i>'
        '<i class="base" style="bottom:0"></i></div>'
    )
    return (
        f'<div class="card"><div class="plot">{rules}'
        f'<div class="cols">{"".join(columns)}</div></div>'
        f'<div class="xrow">{"".join(ticks)}</div>'
        f'<div class="hint">Peak month {escape(money(peak))}. Hover a bar for the detail.</div>'
        f"</div>"
    )


def _categories(slices: list[data.Slice]) -> str:
    """Where the basket went, ranked.

    One hue for every bar. Rank is already carried by order and length, and
    giving each category its own colour would mean a legend, eight hues to keep
    apart, and a repaint every time the set changes.
    """
    if not slices:
        return (
            '<div class="card"><div class="empty">'
            '<div class="big">No line items this month.</div>'
            '<div class="small">Categories appear once a receipt is confirmed.</div>'
            "</div></div>"
        )

    shown = slices[:CATEGORY_LIMIT]
    tail = slices[CATEGORY_LIMIT:]
    if tail:
        shown = [
            *shown,
            data.Slice(
                name=f"Other ({len(tail)})",
                total_minor=sum(s.total_minor for s in tail),
                share=sum(s.share for s in tail),
            ),
        ]

    bars = "".join(
        f'<div class="cat"><div class="cathead">'
        f'<span class="catname">{escape(item.name)}</span>'
        f'<span class="catval"><b>{escape(money(item.total_minor))}</b>'
        f"<em>{item.share * 100:.0f}%</em></span></div>"
        f'<div class="track"><i style="width:{max(item.share * 100, 1.5):.1f}%;'
        f'--i:{index}"></i></div>'
        f"</div>"
        for index, item in enumerate(shown)
    )

    note = (
        '<div class="hint">These add up to the basket, not to the total above: '
        "delivery charges, taxes and order-level discounts sit outside the line items.</div>"
    )
    if all(s.name == data.UNCATEGORIZED for s in slices):
        note = (
            '<div class="note">Everything is still <b>Uncategorized</b>. '
            "These receipts were stored before enrichment ran; "
            "<b>tools/enrich.py --apply</b> categorises them without spending anything.</div>"
        )
    return f'<div class="card">{bars}</div>{note}'


def _row(row: data.Row) -> str:
    flag = (
        f'<span class="tag">gap {escape(money(row.unaccounted_adjustment_minor))}</span>'
        if row.flagged
        else ""
    )
    bits = [f"{row.item_count} item{'s' if row.item_count != 1 else ''}"]
    if row.category_minor is not None:
        # Under a tab the headline figure is this category's share, so the
        # receipt's own total has to stay visible or the row looks wrong.
        bits.append(f"of {money(row.grand_total_minor, row.currency)} on the receipt")
    if row.date_source == DateSource.MESSAGE_TIMESTAMP.value:
        bits.append("date from the message, not the receipt")
    # Escape each part, then join with an entity. Escaping the joined string
    # would turn the separator into a literal "&middot;".
    meta = " &middot; ".join(escape(b) for b in bits)

    return (
        f'<a class="item" href="/receipt/{escape(row.id)}">'
        f'<span><span class="when">{row.occurred_on_local.strftime("%d %b %Y")}</span>{flag}'
        f'<span class="meta">{meta}</span></span>'
        f'<span class="amount">{escape(money(row.amount_minor, row.currency))}'
        f'<span class="chev"> &rsaquo;</span></span></a>'
    )


def dashboard(
    *,
    overview: data.Overview,
    buckets: list[data.Bucket],
    slices: list[data.Slice],
    rows: list[data.Row],
    generated_at: dt.datetime,
    tabs: list[data.Tab] | None = None,
    active_tab: str | None = None,
    active_tab_name: str | None = None,
) -> str:
    """The whole dashboard, as one self-contained page.

    With `active_tab` set, every figure below the nav is scoped to that category
    and is a **line-item** figure, not a grand total. The page says so, because
    a category total and a receipt total are different quantities and quietly
    swapping one for the other is how a dashboard starts lying.
    """
    filtered = active_tab is not None
    what = escape(active_tab_name or "this category")

    if rows:
        listing = f'<div class="card list">{"".join(_row(r) for r in rows)}</div>'
    elif filtered:
        listing = (
            '<div class="card"><div class="empty">'
            f'<div class="big">No {what} this month.</div>'
            '<div class="small">Other months may have some. The tab stays put either '
            "way, so the nav does not move around.</div></div></div>"
        )
    else:
        listing = (
            '<div class="card"><div class="empty">'
            '<div class="big">Nothing logged yet.</div>'
            '<div class="small">Send the bot a photo of a receipt and confirm it. '
            "It will show up here.</div></div></div>"
        )

    if filtered:
        breakdown = f"<h2>Most of it went on</h2>{_items(slices, what)}"
        receipts_heading = "<h2>Receipts with " + what.lower() + "</h2>"
    else:
        breakdown = f"<h2>Where it went</h2>{_categories(slices)}"
        receipts_heading = "<h2>Receipts</h2>"

    # Split into persistent chrome and swappable content. The boot script keeps
    # the chrome element when the tab set is unchanged and only replaces
    # `#content`, which is what lets the active pill animate between tabs
    # instead of being destroyed and rebuilt on every navigation.
    body = (
        f'<div id="chrome">{_brand(overview.period_label)}'
        f"{_tabs(tabs or [], active_tab)}</div>"
        f'<div id="content">'
        f"{_hero(overview, scope=what if filtered else None)}"
        "<h2>Last six months</h2>"
        f"{_chart(buckets)}"
        f"{breakdown}"
        f"{receipts_heading}"
        f"{listing}"
        "<footer><span>Read only. Nothing here can change the ledger.</span>"
        f"<span>{generated_at.strftime('%d %b %Y, %H:%M')}</span></footer>"
        "</div>"
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
        f'<tr class="sum"><td>Printed total</td>'
        f'<td class="num">{escape(money(row.grand_total_minor))}</td></tr>'
    )

    gap = ""
    if delta:
        gap = (
            f'<div class="note">The arithmetic is off by '
            f"<b>{escape(money(abs(delta)))}</b>, logged as an unaccounted adjustment. "
            f"Either something was not printed, or a discount already inside the line "
            f"prices was subtracted twice.</div>"
        )

    guessed = (
        '<div class="delta">The receipt did not print a readable date, so this is the '
        "date the photo arrived.</div>"
        if row.date_source == DateSource.MESSAGE_TIMESTAMP.value
        else ""
    )
    order = (
        f'<div class="eyebrow">Order {escape(receipt.order_id)}</div>' if receipt.order_id else ""
    )
    items = f"{len(receipt.lines)} item{'s' if len(receipt.lines) != 1 else ''}"

    body = (
        f'<div id="chrome"><div class="top"><a class="brand" href="/app">'
        f'<span class="dot"></span>Raseed</a>'
        f'<div class="period">{row.occurred_on_local.strftime("%d %b %Y")}</div>'
        f"</div></div>"
        f'<div id="content">'
        f'<div class="card">{order}'
        f'<div class="total">{escape(money(row.grand_total_minor, row.currency))}</div>'
        f'<div class="eyebrow">{items}</div>{guessed}</div>'
        f'<h2>Items</h2><div class="card"><table>{"".join(lines)}</table></div>'
        f"{_adjustments(receipt)}"
        f'<h2>The arithmetic</h2><div class="card"><table>{maths}</table></div>{gap}'
        f'<footer><span><a href="/app">&lsaquo; All receipts</a></span>'
        f"<span>{generated_at.strftime('%d %b %Y, %H:%M')}</span></footer>"
        "</div>"
    )
    return page("Receipt", body)


def not_found() -> str:
    return page(
        "Not found",
        '<div id="chrome"><div class="top"><a class="brand" href="/app">'
        '<span class="dot"></span>Raseed</a></div></div>'
        '<div id="content"><div class="card"><div class="empty">'
        '<div class="big">No such receipt.</div>'
        '<div class="small"><a href="/app">Back to the dashboard</a></div>'
        "</div></div></div>",
    )


#: The card an unauthenticated visitor sees. Shared by `locked()` and by the
#: shell, so the browser-only case and the server-refusal case look the same
#: instead of one of them dumping unstyled text into the page.
_LOCKED_CARD: str = (
    '<div class="top"><div class="brand"><span class="dot"></span>Raseed</div></div>'
    '<div class="card"><div class="empty">'
    '<div class="big">Open this from the Raseed bot in Telegram.</div>'
    '<div class="small">Send <b>/dashboard</b> to the bot and tap the button it '
    "replies with. This page only opens inside Telegram, which is how it knows "
    "whose receipts to show.</div>"
    # Filled in by the boot script when it is running inside the shell, and
    # left empty by the server-rendered page. "Open it from Telegram" is
    # useless advice to someone who *did* open it from Telegram, so this says
    # which half of the handshake is missing.
    '<div class="small" id="diag" style="margin-top:14px;opacity:.65;'
    'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px"></div>'
    "</div></div>"
)


def locked() -> str:
    """What an unauthenticated request gets.

    Says nothing about whether the ledger exists, how many users there are, or
    who this instance belongs to. Someone who found the URL learns only that
    they need to come through the bot.
    """
    return page("Raseed", _LOCKED_CARD)


#: The only JavaScript in this project, and it exists because of one constraint:
#: a Mini App proves who it is with a blob that only the browser can read, so the
#: first request cannot be the page itself.
#:
#: There is no cookie and no session. Every request carries the signed `initData`
#: in an `Authorization` header, so there is nothing on the server to expire,
#: nothing to steal from storage, and no CSRF surface, because a forged
#: cross-site request cannot attach a header it does not have.
#:
#: **It does not use `document.write`, and that is a bug fix, not a preference.**
#: The previous version replaced the whole document on every navigation with
#: `document.open(); document.write(html); document.close()` and then re-attached
#: its click listener. That works only where `document.open()` clears event
#: listeners, which Chrome does and embedded webviews do not all do. Where they
#: are not cleared, every navigation leaves another live listener behind, so the
#: third tab tapped fires three concurrent fetches and three racing document
#: rewrites. Reported live on 2026-08-09 as "the 3rd category gives an error",
#: and not reproducible in desktop Chrome for exactly that reason.
#:
#: The listener is now attached once, to a document that is never torn down, and
#: pages are swapped by parsing the response and replacing one element. `DOMParser`
#: does not execute scripts, so the swap cannot run anything either, which suits a
#: page built out of model-extracted strings.
_BOOT = """
(function () {
  var app = window.Telegram && window.Telegram.WebApp;
  if (app) { app.ready(); app.expand(); }

  var root = document.getElementById('app');
  var boot = document.getElementById('boot');
  var locked = document.getElementById('locked');

  function only(el) {
    var panes = [boot, locked, document.getElementById('offline')];
    for (var i = 0; i < panes.length; i++) {
      if (panes[i]) { panes[i].hidden = panes[i] !== el; }
    }
  }

  var auth = app && app.initData ? 'tma ' + app.initData : '';
  if (!auth) {
    var diag = document.getElementById('diag');
    if (diag) {
      var bits = [];
      bits.push(window.Telegram ? (app ? 'sdk ok' : 'sdk partial') : 'sdk absent');
      if (app) {
        bits.push('platform ' + (app.platform || '?'));
        bits.push('v' + (app.version || '?'));
        bits.push(app.initData === '' ? 'initData empty' : 'initData missing');
      }
      bits.push(String(location.hash || '').indexOf('tgWebAppData') >= 0
        ? 'launch data in url' : 'none in url');
      var stored = '';
      try { stored = window.sessionStorage.getItem('__telegram__initParams') || ''; }
      catch (e) { stored = 'blocked'; }
      bits.push(stored === 'blocked' ? 'session blocked'
        : (stored.indexOf('tgWebAppData') >= 0 ? 'launch data in session' : 'none in session'));
      diag.textContent = bits.join(' / ');
    }
    only(locked);
    return;
  }

  var still = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var busy = false;
  var current = '/app';

  function buzz() {
    try { app.HapticFeedback.impactOccurred('light'); } catch (e) { /* optional */ }
  }

  function tabsOf(el) {
    var out = [];
    var found = el.querySelectorAll('.tab');
    for (var i = 0; i < found.length; i++) { out.push(found[i].getAttribute('href')); }
    return out.join(',');
  }

  // Keep the live chrome when the nav is the same set of tabs, so the active
  // pill transitions instead of being rebuilt. Replace it when it is not.
  function chromeInto(next) {
    var live = document.getElementById('chrome');
    if (!next) { if (live) { live.remove(); } return; }
    if (!live) { root.insertBefore(next, root.firstChild); return; }
    if (tabsOf(live) && tabsOf(live) === tabsOf(next)) {
      var a = live.querySelectorAll('.tab');
      var b = next.querySelectorAll('.tab');
      for (var i = 0; i < a.length && i < b.length; i++) { a[i].className = b[i].className; }
      var lp = live.querySelector('.period');
      var np = next.querySelector('.period');
      if (lp && np) { lp.textContent = np.textContent; }
      return;
    }
    live.replaceWith(next);
  }

  function paint(html) {
    var doc = new DOMParser().parseFromString(html, 'text/html');
    var content = doc.getElementById('content');
    if (!content) { return false; }
    if (!document.getElementById('content')) { root.innerHTML = ''; }
    chromeInto(doc.getElementById('chrome'));
    var live = document.getElementById('content');
    if (live) { live.replaceWith(content); } else { root.appendChild(content); }
    if (app && app.BackButton) {
      if (current.indexOf('/receipt/') === 0) { app.BackButton.show(); }
      else { app.BackButton.hide(); }
    }
    window.scrollTo(0, 0);
    return true;
  }

  // `answered` means the server replied and refused. That is a different
  // problem from not reaching it at all, and saying "the machine may be
  // asleep" when it in fact answered 401 sends the reader to look at the
  // wrong thing entirely.
  function fail(url, detail, answered) {
    var live = document.getElementById('content');
    if (!live && !answered) { only(document.getElementById('offline')); return; }
    var card =
      '<div class="card"><div class="empty">' +
      '<div class="big">Could not load that.</div>' +
      '<div class="small" id="why"></div>' +
      '<div class="small" style="margin-top:14px">' +
      '<a href="' + url.replace(/"/g, '&quot;') + '">Try again</a></div>' +
      '</div></div>';
    if (live) {
      live.classList.remove('leaving');
      live.innerHTML = card;
    } else {
      // First load never got far enough to build a #content to replace.
      only(null);
      root.innerHTML = '<div id="content">' + card + '</div>';
    }
    var why = document.getElementById('why');
    if (why) { why.textContent = detail; }
  }

  function go(url) {
    if (busy) { return; }
    busy = true;
    var live = document.getElementById('content');
    var faded = new Promise(function (done) {
      if (!live || still) { return done(); }
      live.classList.add('leaving');
      setTimeout(done, 160);
    });
    var got = fetch(url, { headers: { Authorization: auth } }).then(function (r) {
      if (r.ok) { return r.text(); }
      // 401 here is almost always a stale launch: Telegram signs initData at
      // the moment it opens the app, and a webview restored from the
      // background carries the signature it was born with.
      var refused = new Error(r.status === 401
        ? 'The Telegram sign-in for this page has expired. Close the dashboard and open it again from the bot.'
        : 'The server said ' + r.status + '.');
      refused.answered = true;
      throw refused;
    }, function () {
      var unreachable = new Error('Could not reach the dashboard. Check your connection and try again in a moment.');
      unreachable.answered = false;
      throw unreachable;
    });
    Promise.all([got, faded]).then(function (both) {
      current = url;
      if (!paint(both[0])) {
        var shape = new Error('That page came back in a shape I did not expect.');
        shape.answered = true;
        throw shape;
      }
      busy = false;
    }).catch(function (err) {
      busy = false;
      fail(url, err && err.message ? err.message : 'The connection dropped.', !!(err && err.answered));
    });
  }

  // Attached exactly once, to a document that is never replaced.
  document.addEventListener('click', function (e) {
    var a = e.target && e.target.closest ? e.target.closest('a[href^="/"]') : null;
    if (!a) { return; }
    e.preventDefault();
    if (a.classList.contains('tab')) {
      buzz();
      // Answer the finger now; the server confirms a moment later.
      var all = document.querySelectorAll('.tab');
      for (var i = 0; i < all.length; i++) { all[i].classList.remove('on'); }
      a.classList.remove('off');
      a.classList.add('on');
    }
    go(a.getAttribute('href'));
  });

  if (app && app.BackButton && app.BackButton.onClick) {
    app.BackButton.onClick(function () { go('/app'); });
  }

  go('/app');
})();
"""


def shell() -> str:
    """The page Telegram opens, before anything is known about who opened it.

    Carries no ledger data at all. It loads Telegram's SDK, reads the signed
    `initData` the SDK exposes, and fetches the real page with it. Anyone who
    hits this URL without Telegram gets a page with nothing in it.

    Both failure states are in the markup from the start and merely unhidden, so
    a visitor without Telegram, or without a working connection, still lands on
    a styled page. Opening this in a desktop browser is the common case, and it
    should not look like the server broke.
    """
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Raseed</title>"
        f"<style>{STYLE}</style>"
        '<script src="https://telegram.org/js/telegram-web-app.js"></script>'
        "</head><body>"
        '<div id="app">'
        # A skeleton rather than the word "Loading", so the first paint already
        # has the shape of the page it is about to become.
        '<div id="boot"><div class="top"><div class="brand">'
        '<span class="dot"></span>Raseed</div></div>'
        '<div class="card"><div class="skeleton narrow"></div>'
        '<div class="skeleton wide"></div><div class="skeleton"></div>'
        '<div class="skeleton wide"></div></div></div>'
        f'<div id="locked" hidden>{_LOCKED_CARD}</div>'
        '<div id="offline" hidden>'
        '<div class="top"><div class="brand"><span class="dot"></span>Raseed</div></div>'
        '<div class="card"><div class="empty">'
        '<div class="big">Could not reach Raseed.</div>'
        '<div class="small">The dashboard did not answer. '
        "Check your connection and try again in a moment.</div>"
        "</div></div></div>"
        "</div>"
        f"<script>{_BOOT}</script>"
        "</body></html>"
    )


__all__ = [
    "CATEGORY_LIMIT",
    "CHART_HEIGHT",
    "STYLE",
    "dashboard",
    "locked",
    "not_found",
    "page",
    "receipt_page",
    "shell",
]
