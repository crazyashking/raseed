"""Versioned extraction prompts.

Prompts are files, not string literals, and they carry a version that is stored
on every row in `raw_extractions`. When accuracy changes, the version says which
prompt produced which result, so a regression is attributable rather than
mysterious.

Never edit a released version in place. Add `v2.md` instead. Old rows reference
`v1` and that reference has to keep meaning what it meant.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

PROMPT_DIR = Path(__file__).parent

#: What a provider uses when nothing else is specified.
#:
#: v3 since 2026-08-13, when a DoorDash receipt in dollars came back as rupees.
#: v2 named only rupees and only paise, so the model had no reason to read the
#: symbol it was looking at, and `currency` had a default waiting to be taken.
#:
#: v2 since 2026-08-12, when the response schema became `ExtractionGroup`. v1
#: describes a single `ExtractionResult` and does not match that schema, so the
#: two moved together.
#:
#: Old versions stay on disk because rows in `raw_extractions` reference them and
#: that reference has to keep meaning what it meant.
DEFAULT_VERSION = "v3"


class PromptNotFoundError(LookupError):
    """Raised when a version has no file on disk."""


@cache
def load(version: str = DEFAULT_VERSION) -> str:
    """Read one prompt version.

    Args:
        version: A stem such as `v1`. No path separators.

    Raises:
        PromptNotFoundError: No such version.
        ValueError: The version looks like a path rather than a name.
    """
    if not version or "/" in version or "\\" in version or version.startswith("."):
        msg = f"prompt version must be a bare name such as 'v1', got {version!r}"
        raise ValueError(msg)

    path = PROMPT_DIR / f"{version}.md"
    if not path.is_file():
        available = ", ".join(sorted(p.stem for p in PROMPT_DIR.glob("v*.md")))
        msg = f"no prompt {version!r}. Available: {available or 'none'}"
        raise PromptNotFoundError(msg)

    return path.read_text(encoding="utf-8")


def available() -> tuple[str, ...]:
    """Every prompt version on disk, sorted."""
    return tuple(sorted(path.stem for path in PROMPT_DIR.glob("v*.md")))


__all__ = ["DEFAULT_VERSION", "PROMPT_DIR", "PromptNotFoundError", "available", "load"]
