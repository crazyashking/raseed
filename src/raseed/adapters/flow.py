"""What happens to a receipt, independent of how it arrived.

Telegram is one transport. WhatsApp is meant to be another later, and the tests
are a third. So the decisions live here and `telegram.py` only translates them
into messages and buttons.

The order of the checks is the design. Cheapest and most protective first:

1. **Daily cost cap.** Before anything is downloaded or paid for. Brief 16.6.
2. **Dedupe on the image hash.** Free, and catches the common accidental resend.
   Brief 3.6 as revised by 24.4.
3. **Extraction.** The only step that spends money.
4. **`is_receipt`.** Checked before a single line item is looked at, and a false
   stores nothing. Invariant 11.
5. **The reconciliation gate.** Class 1 stores nothing; Class 2 asks the user.
6. **The user.** Nothing commits without a confirmation. Brief 3.4.

Images are deleted the moment a receipt reaches a terminal state, whichever one
it is. Invariant 7.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Final

from sqlalchemy.orm import Session

from raseed.adapters.images import ImageStore
from raseed.adapters.pending import PendingKey, PendingReceipt, PendingStore
from raseed.db import ledger, queries
from raseed.db.models import DateSource, RawExtraction, Source, Transaction
from raseed.extraction.providers.base import (
    ExtractionProvider,
    ExtractionRequest,
    ImagePayload,
    ProviderError,
)
from raseed.timezones import zone
from raseed.validation.reconcile import (
    DEFAULT_TOLERANCE_MINOR,
    Outcome,
    cross_check_mrp,
    reconcile,
)

#: Formats tried when a receipt prints a date. Anything else falls back to the
#: message timestamp, and records that it did. Brief 24.4.
DATE_FORMATS: Final[tuple[str, ...]] = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d %b %Y, %I:%M %p",
    "%d %b %Y",
    "%d %B %Y",
)


class Step(Enum):
    """Where a receipt ended up."""

    #: The daily API budget is spent. Nothing was downloaded or paid for.
    LIMIT_REACHED = "limit_reached"

    #: These exact image bytes are already in the ledger.
    DUPLICATE = "duplicate"

    #: The provider failed after its retries. The image is KEPT so the receipt
    #: is not lost. Brief 16.5.
    EXTRACTION_FAILED = "extraction_failed"

    #: The model says this is not a receipt. Nothing stored. Invariant 11.
    NOT_A_RECEIPT = "not_a_receipt"

    #: Class 1. Extraction was too incomplete to reconcile. Nothing stored.
    REJECTED = "rejected"

    #: Waiting on the user. Nothing is in the ledger yet.
    AWAITING_CONFIRMATION = "awaiting_confirmation"

    #: Written to the ledger, image deleted.
    STORED = "stored"

    #: The user threw it away.
    DISCARDED = "discarded"

    #: The pending entry aged out or was already answered.
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class FlowResult:
    """What happened, and everything the transport needs to say so."""

    step: Step
    message: str
    pending: PendingReceipt | None = None
    transaction: Transaction | None = None
    duplicate_of: Transaction | None = None


@dataclass(frozen=True, slots=True)
class FlowConfig:
    """Knobs, all of which come from the environment."""

    daily_cost_limit_micros: int = 1_000_000
    default_timezone: str = "Asia/Kolkata"
    tolerance_minor: int = DEFAULT_TOLERANCE_MINOR
    min_confidence: float | None = None


def parse_printed_date(text: str | None, *, fallback_tz: str) -> dt.date | None:
    """Read a printed date, or admit that it could not be read.

    Returns None rather than guessing. Brief 24.4 is explicit: never silently
    guess a date without recording that you guessed, and the way this function
    records it is by refusing to invent one.
    """
    if not text:
        return None
    candidate = text.strip()
    for fmt in DATE_FORMATS:
        try:
            parsed = dt.datetime.strptime(candidate, fmt).replace(tzinfo=zone(fallback_tz))
        except ValueError:
            continue
        return parsed.date()
    return None


class ReceiptFlow:
    """Turns an inbound image into a ledger row, with the user in the loop."""

    def __init__(
        self,
        *,
        provider: ExtractionProvider,
        images: ImageStore,
        pending: PendingStore,
        config: FlowConfig,
        clock: Callable[[], dt.datetime],
    ) -> None:
        self._provider = provider
        self._images = images
        self._pending = pending
        self._config = config
        self._clock = clock

    # -- submission ----------------------------------------------------------

    def submit_image(
        self,
        session: Session,
        *,
        user_id: str,
        chat_id: int,
        message_id: int,
        data: bytes,
        mime_type: str,
        message_date: dt.datetime,
        source: Source = Source.TELEGRAM_IMAGE,
    ) -> FlowResult:
        """Run an inbound image through the pipeline up to the confirm prompt."""
        now = self._clock()

        capped = self._check_budget(session, user_id=user_id, now=now)
        if capped is not None:
            return capped

        stored = self._images.save(data, mime_type=mime_type)

        existing = queries.find_by_image_hash(session, user_id=user_id, image_sha256=stored.sha256)
        if existing is not None:
            self._images.delete(stored.path)
            return FlowResult(
                step=Step.DUPLICATE,
                message=f"Already logged on {existing.occurred_on_local.isoformat()}.",
                duplicate_of=existing,
            )

        request = ExtractionRequest(images=(ImagePayload(data=data, mime_type=mime_type),))
        try:
            result = self._provider.extract(request)
        except ProviderError as exc:
            # The image stays on disk. Brief 16.5: the receipt must not be lost.
            return FlowResult(
                step=Step.EXTRACTION_FAILED,
                message=f"I could not read that one right now. {exc}",
            )

        raw = ledger.record_extraction(
            session,
            user_id=user_id,
            result=result,
            source=source,
            image_sha256=stored.sha256,
        )

        extraction = result.extraction

        # Invariant 11: checked before a single line item is looked at.
        if not extraction.is_storable:
            self._images.delete(stored.path)
            reason = extraction.rejection_reason or "That does not look like a receipt."
            return FlowResult(step=Step.NOT_A_RECEIPT, message=reason)

        verdict = reconcile(
            extraction,
            tolerance_minor=self._config.tolerance_minor,
            min_confidence=self._config.min_confidence,
        )
        mrp = cross_check_mrp(extraction, tolerance_minor=self._config.tolerance_minor)

        if verdict.outcome is Outcome.CLASS_1:
            self._images.delete(stored.path)
            return FlowResult(step=Step.REJECTED, message=verdict.reason)

        printed = parse_printed_date(
            extraction.order_datetime_local, fallback_tz=self._config.default_timezone
        )
        occurred_on_local = (
            printed or message_date.astimezone(zone(self._config.default_timezone)).date()
        )

        receipt = PendingReceipt(
            key=PendingKey(chat_id=chat_id, message_id=message_id),
            user_id=user_id,
            raw_extraction_id=raw.id,
            extraction=extraction,
            reconciliation=verdict,
            mrp=mrp,
            occurred_on_local=occurred_on_local,
            created_at=now,
            image_path=stored.path,
        )
        self._pending.put(receipt)

        return FlowResult(
            step=Step.AWAITING_CONFIRMATION,
            message=summarise(receipt),
            pending=receipt,
        )

    def _check_budget(
        self, session: Session, *, user_id: str, now: dt.datetime
    ) -> FlowResult | None:
        """Brief 16.6. Refuse before spending, not after."""
        since = now - dt.timedelta(days=1)
        spent = queries.spend_micros_since(session, user_id=user_id, since=since)
        if spent < self._config.daily_cost_limit_micros:
            return None
        return FlowResult(
            step=Step.LIMIT_REACHED,
            message=(
                "I have hit the daily reading budget, so I did not read that one. "
                "It resets on a rolling 24 hour window."
            ),
        )

    # -- the user's decision -------------------------------------------------

    def confirm(self, session: Session, key: PendingKey) -> FlowResult:
        """Commit a pending receipt and delete its image. Invariant 7."""
        now = self._clock()
        receipt = self._pending.pop(key, now=now)
        if receipt is None:
            return FlowResult(step=Step.EXPIRED, message="That one is no longer waiting.")

        raw = session.get(RawExtraction, receipt.raw_extraction_id)
        if raw is None:
            return FlowResult(
                step=Step.EXPIRED, message="I lost track of that reading. Send it again."
            )

        merchant = None
        if receipt.merchant_slug:
            merchant = ledger.ensure_merchant(
                session,
                user_id=receipt.user_id,
                slug=receipt.merchant_slug,
                display_name=receipt.merchant_slug.replace("-", " ").title(),
                merchant_tz=self._config.default_timezone,
            )

        printed = parse_printed_date(
            receipt.extraction.order_datetime_local, fallback_tz=self._config.default_timezone
        )
        transaction = ledger.record_transaction(
            session,
            user_id=receipt.user_id,
            raw=raw,
            reconciliation=receipt.reconciliation,
            occurred_on_local=receipt.occurred_on_local,
            date_source=(DateSource.RECEIPT_PRINTED if printed else DateSource.MESSAGE_TIMESTAMP),
            merchant=merchant,
        )

        self._images.delete(receipt.image_path)
        return FlowResult(
            step=Step.STORED,
            message=f"Logged {rupees(transaction.grand_total_minor)}.",
            transaction=transaction,
        )

    def discard(self, key: PendingKey) -> FlowResult:
        """Throw a pending receipt away, image and all."""
        receipt = self._pending.pop(key, now=self._clock())
        if receipt is None:
            return FlowResult(step=Step.EXPIRED, message="That one is no longer waiting.")
        self._images.delete(receipt.image_path)
        return FlowResult(step=Step.DISCARDED, message="Discarded. Nothing was saved.")

    def accept_gap(self, key: PendingKey) -> FlowResult:
        """Accept a Class 2 mismatch, to be logged as an unaccounted adjustment."""
        now = self._clock()
        receipt = self._pending.get(key, now=now)
        if receipt is None:
            return FlowResult(step=Step.EXPIRED, message="That one is no longer waiting.")
        receipt.gap_accepted = True
        return FlowResult(
            step=Step.AWAITING_CONFIRMATION, message=summarise(receipt), pending=receipt
        )

    def set_merchant(self, key: PendingKey, slug: str) -> FlowResult:
        """Attach a merchant picked from the quick-pick keyboard. Brief 24.4."""
        now = self._clock()
        receipt = self._pending.get(key, now=now)
        if receipt is None:
            return FlowResult(step=Step.EXPIRED, message="That one is no longer waiting.")
        receipt.merchant_slug = slug
        return FlowResult(
            step=Step.AWAITING_CONFIRMATION, message=summarise(receipt), pending=receipt
        )

    def pending_for(self, key: PendingKey) -> PendingReceipt | None:
        """Look at a waiting receipt without deciding anything about it.

        The transport needs this to re-render a keyboard, and a read must not
        consume the entry the way `confirm` and `discard` do.
        """
        return self._pending.get(key, now=self._clock())

    def expire_stale(self) -> list[PendingReceipt]:
        """Drop timed-out receipts and delete their images. Invariant 7."""
        dead = self._pending.expire(now=self._clock())
        for receipt in dead:
            self._images.delete(receipt.image_path)
        return dead


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def rupees(minor: int) -> str:
    """Format integer paise for a human. Never builds a float."""
    sign = "-" if minor < 0 else ""
    whole, paise = divmod(abs(minor), 100)
    return f"{sign}₹{whole:,}.{paise:02d}"


def summarise(receipt: PendingReceipt) -> str:
    """The parsed summary the user confirms against. Brief 3.4."""
    extraction = receipt.extraction
    lines = [f"{receipt.occurred_on_local.isoformat()}"]

    if receipt.merchant_slug:
        lines.append(receipt.merchant_slug.replace("-", " ").title())

    for item in extraction.line_items:
        quantity = f" ({item.quantity_text})" if item.quantity_text else ""
        lines.append(f"  {item.raw_name}{quantity}  {rupees(item.line_total_minor)}")

    for charge in extraction.charges:
        lines.append(f"  + {charge.label}  {rupees(charge.amount_minor)}")
    for tax in extraction.taxes:
        lines.append(f"  + {tax.label}  {rupees(tax.amount_minor)}")
    for discount in extraction.discounts:
        lines.append(f"  - {discount.label}  {rupees(discount.amount_minor)}")

    lines.append(f"Total  {rupees(extraction.grand_total_minor or 0)}")

    verdict = receipt.reconciliation
    if verdict.outcome is Outcome.CLASS_2 and verdict.delta_minor is not None:
        lines.append("")
        lines.append(
            f"The items add up to {rupees(verdict.computed_total_minor)}, "
            f"which is {rupees(abs(verdict.delta_minor))} off the printed total."
        )
    if receipt.mrp.derived_savings_minor:
        lines.append(f"Saved {rupees(receipt.mrp.derived_savings_minor)} against MRP.")

    return "\n".join(lines)


__all__ = [
    "DATE_FORMATS",
    "FlowConfig",
    "FlowResult",
    "ReceiptFlow",
    "Step",
    "parse_printed_date",
    "rupees",
    "summarise",
]
