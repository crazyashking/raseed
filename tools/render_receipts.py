"""Render Raseed test receipts from structured data.

The expected JSON is the input to the render, not a separate guess, so the image and
the answer key cannot disagree.

Usage:
    pip install jinja2 playwright
    playwright install chromium
    python tools/render_receipts.py
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sys

from jinja2 import Template
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "blinkit.html"
RECORDS = ROOT / "variations.json"
OUT = ROOT / "data" / "eval"
PREFIX = "blinkit"
WIDTH = 800


def validate(records: list[dict]) -> None:
    """Fail loudly before rendering. An unbalanced record must never become a fixture."""
    errors: list[str] = []

    for i, rec in enumerate(records):
        items = sum(x["line_total_minor"] for x in rec["line_items"])
        charges = sum(x["amount_minor"] for x in rec.get("charges", []))
        taxes = sum(x["amount_minor"] for x in rec.get("taxes", []))
        discounts = sum(x["amount_minor"] for x in rec.get("discounts", []))
        calc = items + charges + taxes - discounts
        stated = rec["grand_total_minor"]

        if calc != stated:
            errors.append(
                f"record {i}: reconciliation off by {calc - stated} paise "
                f"(calculated {calc}, stated {stated})"
            )

        for j, x in enumerate(rec["line_items"]):
            mrp = x.get("mrp_minor")
            if mrp is not None and mrp <= x["line_total_minor"]:
                errors.append(
                    f"record {i} item {j} ({x['raw_name']!r}): "
                    f"mrp_minor {mrp} is not above line_total_minor {x['line_total_minor']}"
                )
            for field in ("mrp_minor", "line_total_minor"):
                val = x.get(field)
                if val is not None and not isinstance(val, int):
                    errors.append(f"record {i} item {j}: {field} is {type(val).__name__}, not int")

        if not isinstance(stated, int):
            errors.append(f"record {i}: grand_total_minor is not an integer")

    if errors:
        print(f"VALIDATION FAILED, {len(errors)} problem(s):\n", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)

    print(f"validation passed: {len(records)} records balance")


def render(records: list[dict]) -> None:
    template = Template(TEMPLATE.read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    scratch = ROOT / ".render_tmp"
    scratch.mkdir(exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        # Small viewport height so full_page screenshots size to content rather than
        # padding short receipts with background.
        page = browser.new_page(viewport={"width": WIDTH, "height": 200},
                                device_scale_factor=2)
        try:
            for i, rec in enumerate(records, start=1):
                stem = f"{PREFIX}_{i:03d}"
                html_path = scratch / f"{stem}.html"
                html_path.write_text(template.render(**rec), encoding="utf-8")

                page.goto(html_path.resolve().as_uri())
                page.screenshot(path=OUT / f"{stem}.png", full_page=True)

                (OUT / f"{stem}.expected.json").write_text(
                    json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                print(f"  {stem}.png  ({len(rec['line_items'])} items)")
        finally:
            browser.close()

    shutil.rmtree(scratch, ignore_errors=True)


def main() -> None:
    if not TEMPLATE.exists():
        sys.exit(f"template not found: {TEMPLATE}")
    if not RECORDS.exists():
        sys.exit(f"records not found: {RECORDS}")

    records = json.loads(RECORDS.read_text(encoding="utf-8"))
    validate(records)
    render(records)
    print(f"\nwrote {len(records)} image/JSON pairs to {OUT}")


if __name__ == "__main__":
    main()
