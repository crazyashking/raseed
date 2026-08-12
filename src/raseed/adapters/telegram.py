"""The Telegram front end.

This module owns transport and nothing else. Every decision about a receipt is
made in `raseed.adapters.flow`, which is why that one is testable without a bot
token and this one is thin enough to read in a sitting.

Three things here are load-bearing:

- **Access control is a whitelist, and an empty whitelist admits nobody.** An
  unlisted sender gets no reply at all, not even a refusal, because replying
  confirms the bot exists to whoever is probing it.
- **Callback data carries `(chat_id, message_id)`**, so three receipts sent in a
  row cannot confuse their confirm buttons. Brief 16.4.
- **The bot never tells you how to send a receipt.** Invariant 10. It may say it
  could not read one, which is a different statement, and it never suggests
  cropping, retaking as a file, rotating or resending differently.
"""

from __future__ import annotations

import calendar
import datetime as dt
import logging
from collections.abc import Callable, Iterable
from typing import Final

from sqlalchemy.orm import Session, sessionmaker
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from raseed.adapters.flow import (
    DATE_PROMPT,
    EXPIRED_MESSAGE,
    UNREADABLE_FILE_MESSAGE,
    FlowResult,
    ReceiptFlow,
    Step,
    rupees,
)
from raseed.adapters.pending import PendingKey, PendingReceipt
from raseed.db import ledger, queries
from raseed.db.models import User
from raseed.db.seed import bootstrap
from raseed.identity import user_id_for
from raseed.validation.reconcile import Outcome

log = logging.getLogger(__name__)

ACTION_CONFIRM: Final[str] = "ok"
ACTION_DISCARD: Final[str] = "no"
ACTION_EDIT: Final[str] = "ed"
ACTION_ACCEPT_GAP: Final[str] = "gap"
ACTION_MERCHANT: Final[str] = "m"

#: Picks the date a receipt is logged under. Payload is an ISO date, so the
#: whole answer travels in the button and nothing about the question is held in
#: memory between two taps. A restart mid-question costs nothing.
ACTION_DATE: Final[str] = "d"

#: Pages the calendar. Payload is `YYYY-MM`. Changes no state at all: it swaps
#: one keyboard for another under the same unanswered question.
ACTION_CALENDAR: Final[str] = "cal"

#: The blanks and labels that make the grid look like a calendar. Telegram has
#: no such thing as an inert cell, so they are buttons that do nothing.
ACTION_NOOP: Final[str] = "x"

#: Monday first, matching how a calendar is printed in India and in most of the
#: world. `calendar.monthrange` also counts from Monday, so the two agree and no
#: offset arithmetic is needed.
WEEKDAYS: Final[tuple[str, ...]] = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")

#: The calendar's month arrows. Solid triangles rather than angle quotes, which
#: render at the size of a comma next to a button label on a phone.
BACK: Final[str] = "\N{BLACK LEFT-POINTING TRIANGLE}"
FORWARD: Final[str] = "\N{BLACK RIGHT-POINTING TRIANGLE}"

#: A button this bot did not draw, or one whose payload arrived malformed.
UNKNOWN_BUTTON_MESSAGE: Final[str] = "I do not know what that button does."

#: Telegram compresses `message.photo` and caps the long edge. A document keeps
#: the original bytes. Both are accepted and the user is never told which is
#: better, because that would be an instruction about how to send. Invariant 10.
PHOTO_MIME: Final[str] = "image/jpeg"

GREETING: Final[str] = (
    "Send me a picture of a receipt and I will read it, check the arithmetic, "
    "and show you what I got before saving anything.\n\n"
    "/recent shows the last few.\n"
    "/undo removes the most recent one.\n"
    "/dashboard opens the full view."
)


def keyboard_for(receipt: PendingReceipt, merchants: Iterable[str] = ()) -> InlineKeyboardMarkup:
    """Build the confirm keyboard for one pending receipt.

    A Class 2 receipt gets a different first row: it cannot be confirmed until
    the user has decided what to do about the gap. Brief 3.3.
    """
    key = receipt.key
    rows: list[list[InlineKeyboardButton]] = []

    unresolved_gap = receipt.reconciliation.outcome is Outcome.CLASS_2 and not receipt.gap_accepted

    if unresolved_gap:
        rows.append(
            [
                InlineKeyboardButton("Log the gap", callback_data=key.encode(ACTION_ACCEPT_GAP)),
                InlineKeyboardButton("Discard", callback_data=key.encode(ACTION_DISCARD)),
            ]
        )
        return InlineKeyboardMarkup(rows)

    rows.append(
        [
            InlineKeyboardButton("Confirm", callback_data=key.encode(ACTION_CONFIRM)),
            InlineKeyboardButton("Discard", callback_data=key.encode(ACTION_DISCARD)),
        ]
    )

    if receipt.merchant_slug is None:
        picks = [
            InlineKeyboardButton(
                slug.replace("-", " ").title(),
                callback_data=key.encode(f"{ACTION_MERCHANT}:{slug}"),
            )
            for slug in list(merchants)[:3]
        ]
        if picks:
            rows.append(picks)

    return InlineKeyboardMarkup(rows)


