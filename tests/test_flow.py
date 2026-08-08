"""Tests for the receipt flow, the pending store and the image store.

No network, no bot token, no API key. The provider is a stub and the clock is
injected, so every branch including expiry and the daily cap is reachable.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from conftest import as_extraction_payload
from raseed.adapters.flow import (
    FlowConfig,
    FlowResult,
    ReceiptFlow,
    Step,
    parse_printed_date,
    rupees,
    summarise,
)
from raseed.adapters.images import ImageStore, sha256_of
from raseed.adapters.pending import (
    CALLBACK_DATA_LIMIT,
    CallbackDataTooLongError,
    PendingKey,
    PendingStore,
)
from raseed.db import queries
from raseed.db.ledger import NotStorableError, record_extraction, record_transaction
from raseed.db.models import DateSource, RawExtraction, Source, Transaction, User
from raseed.extraction.providers.base import (
    ExtractionRequest,
    ProviderResult,
    ProviderTransientError,
)
from raseed.extraction.schemas import ExtractionResult
from raseed.validation.reconcile import Outcome, reconcile

NOW = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
PNG = b"\x89PNG\r\n\x1a\n" + b"receipt-bytes"


def an_extraction(name: str = "blinkit_001", **overrides: object) -> ExtractionResult:
    return ExtractionResult.model_validate(as_extraction_payload(name, **overrides))


class StubProvider:
    """Returns whatever it was given, or raises it."""

    def __init__(self, outcome: ExtractionResult | Exception) -> None:
        self._outcome = outcome
        self.calls = 0

    @property
    def model_id(self) -> str:
        return "gemini-3.6-flash"

    def count_input_tokens(self, _request: ExtractionRequest) -> int:
        return 1970

    def extract(self, _request: ExtractionRequest) -> ProviderResult:
        self.calls += 1
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return ProviderResult(
            extraction=self._outcome,
            model_id=self.model_id,
            prompt_version="v1",
            response_text=self._outcome.model_dump_json(),
            input_tokens=1970,
            output_tokens=1280,
            cost_micros_usd=12_555,
        )


@pytest.fixture
def store(tmp_path: Path) -> ImageStore:
    return ImageStore(tmp_path / "incoming")


def make_flow(
    provider: StubProvider,
    store: ImageStore,
    *,
    now: dt.datetime = NOW,
    config: FlowConfig | None = None,
) -> ReceiptFlow:
    return ReceiptFlow(
        provider=provider,
        images=store,
        pending=PendingStore(),
        config=config or FlowConfig(),
        clock=lambda: now,
    )


def submit(
    flow: ReceiptFlow, session: Session, user: User, *, data: bytes = PNG, message_id: int = 1
) -> FlowResult:
    return flow.submit_image(
        session,
        user_id=user.id,
        chat_id=999,
        message_id=message_id,
        data=data,
        mime_type="image/png",
        message_date=NOW,
    )


# ---------------------------------------------------------------------------
# Money formatting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("minor", "text"),
    [(0, "₹0.00"), (100, "₹1.00"), (21900, "₹219.00"), (124050, "₹1,240.50"), (-5600, "-₹56.00")],
)
def test_rupees(minor: int, text: str) -> None:
    assert rupees(minor) == text


# ---------------------------------------------------------------------------
# Callback data (brief 16.4)
# ---------------------------------------------------------------------------


def test_a_key_round_trips_through_callback_data() -> None:
    key = PendingKey(chat_id=-1001234567890, message_id=45678)
    action, decoded = PendingKey.decode(key.encode("ok"))
    assert action == "ok"
    assert decoded == key


def test_callback_data_fits_telegrams_limit() -> None:
    """A real Telegram supergroup ID is the worst realistic case."""
    key = PendingKey(chat_id=-1009999999999999, message_id=999999999)
    assert len(key.encode("gap").encode()) <= CALLBACK_DATA_LIMIT


def test_oversized_callback_data_is_refused_not_truncated() -> None:
    key = PendingKey(chat_id=-1009999999999999, message_id=999999999)
    with pytest.raises(CallbackDataTooLongError):
        key.encode("x" * 60)


@pytest.mark.parametrize("junk", ["", "nonsense", "a|b", "a|b|c|d", "ok|notanint|1"])
def test_unrecognised_callback_data_is_rejected(junk: str) -> None:
    with pytest.raises(ValueError, match="unrecognised"):
        PendingKey.decode(junk)


def test_three_receipts_do_not_share_state(seeded: tuple[Session, User], store: ImageStore) -> None:
    """The bug brief 16.4 exists to prevent.

    Three receipts in flight at once, and confirming the third must commit the
    third, not the first.
    """
    session, user = seeded
    flow = make_flow(StubProvider(an_extraction()), store)

    pendings = [
        submit(flow, session, user, data=PNG + bytes([n]), message_id=n).pending
        for n in (10, 11, 12)
    ]
    assert all(p is not None for p in pendings)
    assert len({p.key for p in pendings if p is not None}) == 3

    third = pendings[2]
    assert third is not None
    result = flow.confirm(session, third.key)

    assert result.step is Step.STORED
    assert result.transaction is not None
    assert result.transaction.raw_extraction_id == third.raw_extraction_id
    # The other two are untouched and still awaiting a decision.
    for other in pendings[:2]:
        assert other is not None
        assert other.image_path is not None
        assert other.image_path.exists()


# ---------------------------------------------------------------------------
# The image store (invariants 7 and 9)
# ---------------------------------------------------------------------------


def test_bytes_are_stored_verbatim(store: ImageStore) -> None:
    """Invariant 9. Nothing is edited, rotated, compressed or enhanced."""
    stored = store.save(PNG, mime_type="image/png")
    assert stored.path.read_bytes() == PNG
    assert stored.sha256 == sha256_of(PNG)


def test_the_same_image_does_not_occupy_two_files(store: ImageStore) -> None:
    first = store.save(PNG, mime_type="image/png")
    second = store.save(PNG, mime_type="image/png")
    assert first.path == second.path
    assert len(store) == 1


def test_an_unsupported_type_is_refused(store: ImageStore) -> None:
    with pytest.raises(ValueError, match="unsupported image type"):
        store.save(PNG, mime_type="application/zip")


def test_deleting_a_missing_image_is_not_an_error(store: ImageStore) -> None:
    assert store.delete(store.root / "nope.png") is False


def test_the_sweep_clears_images_a_restart_would_strand(store: ImageStore) -> None:
    """The pending store is in memory, so a crash must not leak receipts."""
    stored = store.save(PNG, mime_type="image/png")
    removed = store.sweep(older_than=dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=1))
    assert stored.path in removed
    assert len(store) == 0


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_a_good_receipt_waits_for_confirmation(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    session, user = seeded
    flow = make_flow(StubProvider(an_extraction()), store)

    result = submit(flow, session, user)

    assert result.step is Step.AWAITING_CONFIRMATION
    assert result.pending is not None
    assert session.scalars(select(Transaction)).all() == []  # nothing in the ledger yet


def test_the_raw_extraction_is_recorded_before_any_decision(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """What the model said is worth keeping even if the receipt is discarded."""
    session, user = seeded
    flow = make_flow(StubProvider(an_extraction()), store)
    submit(flow, session, user)
    assert len(session.scalars(select(RawExtraction)).all()) == 1


def test_confirming_writes_the_ledger_and_deletes_the_image(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """Invariant 7."""
    session, user = seeded
    flow = make_flow(StubProvider(an_extraction()), store)
    pending = submit(flow, session, user).pending
    assert pending is not None
    image_path = pending.image_path
    assert image_path is not None and image_path.exists()

    result = flow.confirm(session, pending.key)
    session.commit()

    assert result.step is Step.STORED
    assert result.transaction is not None
    assert result.transaction.grand_total_minor == 21900
    assert not image_path.exists()


def test_discarding_stores_nothing_and_deletes_the_image(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    session, user = seeded
    flow = make_flow(StubProvider(an_extraction()), store)
    pending = submit(flow, session, user).pending
    assert pending is not None

    result = flow.discard(pending.key)
    session.commit()

    assert result.step is Step.DISCARDED
    assert session.scalars(select(Transaction)).all() == []
    assert pending.image_path is not None
    assert not pending.image_path.exists()


def test_a_decision_cannot_be_applied_twice(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """Double-tapping Confirm must not write two rows."""
    session, user = seeded
    flow = make_flow(StubProvider(an_extraction()), store)
    pending = submit(flow, session, user).pending
    assert pending is not None

    assert flow.confirm(session, pending.key).step is Step.STORED
    assert flow.confirm(session, pending.key).step is Step.EXPIRED


# ---------------------------------------------------------------------------
# Invariant 11 and the gate
# ---------------------------------------------------------------------------


def test_a_non_receipt_stores_nothing(seeded: tuple[Session, User], store: ImageStore) -> None:
    session, user = seeded
    not_a_receipt = ExtractionResult.model_validate(
        {
            "is_receipt": False,
            "receipt_confidence": 0.02,
            "rejection_reason": "A screenshot of a chat conversation.",
        }
    )
    flow = make_flow(StubProvider(not_a_receipt), store)

    result = submit(flow, session, user)
    session.commit()

    assert result.step is Step.NOT_A_RECEIPT
    assert "chat conversation" in result.message
    assert session.scalars(select(Transaction)).all() == []
    assert len(store) == 0


def test_a_class_1_failure_stores_nothing(seeded: tuple[Session, User], store: ImageStore) -> None:
    session, user = seeded
    no_total = an_extraction(grand_total_minor=None)
    flow = make_flow(StubProvider(no_total), store)

    result = submit(flow, session, user)
    assert result.step is Step.REJECTED
    assert len(store) == 0


def test_a_class_2_gap_must_be_accepted_before_it_can_be_stored(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    session, user = seeded
    off_by_five = an_extraction(grand_total_minor=22400)
    flow = make_flow(StubProvider(off_by_five), store)

    result = submit(flow, session, user)
    assert result.step is Step.AWAITING_CONFIRMATION
    assert result.pending is not None
    assert result.pending.reconciliation.outcome is Outcome.CLASS_2
    assert result.pending.gap_accepted is False
    assert "off the printed total" in result.message

    accepted = flow.accept_gap(result.pending.key)
    assert accepted.pending is not None
    assert accepted.pending.gap_accepted is True


def test_an_accepted_gap_is_stored_as_an_adjustment(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """Brief 3.3: the gap is recorded so the ledger stays arithmetically honest."""
    session, user = seeded
    flow = make_flow(StubProvider(an_extraction(grand_total_minor=22400)), store)
    pending = submit(flow, session, user).pending
    assert pending is not None
    flow.accept_gap(pending.key)

    result = flow.confirm(session, pending.key)
    session.commit()

    assert result.transaction is not None
    assert result.transaction.unaccounted_adjustment_minor == 500


def test_the_ledger_refuses_a_class_1_extraction(seeded: tuple[Session, User]) -> None:
    """Belt and braces: even if the flow let one through, the writer would not."""
    session, user = seeded
    extraction = an_extraction(grand_total_minor=None)
    raw = record_extraction(
        session,
        user_id=user.id,
        result=ProviderResult(
            extraction=extraction,
            model_id="gemini-3.6-flash",
            prompt_version="v1",
            response_text=extraction.model_dump_json(),
            input_tokens=1,
            output_tokens=1,
            cost_micros_usd=1,
        ),
        source=Source.TELEGRAM_IMAGE,
        image_sha256=None,
    )
    with pytest.raises(NotStorableError, match="class_1"):
        record_transaction(
            session,
            user_id=user.id,
            raw=raw,
            reconciliation=reconcile(extraction),
            occurred_on_local=dt.date(2026, 8, 8),
            date_source=DateSource.MESSAGE_TIMESTAMP,
        )


# ---------------------------------------------------------------------------
# Dedupe (brief 3.6)
# ---------------------------------------------------------------------------


def test_the_same_image_twice_is_caught(seeded: tuple[Session, User], store: ImageStore) -> None:
    session, user = seeded
    flow = make_flow(StubProvider(an_extraction()), store)
    pending = submit(flow, session, user).pending
    assert pending is not None
    flow.confirm(session, pending.key)
    session.commit()

    again = submit(flow, session, user, message_id=2)
    assert again.step is Step.DUPLICATE
    assert again.duplicate_of is not None
    assert "Already logged on" in again.message


def test_a_soft_deleted_row_does_not_block_a_resend(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """Resending is how you undo an undo."""
    session, user = seeded
    flow = make_flow(StubProvider(an_extraction()), store)
    pending = submit(flow, session, user).pending
    assert pending is not None
    stored = flow.confirm(session, pending.key)
    assert stored.transaction is not None
    stored.transaction.deleted_at = NOW
    session.commit()

    again = submit(flow, session, user, message_id=2)
    assert again.step is Step.AWAITING_CONFIRMATION


# ---------------------------------------------------------------------------
# The daily cost cap (brief 16.6)
# ---------------------------------------------------------------------------


def test_the_cap_refuses_before_spending(seeded: tuple[Session, User], store: ImageStore) -> None:
    session, user = seeded
    provider = StubProvider(an_extraction())
    flow = make_flow(provider, store, config=FlowConfig(daily_cost_limit_micros=10_000))

    first = submit(flow, session, user)
    session.commit()
    assert first.step is Step.AWAITING_CONFIRMATION
    assert provider.calls == 1

    second = submit(flow, session, user, data=b"different", message_id=2)
    assert second.step is Step.LIMIT_REACHED
    assert provider.calls == 1, "the provider must not be called once the cap is hit"


def test_spend_is_measured_over_a_rolling_window(seeded: tuple[Session, User]) -> None:
    session, user = seeded
    spent = queries.spend_micros_since(session, user_id=user.id, since=NOW - dt.timedelta(days=1))
    assert spent == 0


# ---------------------------------------------------------------------------
# Provider failure (brief 16.5)
# ---------------------------------------------------------------------------


def test_a_provider_failure_keeps_the_image(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """The receipt must not be lost when Gemini rate limits."""
    session, user = seeded
    flow = make_flow(StubProvider(ProviderTransientError("429")), store)

    result = submit(flow, session, user)

    assert result.step is Step.EXTRACTION_FAILED
    assert len(store) == 1, "the image survives so the receipt can be retried"


# ---------------------------------------------------------------------------
# Expiry (brief 16.4)
# ---------------------------------------------------------------------------


def test_a_pending_receipt_expires(seeded: tuple[Session, User], store: ImageStore) -> None:
    session, user = seeded
    pending_store = PendingStore()
    clock = {"now": NOW}
    flow = ReceiptFlow(
        provider=StubProvider(an_extraction()),
        images=store,
        pending=pending_store,
        config=FlowConfig(),
        clock=lambda: clock["now"],
    )
    pending = submit(flow, session, user).pending
    assert pending is not None

    clock["now"] = NOW + dt.timedelta(hours=25)
    assert flow.confirm(session, pending.key).step is Step.EXPIRED


def test_expiring_deletes_the_image(seeded: tuple[Session, User], store: ImageStore) -> None:
    """Invariant 7 does not stop applying because the user walked away."""
    session, user = seeded
    clock = {"now": NOW}
    flow = ReceiptFlow(
        provider=StubProvider(an_extraction()),
        images=store,
        pending=PendingStore(),
        config=FlowConfig(),
        clock=lambda: clock["now"],
    )
    pending = submit(flow, session, user).pending
    assert pending is not None and pending.image_path is not None

    clock["now"] = NOW + dt.timedelta(hours=25)
    dead = flow.expire_stale()

    assert len(dead) == 1
    assert not pending.image_path.exists()


# ---------------------------------------------------------------------------
# Invariant 10
# ---------------------------------------------------------------------------

#: Every way of telling someone to change how they send a receipt. The bot may
#: say it could not read one, which is a statement about itself, not a change of
#: method demanded of the user.
COACHING = (
    "crop",
    "retake",
    "rotate",
    "straighten",
    "flatten",
    "zoom",
    "closer",
    "lighting",
    "brighter",
    "flash",
    "as a file",
    "as a document",
    "uncompressed",
    "higher quality",
    "full screenshot",
    "scroll",
    "one at a time",
)


def every_terminal_message(session: Session, user: User, store: ImageStore) -> dict[Step, str]:
    """Reach every step the user can actually be shown, and collect its text."""
    messages: dict[Step, str] = {}

    good = make_flow(StubProvider(an_extraction()), store)
    awaiting = submit(good, session, user)
    messages[Step.AWAITING_CONFIRMATION] = awaiting.message
    assert awaiting.pending is not None
    messages[Step.STORED] = good.confirm(session, awaiting.pending.key).message
    session.commit()

    duplicate = submit(good, session, user, message_id=2)
    messages[Step.DUPLICATE] = duplicate.message

    second = submit(good, session, user, data=PNG + b"2", message_id=3)
    assert second.pending is not None
    messages[Step.DISCARDED] = good.discard(second.pending.key).message
    messages[Step.EXPIRED] = good.discard(second.pending.key).message

    failed = make_flow(StubProvider(ProviderTransientError("upstream timed out")), store)
    messages[Step.EXTRACTION_FAILED] = submit(
        failed, session, user, data=PNG + b"3", message_id=4
    ).message

    not_a_receipt = ExtractionResult.model_validate(
        {
            "is_receipt": False,
            "receipt_confidence": 0.01,
            "rejection_reason": "A photograph of a cat.",
        }
    )
    rejecting = make_flow(StubProvider(not_a_receipt), store)
    messages[Step.NOT_A_RECEIPT] = submit(
        rejecting, session, user, data=PNG + b"4", message_id=5
    ).message

    broke = make_flow(
        StubProvider(an_extraction()), store, config=FlowConfig(daily_cost_limit_micros=0)
    )
    messages[Step.LIMIT_REACHED] = submit(
        broke, session, user, data=PNG + b"5", message_id=6
    ).message

    return messages


def test_no_message_tells_the_user_how_to_send(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """Invariant 10, checked against every step the user can be shown."""
    session, user = seeded
    for step, message in every_terminal_message(session, user, store).items():
        lowered = message.lower()
        offenders = [phrase for phrase in COACHING if phrase in lowered]
        assert not offenders, f"{step.value} coaches the user: {offenders} in {message!r}"


def test_a_failure_may_still_say_it_could_not_read_one(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """The other half of invariant 10: silence is not required, only restraint."""
    session, user = seeded
    flow = make_flow(StubProvider(ProviderTransientError("upstream timed out")), store)
    result = submit(flow, session, user)
    assert result.step is Step.EXTRACTION_FAILED
    assert "could not read" in result.message.lower()


# ---------------------------------------------------------------------------
# Dates (brief 16.2 and 24.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-08-08 14:30", dt.date(2026, 8, 8)),
        ("08/08/2026", dt.date(2026, 8, 8)),
        ("8 Aug 2026", dt.date(2026, 8, 8)),
    ],
)
def test_a_printed_date_is_read(text: str, expected: dt.date) -> None:
    assert parse_printed_date(text, fallback_tz="Asia/Kolkata") == expected


@pytest.mark.parametrize("text", [None, "", "yesterday", "some time last week"])
def test_an_unreadable_date_is_not_invented(text: str | None) -> None:
    """Brief 24.4: never silently guess without recording that you guessed."""
    assert parse_printed_date(text, fallback_tz="Asia/Kolkata") is None


def test_a_receipt_with_no_printed_date_falls_back_to_the_message(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    session, user = seeded
    flow = make_flow(StubProvider(an_extraction()), store)
    pending = submit(flow, session, user).pending
    assert pending is not None
    assert pending.extraction.order_datetime_local is None

    result = flow.confirm(session, pending.key)
    session.commit()
    assert result.transaction is not None
    assert result.transaction.date_source is DateSource.MESSAGE_TIMESTAMP


# ---------------------------------------------------------------------------
# The summary the user confirms against (brief 3.4)
# ---------------------------------------------------------------------------


def test_the_summary_shows_the_items_and_the_total(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    session, user = seeded
    flow = make_flow(StubProvider(an_extraction("blinkit_026")), store)
    pending = submit(flow, session, user).pending
    assert pending is not None

    text = summarise(pending)
    assert "Real Mixed Fruit Juice 1L" in text
    assert "FLAT100 Promo Code" in text
    assert "₹339.00" in text
    assert "Saved ₹56.00 against MRP" in text
