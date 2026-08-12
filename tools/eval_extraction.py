"""Run extraction against the eval set and score it.

This is the only thing in the repo that spends money, and it is never run by the
test suite. It needs `GEMINI_API_KEY` in the environment, on a PAID tier key:
free-tier content is used to improve Google's products and these are receipts.

    .venv\\Scripts\\python tools\\eval_extraction.py --dry-run     # free, counts tokens
    .venv\\Scripts\\python tools\\eval_extraction.py               # spends money
    .venv\\Scripts\\python tools\\eval_extraction.py --limit 3     # spends less

`--dry-run` calls `count_tokens`, which costs nothing, and prints exactly what
the real run would cost before you authorise it. Run it first, every time.

## What it scores

Three separate things, because they fail for different reasons:

1. **Field accuracy.** Does the extraction match `*.expected.json` exactly?
2. **Gate behaviour.** Does the reconciliation gate accept it, and does the MRP
   cross-check pass?
3. **Total independence (deferred item D2).** The gate only has value if the
   model COPIES the printed total rather than deriving it from its own item sum.
   A model that derives it always balances, including when the items are wrong,
   which makes the gate tautological. This is detected by looking for receipts
   where the items are wrong but the arithmetic still agrees. That combination
   is the signature of a computed total, and no offline test can find it.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from raseed.extraction.pricing import cost_micros, format_usd  # noqa: E402
from raseed.extraction.providers.base import (  # noqa: E402
    ExtractionRequest,
    ImagePayload,
    ProviderError,
)
from raseed.extraction.providers.gemini import GeminiProvider  # noqa: E402
from raseed.extraction.schemas import ExtractionResult  # noqa: E402
from raseed.validation.reconcile import cross_check_mrp, reconcile  # noqa: E402

EVAL_DIR = ROOT / "data" / "eval"
MIME_BY_SUFFIX = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


@dataclass
class Score:
    """One receipt's result."""

    stem: str
    fields_match: bool
    items_match: bool
    total_matches: bool
    gate_accepted: bool
    mrp_passed: bool
    cost_micros: int
    input_tokens: int
    output_tokens: int
    differences: list[str] = field(default_factory=list)

    @property
    def derived_total_suspected(self) -> bool:
        """Items wrong, arithmetic still balanced. See D2 in the module docstring."""
        return (not self.items_match) and self.gate_accepted


def expected_for(image: pathlib.Path) -> ExtractionResult | None:
    path = image.with_suffix("").with_suffix(".expected.json")
    if not path.is_file():
        path = image.parent / f"{image.stem}.expected.json"
    if not path.is_file():
        return None
    record: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return ExtractionResult.model_validate(
        {"is_receipt": True, "receipt_confidence": 1.0, "rejection_reason": None, **record}
    )


def compare(actual: ExtractionResult, expected: ExtractionResult) -> list[str]:
    """Every field that disagrees, described in one line each."""
    out: list[str] = []

    if actual.grand_total_minor != expected.grand_total_minor:
        out.append(f"grand_total {actual.grand_total_minor} != {expected.grand_total_minor}")
    if len(actual.line_items) != len(expected.line_items):
        out.append(f"item count {len(actual.line_items)} != {len(expected.line_items)}")

    for index, (got, want) in enumerate(zip(actual.line_items, expected.line_items, strict=False)):
        if got.raw_name != want.raw_name:
            out.append(f"item {index} name {got.raw_name!r} != {want.raw_name!r}")
        if got.line_total_minor != want.line_total_minor:
            out.append(f"item {index} paid {got.line_total_minor} != {want.line_total_minor}")
        if got.mrp_minor != want.mrp_minor:
            out.append(f"item {index} mrp {got.mrp_minor} != {want.mrp_minor}")

    for label, got_rows, want_rows in (
        ("charge", actual.charges, expected.charges),
        ("tax", actual.taxes, expected.taxes),
        ("discount", actual.discounts, expected.discounts),
    ):
        got_total = sum(row.amount_minor for row in got_rows)
        want_total = sum(row.amount_minor for row in want_rows)
        if got_total != want_total:
            out.append(f"{label} total {got_total} != {want_total}")

    return out


def images(limit: int | None) -> list[pathlib.Path]:
    found = sorted(
        path
        for path in EVAL_DIR.glob("*")
        if path.suffix.lower() in MIME_BY_SUFFIX and not path.name.startswith("blinkit_000")
    )
    return found[:limit] if limit else found


def request_for(image: pathlib.Path) -> ExtractionRequest:
    return ExtractionRequest(
        images=(
            ImagePayload(data=image.read_bytes(), mime_type=MIME_BY_SUFFIX[image.suffix.lower()]),
        )
    )


