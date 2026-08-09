"""The Hinglish lexicon, and the matcher over it. Brief 4.5 and 24.5.

Two halves, and the second one is the point:

**The shipped lexicon is a data file and gets tested like one.** Every category
slug has to exist in the taxonomy, every surface has to resolve to exactly one
term, and the real line items have to match. A YAML typo here is a wrong monthly
total, not a crash.

**The thresholds are pinned from both sides.** `FUZZY_THRESHOLD` was measured
against genuine spelling drift on one side and words that must not collapse into
each other on the other. Tests hold both edges, so moving the number fails loudly
rather than quietly recategorising a year of receipts.
"""

from __future__ import annotations

import pytest

from raseed.db.models import SEED_CATEGORIES
from raseed.enrichment.lexicon import (
    FUZZY_THRESHOLD,
    MIN_FUZZY_LENGTH,
    Lexicon,
    LexiconError,
    Source,
    best_match,
    load,
    match_tokens,
    parse,
    tokenize,
)

TAXONOMY = {slug for slug, _ in SEED_CATEGORIES}

#: The twelve line items off the two real receipts sent through the bot on
#: 2026-08-08. Not synthetic: this is what Indian quick commerce actually prints,
#: which is mostly English with a Hinglish product word buried in it.
REAL_LINE_ITEMS = (
    ("Act II Sour Cream & Cheese Popcorn - Ready to Eat", "popcorn"),
    ("Green Cucumber", "cucumber"),
    ("Mr. Makhana Pudina Party Flavoured Makhana", "fox_nut"),
    ("Banana", "banana"),
    ("Orange Carrot", "carrot"),
    ("Nandini Toned Fresh Milk | Pouch", "milk"),
    ("Fresh White Eggs", "egg"),
    ("Haldiram's Moong Dal | Crispy Fried Lentil Snack", "mung_bean"),
    ("Parle-G Gold Biscuit", "biscuit"),
    ("Godrej Jersey Curd Tub", "yogurt"),
    ("English Oven Milk Bread", "bread"),
)

#: Brief 4.5 names these spellings explicitly. Every one has to reach its term.
DRIFT = (
    ("bhendi", "bhindi"),
    ("dhai", "dahi"),
    ("panner", "paneer"),
    ("zeera", "jeera"),
    ("dhania", "dhaniya"),
    ("makhna", "makhana"),
)

#: Pairs that must NOT collapse. `phone`/`honey` scores 80 on `token_sort_ratio`,
#: above the threshold, and is held apart by the first-letter rule instead. That
#: is the case that decides the rule exists at all: a phone charger in groceries.
MUST_NOT_MATCH = ("phone", "paper", "papercup", "greeting")


@pytest.fixture(scope="module")
def book() -> Lexicon:
    return load()


# ---------------------------------------------------------------------------
# The shipped file
# ---------------------------------------------------------------------------


def test_the_lexicon_loads(book: Lexicon) -> None:
    assert book.terms
    assert book.version >= 1


def test_every_category_exists_in_the_taxonomy(book: Lexicon) -> None:
    """A slug here that is not a real category files an item under nothing."""
    for term in book.terms.values():
        assert term.category in TAXONOMY, f"{term.key} names {term.category!r}"


def test_every_surface_resolves_to_one_term(book: Lexicon) -> None:
    for surface, term in book.by_surface.items():
        assert surface == surface.lower()
        assert term.key in book.terms


def test_the_canonical_name_is_a_surface(book: Lexicon) -> None:
    """The finding that made the lexicon work at all.

    Keyed only on the Hinglish word, it matched 3 of the first 12 real line
    items, because real receipts print "Green Cucumber" rather than "kheera".
    """
    assert book.exact("cucumber") is not None
    assert book.exact("kheera") is not None
    assert book.exact("cucumber") is book.exact("kheera")


# ---------------------------------------------------------------------------
# Matching, measured against the real receipts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("raw_name", "canonical"), REAL_LINE_ITEMS)
def test_real_line_items_match(raw_name: str, canonical: str) -> None:
    match = best_match(raw_name)
    assert match is not None, f"{raw_name!r} matched nothing"
    assert match.term.canonical == canonical


@pytest.mark.parametrize(("spelling", "key"), DRIFT)
def test_spelling_drift_still_reaches_its_term(spelling: str, key: str) -> None:
    match = best_match(spelling)
    assert match is not None, f"{spelling!r} matched nothing"
    assert match.term.key == key


@pytest.mark.parametrize("token", MUST_NOT_MATCH)
def test_words_that_must_not_collapse_into_a_grocery(token: str) -> None:
    assert best_match(token) is None, f"{token!r} matched something it should not"


def test_namkeen_does_not_collapse_into_namak() -> None:
    """The other measured pair: salt and a snack mix, 66.7 apart.

    Both are real terms, so this is not about matching nothing. It is about the
    two staying separate, which is what the threshold is holding open.
    """
    match = best_match("namkeen")
    assert match is not None
    assert match.term.key == "namkeen"


