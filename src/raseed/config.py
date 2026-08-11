"""Process configuration, read once from the environment.

Nothing here talks to an API, a model, or a receipt. It reads `.env` if present,
falls back to real environment variables, and validates types at the boundary so
the rest of the codebase can assume `int` means `int`.

Secrets carry `repr=False` so an accidental log line or traceback cannot print a
bot token or an API key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

from raseed.enrichment.categorize import DEFAULT_CATEGORIZER_MODEL_ID
from raseed.extraction.pricing import MICROS_PER_USD
from raseed.validation.reconcile import DEFAULT_TOLERANCE_MINOR

#: Repository root, resolved from this file so it works regardless of cwd.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent

#: Re-exported so there is exactly one definition of the tolerance. It lives with
#: the gate that uses it, in `raseed.validation.reconcile`. Brief section 18.4.
DEFAULT_RECONCILIATION_TOLERANCE_MINOR: Final[int] = DEFAULT_TOLERANCE_MINOR

#: One dollar a day, per user. Brief section 16.6. Held in integer micro-dollars
#: because it is compared against summed `cost_micros_usd`, which are integers,
#: and because a budget that drifts by a rounding error is not a budget.
DEFAULT_DAILY_COST_LIMIT_MICROS: Final[int] = MICROS_PER_USD

#: Two dollars a day, across everybody. The per-user cap does not bound the bill:
#: N users each stay under their own limit and the total is N times it.
#:
#: Sized against the real plan rather than a round number. Live cost is $0.0246
#: per receipt (measured 2026-08-08), so $2.00 is about 80 receipts a day across
#: all users, and the expected use is two friends at 10 to 15 receipts each,
#: **once**. This is a runaway guard, not a quota: it exists so a loop or a
#: spammer costs $2 rather than a month of Gemini billing.
DEFAULT_GLOBAL_DAILY_COST_LIMIT_MICROS: Final[int] = 2 * MICROS_PER_USD

#: Five dollars a calendar month, across everybody. The daily caps bound a day,
#: and a day is not what gets invoiced: $2.00 a day sustained is roughly $62 a
#: month on the card, which is the exposure that opening the bot up to friends
#: actually creates. Decided 2026-08-10, built 2026-08-11.
#:
#: A **calendar** month, deliberately, while both daily caps roll. A rolling 30
#: day window does not bound a monthly bill: spend the whole ceiling on the 1st,
#: let it age out, spend it again on the 31st, and one invoice carries twice the
#: ceiling. Google bills per calendar month, so this buckets the way the bill
#: does. See `raseed.adapters.flow.month_start_utc`.
DEFAULT_GLOBAL_MONTHLY_COST_LIMIT_MICROS: Final[int] = 5 * MICROS_PER_USD


class ConfigError(RuntimeError):
    """Raised when the environment is missing or malformed."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        msg = f"{name} is not set. Copy .env.example to .env and fill it in."
        raise ConfigError(msg)
    return value


#: Shortest `USER_ID_SECRET` accepted. 32 characters of hex is 128 bits, and a
#: short secret here is not a weak password, it is a secret an attacker with the
#: database can brute force to recover the list of Telegram accounts using it.
MIN_SECRET_LENGTH: Final[int] = 32


def _secret(name: str) -> str:
    """A required secret, with a length floor and a loud message when it is short."""
    value = _require(name)
    if len(value) < MIN_SECRET_LENGTH:
        msg = (
            f"{name} must be at least {MIN_SECRET_LENGTH} characters. "
            'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
        )
        raise ConfigError(msg)
    return value


