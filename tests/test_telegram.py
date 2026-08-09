"""Tests for the Telegram adapter.

No bot token and no network. `RaseedBot` is deliberately thin, so what is worth
testing here is exactly the part that is not: the whitelist, the callback
keyboard, and the action dispatch table. Everything else is `flow.py`, which
`test_flow.py` covers.

`Update` is stubbed rather than constructed, because a real one needs a `Bot`
instance and this file must not need credentials to run.
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
from raseed.adapters.flow import EXPIRED_MESSAGE, FlowConfig, ReceiptFlow, Step
from raseed.adapters.images import ImageStore
from raseed.adapters.pending import PendingKey, PendingReceipt, PendingStore
from raseed.adapters.telegram import (
    ACTION_ACCEPT_GAP,
    ACTION_CONFIRM,
    ACTION_DISCARD,
    ACTION_MERCHANT,
    GREETING,
    RaseedBot,
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
    opens one. The async handlers do, and they are exercised end to end through
    `flow.py` in `test_flow.py` rather than through a fake Telegram runtime.
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
        pending=PendingStore(),
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
    receipt = flow.pending_for(key)
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
        pending=PendingStore(),
        config=FlowConfig(),
        clock=lambda: NOW,
    )
    key = pending_for(gap_flow, session, user)
    receipt = gap_flow.pending_for(key)
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
    receipt = flow.pending_for(key)
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
    first = flow.pending_for(pending_for(flow, session, user, message_id=10))
    second = flow.pending_for(pending_for(flow, session, user, message_id=11))
    assert first is not None and second is not None

    def data_for(receipt: PendingReceipt) -> set[str]:
        markup = keyboard_for(receipt)
        return {str(b.callback_data) for row in markup.inline_keyboard for b in row}

    assert data_for(first).isdisjoint(data_for(second))


def test_every_button_fits_telegrams_callback_limit(
    seeded: tuple[Session, User], flow: ReceiptFlow
) -> None:
    session, user = seeded
    receipt = flow.pending_for(pending_for(flow, session, user))
    assert receipt is not None
    markup = keyboard_for(receipt, ["a-very-long-merchant-slug-indeed", "zepto"])
    for row in markup.inline_keyboard:
        for button in row:
            assert len(str(button.callback_data).encode()) <= 64


# ---------------------------------------------------------------------------
# Action dispatch
# ---------------------------------------------------------------------------


def test_confirm_dispatches_to_the_ledger(
    seeded: tuple[Session, User], bot: RaseedBot, flow: ReceiptFlow
) -> None:
    session, user = seeded
    key = pending_for(flow, session, user)
    result = bot.dispatch(session, ACTION_CONFIRM, key)
    assert result.step is Step.STORED


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
    receipt = flow.pending_for(key)
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
    """A restart empties the pending store while the buttons stay on screen."""
    session, _user = seeded
    result = bot.dispatch(session, ACTION_CONFIRM, PendingKey(chat_id=1, message_id=1))
    assert result.step is Step.EXPIRED
    assert "restarted" in result.message


def test_the_expired_message_names_the_cause_and_the_consequence() -> None:
    """Found live on 2026-08-09.

    Ashrit tapped Confirm on receipts submitted before a restart and got "that
    one is no longer waiting", which reads like a fault rather than like state
    that legitimately no longer exists. The wording now says why and says that
    nothing was saved, without a word about how to send a receipt (invariant 10).
    """
    assert "restarted" in EXPIRED_MESSAGE
    assert "timed out" in EXPIRED_MESSAGE
    assert "Nothing was saved" in EXPIRED_MESSAGE
    for coaching in ("crop", "rotate", "as a file", "retake", "clearer", "better"):
        assert coaching not in EXPIRED_MESSAGE.lower()
