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
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

from raseed.validation.reconcile import DEFAULT_TOLERANCE_MINOR

#: Repository root, resolved from this file so it works regardless of cwd.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent

#: Re-exported so there is exactly one definition of the tolerance. It lives with
#: the gate that uses it, in `raseed.validation.reconcile`. Brief section 18.4.
DEFAULT_RECONCILIATION_TOLERANCE_MINOR: Final[int] = DEFAULT_TOLERANCE_MINOR


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
            log_level=_optional("LOG_LEVEL", "INFO").upper(),
        )
