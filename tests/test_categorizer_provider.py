"""The Gemini Stage 2 provider. Nothing here touches the network.

The properties worth pinning are the ones that would fail silently:

- The taxonomy is baked into the wire schema as an enum, so the decoder cannot
  invent a category. That is why there is no "the model returned a slug that
  does not exist" path to handle downstream.
- The call carries no tools and states that names are data. Brief 16.10.
- Stage 2 is a separate call against a separate schema. Invariant 4.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from google.genai import Client, errors

from raseed.enrichment import prompts
from raseed.enrichment.providers.base import (
    CategorizationRequest,
    ProviderBlockedError,
    ProviderResponseError,
    ProviderTransientError,
)
from raseed.enrichment.providers.gemini import (
    DEFAULT_MODEL_ID,
    SYSTEM_INSTRUCTION,
    GeminiCategorizer,
    render_names,
)
from raseed.enrichment.schemas import CategorizationResult, ItemCategory
from raseed.extraction.pricing import RATES

ALLOWED = ("groceries", "food-and-dining", "drinks", "entertainment", "uncategorized")


class FakeUsage:
    def __init__(self, prompt: int = 420, candidates: int = 60, thoughts: int = 0) -> None:
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.thoughts_token_count = thoughts


class FakeResponse:
    def __init__(
        self,
        *,
        parsed: Any = None,
        text: str = "",
        usage: FakeUsage | None = None,
        prompt_feedback: Any = None,
    ) -> None:
        self.parsed = parsed
        self.text = text
        self.usage_metadata = usage or FakeUsage()
        self.prompt_feedback = prompt_feedback


class FakeModels:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self.models = FakeModels(responses)


def categorizer(responses: list[Any], **kwargs: Any) -> GeminiCategorizer:
    return GeminiCategorizer(client=cast(Client, FakeClient(responses)), **kwargs)


def a_request(*names: str) -> CategorizationRequest:
    return CategorizationRequest(names=names or ("Banana",), allowed=ALLOWED)


def a_result(*pairs: tuple[int, str, float]) -> CategorizationResult:
    return CategorizationResult(
        items=[
            ItemCategory(index=index, category=category, confidence=confidence)
            for index, category, confidence in pairs
        ]
    )


def api_error(code: int) -> errors.APIError:
    cls = errors.ServerError if code >= 500 else errors.ClientError
    return cls(code, {"error": {"message": f"synthetic {code}", "code": code}})


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_a_categorization_round_trips() -> None:
    parsed = a_result((0, "groceries", 0.9))
    provider = categorizer([FakeResponse(parsed=parsed, text=parsed.model_dump_json())])
    result = provider.categorize(a_request())

    assert result.categorization == parsed
    assert result.model_id == DEFAULT_MODEL_ID
    assert result.prompt_version == "categorize-v1"
    assert result.input_tokens == 420
    assert result.output_tokens == 60


def test_the_raw_response_text_is_kept_verbatim() -> None:
    parsed = a_result((0, "drinks", 0.7))
    text = parsed.model_dump_json()
    provider = categorizer([FakeResponse(parsed=parsed, text=text)])
    assert provider.categorize(a_request()).response_text == text


def test_thinking_tokens_are_billed_as_output() -> None:
    parsed = a_result((0, "groceries", 0.9))
    usage = FakeUsage(prompt=400, candidates=50, thoughts=200)
    provider = categorizer([FakeResponse(parsed=parsed, usage=usage)])
    assert provider.categorize(a_request()).output_tokens == 250


def test_the_default_model_has_a_published_rate() -> None:
    """`cost_micros` refuses to price an unknown model, so an unpriced default
    would make every Stage 2 call raise instead of billing."""
    assert DEFAULT_MODEL_ID in RATES


def test_stage_two_is_cheaper_than_stage_one() -> None:
    assert (
        RATES[DEFAULT_MODEL_ID].input_micros_per_million
        < RATES["gemini-3.6-flash"].input_micros_per_million
    )


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


def test_the_taxonomy_is_pinned_into_the_wire_schema() -> None:
    """The decoder cannot emit a category that does not exist."""
    provider = categorizer([FakeResponse(parsed=a_result((0, "groceries", 0.9)))])
    provider.categorize(a_request())

    config = provider._client.models.calls[0]["config"]  # type: ignore[attr-defined]
    item = config.response_schema.properties["items"].items
    assert item.properties["category"].enum == list(ALLOWED)


def test_the_field_order_is_pinned() -> None:
    """Index, then category, then confidence. Brief 21.2: the model commits to
    which item it is talking about before it commits to an answer."""
    provider = categorizer([FakeResponse(parsed=a_result((0, "groceries", 0.9)))])
    provider.categorize(a_request())

    config = provider._client.models.calls[0]["config"]  # type: ignore[attr-defined]
    item = config.response_schema.properties["items"].items
    assert item.property_ordering == ["index", "category", "confidence"]


def test_the_call_carries_no_tools() -> None:
    """Brief 16.10. Nothing the model emits can trigger an action."""
    provider = categorizer([FakeResponse(parsed=a_result((0, "groceries", 0.9)))])
    provider.categorize(a_request())

    config = provider._client.models.calls[0]["config"]  # type: ignore[attr-defined]
    assert getattr(config, "tools", None) is None


def test_the_system_instruction_says_names_are_data() -> None:
    assert "DATA" in SYSTEM_INSTRUCTION
    assert "no tools" in SYSTEM_INSTRUCTION


def test_decoding_is_deterministic_by_default() -> None:
    provider = categorizer([FakeResponse(parsed=a_result((0, "groceries", 0.9)))])
    provider.categorize(a_request())

    config = provider._client.models.calls[0]["config"]  # type: ignore[attr-defined]
    assert config.temperature == 0.0


def test_the_prompt_and_the_names_reach_the_model() -> None:
    provider = categorizer([FakeResponse(parsed=a_result((0, "groceries", 0.9)))])
    provider.categorize(a_request("Bhindi 500g", "Dahi 400g"))

    contents = provider._client.models.calls[0]["contents"]  # type: ignore[attr-defined]
    body = contents.parts[0].text
    assert "0. Bhindi 500g" in body
    assert "1. Dahi 400g" in body
    assert "- groceries" in body
    assert "Categorization prompt" in body


def test_names_are_numbered_from_zero() -> None:
    """`index` is what maps an answer back onto a line item."""
    assert render_names(("a", "b")) == "0. a\n1. b"


def test_a_request_needs_a_taxonomy() -> None:
    with pytest.raises(ValueError, match="allowed category"):
        CategorizationRequest(names=("Banana",), allowed=())


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


def test_a_5xx_is_retried_and_then_succeeds() -> None:
    parsed = a_result((0, "groceries", 0.9))
    provider = categorizer(
        [api_error(503), FakeResponse(parsed=parsed)],
        max_attempts=2,
        temperature=0.0,
    )
    assert provider.categorize(a_request()).categorization == parsed


def test_a_4xx_is_not_retried() -> None:
    provider = categorizer([api_error(400), FakeResponse(parsed=a_result())], max_attempts=3)
    with pytest.raises(ProviderResponseError):
        provider.categorize(a_request())


def test_a_rate_limit_is_transient() -> None:
    provider = categorizer([api_error(429)], max_attempts=1)
    with pytest.raises(ProviderTransientError):
        provider.categorize(a_request())


def test_a_dropped_connection_becomes_a_transient_error() -> None:
    """The live regression of 2026-08-09.

    A DNS failure inside the SDK raises `httpx.ConnectError`, which is NOT an
    `errors.APIError`, so it used to travel straight through this provider, past
    every `ProviderError` handler above it, and take down whatever was calling.
    On the confirm path that meant a paid-for receipt that could not be saved.
    """
    provider = categorizer([OSError("[Errno 11001] getaddrinfo failed")], max_attempts=1)
    with pytest.raises(ProviderTransientError, match="could not reach gemini"):
        provider.categorize(a_request())


def test_a_dropped_connection_is_retried() -> None:
    """Transient, so the retry still gets its chances before anyone gives up."""
    parsed = a_result((0, "groceries", 0.9))
    provider = categorizer(
        [OSError("connection reset"), FakeResponse(parsed=parsed)],
        max_attempts=2,
    )
    assert provider.categorize(a_request()).categorization == parsed


def test_a_refusal_is_reported_as_blocked() -> None:
    feedback = type("Feedback", (), {"block_reason": "SAFETY"})()
    provider = categorizer([FakeResponse(prompt_feedback=feedback)])
    with pytest.raises(ProviderBlockedError):
        provider.categorize(a_request())


def test_an_empty_response_is_a_response_error() -> None:
    provider = categorizer([FakeResponse()])
    with pytest.raises(ProviderResponseError, match="neither"):
        provider.categorize(a_request())


def test_unparseable_text_is_a_response_error() -> None:
    provider = categorizer([FakeResponse(text="not json")])
    with pytest.raises(ProviderResponseError, match="not a valid categorization"):
        provider.categorize(a_request())


def test_a_plain_dict_is_still_validated() -> None:
    """A raw Schema makes the SDK hand back a dict rather than a model."""
    provider = categorizer(
        [FakeResponse(parsed={"items": [{"index": 0, "category": "drinks", "confidence": 0.8}]})]
    )
    result = provider.categorize(a_request())
    assert result.categorization.items[0].category == "drinks"


def test_a_client_needs_a_key_or_a_client() -> None:
    with pytest.raises(ValueError, match="api_key"):
        GeminiCategorizer()


# ---------------------------------------------------------------------------
# The prompt, which is versioned separately from Stage 1
# ---------------------------------------------------------------------------


def test_the_stage_two_prompt_exists_and_is_the_default() -> None:
    assert prompts.DEFAULT_VERSION in prompts.available()
    assert prompts.load().startswith("# Categorization prompt")


def test_the_prompt_version_cannot_be_confused_with_stage_ones() -> None:
    """Both stages write `prompt_version` to the same column."""
    assert prompts.DEFAULT_VERSION != "v1"


def test_the_prompt_states_the_rules_that_matter() -> None:
    text = prompts.load().lower()
    assert "uncategorized" in text
    assert "brand" in text
    assert "instruction" in text


@pytest.mark.parametrize("bad", ["../v1", "a/b", ".hidden", ""])
def test_a_prompt_version_cannot_be_a_path(bad: str) -> None:
    with pytest.raises(ValueError):
        prompts.load(bad)


def test_an_unknown_prompt_version_fails_loudly() -> None:
    with pytest.raises(prompts.PromptNotFoundError, match="Available"):
        prompts.load("categorize-v99")
