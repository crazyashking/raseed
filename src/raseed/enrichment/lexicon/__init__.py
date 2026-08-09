"""The Hinglish lexicon, and the fuzzy matcher over it. Brief section 4.5.

Most of Stage 2 should never touch a model. Indian grocery vocabulary is a
bounded list rather than an open one, which is the whole reason a lookup table
is viable here at all, so this runs **before** any API call and the model is the
fallback rather than the first move.

Three things are deliberate:

- **Fuzzy, never exact.** Spelling is inconsistent across merchants and across
  orders: bhindi/bhendi, dahi/dhai, paneer/panner, jeera/zeera. A dictionary
  lookup would miss most real receipts.
- **Token level, never whole name.** Brief 24.5. A real line reads
  `"Mr. Makhana Pudina Party Flavoured Makhana"`, and no whole-string match will
  ever find `makhana` in that. Tokens will.
- **A miss is data, not a failure.** Every unmatched token is worth recording,
  because the lexicon is meant to grow from real spending rather than from
  guesswork. The brief's own "300 to 400 terms" estimate was invented and
  retracted; the real answer comes out of the miss log.

Nothing here decides a category on a weak match. Below the threshold it returns
no match at all, and the caller sends it to the model or to `uncategorized`.
Brief 18.5: anything below threshold goes to uncategorized rather than getting a
guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from functools import cache
from pathlib import Path
from typing import Any, Final

import yaml
from rapidfuzz import fuzz, process

LEXICON_DIR: Final[Path] = Path(__file__).parent
DEFAULT_LEXICON: Final[str] = "hinglish"

#: Accept a fuzzy match at or above this similarity.
#:
#: **Measured, not chosen.** Scored `fuzz.ratio` over the spelling drift brief
#: 4.5 names, against grocery words that must NOT collapse into each other:
#:
#:     genuine drift, worst case      zeera/jeera     80.0
#:                                    bhendi/bhindi   83.3
#:                                    panner/paneer   83.3
#:     false positive, worst case     paneer/paper    72.7
#:                                    namak/namkeen   66.7
#:
#: 78 sits in that gap with room on both sides. `WRatio` was rejected: it scores
#: paneer/paper at 90, which would put a sheet of paper in the dairy aisle.
#:
#: Genuinely different words for the same thing (curd/dahi at 25, chhole/chana
#: at 36) are not drift and no threshold reaches them. Those are `variants`
#: entries, which is the honest mechanism for them.
#:
#: Tests pin both sides, so moving this number fails loudly rather than quietly
#: recategorising a year of receipts.
FUZZY_THRESHOLD: Final[float] = 78.0

#: Tokens shorter than this are dropped entirely. `dal`, `tel` and `gud` are
#: real grocery words, so this is 3 rather than something safer.
MIN_TOKEN_LENGTH: Final[int] = 3

#: Below this length a token must match EXACTLY. At 78 similarity a four-letter
#: token matches most of the lexicon, and a wrong category is worse than none.
#: Every drift pair in the measurement above is 5 characters or longer, so this
#: costs nothing real.
MIN_FUZZY_LENGTH: Final[int] = 5

#: Words that carry no product meaning. Stripped before matching so they never
#: become misses and never clutter the miss log into uselessness.
STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "pack",
        "packet",
        "pouch",
        "bottle",
        "box",
        "tin",
        "jar",
        "can",
        "bag",
        "combo",
        "value",
        "family",
        "party",
        "fresh",
        "premium",
        "classic",
        "special",
        "original",
        "regular",
        "large",
        "small",
        "medium",
        "mini",
        "ready",
        "instant",
        "flavoured",
        "flavored",
        "flavour",
        "flavor",
        "with",
        "and",
        "the",
        "for",
        "each",
        "unit",
        "units",
        "pcs",
        "piece",
        "pieces",
        "kilogram",
        "gram",
        "grams",
        "litre",
        "liter",
        "millilitre",
        "free",
        "offer",
        "new",
        "best",
        "quality",
        "natural",
        "pure",
        "select",
    }
)

#: Colour words that are also real products. Kept out of a tie so a product is
#: not named after its own adjective.
#:
#: Measured on the real line items: `Orange Carrot` matched both `orange` and
#: `carrot`, both exact, both six letters, and the tie went to whichever came
#: first, which filed a carrot as an orange. Colours precede the noun in these
#: names ("Green Cucumber", "Fresh White Eggs") while flavours follow it
#: ("Makhana Pudina"), so demoting the colour is right where a blanket
#: prefer-the-last-token rule would break `Mr. Makhana Pudina Flavoured Makhana`.
#:
#: Only demoted, never dropped: `Orange 1kg` still matches the fruit, because
#: there is nothing else in the name to prefer.
COLOUR_WORDS: Final[frozenset[str]] = frozenset(
    {"orange", "green", "white", "red", "black", "yellow", "brown"}
)

_SPLIT = re.compile(r"[^a-z0-9]+")


class Source(Enum):
    """How a category was decided. Brief 18.5."""

    LEXICON_EXACT = "lexicon_exact"
    LEXICON_FUZZY = "lexicon_fuzzy"
    LLM = "llm"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class Term:
    """One entry in the lexicon."""

    key: str
    canonical: str
    category: str
    variants: tuple[str, ...] = ()

    @property
    def surfaces(self) -> tuple[str, ...]:
        """Every spelling this term is looked up by.

        The canonical name is one of them, and that is not incidental. Real
        Indian quick-commerce receipts print mostly English: "Green Cucumber",
        "Orange Carrot", "Fresh White Eggs", not kheera, gajar and ande. A
        lexicon keyed only on the Hinglish word matched 3 of the first 12 real
        line items. Indexing `canonical` as a surface is what makes the Hinglish
        entries earn their keep on receipts that never say a Hindi word.
        """
        return (
            self.key.replace("_", " "),
            self.canonical.replace("_", " "),
            *self.variants,
        )


@dataclass(frozen=True, slots=True)
class Match:
    """A token matched to a term, and how confidently."""

    token: str
    term: Term
    source: Source
    score: float

    @property
    def confidence(self) -> float | None:
        """Null for an exact hit, a score for a fuzzy one. Brief 18.5."""
        return None if self.source is Source.LEXICON_EXACT else self.score / 100


class LexiconError(RuntimeError):
    """Raised when a lexicon file is missing or malformed."""


@dataclass(frozen=True, slots=True)
class Lexicon:
    """A loaded lexicon, ready to match against."""

    version: int
    terms: dict[str, Term]
    #: Every surface spelling mapped to its term, for exact lookup.
    by_surface: dict[str, Term] = field(default_factory=dict)

    #: Surfaces grouped by first letter. Both the fuzzy first-letter rule and a
    #: useful speedup: a fuzzy lookup scores one twentieth of the lexicon.
    _by_initial: dict[str, list[str]] = field(default_factory=dict)

    @property
    def surfaces(self) -> list[str]:
        return list(self.by_surface)

    @property
    def max_words(self) -> int:
        """Longest surface, in words. Bounds how wide an n-gram needs to be."""
        return max((len(s.split()) for s in self.by_surface), default=1)

    def exact(self, token: str) -> Term | None:
        return self.by_surface.get(token)

    def fuzzy(self, token: str, *, threshold: float = FUZZY_THRESHOLD) -> tuple[Term, float] | None:
        """The best surface above `threshold`, or nothing at all.

        `token_sort_ratio` rather than `WRatio`, which scores paneer against
        paper at 90 and would file a sheet of paper under dairy. Sorting tokens
        means `toor dal` and `dal toor` still find the same term.

        Returns None rather than a low-confidence guess. Brief 18.5: below
        threshold goes to uncategorized, it does not get a guess.
        """
        if len(token) < MIN_FUZZY_LENGTH:
            return None

        # A fuzzy match must agree on the first letter. Transliteration drift
        # happens in the middle of a word (vowels, doubled consonants):
        # bhendi/bhindi, panner/paneer, dhania/dhaniya, makhna/makhana all keep
        # their initial. Without this rule, `phone` scores 80 against `honey`
        # and a phone charger lands in groceries. Where the initial sound really
        # does change, jeera/zeera being the case brief 4.5 names, that is a
        # `variants` entry and matches exactly rather than fuzzily.
        candidates = self._by_initial.get(token[0])
        if not candidates:
            return None

        best = process.extractOne(
            token, candidates, scorer=fuzz.token_sort_ratio, score_cutoff=threshold
        )
        if best is None:
            return None
        surface, score, _ = best
        return self.by_surface[surface], float(score)


def tokenize(raw_name: str) -> list[str]:
    """Split a printed product name into candidate tokens. Brief 24.5.

    Token level, not whole name. Stopwords, short fragments and pure numbers are
    dropped, because they cannot identify a product and would otherwise flood
    the miss log with `pack`, `500` and `ml`.
    """
    tokens = []
    seen = set()
    for piece in _SPLIT.split(raw_name.lower()):
        if not piece or piece.isdigit():
            continue
        if len(piece) < MIN_TOKEN_LENGTH or piece in STOPWORDS:
            continue
        if piece not in seen:
            seen.add(piece)
            tokens.append(piece)
    return tokens


def _parse(payload: object, *, origin: str) -> Lexicon:
    if not isinstance(payload, dict):
        msg = f"{origin}: expected a mapping at the top level"
        raise LexiconError(msg)

    raw_terms = payload.get("terms")
    if not isinstance(raw_terms, dict):
        msg = f"{origin}: missing a 'terms' mapping"
        raise LexiconError(msg)

    terms: dict[str, Term] = {}
    by_surface: dict[str, Term] = {}

    for key, body in raw_terms.items():
        if not isinstance(body, dict):
            msg = f"{origin}: term {key!r} is not a mapping"
            raise LexiconError(msg)
        canonical = body.get("canonical")
        category = body.get("category")
        if not isinstance(canonical, str) or not isinstance(category, str):
            msg = f"{origin}: term {key!r} needs both 'canonical' and 'category' as strings"
            raise LexiconError(msg)

        variants = body.get("variants") or []
        if not isinstance(variants, list) or not all(isinstance(v, str) for v in variants):
            msg = f"{origin}: term {key!r} has a malformed 'variants' list"
            raise LexiconError(msg)

        term = Term(
            key=str(key),
            canonical=canonical,
            category=category,
            variants=tuple(str(v).lower() for v in variants),
        )
        terms[term.key] = term

        for surface in term.surfaces:
            existing = by_surface.get(surface)
            if existing is not None and existing.key != term.key:
                msg = (
                    f"{origin}: {surface!r} is claimed by both {existing.key!r} and "
                    f"{term.key!r}. A surface spelling must resolve to one term."
                )
                raise LexiconError(msg)
            by_surface[surface] = term

    version = payload.get("version", 1)
    if not isinstance(version, int):
        msg = f"{origin}: 'version' must be an integer"
        raise LexiconError(msg)

    by_initial: dict[str, list[str]] = {}
    for surface in by_surface:
        by_initial.setdefault(surface[0], []).append(surface)

    return Lexicon(version=version, terms=terms, by_surface=by_surface, _by_initial=by_initial)


def parse(text: str, *, origin: str = "<string>") -> Lexicon:
    """Parse lexicon YAML.

    Uses `safe_load`. The file is ours today, but a per-user lexicon layer is
    named in brief 7.5, and `yaml.load` on user-supplied text constructs
    arbitrary Python objects.
    """
    try:
        payload: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        msg = f"{origin}: not valid YAML. {exc}"
        raise LexiconError(msg) from exc
    return _parse(payload, origin=origin)


@cache
def load(name: str = DEFAULT_LEXICON) -> Lexicon:
    """Load a named lexicon from `enrichment/lexicon/`.

    Cached, because it is read on every line item of every receipt.

    Raises:
        LexiconError: No such lexicon, or the file is malformed.
    """
    if "/" in name or "\\" in name or name.startswith("."):
        msg = f"lexicon name {name!r} may not contain a path"
        raise LexiconError(msg)

    path = LEXICON_DIR / f"{name}.yaml"
    if not path.is_file():
        available = ", ".join(sorted(p.stem for p in LEXICON_DIR.glob("*.yaml"))) or "none"
        msg = f"no lexicon named {name!r}. Available: {available}"
        raise LexiconError(msg)

    return parse(path.read_text(encoding="utf-8"), origin=str(path))


def ngrams(tokens: list[str], width: int) -> list[tuple[int, str]]:
    """Every run of `width` consecutive tokens, with its start index."""
    return [(i, " ".join(tokens[i : i + width])) for i in range(len(tokens) - width + 1)]


def match_tokens(
    raw_name: str,
    *,
    lexicon: Lexicon | None = None,
    threshold: float = FUZZY_THRESHOLD,
) -> tuple[list[Match], list[str]]:
    """Match a product name against the lexicon, and report what did not match.

    Matching runs widest first. A third of the lexicon is multi-word (`moong
    dal`, `garam masala`, `shimla mirch`), and single-token matching could never
    reach any of it: "Haldiram's Moong Dal" tokenizes to `moong` and `dal`, and
    neither one alone is a surface. Trying the bigram `moong dal` before the
    unigrams is what makes those entries reachable at all.

    Longest wins, and a token consumed by a wider match is not offered to a
    narrower one, so `moong dal` does not also register as a loose `dal`.

    Returns the matches and the leftover tokens. The leftovers matter as much as
    the matches: they are what lands in `lexicon_misses`, and the lexicon is
    meant to grow from them rather than from guesswork.
    """
    book = lexicon if lexicon is not None else load()
    tokens = tokenize(raw_name)
    matches: list[Match] = []
    taken: set[int] = set()

    for width in range(min(book.max_words, len(tokens)), 0, -1):
        for start, phrase in ngrams(tokens, width):
            span = range(start, start + width)
            if any(i in taken for i in span):
                continue

            term = book.exact(phrase)
            source, score = Source.LEXICON_EXACT, 100.0
            if term is None:
                # A one-word token is too short to fuzzy-match safely; a phrase
                # has enough signal that a typo in one word still resolves.
                found = book.fuzzy(phrase, threshold=threshold)
                if found is None:
                    continue
                term, score = found
                source = Source.LEXICON_FUZZY

            matches.append(Match(token=phrase, term=term, source=source, score=score))
            taken.update(span)

    misses = [token for index, token in enumerate(tokens) if index not in taken]
    return matches, misses


def best_of(matches: list[Match]) -> Match | None:
    """The most confident match in a set, or nothing.

    A colour is set aside first, then exact beats fuzzy, then the higher score,
    then the longer token: on `"Mr. Makhana Pudina Party Flavoured Makhana"`
    both `makhana` and `pudina` hit, and `makhana` is the product while `pudina`
    is the flavour.
    """
    if not matches:
        return None
    return max(
        matches,
        key=lambda m: (
            m.token not in COLOUR_WORDS,
            m.source is Source.LEXICON_EXACT,
            m.score,
            len(m.token),
        ),
    )


def best_match(
    raw_name: str,
    *,
    lexicon: Lexicon | None = None,
    threshold: float = FUZZY_THRESHOLD,
) -> Match | None:
    """The single most confident match in a product name, or nothing."""
    matches, _ = match_tokens(raw_name, lexicon=lexicon, threshold=threshold)
    return best_of(matches)


__all__ = [
    "COLOUR_WORDS",
    "DEFAULT_LEXICON",
    "FUZZY_THRESHOLD",
    "LEXICON_DIR",
    "MIN_TOKEN_LENGTH",
    "STOPWORDS",
    "Lexicon",
    "LexiconError",
    "Match",
    "Source",
    "Term",
    "best_match",
    "best_of",
    "load",
    "match_tokens",
    "parse",
    "tokenize",
]