def dry_run(provider: GeminiProvider, paths: list[pathlib.Path]) -> int:
    """Count tokens without generating anything. Free."""
    total = 0
    print("counting input tokens, no generation, no cost")
    print()
    for image in paths:
        tokens = provider.count_input_tokens(request_for(image))
        total += tokens
        print(f"  {image.stem:14s} {tokens:7d} input tokens")

    estimated = cost_micros(provider.model_id, input_tokens=total, output_tokens=700 * len(paths))
    print()
    print(f"{len(paths)} receipts, {total} input tokens")
    print(f"estimated cost of a real run: {format_usd(estimated)} (output is an estimate)")
    return 0


def live_run(provider: GeminiProvider, paths: list[pathlib.Path]) -> int:
    scores: list[Score] = []

    for image in paths:
        expected = expected_for(image)
        if expected is None:
            print(f"  {image.stem:14s} SKIP, no expected.json")
            continue

        try:
            result = provider.extract(request_for(image))
        except ProviderError as exc:
            print(f"  {image.stem:14s} ERROR {type(exc).__name__}: {exc}")
            continue

        # Every eval image is one receipt, so a group holding anything else is
        # itself the finding and is worth seeing rather than averaging away.
        if len(result.group.receipts) != 1:
            print(
                f"  {image.stem:14s} DIFF read {len(result.group.receipts)} receipts "
                f"out of one image"
            )
            continue

        actual = result.group.receipts[0]
        differences = compare(actual, expected)
        verdict = reconcile(actual)
        mrp = cross_check_mrp(actual)

        score = Score(
            stem=image.stem,
            fields_match=not differences,
            items_match=all(not d.startswith("item") for d in differences),
            total_matches=actual.grand_total_minor == expected.grand_total_minor,
            gate_accepted=verdict.accepted,
            mrp_passed=mrp.passed,
            cost_micros=result.cost_micros_usd,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            differences=differences,
        )
        scores.append(score)

        flag = "ok  " if score.fields_match else "DIFF"
        print(
            f"  {flag} {image.stem:14s} gate={verdict.outcome.value:12s} "
            f"mrp={mrp.outcome.value:15s} in={result.input_tokens:5d} "
            f"out={result.output_tokens:5d} (think {result.thought_tokens:4d}) "
            f"{format_usd(result.cost_micros_usd)}"
        )
        for line in differences:
            print(f"         {line}")

    if not scores:
        print("nothing scored")
        return 1

    exact = sum(1 for s in scores if s.fields_match)
    totals = sum(1 for s in scores if s.total_matches)
    gated = sum(1 for s in scores if s.gate_accepted)
    mrp_ok = sum(1 for s in scores if s.mrp_passed)
    spend = sum(s.cost_micros for s in scores)
    suspicious = [s for s in scores if s.derived_total_suspected]

    print()
    print(f"receipts scored          {len(scores)}")
    print(f"exact field match        {exact}/{len(scores)}")
    print(f"grand total correct      {totals}/{len(scores)}")
    print(f"accepted by the gate     {gated}/{len(scores)}")
    print(f"passed the MRP check     {mrp_ok}/{len(scores)}")
    print(f"total spend              {format_usd(spend)}")

    print()
    print("D2, is the printed total being copied or computed?")
    if suspicious:
        print(f"  WARNING: {len(suspicious)} receipt(s) have wrong items but still balance.")
        print("  That is the signature of a total derived from the item sum, which makes")
        print("  the reconciliation gate tautological. Investigate before trusting it:")
        for s in suspicious:
            print(f"    {s.stem}: {'; '.join(s.differences[:3])}")
    else:
        print("  No receipt had wrong items while still balancing. No evidence of a")
        print("  derived total in this run. Re-check whenever the prompt changes.")

    return 0 if exact == len(scores) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="count tokens only, costs nothing")
    parser.add_argument("--limit", type=int, default=None, help="score only the first N receipts")
    parser.add_argument("--model", default=None, help="override GEMINI_MODEL")
    args = parser.parse_args()

    # Real environment variables win over .env, which is what you want on a host.
    load_dotenv(ROOT / ".env", override=False)

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("GEMINI_API_KEY is not set. Copy .env.example to .env and fill it in.")
        print("It must be a PAID tier key: free tier content trains Google's models.")
        return 2

    if not EVAL_DIR.is_dir():
        print(f"No eval directory at {EVAL_DIR}")
        return 2

    paths = images(args.limit)
    if not paths:
        print(f"No receipt images in {EVAL_DIR}. Only 2 of the 32 PNGs are currently present.")
        return 2

    model = args.model or os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
    provider = GeminiProvider(api_key=api_key, model_id=model)
    print(f"model: {model}, receipts: {len(paths)}")
    print()

    return dry_run(provider, paths) if args.dry_run else live_run(provider, paths)


if __name__ == "__main__":
    raise SystemExit(main())
