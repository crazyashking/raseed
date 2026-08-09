"""Who is asking, without ever storing who they are.

Two problems, one mechanism.

**Problem one: multiple users need separate ledgers.** Every table already
carries `user_id` (invariant 8), so the ledger was ready for this. What was
missing was a way to turn "Telegram user 123456789" into a `user_id`.

The obvious answer is a column mapping one to the other. That answer is not
available: invariant 3 says no PII fields in any schema, `users` is asserted to
hold exactly `id`, `created_at` and `deleted_at`, and a test bans any column
name containing "telegram". A Telegram account ID is an external identifier for
a person, and putting it in a public repo's schema is exactly what invariant 3
exists to prevent.

So the ID is **derived, not stored**:

    user_id = UUID(HMAC-SHA256(USER_ID_SECRET, "telegram:<id>")[:16])

Stable, so the same person comes back to the same ledger. One way, so the
database cannot be turned back into a list of Telegram accounts by anyone who
steals it, including anyone who steals the whole repo. And no mapping table, so
there is nothing to leak in the first place.

**The cost, stated plainly: `USER_ID_SECRET` must never change.** Change it and
every user is a new user with an empty ledger, and the old rows are unreachable
because nothing anywhere records who they belonged to. That is the honest price
of not storing the identifier. `tools/claim.py` exists to move rows if it ever
has to happen.

**Problem two: the dashboard needs to know who is looking.** Telegram signs the
`initData` blob it hands a Mini App with the bot token, so a page opened from
the bot can prove which account opened it without a password, a session store or
an OAuth round trip. Verified here with stdlib `hmac`, no new dependency.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qsl

log = logging.getLogger(__name__)

#: How stale a signed `initData` may be. Telegram refreshes it whenever the Mini
#: App is opened, so an hour is generous for a dashboard someone reads for two
#: minutes, and it bounds how long a leaked blob stays useful.
MAX_AUTH_AGE_SECONDS: Final[int] = 3600

#: Telegram's fixed key derivation constant. Not a secret, part of the protocol.
_WEBAPP_KEY: Final[bytes] = b"WebAppData"

#: Namespaced so a future second transport (email, a web signup) derives into a
#: different id for the same number, instead of silently colliding with Telegram.
_TELEGRAM_NAMESPACE: Final[str] = "telegram"


class AuthError(Exception):
    """The caller did not prove who they are. Never says which check failed."""


def user_id_for(
    external_id: int | str, *, secret: str, namespace: str = _TELEGRAM_NAMESPACE
) -> str:
    """A stable, opaque `user_id` for an external account.

    Args:
        external_id: The Telegram numeric user ID. Never stored anywhere.
        secret: `USER_ID_SECRET`. Must never change. See the module docstring.
        namespace: Which kind of account this is.

    Returns:
        A UUID string, the same shape `db.models.new_id` produces, so nothing
        downstream can tell a derived ID from a generated one or comes to depend
        on the difference.
    """
    if not secret:
        msg = "USER_ID_SECRET is empty, so user IDs would not be private"
        raise ValueError(msg)

    digest = hmac.new(
        secret.encode("utf-8"),
        f"{namespace}:{external_id}".encode(),
        hashlib.sha256,
    ).digest()
    return str(uuid.UUID(bytes=digest[:16], version=5))


@dataclass(frozen=True, slots=True)
class MiniAppUser:
    """The account that opened the Mini App.

    Carries the numeric ID and a display name, and **neither is ever persisted**.
    The name is rendered into a page for the person who is already looking at
    their own dashboard, which is not storage and not a schema field. It is
    dropped the moment the response is written.
    """

    telegram_id: int
    first_name: str = ""

    def user_id(self, *, secret: str) -> str:
        return user_id_for(self.telegram_id, secret=secret)


def _check_string(fields: list[tuple[str, str]]) -> str:
    """Telegram's data-check-string: every field except `hash`, sorted, newline joined."""
    return "\n".join(f"{key}={value}" for key, value in sorted(fields) if key != "hash")


def verify_init_data(
    init_data: str,
    *,
    bot_token: str,
    now: float,
    max_age_seconds: int = MAX_AUTH_AGE_SECONDS,
) -> MiniAppUser:
    """Verify a Telegram Mini App `initData` blob and return who sent it.

    The signature is over the bot token, so a valid blob is proof that Telegram
    itself vouched for this account opening this bot's Mini App. Nothing else in
    the request is trusted.

    Args:
        init_data: The raw query-string blob from `Telegram.WebApp.initData`.
        bot_token: This bot's token. The signing key is derived from it.
        now: Current UNIX time, injected so the freshness check is testable.
        max_age_seconds: How old a blob may be.

    Raises:
        AuthError: Malformed, unsigned, wrongly signed, stale, or missing a user.
            One exception type with one message, on purpose: an error that says
            *which* check failed tells an attacker which half to work on.
    """
    if not bot_token:
        msg = "cannot verify initData without a bot token"
        raise ValueError(msg)

    try:
        fields = parse_qsl(init_data, strict_parsing=True, keep_blank_values=True)
    except ValueError as exc:
        raise AuthError("could not verify") from exc

    received = dict(fields).get("hash", "")
    if not received:
        raise AuthError("could not verify")

    secret_key = hmac.new(_WEBAPP_KEY, bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(
        secret_key, _check_string(fields).encode("utf-8"), hashlib.sha256
    ).hexdigest()

    # Constant time. A `==` here leaks the signature one byte at a time.
    if not hmac.compare_digest(expected, received):
        raise AuthError("could not verify")

    payload = dict(fields)

    try:
        auth_date = int(payload.get("auth_date", ""))
    except ValueError as exc:
        raise AuthError("could not verify") from exc

    # Both directions. A blob dated in the future is either a clock problem or
    # someone trying to mint one that never expires.
    age = now - auth_date
    if age > max_age_seconds or age < -max_age_seconds:
        raise AuthError("could not verify")

    try:
        user = json.loads(payload.get("user", ""))
        telegram_id = int(user["id"])
    except (ValueError, TypeError, KeyError) as exc:
        raise AuthError("could not verify") from exc

    first_name = user.get("first_name") or ""
    return MiniAppUser(telegram_id=telegram_id, first_name=str(first_name))


__all__ = [
    "MAX_AUTH_AGE_SECONDS",
    "AuthError",
    "MiniAppUser",
    "user_id_for",
    "verify_init_data",
]
