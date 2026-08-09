"""Stage 2: lexicon first, model second, uncategorized rather than a guess.

The rules under test come straight out of the brief:

- 4.5, the lexicon is checked before any API call, and a miss is logged.
- 18.5, `category_source` records how it was decided, and anything below the
  threshold goes to `uncategorized` rather than getting a guess.
- 16.5 in spirit, a secondary call must never be able to cost the user the
  receipt.

The provider here is a stub. Nothing in the suite spends money.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from raseed.db.models import CategorySource
from raseed.enrichment.categorize import (
    BASIS_POINTS,
    Decision,
    Item,
    Outcome,
    categorize,
)
from raseed.enrichment.providers.base import (
    CategorizationProviderResult,
    CategorizationRequest,
    ProviderTransientError,
)
from raseed.enrichment.schemas import CategorizationResult, ItemCategory

ALLOWED = ("groceries", "food-and-dining", "drinks", "entertainment", "uncategorized")


@dataclass
class StubCategorizer:
    """Answers with whatever it was handed, and records what it was asked."""

    answers: list[ItemCategory] = field(default_factory=list)
    error: Exception | None = None
    seen: list[CategorizationRequest] = field(default_factory=list)
    model_id: str = "stub-categorizer"

    def categorize(self, request: CategorizationRequest) -> CategorizationProviderResult:
        self.seen.append(request)
        if self.error is not None:
            raise self.error
        return CategorizationProviderResult(
            categorization=CategorizationResult(items=self.answers),
            model_id=self.model_id,
            prompt_version="categorize-v1",
            response_text="{}",
            input_tokens=100,
            output_tokens=20,
            cost_micros_usd=42,
        )


def only(outcome: Outcome) -> Decision:
    assert len(outcome.decisions) == 1
    return outcome.decisions[0]


# ---------------------------------------------------------------------------
# The lexicon path, which is the one that should carry almost everything
# ---------------------------------------------------------------------------


def test_an_exact_hit_never_reaches_the_model() -> None:
    stub = StubCategorizer()
    outcome = categorize([Item("Banana")], allowed=ALLOWED, provider=stub)

    assert stub.seen == []
    assert outcome.called_model is False
    assert outcome.cost_micros_usd == 0

    decision = only(outcome)
    assert decision.source is CategorySource.LEXICON_EXACT
    assert decision.category_slug == "groceries"
    assert decision.canonical_slug == "banana"


def test_an_exact_hit_records_no_confidence() -> None:
    """Brief 18.5: null for the exact path. There is nothing to be unsure of."""
    assert only(categorize([Item("Banana")], allowed=ALLOWED)).confidence_bp is None


def test_a_fuzzy_hit_records_its_confidence_in_basis_points() -> None:
    decision = only(categorize([Item("panner")], allowed=ALLOWED))
    assert decision.source is CategorySource.LEXICON_FUZZY
    assert decision.confidence_bp is not None
    assert 0 < decision.confidence_bp <= BASIS_POINTS


def test_a_lexicon_hit_logs_no_misses() -> None:
    assert only(categorize([Item("Green Cucumber")], allowed=ALLOWED)).misses == ()


def test_a_term_naming_a_category_that_does_not_exist_lands_uncategorized() -> None:
    """The YAML and the categories table can drift. Drift must not point at nothing."""
    narrow = ("uncategorized",)
    decision = only(categorize([Item("Banana")], allowed=narrow))
    assert decision.category_slug == "uncategorized"
    assert decision.source is CategorySource.LEXICON_EXACT


# ---------------------------------------------------------------------------
# Misses, which is how the lexicon is supposed to grow
# ---------------------------------------------------------------------------


def test_an_unknown_item_is_uncategorized_and_undecided() -> None:
    decision = only(categorize([Item("Colgate Strong Teeth")], allowed=ALLOWED))
    assert decision.category_slug == "uncategorized"
    assert decision.source is None
    assert decision.decided is False


def test_an_unknown_item_logs_its_tokens() -> None:
    decision = only(categorize([Item("Colgate Strong Teeth")], allowed=ALLOWED))
    assert decision.misses == ("colgate", "strong", "teeth")


def test_the_outcome_carries_the_lexicon_version() -> None:
    """Recorded on every miss, so "did adding terms help" is a query."""
    outcome = categorize([Item("Banana")], allowed=ALLOWED)
    assert outcome.lexicon_name == "hinglish"
    assert outcome.lexicon_version >= 1


# ---------------------------------------------------------------------------
# The model fallback
# ---------------------------------------------------------------------------


def test_the_fallback_sees_only_the_unmatched_items() -> None:
    stub = StubCategorizer(answers=[ItemCategory(index=0, category="groceries", confidence=0.9)])
    categorize([Item("Banana"), Item("Colgate Strong Teeth")], allowed=ALLOWED, provider=stub)

    assert len(stub.seen) == 1, "one batched call per receipt, not one per item"
    assert stub.seen[0].names == ("Colgate Strong Teeth",)


def test_a_confident_fallback_answer_is_used() -> None:
    stub = StubCategorizer(
        answers=[ItemCategory(index=0, category="entertainment", confidence=0.91)]
    )
    decision = only(categorize([Item("Colgate Strong Teeth")], allowed=ALLOWED, provider=stub))

    assert decision.category_slug == "entertainment"
    assert decision.source is CategorySource.LLM
    assert decision.confidence_bp == 9100


def test_a_weak_fallback_answer_is_discarded() -> None:
    """Brief 18.5, verbatim: below threshold goes to uncategorized, not a guess."""
    stub = StubCategorizer(
        answers=[ItemCategory(index=0, category="entertainment", confidence=0.31)]
    )
    decision = only(categorize([Item("Colgate Strong Teeth")], allowed=ALLOWED, provider=stub))

    assert decision.category_slug == "uncategorized"
    assert decision.source is CategorySource.LLM
    assert decision.confidence_bp == 3100, "the weak score is still recorded"


def test_the_misses_survive_a_fallback_answer() -> None:
    """The model answering does not mean the lexicon knew the word."""
    stub = StubCategorizer(answers=[ItemCategory(index=0, category="groceries", confidence=0.95)])
    decision = only(categorize([Item("Colgate Strong Teeth")], allowed=ALLOWED, provider=stub))
    assert decision.misses == ("colgate", "strong", "teeth")


def test_the_fallback_cost_is_reported() -> None:
    stub = StubCategorizer(answers=[ItemCategory(index=0, category="groceries", confidence=0.9)])
    outcome = categorize([Item("Colgate Strong Teeth")], allowed=ALLOWED, provider=stub)
    assert outcome.called_model is True
    assert outcome.cost_micros_usd == 42


def test_a_fallback_failure_does_not_raise() -> None:
    """A secondary call must never be able to cost the user the receipt."""
    stub = StubCategorizer(error=ProviderTransientError("rate limited"))
    outcome = categorize([Item("Colgate Strong Teeth")], allowed=ALLOWED, provider=stub)

    decision = only(outcome)
    assert decision.category_slug == "uncategorized"
    assert decision.source is None
    assert outcome.cost_micros_usd == 0


def test_an_out_of_range_index_is_ignored() -> None:
    stub = StubCategorizer(answers=[ItemCategory(index=7, category="groceries", confidence=0.99)])
    assert (
        only(categorize([Item("Colgate Strong Teeth")], allowed=ALLOWED, provider=stub)).source
        is None
    )


def test_a_category_outside_the_taxonomy_is_ignored() -> None:
    stub = StubCategorizer(answers=[ItemCategory(index=0, category="transport", confidence=0.99)])
    assert (
        only(categorize([Item("Colgate Strong Teeth")], allowed=ALLOWED, provider=stub)).source
        is None
    )


def test_no_provider_means_no_call_and_no_failure() -> None:
    outcome = categorize([Item("Colgate Strong Teeth")], allowed=ALLOWED, provider=None)
    assert outcome.called_model is False
    assert only(outcome).source is None


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------


def test_a_taxonomy_without_uncategorized_is_refused() -> None:
    """`uncategorized` is where every miss lands, so it is not optional."""
    with pytest.raises(ValueError, match="uncategorized"):
        categorize([Item("Banana")], allowed=("groceries",))


def test_positions_line_up_with_the_input() -> None:
    names = ["Banana", "Colgate Strong Teeth", "Green Cucumber"]
    outcome = categorize([Item(name) for name in names], allowed=ALLOWED)
    assert [d.position for d in outcome.decisions] == [0, 1, 2]
    assert [d.raw_name for d in outcome.decisions] == names


def test_the_quantity_parse_is_independent_of_the_lexicon() -> None:
    """An item nothing recognises still gets a slug and a size. Brief 16.7."""
    decision = only(categorize([Item("Colgate Strong Teeth 150g")], allowed=ALLOWED))

    assert decision.source is None
    assert decision.canonical_slug is None
    assert decision.normalized.normalized_slug == "colgate-strong-teeth"
    assert decision.normalized.quantity == 150
    assert decision.normalized.unit == "g"


def test_the_receipts_own_quantity_column_wins() -> None:
    """A receipt that bothers to print a quantity column is the better source."""
    decision = only(
        categorize([Item("Amul Taaza Toned Milk", quantity_text="1 L")], allowed=ALLOWED)
    )
    assert decision.normalized.quantity == 1000
    assert decision.normalized.unit == "l"
    assert decision.normalized.unit_normalized == "ml"


def test_the_quantity_parse_survives_the_model_fallback() -> None:
    stub = StubCategorizer(answers=[ItemCategory(index=0, category="groceries", confidence=0.9)])
    decision = only(categorize([Item("Colgate Strong Teeth 150g")], allowed=ALLOWED, provider=stub))
    assert decision.source is CategorySource.LLM
    assert decision.normalized.quantity == 150
    assert decision.normalized.normalized_slug == "colgate-strong-teeth"


def test_an_empty_receipt_is_fine() -> None:
    outcome = categorize([], allowed=ALLOWED)
    assert outcome.decisions == ()
    assert outcome.called_model is False


def test_a_request_with_no_names_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one name"):
        CategorizationRequest(names=(), allowed=ALLOWED)