def test_a_colour_does_not_win_over_the_product() -> None:
    """`Orange Carrot` matched both, both exact, both six letters.

    The tie went to whichever came first, which filed a carrot as an orange.
    """
    match = best_match("Orange Carrot")
    assert match is not None
    assert match.term.canonical == "carrot"


def test_a_colour_still_wins_when_it_is_the_product() -> None:
    match = best_match("Orange 1kg")
    assert match is not None
    assert match.term.canonical == "orange"


def test_a_flavour_after_the_product_does_not_win() -> None:
    """The mirror case. Colours precede the noun, flavours follow it."""
    match = best_match("Mr. Makhana Pudina Party Flavoured Makhana")
    assert match is not None
    assert match.term.canonical == "fox_nut"


def test_multi_word_terms_are_reachable() -> None:
    """A third of the lexicon is multi-word and no unigram match can reach it."""
    match = best_match("Haldiram's Moong Dal | Crispy Fried Lentil Snack")
    assert match is not None
    assert match.term.key == "moong_dal"


def test_a_wide_match_consumes_its_tokens() -> None:
    """`moong dal` must not also register as a loose `dal`."""
    matches, _ = match_tokens("Moong Dal")
    assert [m.token for m in matches] == ["moong dal"]


def test_an_exact_match_reports_no_confidence() -> None:
    match = best_match("paneer")
    assert match is not None
    assert match.source is Source.LEXICON_EXACT
    assert match.confidence is None


def test_a_fuzzy_match_reports_its_score() -> None:
    match = best_match("panner")
    assert match is not None
    assert match.source is Source.LEXICON_FUZZY
    assert match.confidence is not None
    assert 0.0 < match.confidence <= 1.0


# ---------------------------------------------------------------------------
# The threshold, held from both sides
# ---------------------------------------------------------------------------


def test_the_threshold_sits_between_the_measurements(book: Lexicon) -> None:
    """Below the worst genuine drift, above the worst false positive.

    Measured: zeera/jeera 80.0 is the tightest real pair, paneer/paper 72.7 the
    loosest false one. Anything outside that gap is a different design, not a
    tuning change.
    """
    assert 72.7 < FUZZY_THRESHOLD < 80.0
    assert book.fuzzy("panner") is not None
    assert book.fuzzy("paper") is None


def test_a_short_token_must_match_exactly(book: Lexicon) -> None:
    """At 78 similarity a four-letter token matches half the lexicon."""
    assert len("atta") < MIN_FUZZY_LENGTH
    assert book.fuzzy("atta") is None
    assert book.exact("atta") is not None


def test_a_fuzzy_match_must_agree_on_the_first_letter(book: Lexicon) -> None:
    """`phone` scores 80 against `honey`, which is above the threshold."""
    assert book.fuzzy("phone") is None


# ---------------------------------------------------------------------------
# Tokenizing
# ---------------------------------------------------------------------------


def test_tokenize_drops_noise() -> None:
    tokens = tokenize("Amul Taaza Toned Milk 500 ml Pack of 2")
    assert "pack" not in tokens
    assert "500" not in tokens
    assert "ml" not in tokens
    assert "milk" in tokens


def test_tokenize_keeps_short_real_words() -> None:
    """`dal`, `tel` and `gud` are real grocery words."""
    assert "dal" in tokenize("Toor Dal 1kg")


def test_tokenize_deduplicates() -> None:
    assert tokenize("Makhana Makhana").count("makhana") == 1


def test_a_matched_item_reports_no_misses() -> None:
    _, misses = match_tokens("Banana")
    assert misses == []


def test_an_unmatched_item_reports_its_tokens() -> None:
    """What the miss log is fed. These are the words worth reviewing weekly."""
    matches, misses = match_tokens("Colgate Strong Teeth")
    assert matches == []
    assert misses == ["colgate", "strong", "teeth"]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_rejects_a_missing_terms_mapping() -> None:
    with pytest.raises(LexiconError, match="terms"):
        parse("version: 1\n")


def test_parse_rejects_a_term_without_a_category() -> None:
    with pytest.raises(LexiconError, match="canonical"):
        parse("terms:\n  bhindi: { canonical: okra }\n")


def test_parse_rejects_two_terms_claiming_one_surface() -> None:
    text = (
        "terms:\n"
        "  bhindi: { canonical: okra, category: groceries }\n"
        "  okra: { canonical: okra_pods, category: groceries }\n"
    )
    with pytest.raises(LexiconError, match="one term"):
        parse(text)


def test_parse_rejects_a_non_integer_version() -> None:
    with pytest.raises(LexiconError, match="version"):
        parse("version: one\nterms: {}\n")


def test_load_refuses_a_path() -> None:
    with pytest.raises(LexiconError, match="path"):
        load("../../../etc/passwd")


def test_load_reports_what_is_available() -> None:
    with pytest.raises(LexiconError, match="Available"):
        load("marathi")