def _calendar_month(action: str) -> dt.date | None:
    """The month a `cal:` action is asking for, or None if it is not one.

    Doubling as the recogniser keeps the parse in one place: a `cal:` action
    that carries something unreadable is not a calendar action, so it falls
    through to the unknown-button branch instead of raising in a handler.
    """
    prefix = f"{ACTION_CALENDAR}:"
    if not action.startswith(prefix):
        return None
    year, _, month = action[len(prefix) :].partition("-")
    try:
        return dt.date(int(year), int(month), 1)
    except ValueError:
        return None


def keyboard_after(key: PendingKey, action: str, today: dt.date) -> InlineKeyboardMarkup:
    """Which of the two date keyboards an action leads to.

    Split out of the handler so the choice can be tested without a Telegram
    runtime. Every action except paging leads back to Today/Yesterday, including
    Confirm itself, which is what makes the calendar's Back button work.
    """
    month = _calendar_month(action)
    if month is None:
        return date_keyboard(key, today)
    return calendar_keyboard(key, month, today)


def date_keyboard(key: PendingKey, today: dt.date) -> InlineKeyboardMarkup:
    """The three answers to "when was this", plus a way out.

    Today and yesterday are sent as ISO dates rather than as the words, so the
    handler never has to work out what "today" meant at the moment the button
    was drawn. A keyboard left open overnight still logs the day it offered.
    """
    yesterday = today - dt.timedelta(days=1)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Today", callback_data=key.encode(f"{ACTION_DATE}:{today.isoformat()}")
                ),
                InlineKeyboardButton(
                    "Yesterday", callback_data=key.encode(f"{ACTION_DATE}:{yesterday.isoformat()}")
                ),
            ],
            [
                InlineKeyboardButton(
                    "Pick a date",
                    callback_data=key.encode(f"{ACTION_CALENDAR}:{today:%Y-%m}"),
                )
            ],
            [InlineKeyboardButton("Discard", callback_data=key.encode(ACTION_DISCARD))],
        ]
    )


def calendar_keyboard(key: PendingKey, month: dt.date, today: dt.date) -> InlineKeyboardMarkup:
    """A month grid for backdating a receipt.

    Every cell is a whole answer, so paging back six months and tapping a day
    involves no stored conversation state and survives a restart. The
    alternative was asking the user to type a date, which means parsing free
    text in the one place where a misread is a row in the wrong month.

    Days after `today` are drawn blank. A future day is not a day money was
    spent, and a greyed-out number that silently ignores a tap is worse than an
    empty square that plainly is not offering anything.
    """
    blank = key.encode(ACTION_NOOP)
    first = month.replace(day=1)
    leading, days = calendar.monthrange(first.year, first.month)

    previous = (first - dt.timedelta(days=1)).replace(day=1)
    following = (first + dt.timedelta(days=days)).replace(day=1)
    ahead = following <= today
    header = [
        InlineKeyboardButton(BACK, callback_data=key.encode(f"{ACTION_CALENDAR}:{previous:%Y-%m}")),
        InlineKeyboardButton(f"{first:%B %Y}", callback_data=blank),
        # No forward arrow out of the current month. There is nothing there.
        InlineKeyboardButton(
            FORWARD if ahead else " ",
            callback_data=(key.encode(f"{ACTION_CALENDAR}:{following:%Y-%m}") if ahead else blank),
        ),
    ]

    cells = [InlineKeyboardButton(" ", callback_data=blank)] * leading
    for day in range(1, days + 1):
        on = first.replace(day=day)
        cells.append(
            InlineKeyboardButton(
                str(day), callback_data=key.encode(f"{ACTION_DATE}:{on.isoformat()}")
            )
            if on <= today
            else InlineKeyboardButton(" ", callback_data=blank)
        )

    rows = [header, [InlineKeyboardButton(name, callback_data=blank) for name in WEEKDAYS]]
    rows.extend(cells[start : start + 7] for start in range(0, len(cells), 7))
    rows.append(
        [
            # Back to Today/Yesterday. Confirm is what asks the question, so
            # asking it again is the way back to it, and the receipt is still
            # unanswered so it cannot commit anything.
            InlineKeyboardButton("Back", callback_data=key.encode(ACTION_CONFIRM)),
            InlineKeyboardButton("Discard", callback_data=key.encode(ACTION_DISCARD)),
        ]
    )
    return InlineKeyboardMarkup(rows)


