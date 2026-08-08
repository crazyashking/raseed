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

from raseed.extraction.pricing import MICROS_PER_USD
from raseed.validation.reconcile import DEFAULT_TOLERANCE_MINOR

#: Repository root, resolved from this file so it works regardless of cwd.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent

#: Re-exported so there is exactly one definition of the tolerance. It lives with
#: the gate that uses it, in `raseed.validation.reconcile`. Brief section 18.4.
DEFAULT_RECONCILIATION_TOLERANCE_MINOR: Final[int] = DEFAULT_TOLERANCE_MINOR

#: One dollar a day. Brief section 16.6. Held in integer micro-dollars because
#: it is compared against summed `cost_micros_usd`, which are integers, and
#: because a budget that drifts by a rounding error is not a budget.
DEFAULT_DAILY_COST_LIMIT_MICROS: Final[int] = MICROS_PER_USD


class ConfigError(RuntimeError):
    """Raised when the environment is missing or malformed."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        msg = f"{name} is not set. Copy .env.example to .env and fill it in."
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
    gemini_api_key: str = field(repr=False)
    gemini_model: str
    database_url: str
    default_currency: str
    default_timezone: str
    reconciliation_tolerance_minor: int
    daily_cost_limit_micros: int
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
            gemini_api_key=_require("GEMINI_API_KEY"),
            gemini_model=_optional("GEMINI_MODEL", "gemini-3.6-flash"),
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
            log_level=_optional("LOG_LEVEL", "INFO").upper(),
        )
