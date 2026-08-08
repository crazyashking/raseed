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

import datetime as dt
import logging
from collections.abc import Callable, Iterable
from typing import Final

from sqlalchemy.orm import Session, sessionmaker
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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

from raseed.adapters.flow import FlowResult, ReceiptFlow, Step, rupees
from raseed.adapters.pending import PendingKey, PendingReceipt
from raseed.db import ledger, queries
from raseed.db.seed import bootstrap
from raseed.validation.reconcile import Outcome

log = logging.getLogger(__name__)

ACTION_CONFIRM: Final[str] = "ok"
ACTION_DISCARD: Final[str] = "no"
ACTION_EDIT: Final[str] = "ed"
ACTION_ACCEPT_GAP: Final[str] = "gap"
ACTION_MERCHANT: Final[str] = "m"

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


class RaseedBot:
    """Wires the receipt flow onto python-telegram-bot.

    Args:
        flow: The transport-agnostic pipeline.
        session_factory: Opens a database session per update.
        allowed_user_ids: Telegram numeric IDs permitted to use this bot. Empty
            admits nobody, which is the safe default for a single-user bot.
        clock: Injected so tests do not depend on wall time.
    """

    def __init__(
        self,
        *,
        flow: ReceiptFlow,
        session_factory: sessionmaker[Session],
        allowed_user_ids: frozenset[int],
        clock: Callable[[], dt.datetime],
        dashboard_url: str | None = None,
    ) -> None:
        self._flow = flow
        self._sessions = session_factory
        self._allowed = allowed_user_ids
        self._clock = clock
        self._dashboard_url = dashboard_url

    # -- access control ------------------------------------------------------

    def permitted(self, update: Update) -> bool:
        """Whitelist check. An empty whitelist admits nobody."""
        user = update.effective_user
        if user is None:
            return False
        return user.id in self._allowed

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
        if document is None or not (document.mime_type or "").startswith("image/"):
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
            owner = bootstrap(session)
            result = self._flow.submit_image(
                session,
                user_id=owner.id,
                chat_id=message.chat_id,
                message_id=message.message_id,
                data=data,
                mime_type=mime_type,
                message_date=message.date,
            )
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

        with self._sessions() as session:
            owner = bootstrap(session)
            result = self.dispatch(session, action, key)
            session.commit()

            if result.step is Step.AWAITING_CONFIRMATION and result.pending is not None:
                merchants = [m.slug for m in queries.known_merchants(session, user_id=owner.id)]
                await query.edit_message_text(
                    result.message, reply_markup=keyboard_for(result.pending, merchants)
                )
            else:
                await query.edit_message_text(result.message)

    def dispatch(self, session: Session, action: str, key: PendingKey) -> FlowResult:
        if action == ACTION_CONFIRM:
            return self._flow.confirm(session, key)
        if action == ACTION_DISCARD:
            return self._flow.discard(key)
        if action == ACTION_ACCEPT_GAP:
            return self._flow.accept_gap(key)
        if action.startswith(f"{ACTION_MERCHANT}:"):
            return self._flow.set_merchant(key, action.split(":", 1)[1])
        if action == ACTION_EDIT:
            return FlowResult(
                step=Step.AWAITING_CONFIRMATION,
                message="Pick a merchant below, or confirm as is.",
            )
        log.warning("unknown callback action: %r", action)
        return FlowResult(step=Step.EXPIRED, message="I do not know what that button does.")

    async def recent(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        """Brief 16.3."""
        if not self.permitted(update) or update.message is None:
            return
        with self._sessions() as session:
            owner = bootstrap(session)
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
        """Hand over the dashboard link.

        A plain message rather than an inline button, because Telegram will not
        render a URL button for `http://127.0.0.1`: it demands https and a real
        host. That is the correct restriction and not one to work around. When
        the dashboard gets a public HTTPS address, this becomes a button and the
        authentication lands in the same change.
        """
        if not self.permitted(update) or update.message is None:
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
            owner = bootstrap(session)
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
        application.add_handler(MessageHandler(filters.Document.IMAGE, self.on_document))
        application.add_handler(CallbackQueryHandler(self.on_button))
        application.add_error_handler(self.on_error)


def build_application(token: str, bot: RaseedBot) -> Application:  # type: ignore[type-arg]
    """Assemble a ready-to-run application."""
    application = ApplicationBuilder().token(token).build()
    bot.register(application)
    return application


__all__ = [
    "ACTION_ACCEPT_GAP",
    "ACTION_CONFIRM",
    "ACTION_DISCARD",
    "ACTION_MERCHANT",
    "GREETING",
    "RaseedBot",
    "build_application",
    "keyboard_for",
]
