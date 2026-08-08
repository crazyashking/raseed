"""The provider interface.

Everything above this line in the stack talks to `ExtractionProvider` and never
to a vendor SDK. That is what makes swapping Gemini for a local Qwen3-VL a new
file rather than a refactor (brief section 4.5), and it is what lets the whole
pipeline be tested without a network.

The interface is deliberately narrow. A provider takes images and returns a
validated `ExtractionResult` plus what the call cost. It does not reconcile, does
not store, does not categorize, and does not decide whether the result is good
enough to keep.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from raseed.extraction.schemas import ExtractionResult


class ProviderError(RuntimeError):
    """Base class for every failure a provider can report."""


class ProviderTransientError(ProviderError):
    """Rate limited, timed out, or a 5xx. Worth retrying.

    Brief section 16.5: the receipt must not be lost when this happens. The
    image survives on disk until extraction succeeds and the user confirms.
    """


class ProviderResponseError(ProviderError):
    """The call succeeded but the response was not a usable extraction.

    Not retryable by itself: retrying an identical request against a
    deterministic decoder gets the same answer back.
    """


class ProviderBlockedError(ProviderError):
    """The provider refused to process the image at all, for example on safety.

    Handled separately from a transient failure because retrying will not help
    and the user needs a different message.
    """


@dataclass(frozen=True, slots=True)
class ImagePayload:
    """One image, as bytes, exactly as received.

    Never edited, rotated, cropped, upscaled or enhanced on the way here.
    Invariant 9.
    """

    data: bytes
    mime_type: str

    def __post_init__(self) -> None:
        if not self.data:
            msg = "image payload is empty"
            raise ValueError(msg)
        if not self.mime_type.startswith("image/"):
            msg = f"expected an image mime type, got {self.mime_type!r}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ExtractionRequest:
    """One receipt, which may be several images if it came from a PDF."""

    images: tuple[ImagePayload, ...]
    prompt_version: str = "v1"

    def __post_init__(self) -> None:
        if not self.images:
            msg = "an extraction request needs at least one image"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """What came back, and what it cost.

    `response_text` is the provider's raw output, stored verbatim in
    `raw_extractions.response_json`. Keeping the unparsed text means a future
    schema change in this repo cannot invalidate history. Invariant 5.
    """

    extraction: ExtractionResult
    model_id: str
    prompt_version: str
    response_text: str
    input_tokens: int
    output_tokens: int
    cost_micros_usd: int

    #: Billed at the output rate and already included in `output_tokens`.
    #: Broken out so an unexpectedly expensive receipt is diagnosable.
    thought_tokens: int = 0

    #: Anything the provider wants to keep that does not fit above.
    extra: dict[str, str] = field(default_factory=dict)


class ExtractionProvider(Protocol):
    """What Stage 1 needs from a vision model.

    Implementations live in this package: `gemini.py` now, `ollama.py` and
    `anthropic.py` later if the benchmark justifies them.
    """

    @property
    def model_id(self) -> str:
        """The exact model string, as it will be recorded in `raw_extractions`."""
        ...

    def count_input_tokens(self, request: ExtractionRequest) -> int:
        """How many input tokens this request would cost, without running it.

        Free on Gemini. Called before a paid request so an unexpectedly large
        image is caught before it is paid for, which matters most for the very
        tall receipts.
        """
        ...

    def extract(self, request: ExtractionRequest) -> ProviderResult:
        """Read the images and return a validated extraction.

        Raises:
            ProviderTransientError: Retryable. Rate limit, timeout, 5xx.
            ProviderResponseError: Unusable response.
            ProviderBlockedError: The provider refused outright.
        """
        ...


__all__ = [
    "ExtractionProvider",
    "ExtractionRequest",
    "ImagePayload",
    "ProviderBlockedError",
    "ProviderError",
    "ProviderResponseError",
    "ProviderResult",
    "ProviderTransientError",
]
