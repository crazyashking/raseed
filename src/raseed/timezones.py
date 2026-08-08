"""Timezone lookup, with an error you can act on.

Invariant 6 requires period queries to bucket on the merchant's local calendar
date. That needs a real IANA timezone database.

Linux and macOS ship one at `/usr/share/zoneinfo`. **Windows does not.** On
Windows, CPython's `zoneinfo` has nothing to read unless the `tzdata` package is
installed, and it fails on every key including `UTC`. The failure surfaces deep
inside `zoneinfo._common` as `ZoneInfoNotFoundError`, which reads like a typo in
a zone name rather than a missing dependency, so this module says what is
actually wrong.

The brief's section 23.1 lists `zoneinfo` as stdlib and needing no install. That
is correct on a deployment host and wrong on a Windows development machine. See
`docs/DECISIONS.md`.
"""

from __future__ import annotations

from functools import cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones


class TimezoneDatabaseMissingError(RuntimeError):
    """Raised when there is no IANA database for `zoneinfo` to read."""


def database_available() -> bool:
    """Whether any timezone at all can be resolved."""
    try:
        ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, KeyError, ModuleNotFoundError):
        return False
    return True


@cache
def zone(name: str) -> ZoneInfo:
    """Resolve an IANA name such as `Asia/Kolkata`.

    Raises:
        TimezoneDatabaseMissingError: No database is installed at all. This is
            the Windows case and it is a setup problem, not a bad zone name.
        ZoneInfoNotFoundError: The database exists but has no such zone.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ModuleNotFoundError) as exc:
        if not _any_zones():
            msg = (
                "No IANA timezone database is available, so no timezone can be "
                "resolved, not even UTC. Windows does not ship one. Install the "
                "'tzdata' package into the project virtualenv. "
                "Period bucketing on merchant-local dates (invariant 6) cannot "
                "work without it."
            )
            raise TimezoneDatabaseMissingError(msg) from exc
        raise


def _any_zones() -> bool:
    try:
        return bool(available_timezones())
    except Exception:
        return False


__all__ = ["TimezoneDatabaseMissingError", "database_available", "zone"]
