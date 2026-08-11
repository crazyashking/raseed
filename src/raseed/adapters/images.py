"""Where a receipt image lives, briefly.

Invariant 7: receipt images are deleted after confirm, and nothing depends on
them persisting. Brief 16.5 adds the other half: the image survives on disk
until extraction succeeds AND the user confirms, so a rate limit or a timeout
does not lose the receipt.

Between those two moments the file sits under `data/incoming/`, which is
gitignored. It is named by the SHA-256 of its own bytes, so the same image
cannot occupy two files and the name is the dedupe key.

Nothing in here edits, rotates, crops, compresses or enhances anything. Bytes
are written exactly as received. Invariant 9.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: Extension by mime type. Only formats a phone actually sends.
SUFFIX_BY_MIME: Final[dict[str, str]] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}


def sha256_of(data: bytes) -> str:
    """The dedupe key from brief 3.6, taken from the bytes as received."""
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredImage:
    """An image on disk, awaiting a decision."""

    path: Path
    sha256: str
    mime_type: str
    size_bytes: int


class ImageStore:
    """Holds receipt images between extraction and confirmation.

    Args:
        root: Directory to write into. Created if absent.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def save(self, data: bytes, *, mime_type: str) -> StoredImage:
        """Write bytes verbatim and return where they went.

        Raises:
            ValueError: The mime type is not one we accept.
        """
        suffix = SUFFIX_BY_MIME.get(mime_type)
        if suffix is None:
            accepted = ", ".join(sorted(SUFFIX_BY_MIME))
            msg = f"unsupported image type {mime_type!r}. Accepted: {accepted}"
            raise ValueError(msg)

        digest = sha256_of(data)
        path = self._root / f"{digest}{suffix}"
        if not path.exists():
            path.write_bytes(data)

        return StoredImage(path=path, sha256=digest, mime_type=mime_type, size_bytes=len(data))

    def delete(self, path: Path | None) -> bool:
        """Remove an image. Invariant 7.

        Returns True if a file went away. Missing is not an error: the point is
        that it is gone, and it already was.
        """
        if path is None:
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def sweep(self, *, older_than: dt.datetime) -> list[Path]:
        """Delete images left behind by a restart or an expired confirmation.

        A crash between extraction and confirm strands the image: the pending
        row rolls back with the transaction, and the file already written to
        disk does not. Three receipts ended up orphaned that way when the
        pending store deadlocked. That is the thing invariant 7 exists to
        prevent, so it gets swept rather than hoped about.
        """
        cutoff = older_than.timestamp()
        removed: list[Path] = []
        for path in self._root.iterdir():
            if not path.is_file():
                continue
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed.append(path)
        return removed

    def __len__(self) -> int:
        return sum(1 for path in self._root.iterdir() if path.is_file())


__all__ = ["SUFFIX_BY_MIME", "ImageStore", "StoredImage", "sha256_of"]
