"""Tests for the Telegram adapter.

No bot token and no network. `RaseedBot` is deliberately thin, so what is worth
testing here is exactly the part that is not: the whitelist, the callback
keyboard, and the action dispatch table. Everything else is `flow.py`, which
`test_flow.py` covers.

`Update` is stubbed rather than constructed, because a real one needs a `Bot`
instance and this file must not need credentials to run. The two handlers that
decide something on their own, `on_document` and `on_button`, get a stub that
records what they said; everything else goes through `dispatch`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.orm import Session, sessionmaker
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from conftest import as_extraction_payload
from raseed.adapters.flow import (
    DATE_PROMPT,
    EXPIRED_MESSAGE,
    UNREADABLE_FILE_MESSAGE,
    FlowConfig,
    ReceiptFlow,
    Step,
)
from raseed.adapters.images import ImageStore
from raseed.adapters.pending import InMemoryPendingStore, PendingKey, PendingReceipt
from raseed.adapters.telegram import (
    ACTION_ACCEPT_GAP,
    ACTION_CALENDAR,
    ACTION_CONFIRM,
    ACTION_DATE,
    ACTION_DISCARD,
    ACTION_MERCHANT,
    ACTION_NOOP,
    GREETING,
    WEEKDAYS,
    RaseedBot,
    calendar_keyboard,
    date_keyboard,
    keyboard_after,
    keyboard_for,
)
from raseed.db.models import User
from raseed.extraction.providers.base import ExtractionRequest, ProviderResult
from raseed.extraction.schemas import ExtractionResult

NOW = dt.datetime(2026, 8, 8, 12, 0, tzinfo=dt.UTC)
PNG = b"\x89PNG\r\n\x1a\n" + b"receipt-bytes"

ALLOWED_ID = 4242

#: Derives `user_id` from a Telegram ID. Long enough to pass the length floor,
#: and fixed so the derived IDs are stable across a run.
SECRET = "test-secret-that-is-long-enough-to-pass"
STRANGER_ID = 9999


class StubProvider:
    """Answers with one fixed extraction. Never touches the network."""

    def __init__(self, outcome: ExtractionResult) -> None:
        self._outcome = outcome

    @property
    def model_id(self) -> str:
        return "gemini-3.6-flash"

    def count_input_tokens(self, _request: ExtractionRequest) -> int:
        return 1970

    def extract(self, _request: ExtractionRequest) -> ProviderResult:
        return ProviderResult(
            extraction=self._outcome,
            model_id=self.model_id,
            prompt_version="v1",
            response_text=self._outcome.model_dump_json(),
            input_tokens=1970,
            output_tokens=1280,
            cost_micros_usd=12_555,
        )


def an_extraction(name: str = "blinkit_001", **overrides: object) -> ExtractionResult:
    return ExtractionResult.model_validate(as_extraction_payload(name, **overrides))


def an_update(user_id: int | None) -> Update:
    """The smallest thing `permitted` can read.

    Cast rather than built, because a genuine `Update` wants a live `Bot`.
    """
    user = None if user_id is None else SimpleNamespace(id=user_id)
    return cast(Update, SimpleNamespace(effective_user=user))


def unbound_sessions() -> sessionmaker[Session]:
    """A factory that would work if anything asked it for a session.

    The synchronous surface under test here (`permitted`, `dispatch`) never
    opens one. The handlers that do get `wired_bot`, which is bound to the same
    ledger the test seeded.
    """
    return sessionmaker()


@pytest.fixture
def store(tmp_path: Path) -> ImageStore:
    return ImageStore(tmp_path / "incoming")


@pytest.fixture
def flow(store: ImageStore) -> ReceiptFlow:
    return ReceiptFlow(
        provider=StubProvider(an_extraction()),
        images=store,
        pending=InMemoryPendingStore(),
        config=FlowConfig(),
        clock=lambda: NOW,
    )


@pytest.fixture
def bot(flow: ReceiptFlow) -> RaseedBot:
    return RaseedBot(
        flow=flow,
        session_factory=unbound_sessions(),
        allowed_user_ids=frozenset({ALLOWED_ID}),
        clock=lambda: NOW,
        user_id_secret=SECRET,
    )


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


def test_a_listed_user_is_admitted(bot: RaseedBot) -> None:
    assert bot.permitted(an_update(ALLOWED_ID)) is True


def test_an_unlisted_user_is_refused(bot: RaseedBot) -> None:
    assert bot.permitted(an_update(STRANGER_ID)) is False


def test_an_update_with_no_user_is_refused(bot: RaseedBot) -> None:
    """Channel posts and service messages carry no sender."""
    assert bot.permitted(an_update(None)) is False


def test_an_empty_whitelist_admits_nobody(flow: ReceiptFlow) -> None:
    """A misconfigured .env must fail closed, not open."""
    open_bot = RaseedBot(
        flow=flow,
        session_factory=unbound_sessions(),
        allowed_user_ids=frozenset(),
        clock=lambda: NOW,
        user_id_secret=SECRET,
    )
    assert open_bot.permitted(an_update(ALLOWED_ID)) is False


# ---------------------------------------------------------------------------
# Invariant 10
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden",
    ["crop", "retake", "rotate", "as a file", "as a document", "better lighting", "resend"],
)
def test_the_greeting_never_instructs_how_to_send(forbidden: str) -> None:
    """Invariant 10. It may say it could not read one. It may not coach."""
    assert forbidden not in GREETING.lower()


@pytest.mark.parametrize(
    "forbidden",
    ["crop", "retake", "rotate", "better lighting", "resend"],
)
def test_the_unreadable_file_message_stays_inside_the_exception(forbidden: str) -> None:
    """Invariant 10's 2026-08-11 exception is narrow, and this is the whole of it.

    Naming the file types that work is allowed, because nothing is in flight to
    resend and the alternative is the silence a PDF used to get. Coaching
    somebody through retaking a photo that *did* arrive is still forbidden, and
    the difference is the entire point of keeping the invariant.
    """
    assert forbidden not in UNREADABLE_FILE_MESSAGE.lower()


def test_the_unreadable_file_message_says_what_does_work() -> None:
    """The reason the exception was added at all."""
    body = UNREADABLE_FILE_MESSAGE.lower()
    assert "images" in body
    assert "screenshot" in body
    assert "paper bill" in body
    # And it is honest about the money, since refusing costs nothing.
    assert "nothing was charged" in body


# ---------------------------------------------------------------------------
# The confirm keyboard
# ---------------------------------------------------------------------------


def pending_for(
    flow: ReceiptFlow, session: Session, user: User, *, message_id: int = 1
) -> PendingKey:
    result = flow.submit_image(
        session,
        user_id=user.id,
        chat_id=999,
        message_id=message_id,
        data=PNG + bytes([message_id]),
        mime_type="image/png",
        message_date=NOW,
    )
    assert result.pending is not None
    return result.pending.key


def test_a_balanced_receipt_offers_confirm_and_discard(
    seeded: tuple[Session, User], flow: ReceiptFlow
) -> None:
    session, user = seeded
    key = pending_for(flow, session, user)
    receipt = flow.pending_for(session, key)
    assert receipt is not None

    markup = keyboard_for(receipt)
    actions = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert any(str(data).startswith(ACTION_CONFIRM) for data in actions)
    assert any(str(data).startswith(ACTION_DISCARD) for data in actions)


def test_a_class_2_receipt_cannot_be_confirmed_until_the_gap_is_answered(
    seeded: tuple[Session, User], store: ImageStore
) -> None:
    """Brief 3.3. Confirm is not on the keyboard while the arithmetic is off."""
    session, user = seeded
    off_by_a_lot = an_extraction(grand_total_minor=99_999)
    gap_flow = ReceiptFlow(
        provider=StubProvider(off_by_a_lot),
        images=store,
        pending=InMemoryPendingStore(),
        config=FlowConfig(),
        clock=lambda: NOW,
    )
    key = pending_for(gap_flow, session, user)
    receipt = gap_flow.pending_for(session, key)
    assert receipt is not None

    markup = keyboard_for(receipt)
    actions = [str(button.callback_data) for row in markup.inline_keyboard for button in row]
    assert any(data.startswith(ACTION_ACCEPT_GAP) for data in actions)
    assert not any(data.startswith(f"{ACTION_CONFIRM}|") for data in actions)


def test_the_merchant_quick_pick_appears_only_when_unset(
    seeded: tuple[Session, User], flow: ReceiptFlow
) -> None:
    """Brief 24.4."""
    session, user = seeded
    key = pending_for(flow, session, user)
    receipt = flow.pending_for(session, key)
    assert receipt is not None

    offered = keyboard_for(receipt, ["blinkit", "zepto"])
    actions = [str(button.callback_data) for row in offered.inline_keyboard for button in row]
    assert f"{ACTION_MERCHANT}:blinkit" in [data.split("|", 1)[0] for data in actions]

    receipt.merchant_slug = "blinkit"
    settled = keyboard_for(receipt, ["blinkit", "zepto"])
    settled_actions = [str(b.callback_data) for row in settled.inline_keyboard for b in row]
    assert not any(data.startswith(f"{ACTION_MERCHANT}:") for data in settled_actions)


def test_every_button_carries_its_own_message_id(
    seeded: tuple[Session, User], flow: ReceiptFlow
) -> None:
    """Brief 16.4. Two receipts in flight must not share callback data."""
    session, user = seeded
    first = flow.pending_for(session, pending_for(flow, session, user, message_id=10))
    second = flow.pending_for(session, pending_for(flow, session, user, message_id=11))
    assert first is not None and second is not None

    def data_for(receipt: PendingReceipt) -> set[str]:
        markup = keyboard_for(receipt)
        return {str(b.callback_data) for row in markup.inline_keyboard for b in row}

    assert data_for(first).isdisjoint(data_for(second))


def test_every_button_fits_telegrams_callback_limit(
    seeded: tuple[Session, User], flow: ReceiptFlow
) -> None:
    session, user = seeded
    receipt = flow.pending_for(session, pending_for(flow, session, user))
    assert receipt is not None
    markup = keyboard_for(receipt, ["a-very-long-merchant-slug-indeed", "zepto"])
    for row in markup.inline_keyboard:
        for button in row:
            assert len(str(button.callback_data).encode()) <= 64


# ---------------------------------------------------------------------------
# The date keyboards
# ---------------------------------------------------------------------------

TODAY = dt.date(2026, 8, 8)
KEY = PendingKey(chat_id=-1009999999999999, message_id=999999999)


def labels(markup: object) -> list[str]:
    return [str(b.text) for row in markup.inline_keyboard for b in row]  # type: ignore[attr-defined]


def payloads(markup: object) -> list[str]:
    return [str(b.callback_data) for row in markup.inline_keyboard for b in row]  # type: ignore[attr-defined]


def test_the_date_keyboard_offers_three_answers_and_a_way_out() -> None:
    markup = date_keyboard(KEY, TODAY)
    assert labels(markup) == ["Today", "Yesterday", "Pick a date", "Discard"]


def test_today_and_yesterday_are_sent_as_dates_not_as_words() -> None:
    """A keyboard left open overnight has to log the day it offered, not the day it was tapped."""
    markup = date_keyboard(KEY, TODAY)
    assert f"{ACTION_DATE}:2026-08-08|{KEY.chat_id}|{KEY.message_id}" in payloads(markup)
    assert f"{ACTION_DATE}:2026-08-07|{KEY.chat_id}|{KEY.message_id}" in payloads(markup)


def test_the_calendar_lays_the_month_out_as_a_month() -> None:
    markup = calendar_keyboard(KEY, dt.date(2026, 8, 1), TODAY)
    rows = markup.inline_keyboard
    assert [str(b.text) for b in rows[1]] == list(WEEKDAYS)
    assert str(rows[0][1].text) == "August 2026"
    # 1 Aug 2026 is a Saturday, so five blanks come before it and the first
    # week reads Mo..Fr empty, Sa 1, Su 2.
    assert [str(b.text) for b in rows[2]] == [" ", " ", " ", " ", " ", "1", "2"]


def test_the_calendar_offers_no_day_that_has_not_happened() -> None:
    """A future day is not a day money was spent, and invariant 6 buckets on this."""
    markup = calendar_keyboard(KEY, dt.date(2026, 8, 1), TODAY)
    offered = {p.split(":", 1)[1].split("|", 1)[0] for p in payloads(markup) if p.startswith("d:")}
    assert max(offered) == "2026-08-08"
    assert len(offered) == 8


def test_the_calendar_does_not_page_past_the_current_month() -> None:
    current = calendar_keyboard(KEY, dt.date(2026, 8, 1), TODAY)
    assert not any(p.startswith(f"{ACTION_CALENDAR}:2026-09") for p in payloads(current))

    older = calendar_keyboard(KEY, dt.date(2026, 6, 1), TODAY)
    assert any(p.startswith(f"{ACTION_CALENDAR}:2026-07") for p in payloads(older))
    assert any(p.startswith(f"{ACTION_CALENDAR}:2026-05") for p in payloads(older))


def test_the_calendar_pages_across_a_year_boundary() -> None:
    """December to January is the one month arithmetic that gets written wrong."""
    markup = calendar_keyboard(KEY, dt.date(2026, 1, 1), dt.date(2026, 8, 8))
    assert any(p.startswith(f"{ACTION_CALENDAR}:2025-12") for p in payloads(markup))
    assert any(p.startswith(f"{ACTION_CALENDAR}:2026-02") for p in payloads(markup))


@pytest.mark.parametrize("month", [dt.date(2026, 2, 1), dt.date(2024, 2, 1), dt.date(2026, 8, 1)])
def test_every_calendar_button_fits_telegrams_callback_limit(month: dt.date) -> None:
    """The worst realistic key is a supergroup ID, and every cell carries one."""
    markup = calendar_keyboard(KEY, month, dt.date(2026, 12, 31))
    for payload in payloads(markup):
        assert len(payload.encode()) <= 64


def test_a_leap_day_is_offered() -> None:
    markup = calendar_keyboard(KEY, dt.date(2024, 2, 1), dt.date(2026, 8, 8))
    assert f"{ACTION_DATE}:2024-02-29|{KEY.chat_id}|{KEY.message_id}" in payloads(markup)


def test_the_calendars_inert_cells_decide_nothing() -> None:
    """Telegram has no inert cell, so the blanks and labels are buttons that must do nothing."""
    markup = calendar_keyboard(KEY, dt.date(2026, 8, 1), TODAY)
    blank = KEY.encode(ACTION_NOOP)
    for row in markup.inline_keyboard:
        for button in row:
            if str(button.text).strip() in {"", *WEEKDAYS, "August 2026"}:
                assert str(button.callback_data) == blank


# ---------------------------------------------------------------------------
# Action dispatch
# ---------------------------------------------------------------------------


def test_confirm_dispatches_to_the_ledger(
    seeded: tuple[Session, User], bot: RaseedBot, flow: ReceiptFlow
) -> None:
    """The fixtures print no date, so confirm asks and the answer is what commits."""
    session, user = seeded
    key = pending_for(flow, session, user)
    assert bot.dispatch(session, ACTION_CONFIRM, key).step is Step.AWAITING_DATE
    result = bot.dispatch(session, f"{ACTION_DATE}:2026-08-01", key)
    assert result.step is Step.STORED
    assert result.transaction is not None
    assert result.transaction.occurred_on_local == dt.date(2026, 8, 1)


def test_discard_dispatches_without_writing(
    seeded: tuple[Session, User], bot: RaseedBot, flow: ReceiptFlow
) -> None:
    session, user = seeded
    key = pending_for(flow, session, user)
    result = bot.dispatch(session, ACTION_DISCARD, key)
    assert result.step is Step.DISCARDED


def test_a_merchant_pick_dispatches_with_its_slug(
    seeded: tuple[Session, User], bot: RaseedBot, flow: ReceiptFlow
) -> None:
    session, user = seeded
    key = pending_for(flow, session, user)
    bot.dispatch(session, f"{ACTION_MERCHANT}:blinkit", key)
    receipt = flow.pending_for(session, key)
    assert receipt is not None
    assert receipt.merchant_slug == "blinkit"


def test_an_unknown_action_is_survivable(
    seeded: tuple[Session, User], bot: RaseedBot, flow: ReceiptFlow
) -> None:
    """A stale button from an older deploy must not raise."""
    session, user = seeded
    key = pending_for(flow, session, user)
    result = bot.dispatch(session, "wat", key)
    assert result.step is Step.EXPIRED


def test_paging_the_calendar_decides_nothing(
    seeded: tuple[Session, User], bot: RaseedBot, flow: ReceiptFlow
) -> None:
    """Six taps through the months must leave the receipt exactly where it was."""
    session, user = seeded
    key = pending_for(flow, session, user)
    before = flow.pending_for(session, key)
    assert before is not None

    for month in ("2026-07", "2026-06", "2026-05"):
        assert bot.dispatch(session, f"{ACTION_CALENDAR}:{month}", key).step is Step.AWAITING_DATE

    after = flow.pending_for(session, key)
    assert after is not None
    assert after.occurred_on_local == before.occurred_on_local
    assert after.date_source is before.date_source


def test_paging_a_calendar_whose_receipt_is_gone_says_so(
    seeded: tuple[Session, User], bot: RaseedBot, flow: ReceiptFlow
) -> None:
    session, user = seeded
    key = pending_for(flow, session, user)
    bot.dispatch(session, ACTION_DISCARD, key)
    result = bot.dispatch(session, f"{ACTION_CALENDAR}:2026-07", key)
    assert result.step is Step.EXPIRED
    assert result.message == EXPIRED_MESSAGE


@pytest.mark.parametrize(
    "action",
    [
        f"{ACTION_DATE}:not-a-date",
        f"{ACTION_DATE}:2026-02-30",
        f"{ACTION_DATE}:",
        f"{ACTION_CALENDAR}:2026-13",
        f"{ACTION_CALENDAR}:nonsense",
        f"{ACTION_CALENDAR}:",
    ],
)
def test_a_malformed_date_payload_writes_nothing(
    action: str, seeded: tuple[Session, User], bot: RaseedBot, flow: ReceiptFlow
) -> None:
    """Callback data is user-supplied. Anything can arrive on it."""
    session, user = seeded
    key = pending_for(flow, session, user)
    result = bot.dispatch(session, action, key)
    assert result.step is Step.EXPIRED
    assert result.transaction is None
    # And the receipt is still waiting, so a real button still works.
    assert flow.pending_for(session, key) is not None


# ---------------------------------------------------------------------------
# Which keyboard a tap leads to
# ---------------------------------------------------------------------------


def test_only_paging_opens_the_calendar() -> None:
    assert labels(keyboard_after(KEY, ACTION_CONFIRM, TODAY))[0] == "Today"
    assert str(
        keyboard_after(KEY, f"{ACTION_CALENDAR}:2026-06", TODAY).inline_keyboard[0][1].text
    ) == ("June 2026")


def test_the_calendars_back_button_returns_to_the_three_answers() -> None:
    """Back is a Confirm, because Confirm is what asks the question."""
    calendar = calendar_keyboard(KEY, dt.date(2026, 6, 1), TODAY)
    back = next(b for row in calendar.inline_keyboard for b in row if str(b.text) == "Back")
    action, key = PendingKey.decode(str(back.callback_data))
    assert key == KEY
    assert labels(keyboard_after(key, action, TODAY))[0] == "Today"


class Tapper:
    """The smallest stand-in for a callback query that records what it did."""

    def __init__(self, data: str) -> None:
        self.data = data
        self.answers = 0
        self.edits: list[tuple[str, object]] = []

    async def answer(self, *_args: object, **_kwargs: object) -> None:
        self.answers += 1

    async def edit_message_text(self, text: str, reply_markup: object = None, **_k: object) -> None:
        self.edits.append((text, reply_markup))


def tap(data: str) -> tuple[Tapper, Update]:
    query = Tapper(data)
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=ALLOWED_ID),
        message=None,
    )
    return query, cast(Update, update)


@pytest.fixture
def wired_bot(flow: ReceiptFlow, seeded: tuple[Session, User]) -> RaseedBot:
    """A bot whose sessions land in the same ledger the test seeded."""
    session, _ = seeded
    return RaseedBot(
        flow=flow,
        session_factory=sessionmaker(bind=session.get_bind()),
        allowed_user_ids=frozenset({ALLOWED_ID}),
        clock=lambda: NOW,
        user_id_secret=SECRET,
    )


def test_an_inert_calendar_cell_edits_nothing(
    seeded: tuple[Session, User], wired_bot: RaseedBot, flow: ReceiptFlow
) -> None:
    """Telegram rejects an edit to the text a message already has.

    So a blank square has to be acknowledged and dropped. Editing anything here
    surfaces to the user as an error on a button that was drawn to do nothing.
    """
    session, user = seeded
    key = pending_for(flow, session, user)
    session.commit()

    query, update = tap(key.encode(ACTION_NOOP))
    asyncio.run(wired_bot.on_button(update, cast(ContextTypes.DEFAULT_TYPE, None)))

    assert query.answers == 1
    assert query.edits == []


def test_confirming_a_dateless_receipt_puts_the_date_keyboard_on_screen(
    seeded: tuple[Session, User], wired_bot: RaseedBot, flow: ReceiptFlow
) -> None:
    session, user = seeded
    key = pending_for(flow, session, user)
    session.commit()

    query, update = tap(key.encode(ACTION_CONFIRM))
    asyncio.run(wired_bot.on_button(update, cast(ContextTypes.DEFAULT_TYPE, None)))

    text, markup = query.edits[-1]
    assert text == DATE_PROMPT
    assert labels(markup) == ["Today", "Yesterday", "Pick a date", "Discard"]

    query, update = tap(key.encode(f"{ACTION_CALENDAR}:2026-08"))
    asyncio.run(wired_bot.on_button(update, cast(ContextTypes.DEFAULT_TYPE, None)))
    assert "August 2026" in labels(query.edits[-1][1])


# ---------------------------------------------------------------------------
# Failure (found live, 2026-08-08)
# ---------------------------------------------------------------------------


class Replier:
    """The smallest stand-in for a message that records what it was told."""

    def __init__(self, *, fails: bool = False) -> None:
        self.said: list[str] = []
        self._fails = fails

    async def reply_text(self, text: str, **_kwargs: object) -> None:
        if self._fails:
            raise TelegramError("chat not found")
        self.said.append(text)


def failing_update(message: Replier | None) -> object:
    return SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=ALLOWED_ID))


def error_context(exc: Exception) -> ContextTypes.DEFAULT_TYPE:
    return cast("ContextTypes.DEFAULT_TYPE", SimpleNamespace(error=exc))


def document_update(mime_type: str, message: Replier) -> Update:
    """An update carrying a file rather than a photo."""
    return cast(
        "Update",
        SimpleNamespace(
            effective_user=SimpleNamespace(id=ALLOWED_ID),
            message=SimpleNamespace(
                document=SimpleNamespace(mime_type=mime_type),
                reply_text=message.reply_text,
            ),
        ),
    )


def test_a_pdf_gets_an_answer_rather_than_silence(bot: RaseedBot) -> None:
    """The reported behaviour: a PDF used to be indistinguishable from a dead bot.

    Silence is what a sender who is not on the allowlist gets, deliberately. A
    permitted user sending the wrong file type is doing nothing wrong and must
    not get the same treatment.
    """
    message = Replier()
    update = document_update("application/pdf", message)

    asyncio.run(bot.on_document(update, cast("ContextTypes.DEFAULT_TYPE", None)))

    assert message.said == [UNREADABLE_FILE_MESSAGE]


def test_an_unlisted_sender_still_gets_silence_for_a_pdf(flow: ReceiptFlow) -> None:
    """The whitelist outranks the new message. Replying confirms the bot exists."""
    closed = RaseedBot(
        flow=flow,
        session_factory=cast("sessionmaker[Session]", None),
        allowed_user_ids=frozenset(),
        clock=lambda: NOW,
        user_id_secret=SECRET,
    )
    message = Replier()
    update = document_update("application/pdf", message)

    asyncio.run(closed.on_document(update, cast("ContextTypes.DEFAULT_TYPE", None)))

    assert message.said == []


def test_a_crash_gets_a_reply_instead_of_silence(bot: RaseedBot) -> None:
    """Found live: a handler raised, nothing was registered, the user saw nothing.

    Silence is the worst answer available, because it is indistinguishable from
    the bot being down.
    """
    message = Replier()
    asyncio.run(bot.on_error(failing_update(message), error_context(RuntimeError("boom"))))

    assert len(message.said) == 1
    assert "went wrong" in message.said[0].lower()
    assert "nothing was changed" in message.said[0].lower()


def test_the_error_message_does_not_coach_the_user(bot: RaseedBot) -> None:
    """Invariant 10 still applies when the bot is the thing that broke."""
    message = Replier()
    asyncio.run(bot.on_error(failing_update(message), error_context(RuntimeError("boom"))))

    lowered = message.said[0].lower()
    for phrase in ("crop", "retake", "rotate", "as a file", "resend", "try again", "lighting"):
        assert phrase not in lowered


def test_an_update_with_no_message_is_survivable(bot: RaseedBot) -> None:
    """Callback queries and channel posts may carry nothing to reply to."""
    asyncio.run(bot.on_error(failing_update(None), error_context(RuntimeError("boom"))))


def test_a_failure_to_deliver_the_error_does_not_raise(bot: RaseedBot) -> None:
    """The error handler is the last line. If it raises, PTB has nowhere to go."""
    asyncio.run(
        bot.on_error(failing_update(Replier(fails=True)), error_context(RuntimeError("boom")))
    )


def test_the_error_handler_is_registered() -> None:
    """The bug was not a missing message, it was a missing registration."""
    source = (
        Path(__file__).resolve().parent.parent / "src" / "raseed" / "adapters" / "telegram.py"
    ).read_text(encoding="utf-8")
    assert "add_error_handler(self.on_error)" in source


def test_a_button_for_an_unknown_receipt_does_not_raise(
    seeded: tuple[Session, User], bot: RaseedBot
) -> None:
    """A button can outlive its state: the TTL runs out, or it was already used."""
    session, _user = seeded
    result = bot.dispatch(session, ACTION_CONFIRM, PendingKey(chat_id=1, message_id=1))
    assert result.step is Step.EXPIRED
    assert "24 hours" in result.message


def test_the_expired_message_names_the_cause_and_the_consequence() -> None:
    """Found live on 2026-08-09, and corrected on 2026-08-11.

    The first version said "that one is no longer waiting", which reads like a
    fault rather than like state that legitimately no longer exists. The second
    blamed a restart, which was true until `DatabasePendingStore` moved pending
    state to disk and false for two days afterwards. What is left are the two
    causes that can actually put a live-looking button in front of someone.

    Still not a word about how to send a receipt. Invariant 10.
    """
    assert "24 hours" in EXPIRED_MESSAGE
    assert "already answered" in EXPIRED_MESSAGE
    assert "Nothing new was saved" in EXPIRED_MESSAGE
    assert "restart" not in EXPIRED_MESSAGE.lower()
    for coaching in ("crop", "rotate", "as a file", "retake", "clearer", "better"):
        assert coaching not in EXPIRED_MESSAGE.lower()