class RaseedBot:
    """Wires the receipt flow onto python-telegram-bot.

    Args:
        flow: The transport-agnostic pipeline.
        session_factory: Opens a database session per update.
        allowed_user_ids: Telegram numeric IDs permitted to use this bot. Empty
            admits nobody, which is the safe default. This is the whole invite
            mechanism: adding a friend is adding their number here.
        clock: Injected so tests do not depend on wall time.
        user_id_secret: Derives each person's `user_id` from their Telegram ID.
            See `raseed.identity`. Never stored, and must never change.
        dashboard_url: The loopback address, for when there is no public one.
        public_url: The public HTTPS address. Its presence is what turns
            `/dashboard` into a Mini App button, which is the only form that
            carries the signed `initData` the dashboard authenticates with.
    """

    def __init__(
        self,
        *,
        flow: ReceiptFlow,
        session_factory: sessionmaker[Session],
        allowed_user_ids: frozenset[int],
        clock: Callable[[], dt.datetime],
        user_id_secret: str,
        dashboard_url: str | None = None,
        public_url: str = "",
    ) -> None:
        self._flow = flow
        self._sessions = session_factory
        self._allowed = allowed_user_ids
        self._clock = clock
        self._secret = user_id_secret
        self._dashboard_url = dashboard_url
        self._public_url = public_url

    # -- access control ------------------------------------------------------

    def permitted(self, update: Update) -> bool:
        """Whitelist check. An empty whitelist admits nobody."""
        user = update.effective_user
        if user is None:
            return False
        return user.id in self._allowed

    def owner(self, session: Session, update: Update) -> User | None:
        """The ledger this update belongs to, created on first sight.

        Derived per person, so a friend's first photo is also their signup and
        two people never share a ledger. Returns None when the update carries no
        user at all, which a caller must treat as "do nothing" rather than as
        "use the default account": the old code called `bootstrap(session)` with
        no argument and would have handed friend number two the owner's ledger.
        """
        user = update.effective_user
        if user is None:
            return None
        return bootstrap(session, user_id_for(user.id, secret=self._secret))

    # -- handlers ------------------------------------------------------------

    async def start(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.permitted(update) or update.message is None:
            return
        await update.message.reply_text(GREETING)

    async def on_photo(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        """A compressed photo, which is what tapping the camera icon produces."""
        if not self.permitted(update) or update.message is None:
            return
        photos = update.message.photo
        if not photos:
            return
        # The last entry is the largest size Telegram kept.
        file = await photos[-1].get_file()
        data = bytes(await file.download_as_bytearray())
        await self._ingest(update, data=data, mime_type=PHOTO_MIME)

    async def on_document(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        """An image sent as a file, which keeps the original bytes."""
        if not self.permitted(update) or update.message is None:
            return
        document = update.message.document
        if document is None:
            return
        if not (document.mime_type or "").startswith("image/"):
            # A PDF used to land here and get silence, which is what a stranger
            # who is not on the allowlist gets, so the bot looked broken to
            # someone doing nothing wrong. Invariant 10's 2026-08-11 exception
            # is what lets this say which types do work.
            log.info("refused a %s document, nothing was read", document.mime_type or "unknown")
            await update.message.reply_text(UNREADABLE_FILE_MESSAGE)
            return
        file = await document.get_file()
        data = bytes(await file.download_as_bytearray())
        await self._ingest(update, data=data, mime_type=document.mime_type or PHOTO_MIME)

    async def _ingest(self, update: Update, *, data: bytes, mime_type: str) -> None:
        message = update.message
        user = update.effective_user
        if message is None or user is None:
            return

        with self._sessions() as session:
            owner = self.owner(session, update)
            if owner is None:
                return
            result = self._flow.submit_image(
                session,
                user_id=owner.id,
                chat_id=message.chat_id,
                message_id=message.message_id,
                data=data,
                mime_type=mime_type,
                message_date=message.date,
            )
            log.info("receipt -> %s", result.step.value)
            session.commit()
            await self._reply(update, result, session=session, user_id=owner.id)

    async def on_button(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        """Every inline keyboard press lands here."""
        query = update.callback_query
        if query is None or not self.permitted(update):
            return
        await query.answer()

        try:
            action, key = PendingKey.decode(query.data or "")
        except ValueError:
            log.warning("ignoring unrecognised callback data: %r", query.data)
            return

        if action == ACTION_NOOP:
            # A blank square or a weekday label in the calendar. The tap has
            # already been acknowledged above, and editing a message to the text
            # it already has is an error from Telegram, not a no-op.
            return

        with self._sessions() as session:
            owner = self.owner(session, update)
            if owner is None:
                return
            result = self.dispatch(session, action, key)
            # One line per press, at INFO. A confirm that quietly answered
            # "no longer waiting" used to leave no trace anywhere, which is why
            # a broken-looking button on 2026-08-09 had to be reconstructed from
            # the ledger rather than read off a log. The action and the step are
            # enough to tell a restart from a duplicate from a crash, and
            # neither is a receipt, an amount, or anything about the sender.
            log.info("button %s -> %s", action, result.step.value)
            session.commit()

            if result.step is Step.AWAITING_CONFIRMATION and result.pending is not None:
                merchants = [m.slug for m in queries.known_merchants(session, user_id=owner.id)]
                await query.edit_message_text(
                    result.message, reply_markup=keyboard_for(result.pending, merchants)
                )
            elif result.step is Step.AWAITING_DATE:
                await query.edit_message_text(
                    result.message,
                    reply_markup=keyboard_after(key, action, self._flow.local_today()),
                )
            else:
                await query.edit_message_text(result.message)

    def dispatch(self, session: Session, action: str, key: PendingKey) -> FlowResult:
        decisions: dict[str, Callable[[Session, PendingKey], FlowResult]] = {
            ACTION_CONFIRM: self._flow.confirm,
            ACTION_DISCARD: self._flow.discard,
            ACTION_ACCEPT_GAP: self._flow.accept_gap,
        }
        decide = decisions.get(action)
        if decide is not None:
            return decide(session, key)
        if action.startswith(f"{ACTION_MERCHANT}:"):
            return self._flow.set_merchant(session, key, action.split(":", 1)[1])
        if action.startswith((f"{ACTION_DATE}:", f"{ACTION_CALENDAR}:")):
            return self._dispatch_date(session, action, key)
        if action == ACTION_EDIT:
            return FlowResult(
                step=Step.AWAITING_CONFIRMATION,
                message="Pick a merchant below, or confirm as is.",
            )
        log.warning("unknown callback action: %r", action)
        return FlowResult(step=Step.EXPIRED, message=UNKNOWN_BUTTON_MESSAGE)

    def _dispatch_date(self, session: Session, action: str, key: PendingKey) -> FlowResult:
        """Both halves of the date question: paging the calendar and answering it."""
        if action.startswith(f"{ACTION_CALENDAR}:"):
            if _calendar_month(action) is None:
                log.warning("callback carried an unreadable month: %r", action)
                return FlowResult(step=Step.EXPIRED, message=UNKNOWN_BUTTON_MESSAGE)
            # Paging decides nothing, so it only has to establish that there is
            # still a receipt behind the buttons.
            if self._flow.pending_for(session, key) is None:
                return FlowResult(step=Step.EXPIRED, message=EXPIRED_MESSAGE)
            return FlowResult(step=Step.AWAITING_DATE, message=DATE_PROMPT)
        try:
            on = dt.date.fromisoformat(action.split(":", 1)[1])
        except ValueError:
            log.warning("callback carried an unreadable date: %r", action)
            return FlowResult(step=Step.EXPIRED, message=UNKNOWN_BUTTON_MESSAGE)
        return self._flow.set_date(session, key, on)

    async def recent(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        """Brief 16.3."""
        if not self.permitted(update) or update.message is None:
            return
        with self._sessions() as session:
            owner = self.owner(session, update)
            if owner is None:
                return
            rows = queries.recent_transactions(session, user_id=owner.id)
            session.commit()

        if not rows:
            await update.message.reply_text("Nothing logged yet.")
            return

        lines = [
            f"{row.occurred_on_local.isoformat()}  {rupees(row.grand_total_minor)}" for row in rows
        ]
        await update.message.reply_text("\n".join(lines))

    async def dashboard(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        """Open the dashboard.

        A Mini App button when there is a public HTTPS address, because that is
        what carries the signed `initData` the dashboard authenticates with. A
        plain link otherwise: Telegram refuses to render a button for
        `http://127.0.0.1`, which is the correct restriction and not one to work
        around, and the loopback dashboard is only reachable from this machine
        anyway.
        """
        if not self.permitted(update) or update.message is None:
            return
        if self._public_url:
            await update.message.reply_text(
                "Your dashboard:",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Open dashboard", web_app=WebAppInfo(url=self._public_url)
                            )
                        ]
                    ]
                ),
            )
            return
        if self._dashboard_url is None:
            await update.message.reply_text("The dashboard is not running in this process.")
            return
        await update.message.reply_text(f"Your dashboard:\n{self._dashboard_url}")

    async def undo(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        """Soft delete the newest row. Never a hard delete. Invariant 2."""
        if not self.permitted(update) or update.message is None:
            return
        with self._sessions() as session:
            owner = self.owner(session, update)
            if owner is None:
                return
            rows = queries.recent_transactions(session, user_id=owner.id, limit=1)
            if not rows:
                session.commit()
                await update.message.reply_text("Nothing to undo.")
                return
            ledger.soft_delete_transaction(session, transaction=rows[0], when=self._clock())
            amount = rupees(rows[0].grand_total_minor)
            session.commit()
        await update.message.reply_text(f"Removed {amount}.")

    async def _reply(
        self, update: Update, result: FlowResult, *, session: Session, user_id: str
    ) -> None:
        message = update.message
        if message is None:
            return
        if result.step is Step.AWAITING_CONFIRMATION and result.pending is not None:
            merchants = [m.slug for m in queries.known_merchants(session, user_id=user_id)]
            await message.reply_text(
                result.message, reply_markup=keyboard_for(result.pending, merchants)
            )
            return
        await message.reply_text(result.message)

    # -- failure -------------------------------------------------------------

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Say something when a handler raises, instead of nothing.

        Without this, python-telegram-bot logs `No error handlers are
        registered` and the user gets silence. That happened live on
        2026-08-08: a `/undo` then a resend hit an integrity error on confirm,
        and the receipt simply never came back. Silence is the worst possible
        answer, because it is indistinguishable from the bot being down.

        The message deliberately says nothing about how to send the receipt.
        Invariant 10. "Something went wrong on my end" is a statement about the
        bot; "try sending it as a file" would be an instruction to the user.
        """
        log.exception("unhandled error while processing an update", exc_info=context.error)

        message = getattr(update, "effective_message", None)
        if message is None:
            return
        try:
            await message.reply_text(
                "Something went wrong on my end and I did not save that one. Nothing was changed."
            )
        except TelegramError:
            # The reply itself failed. Nothing further to try, and raising here
            # would take down the error handler as well.
            log.exception("could not deliver the error message")

    # -- wiring --------------------------------------------------------------

    def register(self, application: Application) -> None:  # type: ignore[type-arg]
        """Attach every handler. Order matters: commands before the catch-all."""
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("help", self.start))
        application.add_handler(CommandHandler("recent", self.recent))
        application.add_handler(CommandHandler("undo", self.undo))
        application.add_handler(CommandHandler("dashboard", self.dashboard))
        application.add_handler(MessageHandler(filters.PHOTO, self.on_photo))
        # Every document, not `filters.Document.IMAGE`. The narrow filter is what
        # a PDF used to fall through, and `on_document` refusing it politely was
        # dead code because the update never arrived. `on_document` does the MIME
        # check itself, so widening here is what makes that refusal reachable.
        application.add_handler(MessageHandler(filters.Document.ALL, self.on_document))
        application.add_handler(CallbackQueryHandler(self.on_button))
        application.add_error_handler(self.on_error)


def build_application(token: str, bot: RaseedBot) -> Application:  # type: ignore[type-arg]
    """Assemble a ready-to-run application."""
    application = ApplicationBuilder().token(token).build()
    bot.register(application)
    return application


__all__ = [
    "ACTION_ACCEPT_GAP",
    "ACTION_CALENDAR",
    "ACTION_CONFIRM",
    "ACTION_DATE",
    "ACTION_DISCARD",
    "ACTION_EDIT",
    "ACTION_MERCHANT",
    "ACTION_NOOP",
    "GREETING",
    "PHOTO_MIME",
    "WEEKDAYS",
    "RaseedBot",
    "build_application",
    "calendar_keyboard",
    "date_keyboard",
    "keyboard_for",
]
