"""Tests for reading the environment.

The daily cost cap is money, so it gets the same treatment as every other
amount in this project: parsed through `Decimal`, held as an integer, and
refused rather than rounded when it does not fit. Invariant 1 is about the
ledger, but a budget that silently drifts is no better than a float rupee.

`monkeypatch.delenv` runs before every case because a real `.env` sits in the
repo root on the development machine, and a test must not read it.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from raseed.__main__ import TOKEN_LEAKING_LOGGERS, _silence_token_leaking_loggers
from raseed.config import (
    DEFAULT_DAILY_COST_LIMIT_MICROS,
    ConfigError,
    Settings,
    _int_set,
    _usd_micros,
)

ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_IDS",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "DATABASE_URL",
    "DEFAULT_CURRENCY",
    "DEFAULT_TIMEZONE",
    "RECONCILIATION_TOLERANCE_MINOR",
    "DAILY_COST_LIMIT_USD",
    "LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# The daily cap (brief 16.6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "micros"),
    [
        ("1.00", 1_000_000),
        ("1", 1_000_000),
        ("0.50", 500_000),
        ("$2.25", 2_250_000),
        ("0", 0),
        ("0.000001", 1),
        ("  1.00  ", 1_000_000),
    ],
)
def test_a_dollar_amount_becomes_integer_micros(
    monkeypatch: pytest.MonkeyPatch, raw: str, micros: int
) -> None:
    monkeypatch.setenv("DAILY_COST_LIMIT_USD", raw)
    assert _usd_micros("DAILY_COST_LIMIT_USD", 0) == micros


def test_an_unset_cap_falls_back_to_the_default() -> None:
    assert _usd_micros("DAILY_COST_LIMIT_USD", 777) == 777
    assert DEFAULT_DAILY_COST_LIMIT_MICROS == 1_000_000


@pytest.mark.parametrize("raw", ["one dollar", "1.0.0", "", " ", "1,00"])
def test_a_malformed_cap_is_refused_not_guessed(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("DAILY_COST_LIMIT_USD", raw)
    if not raw.strip():
        assert _usd_micros("DAILY_COST_LIMIT_USD", 5) == 5
        return
    with pytest.raises(ConfigError, match="dollar amount"):
        _usd_micros("DAILY_COST_LIMIT_USD", 0)


def test_a_negative_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAILY_COST_LIMIT_USD", "-1.00")
    with pytest.raises(ConfigError, match="negative"):
        _usd_micros("DAILY_COST_LIMIT_USD", 0)


def test_a_cap_finer_than_a_micro_is_refused_not_rounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rounding a budget down is a decision, and it is not this function's."""
    monkeypatch.setenv("DAILY_COST_LIMIT_USD", "0.0000005")
    with pytest.raises(ConfigError, match="finer than a micro"):
        _usd_micros("DAILY_COST_LIMIT_USD", 0)


# ---------------------------------------------------------------------------
# The whitelist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", frozenset()),
        ("123", frozenset({123})),
        ("123,456", frozenset({123, 456})),
        (" 123 , 456 ", frozenset({123, 456})),
        ("123,,456,", frozenset({123, 456})),
    ],
)
def test_the_whitelist_parses(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: frozenset[int]
) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", raw)
    assert _int_set("TELEGRAM_ALLOWED_USER_IDS") == expected


def test_a_username_in_the_whitelist_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The common mistake: @handle instead of the numeric id."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "@ashrit")
    with pytest.raises(ConfigError, match="comma separated integers"):
        _int_set("TELEGRAM_ALLOWED_USER_IDS")


# ---------------------------------------------------------------------------
# Settings as a whole
# ---------------------------------------------------------------------------


def test_a_missing_token_names_itself(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        Settings.from_env(dotenv_path=tmp_path / "absent.env")


def test_settings_load_from_a_dotenv_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "TELEGRAM_BOT_TOKEN=t\n"
        "TELEGRAM_ALLOWED_USER_IDS=4242\n"
        "GEMINI_API_KEY=k\n"
        "DAILY_COST_LIMIT_USD=0.75\n",
        encoding="utf-8",
    )
    settings = Settings.from_env(dotenv_path=env)

    assert settings.telegram_allowed_user_ids == frozenset({4242})
    assert settings.daily_cost_limit_micros == 750_000
    assert settings.gemini_model == "gemini-3.6-flash"
    assert settings.default_timezone == "Asia/Kolkata"


def test_the_token_leaking_loggers_are_silenced(caplog: pytest.LogCaptureFixture) -> None:
    """The Telegram API puts the bot token in the URL path.

    `httpx` logs the full request URL at INFO, so a default `LOG_LEVEL=INFO`
    writes the token to disk on every poll, several times a minute. Keeping it
    out of `Settings.__repr__` is pointless if a dependency prints it anyway.
    """
    _silence_token_leaking_loggers()
    for name in TOKEN_LEAKING_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING

    with caplog.at_level(logging.DEBUG):
        logging.getLogger("httpx").info("POST https://api.telegram.org/bot123:SECRET/getMe")
    assert "SECRET" not in caplog.text


def test_secrets_stay_out_of_the_repr(tmp_path: Path) -> None:
    """A traceback or a log line must not print the bot token or the API key."""
    env = tmp_path / ".env"
    env.write_text(
        "TELEGRAM_BOT_TOKEN=sekrit-token\nGEMINI_API_KEY=sekrit-key\n",
        encoding="utf-8",
    )
    text = repr(Settings.from_env(dotenv_path=env))
    assert "sekrit-token" not in text
    assert "sekrit-key" not in text
