"""The Stage 2 provider interface.

Deliberately a different Protocol from `ExtractionProvider` rather than a second
method on it. Invariant 4 keeps extraction and categorization apart, and one
interface with an `extract` and a `categorize` method would make it natural for
a future provider to answer both from a single call, which is exactly the thing
the invariant forbids.

It is also the practical shape. Stage 1 wants a vision model and Stage 2 wants a
cheap text model, and those do not have to be the same model, the same vendor,
or even the same machine.

A categorization provider takes product names and returns a category for each
plus what the call cost. It does not read the lexicon, does not decide what
counts as confident enough, and does not write anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from raseed.enrichment.schemas import CategorizationResult
from raseed.extraction.providers.base import (
    ProviderBlockedError,
    ProviderError,
    ProviderResponseError,
    ProviderTransientError,
)


@dataclass(frozen=True, slots=True)
class CategorizationRequest:
    """One batch of product names, and the categories they may be assigned.

    Batched per receipt rather than per item. A receipt with eleven unknown items
    is one call, not eleven, and the whole point of the lexicon is that most
    receipts make no call at all.

    `allowed` is passed in rather than imported, because the taxonomy lives in
    the `categories` table and grows from what accumulates in `uncategorized`
    (brief 3.8). A hard-coded list here would go stale the first time it does.
    """

    names: tuple[str, ...]
    allowed: tuple[str, ...]
    prompt_version: str = "categorize-v1"

    def __post_init__(self) -> None:
        if not self.names:
            msg = "a categorization request needs at least one name"
            raise ValueError(msg)
        if not self.allowed:
            msg = "a categorization request needs at least one allowed category"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class CategorizationProviderResult:
    """What came back, and what it cost.

    `response_text` is kept verbatim for the same reason Stage 1 keeps it: it is
    written to `raw_extractions.response_json`, and a text blob survives a schema
    change in this repo when a parsed structure would not.
    """

    categorization: CategorizationResult
    model_id: str
    prompt_version: str
    response_text: str
    input_tokens: int
    output_tokens: int
    cost_micros_usd: int


class CategorizationProvider(Protocol):
    """What Stage 2 needs from a text model."""

    @property
    def model_id(self) -> str:
        """The exact model string, as it will be recorded in `raw_extractions`."""
        ...

    def categorize(self, request: CategorizationRequest) -> CategorizationProviderResult:
        """Assign a category to every name in the request.

        Raises:
            ProviderTransientError: Retryable. Rate limit, timeout, 5xx.
            ProviderResponseError: Unusable response.
            ProviderBlockedError: The provider refused outright.
        """
        ...


__all__ = [
    "CategorizationProvider",
    "CategorizationProviderResult",
    "CategorizationRequest",
    "ProviderBlockedError",
    "ProviderError",
    "ProviderResponseError",
    "ProviderTransientError",
]
