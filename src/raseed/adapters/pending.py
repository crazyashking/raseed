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

This store is in memory. A restart loses pending confirms, which is acceptable
because nothing has been committed to the ledger yet and the user can resend.
It is not acceptable to leak the images, so `raseed.adapters.images` sweeps.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from raseed.extraction.schemas import ExtractionResult
from raseed.validation.reconcile import MrpCrossCheck, Reconciliation

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

    def expired_at(self, ttl: dt.timedelta = DEFAULT_TTL) -> dt.datetime:
        return self.created_at + ttl


@dataclass
class PendingStore:
    """Receipts awaiting confirmation, keyed so they cannot be confused."""

    ttl: dt.timedelta = DEFAULT_TTL
    _entries: dict[PendingKey, PendingReceipt] = field(default_factory=dict)

    def put(self, receipt: PendingReceipt) -> None:
        self._entries[receipt.key] = receipt

    def get(self, key: PendingKey, *, now: dt.datetime) -> PendingReceipt | None:
        """Fetch a live entry. An expired one is treated as absent."""
        receipt = self._entries.get(key)
        if receipt is None:
            return None
        if now >= receipt.expired_at(self.ttl):
            return None
        return receipt

    def pop(self, key: PendingKey, *, now: dt.datetime) -> PendingReceipt | None:
        """Fetch and remove a live entry, so a decision cannot be applied twice."""
        receipt = self.get(key, now=now)
        if receipt is not None:
            del self._entries[key]
        return receipt

    def expire(self, *, now: dt.datetime) -> list[PendingReceipt]:
        """Drop everything past its TTL and return it.

        Returned rather than discarded because each one may own an image on
        disk, and invariant 7 does not permit leaving those behind.
        """
        dead = [
            receipt for receipt in self._entries.values() if now >= receipt.expired_at(self.ttl)
        ]
        for receipt in dead:
            del self._entries[receipt.key]
        return dead

    def __len__(self) -> int:
        return len(self._entries)


__all__ = [
    "CALLBACK_DATA_LIMIT",
    "DEFAULT_TTL",
    "CallbackDataTooLongError",
    "PendingKey",
    "PendingReceipt",
    "PendingStore",
]
