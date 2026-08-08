"""Run the reconciliation gate across the whole eval set.

This is a one-off check, not part of the test suite: `data/` is gitignored, so
the suite must not depend on it. Run it by hand after touching `reconcile.py`.

    .venv\\Scripts\\python tools\\sweep_eval.py

Every record in `data/eval/` was verified to balance before it was rendered, so a
failure here means the reconciliation logic is wrong rather than the data.

`blinkit_000` is a redacted real receipt rather than a synthetic one. It is
reported separately and excluded from the pass count, because it is the only
record whose arithmetic was not constructed to balance.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from raseed.extraction.schemas import ExtractionResult  # noqa: E402
from raseed.validation.reconcile import (  # noqa: E402
    MrpOutcome,
    Outcome,
    cross_check_mrp,
    reconcile,
)

EVAL_DIR = ROOT / "data" / "eval"
REAL_RECEIPT_STEM = "blinkit_000"


def load(path: pathlib.Path) -> ExtractionResult:
    """Read one expected.json and give it the Stage 1 reasoning header.

    The eval JSON is the input that rendered each image, so it holds what is
    printed and nothing more. A real extraction adds `is_receipt` and friends.
    """
    record: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return ExtractionResult.model_validate(
        {"is_receipt": True, "receipt_confidence": 1.0, "rejection_reason": None, **record}
    )


def main() -> int:
    if not EVAL_DIR.is_dir():
        print(f"No eval directory at {EVAL_DIR}. Nothing to sweep.")
        return 1

    paths = sorted(EVAL_DIR.glob("*.expected.json"))
    synthetic = [p for p in paths if not p.name.startswith(REAL_RECEIPT_STEM)]
    real = [p for p in paths if p.name.startswith(REAL_RECEIPT_STEM)]

    if not synthetic:
        print(f"No expected.json files in {EVAL_DIR}.")
        return 1

    failures: list[str] = []
    outcomes: dict[Outcome, int] = {}
    mrp_outcomes: dict[MrpOutcome, int] = {}
    total_savings = 0

    for path in synthetic:
        stem = path.name.removesuffix(".expected.json")
        result = load(path)
        verdict = reconcile(result)
        mrp = cross_check_mrp(result)

        outcomes[verdict.outcome] = outcomes.get(verdict.outcome, 0) + 1
        mrp_outcomes[mrp.outcome] = mrp_outcomes.get(mrp.outcome, 0) + 1
        total_savings += mrp.derived_savings_minor

        flag = "ok  " if verdict.accepted and mrp.passed else "FAIL"
        if flag == "FAIL":
            failures.append(f"{stem}: {verdict.reason or ''} {mrp.reason or ''}".strip())

        print(
            f"{flag} {stem}  items={len(result.line_items):2d}  "
            f"computed={verdict.computed_total_minor:7d}  "
            f"stated={verdict.stated_total_minor or 0:7d}  "
            f"delta={verdict.delta_minor if verdict.delta_minor is not None else 0:5d}  "
            f"{verdict.outcome.value:12s}  mrp={mrp.outcome.value}"
            f" saved={mrp.derived_savings_minor}"
        )

    print()
    print(f"records swept: {len(synthetic)}")
    for outcome, count in sorted(outcomes.items(), key=lambda kv: kv[0].value):
        print(f"  reconcile {outcome.value:28s} {count}")
    for mrp_outcome, count in sorted(mrp_outcomes.items(), key=lambda kv: kv[0].value):
        print(f"  mrp       {mrp_outcome.value:28s} {count}")
    print(f"  total product savings across the set: {total_savings} paise")

    for path in real:
        stem = path.name.removesuffix(".expected.json")
        result = load(path)
        verdict = reconcile(result)
        mrp = cross_check_mrp(result)
        print()
        print(f"redacted real receipt, reported separately and not counted: {stem}")
        print(f"  reconcile {verdict.outcome.value} delta={verdict.delta_minor} {verdict.reason}")
        print(f"  mrp       {mrp.outcome.value} {mrp.reason}")

    if failures:
        print()
        print(f"{len(failures)} FAILURES:")
        for line in failures:
            print(f"  {line}")
        return 1

    print()
    print(f"all {len(synthetic)} synthetic records accepted and passed the MRP cross-check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
