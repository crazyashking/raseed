"""Versioned Stage 2 categorization prompts.

A separate directory from `extraction/prompts` rather than a shared one with two
namespaces, because invariant 4 keeps the two stages apart and a shared loader
would be the first place that separation quietly leaked.

Same rule as Stage 1: never edit a released version in place, add `v2.md`. The
version is recorded on the `raw_extractions` row the fallback writes, so a
regression stays attributable.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

PROMPT_DIR = Path(__file__).parent

#: What the categorizer uses when nothing else is specified. Recorded as
#: `raw_extractions.prompt_version` on every Stage 2 row, and prefixed so it can
#: never be confused with the Stage 1 `v1` on a row someone reads by eye.
DEFAULT_VERSION = "categorize-v1"


class PromptNotFoundError(LookupError):
    """Raised when a version has no file on disk."""


@cache
def load(version: str = DEFAULT_VERSION) -> str:
    """Read one prompt version.

    Args:
        version: A stem such as `categorize-v1`. No path separators.

    Raises:
        PromptNotFoundError: No such version.
        ValueError: The version looks like a path rather than a name.
    """
    if not version or "/" in version or "\\" in version or version.startswith("."):
        msg = f"prompt version must be a bare name such as 'categorize-v1', got {version!r}"
        raise ValueError(msg)

    path = PROMPT_DIR / f"{version}.md"
    if not path.is_file():
        available_now = ", ".join(sorted(p.stem for p in PROMPT_DIR.glob("categorize-*.md")))
        msg = f"no prompt {version!r}. Available: {available_now or 'none'}"
        raise PromptNotFoundError(msg)

    return path.read_text(encoding="utf-8")


def available() -> tuple[str, ...]:
    """Every prompt version on disk, sorted."""
    return tuple(sorted(path.stem for path in PROMPT_DIR.glob("categorize-*.md")))


__all__ = ["DEFAULT_VERSION", "PROMPT_DIR", "PromptNotFoundError", "available", "load"]