def _optional(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        msg = f"{name} must be an integer, got {raw!r}."
        raise ConfigError(msg) from exc


def _usd_micros(name: str, default_micros: int) -> int:
    """Read a dollar amount such as `1.00` into integer micro-dollars.

    Parsed through `Decimal`, never `float`. The env file is written by a human
    in dollars, but nothing downstream is allowed to see a float, for the same
    reason invariant 1 keeps money in integer minor units.
    """
    raw = os.environ.get(name, "").strip().lstrip("$")
    if not raw:
        return default_micros
    try:
        dollars = Decimal(raw)
    except InvalidOperation as exc:
        msg = f"{name} must be a dollar amount such as 1.00, got {raw!r}."
        raise ConfigError(msg) from exc
    if dollars < 0:
        msg = f"{name} may not be negative, got {raw!r}."
        raise ConfigError(msg)
    micros = dollars * MICROS_PER_USD
    if micros != micros.to_integral_value():
        msg = f"{name} is finer than a micro-dollar, got {raw!r}."
        raise ConfigError(msg)
    return int(micros)


def _https_url(name: str) -> str:
    """An optional public URL, which must be HTTPS if it is set at all.

    Refused rather than warned about. Telegram will not open a Mini App over
    plain HTTP, so an `http://` value here is not a weaker setup, it is a
    non-working one that fails much later and much less clearly.
    """
    value = os.environ.get(name, "").strip().rstrip("/")
    if not value:
        return ""
    if not value.startswith("https://"):
        msg = f"{name} must start with https://, got {value!r}. Telegram requires TLS."
        raise ConfigError(msg)
    return value


def _int_set(name: str) -> frozenset[int]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return frozenset()
    out: set[int] = set()
    for chunk in raw.split(","):
        token = chunk.strip()
        if not token:
            continue
        try:
            out.add(int(token))
        except ValueError as exc:
            msg = f"{name} must be comma separated integers, got {token!r}."
            raise ConfigError(msg) from exc
    return frozenset(out)


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the process needs from its environment."""

    telegram_bot_token: str = field(repr=False)
    telegram_allowed_user_ids: frozenset[int]
    #: Derives each person's `user_id` from their Telegram account, so no
    #: external identifier is stored anywhere (invariant 3). See
    #: `raseed.identity`. **Changing this orphans every existing ledger.**
    user_id_secret: str = field(repr=False)
    gemini_api_key: str = field(repr=False)
    gemini_model: str
    #: Stage 2's model, which does not have to be Stage 1's. Categorizing short
    #: strings is a cheaper job than reading a photograph, and pinning it
    #: separately is what lets the expensive one move without dragging this along.
    gemini_categorizer_model: str
    database_url: str
    default_currency: str
    default_timezone: str
    reconciliation_tolerance_minor: int
    daily_cost_limit_micros: int
    #: Across every user. Checked before the per-user cap, because the whole
    #: point of it is that one user staying inside their own budget says nothing
    #: about the total.
    global_daily_cost_limit_micros: int
    #: Across every user, per calendar month. Checked before both daily caps,
    #: because a day staying inside its budget says nothing about the invoice.
    global_monthly_cost_limit_micros: int
    #: The public HTTPS address the dashboard is reachable at, or empty. Telegram
    #: will not open a Mini App over plain HTTP or at 127.0.0.1, so while this is
    #: empty the bot hands over a loopback link instead of a button.
    dashboard_public_url: str
    log_level: str

    @classmethod
    def from_env(cls, *, dotenv_path: Path | None = None) -> Settings:
        """Load settings, reading `.env` from the repo root unless told otherwise.

        Real environment variables win over `.env`, which is what you want on a
        deployed host.
        """
        load_dotenv(dotenv_path or (PROJECT_ROOT / ".env"), override=False)
        return cls(
            telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
            telegram_allowed_user_ids=_int_set("TELEGRAM_ALLOWED_USER_IDS"),
            user_id_secret=_secret("USER_ID_SECRET"),
            gemini_api_key=_require("GEMINI_API_KEY"),
            gemini_model=_optional("GEMINI_MODEL", "gemini-3.6-flash"),
            gemini_categorizer_model=_optional(
                "GEMINI_CATEGORIZER_MODEL", DEFAULT_CATEGORIZER_MODEL_ID
            ),
            database_url=_optional("DATABASE_URL", "sqlite:///raseed.db"),
            default_currency=_optional("DEFAULT_CURRENCY", "INR"),
            default_timezone=_optional("DEFAULT_TIMEZONE", "Asia/Kolkata"),
            reconciliation_tolerance_minor=_int(
                "RECONCILIATION_TOLERANCE_MINOR",
                DEFAULT_RECONCILIATION_TOLERANCE_MINOR,
            ),
            daily_cost_limit_micros=_usd_micros(
                "DAILY_COST_LIMIT_USD", DEFAULT_DAILY_COST_LIMIT_MICROS
            ),
            global_daily_cost_limit_micros=_usd_micros(
                "GLOBAL_DAILY_COST_LIMIT_USD", DEFAULT_GLOBAL_DAILY_COST_LIMIT_MICROS
            ),
            global_monthly_cost_limit_micros=_usd_micros(
                "GLOBAL_MONTHLY_COST_LIMIT_USD", DEFAULT_GLOBAL_MONTHLY_COST_LIMIT_MICROS
            ),
            dashboard_public_url=_https_url("DASHBOARD_PUBLIC_URL"),
            log_level=_optional("LOG_LEVEL", "INFO").upper(),
        )
