"""Run the bot: `python -m raseed`.

This is the only place the pieces are wired together, and it does nothing else.
Every decision it makes is read from `Settings`, so there is no configuration
hiding in code.

The schema is Alembic's job, not this module's. If the database has not been
migrated, the first write fails loudly rather than being papered over by a
`create_all` that would then disagree with the migration history.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys

from sqlalchemy.orm import sessionmaker

from raseed.adapters.flow import FlowConfig, ReceiptFlow
from raseed.adapters.images import ImageStore
from raseed.adapters.pending import PendingStore
from raseed.adapters.telegram import RaseedBot, build_application
from raseed.config import PROJECT_ROOT, ConfigError, Settings
from raseed.db.engine import create_engine
from raseed.db.seed import bootstrap
from raseed.extraction.providers.gemini import GeminiProvider
from raseed.timezones import TimezoneDatabaseMissingError, zone

log = logging.getLogger("raseed")

#: Gitignored, and swept on every start. Invariant 7 and brief 16.5.
INCOMING = PROJECT_ROOT / "data" / "incoming"


def now_utc() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def build(settings: Settings) -> tuple[RaseedBot, ImageStore]:
    """Assemble everything from settings. No side effects beyond the engine."""
    engine = create_engine(settings.database_url)
    sessions = sessionmaker(bind=engine)

    with sessions() as session:
        bootstrap(session)
        session.commit()

    images = ImageStore(INCOMING)
    flow = ReceiptFlow(
        provider=GeminiProvider(api_key=settings.gemini_api_key, model_id=settings.gemini_model),
        images=images,
        pending=PendingStore(),
        config=FlowConfig(
            daily_cost_limit_micros=settings.daily_cost_limit_micros,
            default_timezone=settings.default_timezone,
            tolerance_minor=settings.reconciliation_tolerance_minor,
        ),
        clock=now_utc,
    )
    bot = RaseedBot(
        flow=flow,
        session_factory=sessions,
        allowed_user_ids=settings.telegram_allowed_user_ids,
        clock=now_utc,
    )
    return bot, images


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)  # noqa: T201
        return 2

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    try:
        zone(settings.default_timezone)
    except TimezoneDatabaseMissingError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)  # noqa: T201
        return 2

    if not settings.telegram_allowed_user_ids:
        log.error(
            "TELEGRAM_ALLOWED_USER_IDS is empty, so this bot would answer nobody. "
            "Set it to your numeric Telegram user id."
        )
        return 2

    bot, images = build(settings)

    # A crash between extraction and confirm strands an image on disk, and the
    # pending store that knew about it lives in memory. Invariant 7.
    stranded = images.sweep(older_than=now_utc() - dt.timedelta(hours=24))
    if stranded:
        log.info("swept %d stranded receipt image(s)", len(stranded))

    log.info("starting, %d allowed user(s)", len(settings.telegram_allowed_user_ids))
    build_application(settings.telegram_bot_token, bot).run_polling()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
