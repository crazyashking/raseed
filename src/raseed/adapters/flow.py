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
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from raseed.adapters.images import ImageStore
from raseed.adapters.pending import PendingKey, PendingReceipt, PendingStore
from raseed.db import ledger, queries
from raseed.db.models import (
    UNCATEGORIZED_SLUG,
    Category,
    DateSource,
    RawExtraction,
    Source,
    Transaction,
)
from raseed.enrichment.categorize import Item as Stage2Item
from raseed.enrichment.categorize import Outcome as Stage2Outcome
from raseed.enrichment.categorize import categorize
from raseed.enrichment.providers.base import CategorizationProvider
from raseed.extraction.providers.base import (
    ExtractionProvider,
    ExtractionRequest,
    ImagePayload,
    ProviderBlockedError,
    ProviderError,
    ProviderResponseError,
    ProviderTransientError,
)
from raseed.money import rupees
from raseed.timezones import zone
from raseed.validation.reconcile import (
    DEFAULT_TOLERANCE_MINOR,
    Outcome,
    cross_check_mrp,
    reconcile,
)

log = logging.getLogger(__name__)

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

#: What a button says when the state behind it is gone.
#:
#: Two things leave a live-looking button with nothing behind it: the 24 hour
#: TTL in brief 16.4 running out, or the receipt having already been confirmed
#: or discarded. A restart was the third and by far the most common, until
#: `DatabasePendingStore` moved the state to disk on 2026-08-09. The wording
#: kept naming a restart for two days after that stopped being true, which is
#: the bot telling a person something false about its own behaviour.
#:
#: "That one is no longer waiting" was the version before that: true, and
#: useless, because it reads like a malfunction and says nothing about whether
#: money was spent or a row was written.
#:
#: It says nothing about *how* to send a receipt. Invariant 10.
EXPIRED_MESSAGE: Final[str] = (
    "I do not have that one waiting any more. A confirmation is good for 24 "
    "hours, and this one either ran out or was already answered. Nothing new "
    "was saved."
)


def already_logged_message(logged_on: dt.date, *, charged: bool) -> str:
    """What a person is told when the receipt they sent is already in the ledger.

    Resending is what someone does when they are not sure the first one landed,
    so the answer is the date it landed on, the fact that it was not counted
    twice, and somewhere to go and look.

    `charged` separates the two paths, because the difference is real money.
    A duplicate caught at submit is caught before extraction runs and costs
    nothing. A duplicate caught at confirm has already paid for Stage 1, and
    saying otherwise would be a lie told to make a message friendlier.

    Says nothing about *how* to send a receipt. Invariant 10.
    """
    free = "" if charged else " Nothing was charged for it."
    return (
        f"You have already logged this one, on {logged_on.strftime('%d %b %Y')}."
        f"{free} I have not counted it twice, so your totals are unchanged. It is "
        "on your dashboard, and I am ready for the next receipt whenever you are."
    )


def extraction_failed_message(exc: Exception, reference: str) -> str:
    """What a person is told when extraction fails, and why it is not `str(exc)`.

    Until 2026-08-11 the provider's own exception text went straight into the
    chat. That text is written by a third party and bounded by nothing: a URL,
    a request id, sometimes a paragraph of JSON. Showing it to whoever happens
    to be using the bot is output nobody reviewed, and open signup makes that
    everybody.

    So the log keeps the whole exception and a person gets two things: the part
    they can act on, and a reference that ties their report to the log line.
    The reference is the image digest, which is already computed, already
    stable, and reveals nothing once the image itself is deleted.

    Says nothing about *how* to send a receipt. Invariant 10.
    """
    if isinstance(exc, ProviderTransientError):
        cause = "I could not reach the service that reads receipts, which is usually temporary."
    elif isinstance(exc, ProviderBlockedError):
        cause = "The service that reads receipts refused to process that image."
    elif isinstance(exc, ProviderResponseError):
        cause = "The service that reads receipts answered with something I could not use."
    else:
        cause = "Something broke on my end."
    return (
        f"{cause} I still have your receipt, so nothing is lost. Quote reference "
        f"{reference} and I can tell you exactly what happened."
    )


