"""Per-message state for receipts awaiting confirmation.

Brief section 16.4 names the bug this exists to prevent. Send three receipts in
quick succession and there are three pending confirmations at once. A single
global "pending receipt" variable produces the classic failure where confirming
the third one commits the first.

So state is keyed by `(chat_id, message_id)` and that key travels in the inline
keyboard's callback data. Telegram caps callback data at 64 bytes, which the key
format below stays comfortably inside.

Entries expire after 24 hours. An expired entry is not silently dropped: the
image it points at has to be deleted too, which is why `expire` returns what it
removed rather than just forgetting.

There are two implementations behind one Protocol.

`InMemoryPendingStore` is a dict. It is what the tests use, and it was what the
bot used until 2026-08-09. Its failure mode is not theoretical: restarting the
bot invalidated every outstanding confirm button while the buttons stayed on
screen, so a receipt that had already been read and paid for could only be
resent and paid for again. That happened, and it cost three real receipts.

`DatabasePendingStore` is the one the bot runs now. It stores almost nothing,
because almost nothing needs storing: `raw_extractions` already holds the
model's response verbatim and is immutable, and the reconciliation and MRP
checks are pure functions of it. So a row carries the decisions a human made
plus a pointer, and everything else is recomputed on read for free.

**Neither one writes a Telegram identifier to disk.** The database store keys on
an HMAC of `(chat_id, message_id)` under the same secret `identity.py` uses. The
key always arrives in the callback data, so hashing costs nothing at lookup
time, and a stolen database still cannot be turned back into a list of accounts.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from raseed.db.models import DateSource, PendingReceiptRow, RawExtraction
from raseed.extraction.schemas import ExtractionResult
from raseed.validation.reconcile import MrpCrossCheck, Reconciliation, cross_check_mrp, reconcile

#: Brief 16.4. Long enough to survive a night, short enough to bound the store.
DEFAULT_TTL: Final[dt.timedelta] = dt.timedelta(hours=24)

#: Telegram's hard limit on `callback_data`.
CALLBACK_DATA_LIMIT: Final[int] = 64

_SEPARATOR: Final[str] = "|"


class CallbackDataTooLongError(ValueError):
    """Raised when an encoded action would exceed Telegram's 64 byte cap."""


@dataclass(frozen=True, slots=True)
class PendingKey:
    """Identifies one awaiting confirmation. Brief 16.4."""

    chat_id: int
    message_id: int

    def encode(self, action: str) -> str:
        """Pack into callback data.

        Raises:
            CallbackDataTooLongError: The result would not fit in 64 bytes.
        """
        if _SEPARATOR in action:
            msg = f"action may not contain {_SEPARATOR!r}, got {action!r}"
            raise ValueError(msg)
        packed = f"{action}{_SEPARATOR}{self.chat_id}{_SEPARATOR}{self.message_id}"
        if len(packed.encode("utf-8")) > CALLBACK_DATA_LIMIT:
            msg = f"callback data {packed!r} exceeds {CALLBACK_DATA_LIMIT} bytes"
            raise CallbackDataTooLongError(msg)
        return packed

    @classmethod
    def decode(cls, data: str) -> tuple[str, PendingKey]:
        """Unpack callback data into an action and a key.

        Raises:
            ValueError: The data is not something this bot produced.
        """
        parts = data.split(_SEPARATOR)
        if len(parts) != 3:
            msg = f"unrecognised callback data: {data!r}"
            raise ValueError(msg)
        action, chat_id, message_id = parts
        try:
            return action, cls(chat_id=int(chat_id), message_id=int(message_id))
        except ValueError as exc:
            msg = f"unrecognised callback data: {data!r}"
            raise ValueError(msg) from exc


@dataclass(slots=True)
class PendingReceipt:
    """One extraction shown to the user and awaiting a decision.

    Nothing here is in the ledger yet. `raw_extraction_id` is, because what the
    model said is recorded the moment it says it.
    """

    key: PendingKey
    user_id: str
    raw_extraction_id: str
    extraction: ExtractionResult
    reconciliation: Reconciliation
    mrp: MrpCrossCheck
    occurred_on_local: dt.date
    created_at: dt.datetime

    #: Kept until confirm or discard, then deleted. Invariant 7.
    image_path: Path | None = None

    #: Set when the user picks one from the quick-pick keyboard. Brief 24.4.
    merchant_slug: str | None = None

    #: True once the user has accepted a Class 2 gap rather than retaking.
    gap_accepted: bool = False

    #: Where `occurred_on_local` came from, so a rebuilt receipt still knows
    #: whether the date was printed or guessed. Brief 24.4.
    date_source: DateSource = DateSource.RECEIPT_PRINTED

    def expired_at(self, ttl: dt.timedelta = DEFAULT_TTL) -> dt.datetime:
        return self.created_at + ttl


