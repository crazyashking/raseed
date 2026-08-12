"""Tests for the receipt flow, the pending store and the image store.

No network, no bot token, no API key. The provider is a stub and the clock is
injected, so every branch including expiry and the daily cap is reachable.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import as_extraction_payload
from raseed.adapters.flow import (
    FlowConfig,
    FlowResult,
    ReceiptFlow,
    Step,
    month_start_utc,
    parse_printed_date,
    rupees,
    summarise,
)
from raseed.adapters.images import ImageStore, sha256_of
from raseed.adapters.pending import (
    CALLBACK_DATA_LIMIT,
    CallbackDataTooLongError,
    DatabasePendingStore,
    InMemoryPendingStore,
    PendingKey,
)
from raseed.db import queries
from raseed.db.engine import create_engine
from raseed.db.ledger import NotStorableError, record_extraction, record_transaction
from raseed.db.models import (
    Base,
    CategorySource,
    DateSource,
    ExtractionStage,
    LexiconMiss,
    PendingReceiptRow,
    RawExtraction,
    Source,
    Transaction,
    TransactionLineItem,
    User,
)
from raseed.db.seed import bootstrap
from raseed.enrichment.providers.base import (
    CategorizationProviderResult,
    CategorizationRequest,
)
from raseed.enrichment.schemas import CategorizationResult, ItemCategory
from raseed.extraction.providers.base import (
    ExtractionRequest,
    ProviderResult,
    ProviderTransientError,
)
from raseed.extraction.schemas import ExtractionResult
from raseed.timezones import zone
from raseed.validation.reconcile import Outcome, reconcile

NOW = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
PNG = b"\x89PNG\r\n\x1a\n" + b"receipt-bytes"

#: A day well before `NOW`, for the case the date question exists to serve:
#: someone photographing a bill a week after they paid it.
BACKDATED = dt.date(2026, 8, 1)


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


class StubCategorizer:
    """Stage 2's model, which must never be reached for a known product."""

    def __init__(self, answers: list[ItemCategory] | None = None) -> None:
        self.answers = answers or []
        self.calls = 0

    @property
    def model_id(self) -> str:
        return "gemini-3.5-flash-lite"

    def categorize(self, _request: CategorizationRequest) -> CategorizationProviderResult:
        self.calls += 1
        return CategorizationProviderResult(
            categorization=CategorizationResult(items=self.answers),
            model_id=self.model_id,
            prompt_version="categorize-v1",
            response_text="{}",
            input_tokens=300,
            output_tokens=40,
            cost_micros_usd=95,
        )


