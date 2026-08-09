"""Stage 2: decide a category for every line item. Brief 4.5 and 18.5.

The order is the whole design. Lexicon first, model only on what is left over,
and a weak answer becomes `uncategorized` rather than a guess.

    raw_name ──► lexicon ──exact/fuzzy──► category, done, cost nothing
                    │
                    └──miss──► log the tokens ──► one batched model call
                                                       │
                                          confident ───┴─── not confident
                                              │                  │
                                           category         uncategorized

Three properties this module is responsible for:

**Most receipts must make no API call at all.** That is the point of brief 4.5,
and it is measurable: on the real line items available today the lexicon covers
all twelve, so the fallback never fires. When it does fire it fires once per
receipt with every unknown item in it, not once per item.

**A miss is recorded, not swallowed.** The brief retracts its own guess at how
many terms the lexicon needs and says the answer comes out of `lexicon_misses`
after a month of real receipts. Misses are logged for items the lexicon could
not identify at all. Leftover tokens on an item that *did* match are not logged,
because they are brands, flavours and sizes, and drowning the weekly review in
`haldiram` and `pudina` would make the table useless for its one job.

**Nothing here writes to the database.** It returns decisions and the caller
persists them. That is what lets the same function serve the live confirm path
and the backfill tool without either one growing a special case.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from raseed.db.models import UNCATEGORIZED_SLUG, CategorySource
from raseed.enrichment import lexicon as lex
from raseed.enrichment.normalize import Normalized, normalize
from raseed.enrichment.providers.base import (
    CategorizationProvider,
    CategorizationProviderResult,
    CategorizationRequest,
    ProviderError,
)

log = logging.getLogger(__name__)

#: Confidence in basis points: 0 to 10000, integer. Not a float, for the same
#: reason nothing else in this codebase is one, and stored as basis points on the
#: line item so the column is comparable with what the model reported.
BASIS_POINTS: Final[int] = 10_000

#: Below this the model's answer is discarded and the item is filed as
#: `uncategorized`. Brief 18.5, verbatim: anything below threshold goes to
#: uncategorized rather than getting a guess.
#:
#: 0.60 rather than something tighter. The floor is not "probably right", it is
#: "better than the no-information answer", and `uncategorized` carries real
#: information too, namely that the lexicon needs a term. A too-high floor
#: silently converts a working fallback into an empty one.
DEFAULT_LLM_THRESHOLD: Final[float] = 0.60

#: The default Stage 2 model. Lives here rather than beside the Gemini client so
#: that `raseed.config` can name it without importing a vendor SDK.
#:
#: The cheap tier: $0.30 in and $2.50 out per million against Stage 1's $1.50
#: and $7.50. It also has a rate already on file in `extraction.pricing`, which
#: is the binding constraint rather than a preference: `cost_micros` refuses to
#: price a model it has no published rate for, and that refusal is what keeps
#: brief 16.6's daily cap honest. Moving to a newer lite model means verifying
#: its price first, not guessing it here.
DEFAULT_CATEGORIZER_MODEL_ID: Final[str] = "gemini-3.5-flash-lite"


@dataclass(frozen=True, slots=True)
class Item:
    """One line item, as Stage 2 sees it.

    Two strings and no price. Stage 2 is not shown what anything cost, because a
    category has no business being influenced by an amount, and a Stage 2 that
    cannot see money cannot corrupt a total.

    `quantity_text` is the receipt's own quantity column when it prints one, and
    is preferred over a size buried in the title.
    """

    raw_name: str
    quantity_text: str | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    """What Stage 2 concluded about one line item.

    `position` indexes into the transaction's line items, so a decision can be
    applied without carrying a row identity through the pipeline.
    """

    position: int
    raw_name: str
    category_slug: str
    source: CategorySource | None
    confidence_bp: int | None

    #: The lexicon's canonical name, which is what makes `Bhindi 500g` and
    #: `Okra 500g` the same product to a later query. Brief 3.7. Null when the
    #: lexicon did not know it, because a model is not asked to normalize.
    canonical_slug: str | None

    #: The printed name with the quantity stripped, and the quantity itself.
    #: Deterministic and independent of the lexicon: an item nothing recognises
    #: still gets a slug and a size out of `enrichment.normalize`. Brief 16.7.
    normalized: Normalized

    #: Tokens the lexicon did not know. Non-empty only when nothing matched.
    misses: tuple[str, ...] = ()

    @property
    def decided(self) -> bool:
        """False when neither the lexicon nor a model produced an answer."""
        return self.source is not None


@dataclass(frozen=True, slots=True)
class Outcome:
    """Every decision for one receipt, and what the fallback cost.

    `provider_result` is present only when the model was actually called, and is
    what the caller writes to `raw_extractions` so the Stage 2 spend counts
    against the daily cap in brief 16.6 like any other call.
    """

    decisions: tuple[Decision, ...]
    provider_result: CategorizationProviderResult | None = None
    lexicon_name: str = lex.DEFAULT_LEXICON
    lexicon_version: int = 0

    @property
    def cost_micros_usd(self) -> int:
        return self.provider_result.cost_micros_usd if self.provider_result else 0

    @property
    def called_model(self) -> bool:
        return self.provider_result is not None


def _from_match(
    position: int,
    raw_name: str,
    match: lex.Match,
    *,
    allowed: frozenset[str],
    normalized: Normalized,
) -> Decision:
    """Turn a lexicon hit into a decision.

    A term whose category is not in the taxonomy is not silently honoured. The
    YAML and the `categories` table can drift apart, and a category slug that no
    longer exists would produce a line item pointing at nothing. It becomes
    `uncategorized` with the source still recorded, so the drift shows up in a
    query rather than in a wrong total.
    """
    source = (
        CategorySource.LEXICON_EXACT
        if match.source is lex.Source.LEXICON_EXACT
        else CategorySource.LEXICON_FUZZY
    )
    confidence = match.confidence
    slug = match.term.category

    if slug not in allowed:
        log.warning(
            "lexicon term %r names category %r, which is not in the taxonomy",
            match.term.key,
            slug,
        )
        slug = UNCATEGORIZED_SLUG

    return Decision(
        position=position,
        raw_name=raw_name,
        category_slug=slug,
        source=source,
        confidence_bp=None if confidence is None else round(confidence * BASIS_POINTS),
        canonical_slug=match.term.canonical,
        normalized=normalized,
    )


def _unmatched(
    position: int, raw_name: str, misses: tuple[str, ...], *, normalized: Normalized
) -> Decision:
    """The decision for an item nothing has answered for yet.

    `source` is null rather than a fifth enum member. Brief 18.5 names four
    sources and every one of them is something that *decided*; "nobody decided"
    is the absence of one. It is also the exact query the backfill tool wants:
    `category_source IS NULL` is the set of rows worth re-running.
    """
    return Decision(
        position=position,
        raw_name=raw_name,
        category_slug=UNCATEGORIZED_SLUG,
        source=None,
        confidence_bp=None,
        canonical_slug=None,
        normalized=normalized,
        misses=misses,
    )


def categorize(
    items: Sequence[Item],
    *,
    allowed: tuple[str, ...],
    provider: CategorizationProvider | None = None,
    lexicon: lex.Lexicon | None = None,
    threshold: float = lex.FUZZY_THRESHOLD,
    llm_threshold: float = DEFAULT_LLM_THRESHOLD,
    lexicon_name: str = lex.DEFAULT_LEXICON,
) -> Outcome:
    """Categorize a receipt's line items, cheaply first.

    Args:
        items: Printed product names, in line-item order.
        allowed: Category slugs that exist for this user, from the `categories`
            table. `uncategorized` must be among them.
        provider: The model fallback. Omit it and unmatched items simply stay
            uncategorized, which is a supported mode rather than a degraded one:
            the backfill tool runs that way to price a lexicon change for free.
        lexicon: Loaded lexicon. Loaded from disk when omitted.
        threshold: Lexicon fuzzy-match floor, 0 to 100.
        llm_threshold: Model confidence floor, 0 to 1. Below it the answer is
            discarded and the item is filed as uncategorized. Brief 18.5.
        lexicon_name: Recorded on every miss, so the weekly review knows which
            lexicon failed to know the word.

    Returns:
        One `Decision` per name, in the order given, plus the provider result if
        the fallback ran.
    """
    if UNCATEGORIZED_SLUG not in allowed:
        msg = f"the taxonomy must contain {UNCATEGORIZED_SLUG!r}, got {allowed!r}"
        raise ValueError(msg)

    book = lexicon if lexicon is not None else lex.load(lexicon_name)
    permitted = frozenset(allowed)

    decisions: list[Decision] = []
    unresolved: list[int] = []

    for position, item in enumerate(items):
        raw_name = item.raw_name
        # Deterministic and independent of the lexicon, so an item nothing
        # recognises still gets a slug and a size. Brief 16.7.
        parsed = normalize(raw_name, quantity_text=item.quantity_text)

        matches, misses = lex.match_tokens(raw_name, lexicon=book, threshold=threshold)
        best = lex.best_of(matches)
        if best is not None:
            decisions.append(
                _from_match(position, raw_name, best, allowed=permitted, normalized=parsed)
            )
            continue

        decisions.append(_unmatched(position, raw_name, tuple(misses), normalized=parsed))
        unresolved.append(position)

    outcome = Outcome(
        decisions=tuple(decisions),
        lexicon_name=lexicon_name,
        lexicon_version=book.version,
    )
    if not unresolved or provider is None:
        return outcome

    return _apply_fallback(
        outcome,
        unresolved=unresolved,
        provider=provider,
        allowed=allowed,
        llm_threshold=llm_threshold,
    )


def _apply_fallback(
    outcome: Outcome,
    *,
    unresolved: list[int],
    provider: CategorizationProvider,
    allowed: tuple[str, ...],
    llm_threshold: float,
) -> Outcome:
    """One batched model call for everything the lexicon could not place.

    A provider failure is not an error here. The lexicon's answers are already
    good and the unmatched items are already `uncategorized`, so the honest
    outcome of a failed fallback is a receipt that stores fine with some items
    uncategorized, not a receipt the user loses. Brief 16.5 in spirit: never
    lose the receipt over a secondary call.
    """
    decisions = list(outcome.decisions)
    request = CategorizationRequest(
        names=tuple(decisions[i].raw_name for i in unresolved),
        allowed=allowed,
    )

    try:
        result = provider.categorize(request)
    except ProviderError:
        log.warning("stage 2 fallback failed, leaving %d items uncategorized", len(unresolved))
        return outcome

    permitted = frozenset(allowed)
    for guess in result.categorization.items:
        if not 0 <= guess.index < len(unresolved):
            log.warning("stage 2 returned index %d, which is not in the batch", guess.index)
            continue
        if guess.category not in permitted:
            log.warning("stage 2 returned category %r, which is not allowed", guess.category)
            continue

        position = unresolved[guess.index]
        confident = guess.confidence >= llm_threshold
        previous = decisions[position]
        decisions[position] = Decision(
            position=position,
            raw_name=previous.raw_name,
            category_slug=guess.category if confident else UNCATEGORIZED_SLUG,
            source=CategorySource.LLM,
            confidence_bp=round(guess.confidence * BASIS_POINTS),
            # A model is not asked what a product canonically is, only what
            # category it belongs to. The quantity parse is deterministic and
            # already done, so it carries across untouched.
            canonical_slug=None,
            normalized=previous.normalized,
            # The misses stand whether or not the model answered. The lexicon
            # still did not know the word, and that is what the log is for.
            misses=previous.misses,
        )

    return Outcome(
        decisions=tuple(decisions),
        provider_result=result,
        lexicon_name=outcome.lexicon_name,
        lexicon_version=outcome.lexicon_version,
    )


__all__ = [
    "BASIS_POINTS",
    "DEFAULT_CATEGORIZER_MODEL_ID",
    "DEFAULT_LLM_THRESHOLD",
    "Decision",
    "Item",
    "Outcome",
    "categorize",
]