class PendingStore(Protocol):
    """What a flow needs from a pending store, whichever one it is given.

    Every method takes the caller's `session` and none of them commit. A store
    that opened its own connection instead deadlocked against the transaction
    it was called from: SQLite permits one writer, the caller already held that
    write lock, and the store then waited on a lock only the caller could
    release. The in-memory store ignores the argument.
    """

    def put(self, session: Session, receipt: PendingReceipt) -> None: ...

    def get(
        self, session: Session, key: PendingKey, *, now: dt.datetime
    ) -> PendingReceipt | None: ...

    def pop(
        self, session: Session, key: PendingKey, *, now: dt.datetime
    ) -> PendingReceipt | None: ...

    def expire(self, session: Session, *, now: dt.datetime) -> list[PendingReceipt]: ...

    def count(self, session: Session) -> int: ...


@dataclass
class InMemoryPendingStore:
    """Receipts awaiting confirmation, keyed so they cannot be confused.

    Loses everything on restart. Kept because it is exactly what a test wants
    and nothing in it can fail for reasons unrelated to the test.
    """

    ttl: dt.timedelta = DEFAULT_TTL
    _entries: dict[PendingKey, PendingReceipt] = field(default_factory=dict)

    def put(self, session: Session, receipt: PendingReceipt) -> None:
        del session  # No database, nothing to enlist in.
        self._entries[receipt.key] = receipt

    def get(self, session: Session, key: PendingKey, *, now: dt.datetime) -> PendingReceipt | None:
        """Fetch a live entry. An expired one is treated as absent."""
        del session
        receipt = self._entries.get(key)
        if receipt is None:
            return None
        if now >= receipt.expired_at(self.ttl):
            return None
        return receipt

    def pop(self, session: Session, key: PendingKey, *, now: dt.datetime) -> PendingReceipt | None:
        """Fetch and remove a live entry, so a decision cannot be applied twice."""
        receipt = self.get(session, key, now=now)
        if receipt is not None:
            del self._entries[key]
        return receipt

    def expire(self, session: Session, *, now: dt.datetime) -> list[PendingReceipt]:
        """Drop everything past its TTL and return it.

        Returned rather than discarded because each one may own an image on
        disk, and invariant 7 does not permit leaving those behind.
        """
        del session
        dead = [
            receipt for receipt in self._entries.values() if now >= receipt.expired_at(self.ttl)
        ]
        for receipt in dead:
            del self._entries[receipt.key]
        return dead

    def count(self, session: Session) -> int:
        del session
        return len(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


def key_hash(key: PendingKey, *, secret: str) -> str:
    """A lookup token that cannot be turned back into a Telegram chat.

    The same construction as `identity.user_id_for`, and for the same reason:
    the bot always holds the real key when it needs to look a row up, so there
    is nothing to gain by storing it and a mapping table to lose by doing so.
    """
    message = f"pending:{key.chat_id}:{key.message_id}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


@dataclass
class DatabasePendingStore:
    """Pending receipts that survive a restart.

    Stores the human's decisions and a pointer to the immutable extraction, and
    recomputes the rest.

    Takes the caller's session on every call and never commits. The first cut
    held a `session_factory` and opened its own session per call, which read
    fine and deadlocked in production on the very first receipt: `submit_image`
    writes `raw_extractions` and so holds SQLite's single write lock, and the
    `put` that followed sat waiting for a lock that only the caller's own
    commit could release. Sharing the caller's transaction also makes the
    pending row atomic with the extraction it points at, which is what it
    should always have been.
    """

    secret: str
    ttl: dt.timedelta = DEFAULT_TTL

    def _live(self, session: Session, token: str, *, now: dt.datetime) -> PendingReceiptRow | None:
        row = session.scalars(
            select(PendingReceiptRow)
            .where(
                PendingReceiptRow.key_hash == token,
                PendingReceiptRow.deleted_at.is_(None),
            )
            .limit(1)
        ).first()
        if row is None:
            return None
        if now >= _aware(row.created_at) + self.ttl:
            return None
        return row

    def _rebuild(self, session: Session, row: PendingReceiptRow, key: PendingKey) -> PendingReceipt:
        """Reconstitute from the immutable extraction plus the stored decisions.

        The key is the caller's, never the database's: only an HMAC of it was
        ever written down.
        """
        raw = session.get(RawExtraction, row.raw_extraction_id)
        if raw is None:  # pragma: no cover - a foreign key makes this unreachable
            msg = f"pending row {row.id} points at a missing extraction"
            raise LookupError(msg)

        extraction = ExtractionResult.model_validate(json.loads(raw.response_json))
        return PendingReceipt(
            key=key,
            user_id=row.user_id,
            raw_extraction_id=row.raw_extraction_id,
            extraction=extraction,
            reconciliation=reconcile(extraction),
            mrp=cross_check_mrp(extraction),
            occurred_on_local=row.occurred_on_local,
            created_at=_aware(row.created_at),
            image_path=Path(row.image_path) if row.image_path else None,
            merchant_slug=row.merchant_slug,
            gap_accepted=row.gap_accepted,
            date_source=row.date_source,
        )

    def put(self, session: Session, receipt: PendingReceipt) -> None:
        """Write or overwrite. Overwrite matters: `accept_gap` and `set_merchant`
        both re-put the same key after changing a decision.

        Flushed, not committed. The caller owns the transaction boundary.
        """
        token = key_hash(receipt.key, secret=self.secret)
        row = session.scalars(
            select(PendingReceiptRow).where(PendingReceiptRow.key_hash == token).limit(1)
        ).first()
        if row is None:
            row = PendingReceiptRow(
                user_id=receipt.user_id,
                key_hash=token,
                raw_extraction_id=receipt.raw_extraction_id,
                occurred_on_local=receipt.occurred_on_local,
                date_source=receipt.date_source,
                # The flow's injected clock, never the database's. A row
                # stamped by `func.now()` expires against the wall clock
                # while everything else reasons about the injected one, so
                # the TTL would hold or not hold depending on the time of
                # day. That already happened once, to a cost-cap test.
                created_at=receipt.created_at,
            )
            session.add(row)
        row.image_path = str(receipt.image_path) if receipt.image_path else None
        row.merchant_slug = receipt.merchant_slug
        row.gap_accepted = receipt.gap_accepted
        row.occurred_on_local = receipt.occurred_on_local
        row.date_source = receipt.date_source
        row.deleted_at = None
        session.flush()

    def get(self, session: Session, key: PendingKey, *, now: dt.datetime) -> PendingReceipt | None:
        token = key_hash(key, secret=self.secret)
        row = self._live(session, token, now=now)
        if row is None:
            return None
        return self._rebuild(session, row, key)

    def pop(self, session: Session, key: PendingKey, *, now: dt.datetime) -> PendingReceipt | None:
        """Fetch and retire, so a decision cannot be applied twice."""
        token = key_hash(key, secret=self.secret)
        row = self._live(session, token, now=now)
        if row is None:
            return None
        receipt = self._rebuild(session, row, key)
        row.deleted_at = now
        session.flush()
        return receipt

    def expire(self, session: Session, *, now: dt.datetime) -> list[PendingReceipt]:
        """Retire everything past its TTL and return enough to clean up after it.

        The returned receipts carry a **placeholder key**, because the real one
        was never stored. `flow.expire_stale` only reads `image_path`, which is
        the one thing expiry has to act on: invariant 7 does not permit leaving
        images behind.
        """
        cutoff = now - self.ttl
        dead: list[PendingReceipt] = []
        rows = list(
            session.scalars(select(PendingReceiptRow).where(PendingReceiptRow.deleted_at.is_(None)))
        )
        for row in rows:
            if _aware(row.created_at) > cutoff:
                continue
            dead.append(self._rebuild(session, row, PendingKey(chat_id=0, message_id=0)))
            row.deleted_at = now
        session.flush()
        return dead

    def count(self, session: Session) -> int:
        return len(
            list(
                session.scalars(
                    select(PendingReceiptRow.id).where(PendingReceiptRow.deleted_at.is_(None))
                )
            )
        )


def _aware(when: dt.datetime) -> dt.datetime:
    """SQLite hands back naive datetimes. Compare in UTC or not at all."""
    return when if when.tzinfo is not None else when.replace(tzinfo=dt.UTC)


__all__ = [
    "CALLBACK_DATA_LIMIT",
    "DEFAULT_TTL",
    "CallbackDataTooLongError",
    "DatabasePendingStore",
    "InMemoryPendingStore",
    "PendingKey",
    "PendingReceipt",
    "PendingStore",
    "key_hash",
]