class Step(Enum):
    """Where a receipt ended up."""

    #: This user's daily API budget is spent. Nothing was downloaded or paid for.
    LIMIT_REACHED = "limit_reached"

    #: The budget for EVERYBODY is spent. Told apart from `LIMIT_REACHED` because
    #: the user did nothing wrong and "you have hit your limit" would be a lie.
    GLOBAL_LIMIT_REACHED = "global_limit_reached"

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
    #: Across every user. Checked first, because a per-user cap says nothing
    #: about the total bill once there is more than one user.
    global_daily_cost_limit_micros: int = 2_000_000
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

    # ISO 8601 first. The schema asks for the date "exactly as printed", but the
    # field is named `order_datetime_local` and models normalise it to ISO
    # anyway: the first live receipt came back as `2026-07-30T12:44:00`, which
    # every format below rejects. That silently dated a July receipt to August,
    # and invariant 6 buckets period queries on exactly this value.
    try:
        return dt.datetime.fromisoformat(candidate).date()
    except ValueError:
        pass

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
        categorizer: CategorizationProvider | None = None,
    ) -> None:
        self._provider = provider
        self._images = images
        self._pending = pending
        self._config = config
        self._clock = clock
        self._categorizer = categorizer

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
                message=already_logged_message(existing.occurred_on_local, charged=False),
                duplicate_of=existing,
            )

        request = ExtractionRequest(images=(ImagePayload(data=data, mime_type=mime_type),))
        try:
            result = self._provider.extract(request)
        except Exception as exc:
            # The image stays on disk either way. Brief 16.5: the receipt must
            # not be lost.
            #
            # `Exception` rather than `ProviderError`, because a provider that
            # raises outside its own taxonomy is a provider bug and a receipt is
            # the wrong thing to spend on one. That is not hypothetical: a DNS
            # failure inside the Gemini SDK arrives as an `httpx` error, which
            # is not an `APIError` and so was never translated at all.
            #
            # Logged in full, including the cases the taxonomy does cover, and
            # tagged with a reference. That reference is the only thing tying
            # "it did not work" from a person to the line in this log that says
            # what actually happened.
            reference = stored.sha256[:8]
            log.warning(
                "extraction failed, reference=%s, kind=%s",
                reference,
                type(exc).__name__ if isinstance(exc, ProviderError) else "outside-taxonomy",
                exc_info=True,
            )
            return FlowResult(
                step=Step.EXTRACTION_FAILED,
                message=extraction_failed_message(exc, reference),
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
            # Decided here, where `printed` is already known, and carried to
            # confirm. It used to be left at its default and re-derived later,
            # which meant `pending_receipts.date_source` said RECEIPT_PRINTED on
            # every row including the guessed ones. Nothing read it, so nothing
            # broke, and a stored column that is always the same wrong value is
            # a trap for whoever reads it next. Brief 24.4.
            date_source=(DateSource.RECEIPT_PRINTED if printed else DateSource.MESSAGE_TIMESTAMP),
        )
        self._pending.put(session, receipt)

        return FlowResult(
            step=Step.AWAITING_CONFIRMATION,
            message=summarise(receipt),
            pending=receipt,
        )

    def _over_budget(self, session: Session, *, user_id: str, now: dt.datetime) -> bool:
        """Whether either rolling 24 hour API budget is already spent. Brief 16.6."""
        since = now - dt.timedelta(days=1)
        if queries.global_spend_micros_since(session, since=since) >= (
            self._config.global_daily_cost_limit_micros
        ):
            return True
        spent = queries.spend_micros_since(session, user_id=user_id, since=since)
        return spent >= self._config.daily_cost_limit_micros

    def _check_budget(
        self, session: Session, *, user_id: str, now: dt.datetime
    ) -> FlowResult | None:
        """Brief 16.6. Refuse before spending, not after.

        The global cap is checked first and reported differently. A user who has
        read two receipts today has not hit *their* limit, and telling them they
        have would be a lie about their own account.
        """
        since = now - dt.timedelta(days=1)

        everyone = queries.global_spend_micros_since(session, since=since)
        if everyone >= self._config.global_daily_cost_limit_micros:
            log.warning(
                "global daily cap reached: %d of %d micro-dollars spent",
                everyone,
                self._config.global_daily_cost_limit_micros,
            )
            return FlowResult(
                step=Step.GLOBAL_LIMIT_REACHED,
                message=(
                    "Raseed has hit its total reading budget for today, so I did "
                    "not read that one. Nothing is wrong on your side. It resets "
                    "on a rolling 24 hour window."
                ),
            )

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

    def _categorize(
        self, session: Session, *, receipt: PendingReceipt, now: dt.datetime
    ) -> Stage2Outcome:
        """Run Stage 2 over the confirmed receipt's line items.

        On confirm rather than on submit, so a receipt the user discards costs
        nothing to categorize.

        The model fallback is dropped when the daily budget is already gone: the
        lexicon still runs, unmatched items land in `uncategorized`, and they are
        re-runnable later for free from `raw_extractions`. Being over budget must
        never cost the user the receipt.
        """
        allowed = tuple(
            session.scalars(
                select(Category.slug).where(
                    Category.user_id == receipt.user_id, Category.deleted_at.is_(None)
                )
            ).all()
        )
        if UNCATEGORIZED_SLUG not in allowed:
            # Seeding has not run for this user. Nothing to categorize into, so
            # Stage 2 is skipped entirely rather than half applied.
            return Stage2Outcome(decisions=())

        provider = self._categorizer
        if provider is not None and self._over_budget(session, user_id=receipt.user_id, now=now):
            provider = None

        return categorize(
            [
                Stage2Item(raw_name=item.raw_name, quantity_text=item.quantity_text)
                for item in receipt.extraction.line_items
            ],
            allowed=allowed,
            provider=provider,
        )

    # -- the user's decision -------------------------------------------------

    def confirm(self, session: Session, key: PendingKey) -> FlowResult:
        """Commit a pending receipt and delete its image. Invariant 7.

        The pending entry is **read, not consumed**, and is only removed once the
        row is actually in the ledger. Popping first looks tidier and is wrong:
        anything that fails afterwards, a duplicate, a dropped connection during
        Stage 2, a bug, leaves the user holding a button that can never work
        again, with the receipt gone and the extraction already paid for. Every
        failure below therefore leaves the entry in place so the same button
        works on the next tap.
        """
        now = self._clock()
        receipt = self._pending.get(session, key, now=now)
        if receipt is None:
            return FlowResult(step=Step.EXPIRED, message=EXPIRED_MESSAGE)

        raw = session.get(RawExtraction, receipt.raw_extraction_id)
        if raw is None:
            self._pending.pop(session, key, now=now)
            return FlowResult(
                step=Step.EXPIRED, message="I lost track of that reading. Send it again."
            )

        # Before Stage 2, which is where the money is. Submitting checks this
        # too, but the receipt can be logged in between: send the same image
        # twice, confirm one, then confirm the other. Without this the second
        # tap reaches the unique index and surfaces as a crash rather than as
        # the plain fact that it is already logged.
        if raw.image_sha256:
            already = queries.find_by_image_hash(
                session, user_id=receipt.user_id, image_sha256=raw.image_sha256
            )
            if already is not None:
                self._pending.pop(session, key, now=now)
                self._images.delete(receipt.image_path)
                return FlowResult(
                    step=Step.DUPLICATE,
                    message=already_logged_message(already.occurred_on_local, charged=True),
                    duplicate_of=already,
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

        stage2 = self._categorize(session, receipt=receipt, now=now)
        if stage2.provider_result is not None:
            ledger.record_categorization(
                session,
                user_id=receipt.user_id,
                result=stage2.provider_result,
                source=raw.source,
                image_sha256=raw.image_sha256,
            )

        try:
            transaction = ledger.record_transaction(
                session,
                raw=raw,
                reconciliation=receipt.reconciliation,
                occurred_on_local=receipt.occurred_on_local,
                date_source=receipt.date_source,
                merchant=merchant,
                enrichment=stage2,
            )
        except IntegrityError:
            # The pre-check above closes the ordinary path, so reaching here
            # means two confirms of the same image raced. Rolling back also
            # discards the Stage 2 billing row written moments ago, which is the
            # one thing here worth regretting: brief 16.6's cap cannot see spend
            # that was rolled back. It is bounded to this race and to a single
            # cheap call, and the alternative is a half-written ledger.
            session.rollback()
            log.exception("confirm hit the duplicate index for %s", key)
            self._pending.pop(session, key, now=now)
            self._images.delete(receipt.image_path)
            return FlowResult(
                step=Step.DUPLICATE,
                message=(
                    "That one is already in your ledger. I have not counted it twice, so "
                    "your totals are unchanged, and it is on your dashboard."
                ),
            )

        # Only now. Everything above can fail and leave the button usable.
        self._pending.pop(session, key, now=now)
        self._images.delete(receipt.image_path)
        return FlowResult(
            step=Step.STORED,
            message=f"Logged {rupees(transaction.grand_total_minor)}.",
            transaction=transaction,
        )

    def discard(self, session: Session, key: PendingKey) -> FlowResult:
        """Throw a pending receipt away, image and all."""
        receipt = self._pending.pop(session, key, now=self._clock())
        if receipt is None:
            return FlowResult(step=Step.EXPIRED, message=EXPIRED_MESSAGE)
        self._images.delete(receipt.image_path)
        return FlowResult(step=Step.DISCARDED, message="Discarded. Nothing was saved.")

    def accept_gap(self, session: Session, key: PendingKey) -> FlowResult:
        """Accept a Class 2 mismatch, to be logged as an unaccounted adjustment."""
        now = self._clock()
        receipt = self._pending.get(session, key, now=now)
        if receipt is None:
            return FlowResult(step=Step.EXPIRED, message=EXPIRED_MESSAGE)
        receipt.gap_accepted = True
        # Written back explicitly. The in-memory store hands out the very object
        # it holds, so mutating it was enough; a persistent store hands out a
        # copy, and relying on aliasing would silently lose the user's answer
        # the next time the bot restarted.
        self._pending.put(session, receipt)
        return FlowResult(
            step=Step.AWAITING_CONFIRMATION, message=summarise(receipt), pending=receipt
        )

    def set_merchant(self, session: Session, key: PendingKey, slug: str) -> FlowResult:
        """Attach a merchant picked from the quick-pick keyboard. Brief 24.4."""
        now = self._clock()
        receipt = self._pending.get(session, key, now=now)
        if receipt is None:
            return FlowResult(step=Step.EXPIRED, message=EXPIRED_MESSAGE)
        receipt.merchant_slug = slug
        self._pending.put(session, receipt)  # See `accept_gap`: aliasing is not portable.
        return FlowResult(
            step=Step.AWAITING_CONFIRMATION, message=summarise(receipt), pending=receipt
        )

    def pending_for(self, session: Session, key: PendingKey) -> PendingReceipt | None:
        """Look at a waiting receipt without deciding anything about it.

        The transport needs this to re-render a keyboard, and a read must not
        consume the entry the way `confirm` and `discard` do.
        """
        return self._pending.get(session, key, now=self._clock())

    def expire_stale(self, session: Session) -> list[PendingReceipt]:
        """Drop timed-out receipts and delete their images. Invariant 7."""
        dead = self._pending.expire(session, now=self._clock())
        for receipt in dead:
            self._images.delete(receipt.image_path)
        return dead


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


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
    "EXPIRED_MESSAGE",
    "FlowConfig",
    "FlowResult",
    "ReceiptFlow",
    "Step",
    "already_logged_message",
    "extraction_failed_message",
    "parse_printed_date",
    "rupees",
    "summarise",
]