def make_flow(
    provider: StubProvider,
    store: ImageStore,
    *,
    now: dt.datetime = NOW,
    config: FlowConfig | None = None,
    categorizer: StubCategorizer | None = None,
) -> ReceiptFlow:
    return ReceiptFlow(
        provider=provider,
        images=store,
        pending=InMemoryPendingStore(),
        config=config or FlowConfig(),
        clock=lambda: now,
        categorizer=categorizer,
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


def commit(flow: ReceiptFlow, session: Session, key: PendingKey) -> FlowResult:
    """Confirm, and answer the date question if the receipt raises one.

    Every committed fixture has `order_datetime_local` null, because that is
    what the app screenshots in brief 24.4 actually look like, so confirming one
    always asks when it was. Tests that are about something else say so by going
    through here; tests that are about the date question call `confirm` and
    `set_date` directly and assert on both halves.
    """
    result = flow.confirm(session, key)
    if result.step is Step.AWAITING_DATE:
        result = flow.set_date(session, key, NOW.date())
    return result


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
    result = commit(flow, session, third.key)

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

    result = commit(flow, session, pending.key)
    session.commit()

    assert result.step is Step.STORED
    assert result.transaction is not None
    assert result.transaction.grand_total_minor == 21900
    assert not image_path.exists()


# ---------------------------------------------------------------------------
# A confirm that fails must not cost the user the receipt
#
# Live regression, 2026-08-09: three receipts were extracted and paid for, and
# none of the three reached the ledger. `confirm` used to pop the pending entry
# before doing any of the work that can fail, so a single failure left a button
# on screen with nothing behind it, and the only way forward was to resend and
# pay for extraction again.
# ---------------------------------------------------------------------------


def test_a_failed_confirm_leaves_the_button_usable(
    seeded: tuple[Session, User], store: ImageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receipt survives the failure and the same button works on the retry."""
    session, user = seeded
    flow = make_flow(StubProvider(an_extraction()), store)
    pending = submit(flow, session, user).pending
    assert pending is not None

    def explode(*_args: object, **_kwargs: object) -> Transaction:
        msg = "the connection went away mid-write"
        raise RuntimeError(msg)

    monkeypatch.setattr("raseed.adapters.flow.ledger.record_transaction", explode)

    with pytest.raises(RuntimeError):
        commit(flow, session, pending.key)

    # The entry is still there, the image is still there, nothing was stored.
    assert flow.pending_for(session, pending.key) is not None
    assert pending.image_path is not None
    assert pending.image_path.exists()
    assert session.scalars(select(Transaction)).all() == []

    # And the second tap on the same button works.
    monkeypatch.undo()
    result = commit(flow, session, pending.key)
    assert result.step is Step.STORED
    assert not pending.image_path.exists()


def test_stage_two_failing_never_costs_the_receipt(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """A categorizer that raises something outside the provider taxonomy.

    `httpx.ConnectError` is the real one: a DNS failure inside the Gemini SDK is
    not an `APIError`, so it used to escape every `ProviderError` handler
    between there and here and take the confirm down with it.
    """

    class Unreachable:
        model_id = "gemini-3.5-flash-lite"

        def categorize(self, _request: CategorizationRequest) -> CategorizationProviderResult:
            msg = "[Errno 11001] getaddrinfo failed"
            raise OSError(msg)

    session, user = seeded
    unknown = an_extraction(
        line_items=[
            {
                "raw_name": "Colgate Strong Teeth",
                "quantity_text": None,
                "mrp_minor": None,
                "line_total_minor": 21900,
            }
        ]
    )
    flow = make_flow(
        StubProvider(unknown),
        store,
        categorizer=Unreachable(),  # type: ignore[arg-type]
    )
    pending = submit(flow, session, user).pending
    assert pending is not None

    result = commit(flow, session, pending.key)
    session.commit()

    assert result.step is Step.STORED
    items = session.scalars(select(TransactionLineItem)).all()
    assert items, "the receipt is in the ledger"
    # Uncategorized where the lexicon could not place it, which is the whole
    # point: re-runnable later from raw_extractions, for free.
    assert any(item.category_source is None for item in items)


def test_confirming_the_same_image_twice_is_not_a_crash(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """Two pending entries for one image, which is what resending produces.

    The second confirm used to reach the partial unique index and surface as
    "something went wrong on my end". It is not a crash, it is a duplicate, and
    the user is entitled to be told which.
    """
    session, user = seeded
    flow = make_flow(StubProvider(an_extraction()), store)

    first = submit(flow, session, user, message_id=1).pending
    second = submit(flow, session, user, message_id=2).pending
    assert first is not None and second is not None
    assert first.key != second.key

    assert commit(flow, session, first.key).step is Step.STORED
    session.commit()

    result = commit(flow, session, second.key)
    session.commit()

    assert result.step is Step.DUPLICATE
    assert result.duplicate_of is not None
    assert len(session.scalars(select(Transaction)).all()) == 1


def test_an_expired_confirm_says_which_thing_happened(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """The pending store is in memory, so a restart empties it silently.

    The buttons stay on screen looking live. "That one is no longer waiting"
    was true and read like a malfunction, so it now names the two real causes
    and states that nothing was saved.
    """
    session, user = seeded
    flow = make_flow(StubProvider(an_extraction()), store)
    pending = submit(flow, session, user).pending
    assert pending is not None

    # What a restart looks like from here: same key, empty store.
    restarted = make_flow(StubProvider(an_extraction()), store)

    result = commit(restarted, session, pending.key)

    assert result.step is Step.EXPIRED
    assert "24 hours" in result.message
    assert "already answered" in result.message
    assert "Nothing new was saved" in result.message
    # It no longer blames a restart. `DatabasePendingStore` survives one, so
    # naming it told the person something false about how the bot behaves.
    assert "restart" not in result.message.lower()
    # And it does not tell anyone how to send a receipt. Invariant 10.
    for banned in ("crop", "rotate", "as a file", "retake", "better photo"):
        assert banned not in result.message.lower()


def test_a_provider_that_ignores_its_own_contract_does_not_lose_the_image(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """Brief 16.5: a failure at extraction keeps the image so the receipt survives."""
    session, user = seeded
    flow = make_flow(StubProvider(OSError("[Errno 11001] getaddrinfo failed")), store)

    result = submit(flow, session, user)

    assert result.step is Step.EXTRACTION_FAILED
    assert len(store) == 1, "the image stays on disk"


def test_discarding_stores_nothing_and_deletes_the_image(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    session, user = seeded
    flow = make_flow(StubProvider(an_extraction()), store)
    pending = submit(flow, session, user).pending
    assert pending is not None

    result = flow.discard(session, pending.key)
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

    assert commit(flow, session, pending.key).step is Step.STORED
    assert commit(flow, session, pending.key).step is Step.EXPIRED


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

    accepted = flow.accept_gap(session, result.pending.key)
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
    flow.accept_gap(session, pending.key)

    result = commit(flow, session, pending.key)
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
    commit(flow, session, pending.key)
    session.commit()

    again = submit(flow, session, user, message_id=2)
    assert again.step is Step.DUPLICATE
    assert again.duplicate_of is not None
    assert "already logged this one" in again.message.lower()
    # Caught before extraction runs, so the claim that it was free is true.
    assert "Nothing was charged" in again.message
    assert "dashboard" in again.message.lower()


def test_a_soft_deleted_row_does_not_block_a_resend(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """Resending is how you undo an undo."""
    session, user = seeded
    flow = make_flow(StubProvider(an_extraction()), store)
    pending = submit(flow, session, user).pending
    assert pending is not None
    stored = commit(flow, session, pending.key)
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
    pending_store = InMemoryPendingStore()
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
    assert commit(flow, session, pending.key).step is Step.EXPIRED


# ---------------------------------------------------------------------------
# D9: a restart must not eat a receipt
# ---------------------------------------------------------------------------


@pytest.fixture
def shared_sessions() -> Iterator[sessionmaker[Session]]:
    """A ledger several sessions can share, which plain `sqlite://` cannot.

    Every new connection to an anonymous in-memory database gets its own empty
    one. `StaticPool` hands out the same connection, which is what makes it
    possible to test that a *second* store sees what the first one wrote.
    """
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


PENDING_SECRET = "a-secret-long-enough-to-be-realistic"


@pytest.fixture
def file_sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A ledger on disk, where separate sessions are separate connections.

    `shared_sessions` cannot see a whole class of bug. `StaticPool` hands every
    session the *same* connection, so no two of them can ever contend for
    SQLite's single write lock, and code that opens its own connection mid
    transaction looks perfectly correct. On a real file it deadlocks. That is
    exactly what shipped: the D9 store opened its own session inside a
    transaction the caller was already holding open, every test passed, and the
    first real receipt after it came back "something went wrong on my end".
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def test_the_pending_store_does_not_deadlock_against_its_caller(
    file_sessions: sessionmaker[Session], store: ImageStore
) -> None:
    """A pending write must join the caller's transaction, not race it.

    On a real database file this is the whole bug: `submit_image` has already
    written `raw_extractions` and so holds the write lock, and a store that
    reaches for a second connection waits on a lock only its own caller can
    release. Asserting "no exception" is the point.
    """
    flow = ReceiptFlow(
        provider=StubProvider(an_extraction()),
        images=store,
        pending=DatabasePendingStore(secret=PENDING_SECRET),
        config=FlowConfig(),
        clock=lambda: NOW,
    )

    with file_sessions() as session:
        user_id = bootstrap(session).id
        session.commit()

    with file_sessions() as session:
        result = flow.submit_image(
            session,
            user_id=user_id,
            chat_id=999,
            message_id=1,
            data=PNG,
            mime_type="image/png",
            message_date=NOW,
        )
        # Still inside the transaction that wrote the extraction.
        assert result.step is Step.AWAITING_CONFIRMATION
        assert result.pending is not None
        session.commit()

    with file_sessions() as session:
        assert commit(flow, session, result.pending.key).step is Step.STORED
        session.commit()

    with file_sessions() as session:
        assert session.scalars(select(Transaction)).all() != []


def test_a_pending_row_rolls_back_with_the_extraction_it_points_at(
    file_sessions: sessionmaker[Session], store: ImageStore
) -> None:
    """Sharing the caller's transaction is what makes the pair atomic.

    A store on its own connection commits the pending row immediately, so a
    caller that later rolls back leaves a row pointing at an extraction that no
    longer exists.
    """
    flow = ReceiptFlow(
        provider=StubProvider(an_extraction()),
        images=store,
        pending=DatabasePendingStore(secret=PENDING_SECRET),
        config=FlowConfig(),
        clock=lambda: NOW,
    )

    with file_sessions() as session:
        user_id = bootstrap(session).id
        session.commit()

    with file_sessions() as session:
        result = flow.submit_image(
            session,
            user_id=user_id,
            chat_id=999,
            message_id=1,
            data=PNG,
            mime_type="image/png",
            message_date=NOW,
        )
        assert result.step is Step.AWAITING_CONFIRMATION
        session.rollback()

    with file_sessions() as session:
        assert session.scalars(select(PendingReceiptRow)).all() == []
        assert session.scalars(select(RawExtraction)).all() == []


def test_a_restart_does_not_kill_an_outstanding_confirm(
    shared_sessions: sessionmaker[Session], store: ImageStore
) -> None:
    """D9, and the reason it was urgent.

    The in-memory store lost pending receipts on restart while leaving the
    buttons on screen, so a receipt already read and paid for could only be
    resent and paid for again. This is that exact sequence.
    """
    with shared_sessions() as session:
        user = bootstrap(session)
        session.commit()
        user_id = user.id

    def build() -> ReceiptFlow:
        return ReceiptFlow(
            provider=StubProvider(an_extraction()),
            images=store,
            pending=DatabasePendingStore(secret=PENDING_SECRET),
            config=FlowConfig(),
            clock=lambda: NOW,
        )

    with shared_sessions() as session:
        result = build().submit_image(
            session,
            user_id=user_id,
            chat_id=999,
            message_id=1,
            data=PNG,
            mime_type="image/png",
            message_date=NOW,
        )
        session.commit()
    assert result.pending is not None
    key = result.pending.key

    # The restart. A brand new flow and a brand new store, same database.
    with shared_sessions() as session:
        after = commit(build(), session, key)
        session.commit()

    assert after.step is Step.STORED, "the confirm button died across a restart"


def test_a_confirm_still_cannot_be_applied_twice(
    shared_sessions: sessionmaker[Session], store: ImageStore
) -> None:
    """Retiring the row is what stops a double tap becoming a double row."""
    with shared_sessions() as session:
        user = bootstrap(session)
        session.commit()
        user_id = user.id

    pending_store = DatabasePendingStore(secret=PENDING_SECRET)
    flow = ReceiptFlow(
        provider=StubProvider(an_extraction()),
        images=store,
        pending=pending_store,
        config=FlowConfig(),
        clock=lambda: NOW,
    )

    with shared_sessions() as session:
        result = flow.submit_image(
            session,
            user_id=user_id,
            chat_id=999,
            message_id=1,
            data=PNG,
            mime_type="image/png",
            message_date=NOW,
        )
        session.commit()
    assert result.pending is not None

    with shared_sessions() as session:
        assert commit(flow, session, result.pending.key).step is Step.STORED
        session.commit()
    with shared_sessions() as session:
        assert commit(flow, session, result.pending.key).step is Step.EXPIRED
        assert pending_store.count(session) == 0


def test_no_telegram_identifier_reaches_the_database(
    shared_sessions: sessionmaker[Session], store: ImageStore
) -> None:
    """W3 stopped storing Telegram IDs. Persisting pending state must not undo it.

    The chat ID is picked to be long and distinctive so that finding it anywhere
    in the table's text is proof, not coincidence.
    """
    chat_id = 8675309123
    with shared_sessions() as session:
        user = bootstrap(session)
        session.commit()
        user_id = user.id

    flow = ReceiptFlow(
        provider=StubProvider(an_extraction()),
        images=store,
        pending=DatabasePendingStore(secret=PENDING_SECRET),
        config=FlowConfig(),
        clock=lambda: NOW,
    )
    with shared_sessions() as session:
        flow.submit_image(
            session,
            user_id=user_id,
            chat_id=chat_id,
            message_id=4242,
            data=PNG,
            mime_type="image/png",
            message_date=NOW,
        )
        session.commit()

    with shared_sessions() as session:
        rows = list(session.scalars(select(PendingReceiptRow)))
        assert rows, "nothing was stored, so this test proves nothing"
        blob = " ".join(
            str(getattr(row, column.name)) for row in rows for column in rows[0].__table__.columns
        )
    assert str(chat_id) not in blob
    assert "4242" not in blob


def test_a_stored_pending_receipt_keeps_the_decisions_already_made(
    shared_sessions: sessionmaker[Session], store: ImageStore
) -> None:
    """An accepted gap is a human's answer. A restart must not ask again."""
    with shared_sessions() as session:
        user = bootstrap(session)
        session.commit()
        user_id = user.id

    pending_store = DatabasePendingStore(secret=PENDING_SECRET)
    flow = ReceiptFlow(
        provider=StubProvider(an_extraction()),
        images=store,
        pending=pending_store,
        config=FlowConfig(),
        clock=lambda: NOW,
    )
    with shared_sessions() as session:
        result = flow.submit_image(
            session,
            user_id=user_id,
            chat_id=999,
            message_id=1,
            data=PNG,
            mime_type="image/png",
            message_date=NOW,
        )
        session.commit()
    assert result.pending is not None
    key = result.pending.key

    with shared_sessions() as session:
        flow.accept_gap(session, key)
        flow.set_merchant(session, key, "blinkit")
        session.commit()

    fresh = DatabasePendingStore(secret=PENDING_SECRET)
    with shared_sessions() as session:
        reloaded = fresh.get(session, key, now=NOW)
    assert reloaded is not None
    assert reloaded.gap_accepted is True
    assert reloaded.merchant_slug == "blinkit"


def test_a_persisted_pending_receipt_still_expires(
    shared_sessions: sessionmaker[Session], store: ImageStore
) -> None:
    """Brief 16.4 gives a confirm button 24 hours. Durable is not forever."""
    with shared_sessions() as session:
        user = bootstrap(session)
        session.commit()
        user_id = user.id

    pending_store = DatabasePendingStore(secret=PENDING_SECRET)
    clock = {"now": NOW}
    flow = ReceiptFlow(
        provider=StubProvider(an_extraction()),
        images=store,
        pending=pending_store,
        config=FlowConfig(),
        clock=lambda: clock["now"],
    )
    with shared_sessions() as session:
        result = flow.submit_image(
            session,
            user_id=user_id,
            chat_id=999,
            message_id=1,
            data=PNG,
            mime_type="image/png",
            message_date=NOW,
        )
        session.commit()
    assert result.pending is not None and result.pending.image_path is not None

    clock["now"] = NOW + dt.timedelta(hours=25)
    with shared_sessions() as session:
        assert commit(flow, session, result.pending.key).step is Step.EXPIRED

    with shared_sessions() as session:
        dead = flow.expire_stale(session)
        session.commit()
    assert len(dead) == 1
    assert not result.pending.image_path.exists(), "invariant 7: the image goes too"


def test_expiring_deletes_the_image(seeded: tuple[Session, User], store: ImageStore) -> None:
    """Invariant 7 does not stop applying because the user walked away."""
    session, user = seeded
    clock = {"now": NOW}
    flow = ReceiptFlow(
        provider=StubProvider(an_extraction()),
        images=store,
        pending=InMemoryPendingStore(),
        config=FlowConfig(),
        clock=lambda: clock["now"],
    )
    pending = submit(flow, session, user).pending
    assert pending is not None and pending.image_path is not None

    clock["now"] = NOW + dt.timedelta(hours=25)
    dead = flow.expire_stale(session)

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
    messages[Step.STORED] = commit(good, session, awaiting.pending.key).message
    session.commit()

    duplicate = submit(good, session, user, message_id=2)
    messages[Step.DUPLICATE] = duplicate.message

    second = submit(good, session, user, data=PNG + b"2", message_id=3)
    assert second.pending is not None
    messages[Step.DISCARDED] = good.discard(session, second.pending.key).message
    messages[Step.EXPIRED] = good.discard(session, second.pending.key).message

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


def test_a_failure_names_the_cause_and_does_not_leak_the_exception(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """The other half of invariant 10: silence is not required, only restraint.

    The provider's own text is written by a third party and bounded by nothing,
    so it belongs in the log. A person gets the cause and a reference that ties
    their report back to the line that has the detail.
    """
    session, user = seeded
    flow = make_flow(StubProvider(ProviderTransientError("upstream timed out")), store)
    result = submit(flow, session, user)
    assert result.step is Step.EXTRACTION_FAILED
    assert "could not reach" in result.message.lower()
    assert "nothing is lost" in result.message.lower()
    assert "reference" in result.message.lower()
    # The exception's own words never reach the chat.
    assert "upstream timed out" not in result.message


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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The exact string the first live receipt returned. It parsed to None,
        # which dated a 30 July receipt to 9 August.
        ("2026-07-30T12:44:00", dt.date(2026, 7, 30)),
        ("2026-07-30T12:44", dt.date(2026, 7, 30)),
        ("2026-07-30T12:44:00+05:30", dt.date(2026, 7, 30)),
        ("2026-07-30", dt.date(2026, 7, 30)),
    ],
)
def test_an_iso_8601_datetime_is_read(text: str, expected: dt.date) -> None:
    """The schema says "exactly as printed"; the model normalises anyway.

    Invariant 6 buckets on this value, so rejecting the format the model
    actually emits is a wrong-month bug, not a cosmetic one.
    """
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

    # The pending receipt records the guess at the moment it is made. It used to
    # be left at its RECEIPT_PRINTED default and re-derived at confirm, so the
    # ledger came out right while `pending_receipts.date_source` said the date
    # was printed on every row it ever held.
    assert pending.date_source is DateSource.MESSAGE_TIMESTAMP
    assert pending.occurred_on_local == NOW.astimezone(zone("Asia/Kolkata")).date()

    # And the guess is what the question is asked about, so it is never what
    # gets written. Nothing is in the ledger at this point.
    result = flow.confirm(session, pending.key)
    session.commit()
    assert result.step is Step.AWAITING_DATE
    assert result.transaction is None
    assert session.scalars(select(Transaction)).all() == []


def test_a_guessed_date_survives_the_pending_store(
    file_sessions: sessionmaker[Session], store: ImageStore
) -> None:
    """The guess has to outlive a restart, because confirm no longer re-derives it.

    `confirm` reads `receipt.date_source` instead of parsing the extraction a
    second time, so a store that dropped the field on the way to disk would file
    every guessed date as printed. Only a real round trip can show that.

    It is also what decides whether the date question gets asked, so a store
    that lost it would send a receipt straight to the ledger under a date nobody
    was ever shown.
    """
    flow = ReceiptFlow(
        provider=StubProvider(an_extraction()),
        images=store,
        pending=DatabasePendingStore(secret=PENDING_SECRET),
        config=FlowConfig(),
        clock=lambda: NOW,
    )

    with file_sessions() as session:
        user_id = bootstrap(session).id
        session.commit()

    with file_sessions() as session:
        result = flow.submit_image(
            session,
            user_id=user_id,
            chat_id=999,
            message_id=1,
            data=PNG,
            mime_type="image/png",
            message_date=NOW,
        )
        assert result.pending is not None
        assert result.pending.date_source is DateSource.MESSAGE_TIMESTAMP
        session.commit()

    with file_sessions() as session:
        rebuilt = flow.pending_for(session, result.pending.key)
        assert rebuilt is not None
        assert rebuilt.date_source is DateSource.MESSAGE_TIMESTAMP
        assert flow.confirm(session, result.pending.key).step is Step.AWAITING_DATE
        assert flow.set_date(session, result.pending.key, BACKDATED).transaction is not None
        session.commit()

    with file_sessions() as session:
        stored_row = session.scalars(select(Transaction)).one()
        assert stored_row.date_source is DateSource.USER_SUPPLIED
        assert stored_row.occurred_on_local == BACKDATED


def test_a_printed_date_is_never_questioned(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """The whole point of gating the question. Two taps stays two taps."""
    session, user = seeded
    flow = make_flow(StubProvider(an_extraction(order_datetime_local="2026-07-30")), store)
    pending = submit(flow, session, user).pending
    assert pending is not None
    assert pending.date_source is DateSource.RECEIPT_PRINTED

    result = flow.confirm(session, pending.key)
    assert result.step is Step.STORED
    assert result.transaction is not None
    assert result.transaction.occurred_on_local == dt.date(2026, 7, 30)


def test_answering_the_date_question_backdates_the_row(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """Invariant 6 buckets on this value, so a week-old bill has to land in its own week."""
    session, user = seeded
    flow = make_flow(StubProvider(an_extraction()), store)
    pending = submit(flow, session, user).pending
    assert pending is not None

    assert flow.confirm(session, pending.key).step is Step.AWAITING_DATE
    result = flow.set_date(session, pending.key, BACKDATED)
    session.commit()

    assert result.step is Step.STORED
    assert result.transaction is not None
    assert result.transaction.occurred_on_local == BACKDATED
    # Neither printed nor guessed. The dashboard flags a guess, and this is not one.
    assert result.transaction.date_source is DateSource.USER_SUPPLIED


def test_the_date_question_is_asked_before_stage_two_is_paid_for(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """A person who answers nothing must not have been billed for the answer."""
    session, user = seeded
    categorizer = StubCategorizer()
    flow = make_flow(StubProvider(an_extraction()), store, categorizer=categorizer)
    pending = submit(flow, session, user).pending
    assert pending is not None

    assert flow.confirm(session, pending.key).step is Step.AWAITING_DATE
    assert categorizer.calls == 0
    # And the image is still there, so the receipt is not lost either.
    assert pending.image_path is not None
    assert pending.image_path.exists()


def test_an_unanswered_date_question_leaves_the_button_usable(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """Asking is not consuming. The same pending entry has to survive the question."""
    session, user = seeded
    flow = make_flow(StubProvider(an_extraction()), store)
    pending = submit(flow, session, user).pending
    assert pending is not None

    for _ in range(3):
        assert flow.confirm(session, pending.key).step is Step.AWAITING_DATE
    assert flow.pending_for(session, pending.key) is not None
    assert flow.set_date(session, pending.key, BACKDATED).step is Step.STORED


def test_a_date_that_has_not_happened_is_refused(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """Only reachable from a stale keyboard, and cheap next to a row in a future month."""
    session, user = seeded
    flow = make_flow(StubProvider(an_extraction()), store)
    pending = submit(flow, session, user).pending
    assert pending is not None

    result = flow.set_date(session, pending.key, flow.local_today() + dt.timedelta(days=1))
    session.commit()

    assert result.step is Step.AWAITING_DATE
    assert session.scalars(select(Transaction)).all() == []
    # Still answerable. A refusal that also threw the receipt away would be worse
    # than the row it prevented.
    assert flow.set_date(session, pending.key, BACKDATED).step is Step.STORED


def test_today_means_today_where_the_user_is(store: ImageStore) -> None:
    """Between 18:30 UTC and midnight, the server's date is tomorrow in Kolkata.

    `dt.date.today()` on an Oracle box in UTC would put a receipt logged at 8pm
    IST into the next day, which invariant 6 then buckets into the next month
    twice a year.
    """
    late = dt.datetime(2026, 8, 8, 20, 0, tzinfo=dt.UTC)  # 2026-08-09 01:30 in Kolkata
    flow = make_flow(StubProvider(an_extraction()), store, now=late)
    assert flow.local_today() == dt.date(2026, 8, 9)
    assert late.date() == dt.date(2026, 8, 8)


def test_the_date_question_says_nothing_about_how_to_send(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """Invariant 10, which the 2026-08-11 exception does not extend to here."""
    session, user = seeded
    flow = make_flow(StubProvider(an_extraction()), store)
    pending = submit(flow, session, user).pending
    assert pending is not None

    text = flow.confirm(session, pending.key).message.lower()
    for banned in ("send", "resend", "photo", "screenshot", "crop", "rotate", "file"):
        assert banned not in text, text


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


# ---------------------------------------------------------------------------
# Stage 2 on confirm (brief 4.5 and 18.5)
# ---------------------------------------------------------------------------


def confirm_one(flow: ReceiptFlow, session: Session, user: User) -> list[TransactionLineItem]:
    pending = submit(flow, session, user).pending
    assert pending is not None
    result = commit(flow, session, pending.key)
    session.commit()
    assert result.transaction is not None
    return list(
        session.scalars(
            select(TransactionLineItem)
            .where(TransactionLineItem.transaction_id == result.transaction.id)
            .order_by(TransactionLineItem.position)
        ).all()
    )


def test_confirming_categorizes_the_line_items(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    session, user = seeded
    items = confirm_one(make_flow(StubProvider(an_extraction()), store), session, user)

    assert len(items) == 6
    assert all(item.category_id is not None for item in items)
    assert all(item.category_source is CategorySource.LEXICON_EXACT for item in items)
    assert [item.canonical_slug for item in items] == [
        "milk",
        "bread",
        "onion",
        "potato",
        "tomato",
        "coriander",
    ]
    assert [item.normalized_slug for item in items] == [
        "amul-taaza-toned-milk",
        "britannia-brown-bread",
        "fresho-pyaz-onion",
        "aalu-potato",
        "desi-tamatar",
        "dhaniya-coriander-leaves",
    ]
    # `Fresho Pyaz / Onion` carries no size in its name at all. Its 1 kg comes
    # from the receipt's own quantity column, which is why that column is read
    # in preference to the title.
    assert [item.quantity for item in items] == [500, 400, 1000, 1000, 500, 100]
    assert [item.unit_normalized for item in items] == ["ml", "g", "g", "g", "g", "g"]
    # "500 ml x 2" is two units of 500 ml, not a 1 L bottle.
    assert [item.pack_count for item in items] == [2, 1, 1, 1, 1, 1]


def test_a_known_receipt_never_reaches_the_stage_two_model(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """The whole point of brief 4.5. Most receipts must cost nothing to categorize."""
    session, user = seeded
    categorizer = StubCategorizer()
    confirm_one(
        make_flow(StubProvider(an_extraction()), store, categorizer=categorizer), session, user
    )
    assert categorizer.calls == 0


def test_stage_two_runs_on_confirm_not_on_submit(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """A discarded receipt must not cost anything to categorize."""
    session, user = seeded
    categorizer = StubCategorizer(
        answers=[ItemCategory(index=0, category="groceries", confidence=0.9)]
    )
    flow = make_flow(StubProvider(an_extraction("blinkit_032")), store, categorizer=categorizer)

    pending = submit(flow, session, user).pending
    assert pending is not None
    assert categorizer.calls == 0

    flow.discard(session, pending.key)
    assert categorizer.calls == 0


def test_the_stage_two_call_is_billed_to_the_same_cap(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """A Stage 2 row lands in `raw_extractions`, marked as Stage 2. Brief 16.6."""
    session, user = seeded
    unknown = an_extraction(
        line_items=[
            {
                "raw_name": "Colgate Strong Teeth",
                "quantity_text": None,
                "mrp_minor": None,
                "line_total_minor": 21900,
            }
        ]
    )
    categorizer = StubCategorizer(
        answers=[ItemCategory(index=0, category="groceries", confidence=0.88)]
    )
    items = confirm_one(
        make_flow(StubProvider(unknown), store, categorizer=categorizer), session, user
    )

    assert categorizer.calls == 1
    assert items[0].category_source is CategorySource.LLM
    assert items[0].category_confidence_bp == 8800

    rows = session.scalars(
        select(RawExtraction).where(RawExtraction.stage == ExtractionStage.CATEGORIZATION)
    ).all()
    assert len(rows) == 1
    assert rows[0].cost_micros_usd == 95
    assert (
        queries.spend_micros_since(session, user_id=user.id, since=NOW - dt.timedelta(days=1))
        == 12_555 + 95
    )


def test_the_fallback_is_skipped_when_the_budget_is_gone(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """Being over budget must never cost the user the receipt.

    The submission is paid for while there is budget. By confirm time the cap
    has been reached, so the lexicon still runs and the model does not.
    """
    session, user = seeded
    unknown = an_extraction(
        line_items=[
            {
                "raw_name": "Colgate Strong Teeth",
                "quantity_text": None,
                "mrp_minor": None,
                "line_total_minor": 21900,
            }
        ]
    )
    categorizer = StubCategorizer()
    flow = make_flow(
        StubProvider(unknown),
        store,
        categorizer=categorizer,
        # Below what Stage 1 costs, so the submission passes the check at zero
        # spend and the cap is already gone by the time confirm runs.
        config=FlowConfig(daily_cost_limit_micros=1_000),
    )

    pending = submit(flow, session, user).pending
    assert pending is not None, "the submission itself was within budget"

    result = commit(flow, session, pending.key)
    session.commit()

    assert categorizer.calls == 0
    assert result.transaction is not None, "the receipt still lands"


def test_confirming_writes_the_misses(seeded: tuple[Session, User], store: ImageStore) -> None:
    session, user = seeded
    unknown = an_extraction(
        line_items=[
            {
                "raw_name": "Colgate Strong Teeth",
                "quantity_text": None,
                "mrp_minor": None,
                "line_total_minor": 21900,
            }
        ]
    )
    confirm_one(make_flow(StubProvider(unknown), store), session, user)

    terms = sorted(m.term for m in session.scalars(select(LexiconMiss)).all())
    assert terms == ["colgate", "strong", "teeth"]


# ---------------------------------------------------------------------------
# The global cost cap
# ---------------------------------------------------------------------------


def another_user(session: Session) -> User:
    """A second user, so "everyone" is more than "this one"."""
    user = User()
    session.add(user)
    session.flush()
    return user


def burn(session: Session, user: User, micros: int, *, when: dt.datetime = NOW) -> None:
    """Spend money on the API without going through the flow.

    `when` is stamped explicitly rather than left to `server_default=func.now()`.
    The cap compares a row's `created_at` against a window derived from the
    flow's *injected* clock, so a row timestamped by the real one makes the test
    depend on what day it is run: the rolling-window case below passed until the
    wall clock drifted past `NOW + 1 day` and then began failing on its own.
    """
    raw = record_extraction(
        session,
        user_id=user.id,
        result=ProviderResult(
            extraction=an_extraction(),
            model_id="gemini-3.6-flash",
            prompt_version="v1",
            response_text="{}",
            input_tokens=1,
            output_tokens=1,
            cost_micros_usd=micros,
        ),
        source=Source.TELEGRAM_IMAGE,
        image_sha256=None,
    )
    raw.created_at = when
    session.commit()


def test_one_user_cannot_be_stopped_by_another_users_spend_alone(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """The per-user cap is still per user."""
    session, user = seeded
    burn(session, another_user(session), 900_000)

    flow = make_flow(StubProvider(an_extraction()), store)
    assert submit(flow, session, user).step is Step.AWAITING_CONFIRMATION


def test_the_global_cap_stops_a_user_who_is_inside_their_own(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """The bug the global cap exists for.

    Two users, each obediently under their own $1, and the bill is $2. Only a
    cap that ignores `user_id` can see that.
    """
    session, user = seeded
    burn(session, another_user(session), 900_000)
    burn(session, user, 900_000)

    flow = make_flow(
        StubProvider(an_extraction()),
        store,
        config=FlowConfig(
            daily_cost_limit_micros=1_000_000,
            global_daily_cost_limit_micros=1_500_000,
        ),
    )
    result = submit(flow, session, user)
    assert result.step is Step.GLOBAL_LIMIT_REACHED


def test_the_global_message_does_not_blame_the_user(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """They did nothing wrong, and invariant 10 forbids telling them to change."""
    session, user = seeded
    burn(session, another_user(session), 2_000_000)

    flow = make_flow(StubProvider(an_extraction()), store)
    message = submit(flow, session, user).message.lower()

    assert "nothing is wrong on your side" in message
    assert "your limit" not in message


def test_nothing_is_downloaded_or_paid_for_once_the_global_cap_is_hit(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    session, user = seeded
    burn(session, another_user(session), 2_000_000)

    provider = StubProvider(an_extraction())
    flow = make_flow(provider, store)
    submit(flow, session, user)

    assert provider.calls == 0
    assert list(store.root.glob("*")) == []


def test_a_deleted_users_spend_still_counts(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """Money spent is money spent, whoever spent it and whether they are still here."""
    session, user = seeded
    gone = another_user(session)
    burn(session, gone, 2_000_000)
    gone.deleted_at = NOW
    session.commit()

    flow = make_flow(StubProvider(an_extraction()), store)
    assert submit(flow, session, user).step is Step.GLOBAL_LIMIT_REACHED


def test_the_global_cap_is_a_rolling_window(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    session, user = seeded
    burn(session, another_user(session), 2_000_000)

    later = make_flow(StubProvider(an_extraction()), store, now=NOW + dt.timedelta(days=2))
    assert submit(later, session, user).step is Step.AWAITING_CONFIRMATION


# ---------------------------------------------------------------------------
# The monthly cap. NOW is 2026-08-08, so the month began on 2026-08-01.
# ---------------------------------------------------------------------------


def test_month_start_utc_truncates_to_the_first() -> None:
    assert month_start_utc(NOW) == dt.datetime(2026, 8, 1, tzinfo=dt.UTC)


def test_month_start_utc_normalises_a_non_utc_clock() -> None:
    """The boundary is a UTC one wherever the caller's clock is."""
    kolkata = NOW.astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30)))
    assert month_start_utc(kolkata) == dt.datetime(2026, 8, 1, tzinfo=dt.UTC)


def test_the_monthly_cap_stops_days_that_are_each_inside_their_own(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """The bug the monthly cap exists for.

    Spent on 2 August, so both rolling 24 hour windows are clean on the 8th and
    only the calendar month can see this money at all. N days each obediently
    under $2 is still one invoice.
    """
    session, user = seeded
    burn(session, another_user(session), 5_000_000, when=dt.datetime(2026, 8, 2, tzinfo=dt.UTC))

    flow = make_flow(StubProvider(an_extraction()), store)
    assert submit(flow, session, user).step is Step.GLOBAL_MONTHLY_LIMIT_REACHED


def test_the_monthly_cap_is_a_calendar_month_and_not_a_rolling_thirty_days(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """Why this cap does not roll.

    31 July is inside a rolling 30 day window on 8 August and outside August's
    invoice. A rolling window would refuse here, and would also let the whole
    ceiling be spent twice inside one calendar month, which is the thing this
    cap was added to prevent.
    """
    session, user = seeded
    burn(session, another_user(session), 5_000_000, when=dt.datetime(2026, 7, 31, tzinfo=dt.UTC))

    flow = make_flow(StubProvider(an_extraction()), store)
    assert submit(flow, session, user).step is Step.AWAITING_CONFIRMATION


def test_the_monthly_cap_is_checked_before_the_daily_ones(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """With every cap blown at once, the honest answer is the longest one."""
    session, user = seeded
    burn(session, user, 5_000_000)

    flow = make_flow(StubProvider(an_extraction()), store)
    assert submit(flow, session, user).step is Step.GLOBAL_MONTHLY_LIMIT_REACHED


def test_the_monthly_message_does_not_promise_a_reset_in_24_hours(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """A spent month does not free itself tomorrow, and saying so would be a lie."""
    session, user = seeded
    burn(session, another_user(session), 5_000_000, when=dt.datetime(2026, 8, 2, tzinfo=dt.UTC))

    flow = make_flow(StubProvider(an_extraction()), store)
    message = submit(flow, session, user).message.lower()

    assert "nothing is wrong on your side" in message
    assert "first of next month" in message
    assert "24 hour" not in message
    assert "your limit" not in message


def test_nothing_is_downloaded_or_paid_for_once_the_monthly_cap_is_hit(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    session, user = seeded
    burn(session, another_user(session), 5_000_000, when=dt.datetime(2026, 8, 2, tzinfo=dt.UTC))

    provider = StubProvider(an_extraction())
    flow = make_flow(provider, store)
    submit(flow, session, user)

    assert provider.calls == 0
    assert list(store.root.glob("*")) == []


def test_the_monthly_cap_frees_itself_on_the_first(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    session, user = seeded
    burn(session, another_user(session), 5_000_000, when=dt.datetime(2026, 8, 2, tzinfo=dt.UTC))

    september = make_flow(
        StubProvider(an_extraction()), store, now=dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    )
    assert submit(september, session, user).step is Step.AWAITING_CONFIRMATION
