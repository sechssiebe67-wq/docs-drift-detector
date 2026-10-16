"""Shared, dependency-free cursor pagination for the read-only trend views.

The four drift-trend endpoints answer "how bad was drift during the 4.2 release
window?" but were bounded to the most recent ``limit`` scans or buckets, so a
documentation lead could not page further back to an older release once the
recent window was exhausted. This module adds a stable, opaque cursor so older
scans and buckets can be fetched in successive pages.

The design keeps the existing default behaviour: an *ordered* list of items is
presented **oldest-first**, and a cursor-less call returns the most recent
``limit`` items exactly as before. Following ``next_cursor`` walks *backwards*
in time, each page still oldest-first, so a client that prepends successive
pages reconstructs the full chronological series.

The cursor is a URL-safe base64 of the JSON-encoded sort key of the oldest item
on the page (for example ``[created_at, scan_id]``). It is intentionally *not*
an integer offset: an item inserted between two requests (always newer, so
appended after the current window) never shifts the anchor, so older pages stay
stable. Malformed cursor strings raise :class:`CursorError` so the HTTP layer
can answer ``400``; a well-formed cursor that matches no item is treated as an
unknown/expired cursor and yields an empty page.

The module is standard-library only and never mutates state.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Callable, List, Optional, Sequence, Tuple

DEFAULT_LIMIT = 10
MAX_LIMIT = 50


class CursorError(ValueError):
    """Raised when a cursor string is not a well-formed opaque cursor."""


def clamp_limit(limit: Any) -> int:
    """Clamp ``limit`` into ``1..MAX_LIMIT``; fall back to ``DEFAULT_LIMIT``."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return DEFAULT_LIMIT
    if limit < 1:
        return 1
    if limit > MAX_LIMIT:
        return MAX_LIMIT
    return limit


def encode_cursor(key: Sequence[str]) -> str:
    """Return the opaque, URL-safe cursor for a sort ``key``.

    ``key`` is a sequence of strings (for example ``(created_at, scan_id)``).
    The result is unpadded base64 so it is safe to place in a query string.
    """
    payload = json.dumps(
        [str(part) for part in key], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> Tuple[str, ...]:
    """Decode an opaque cursor into its sort key, or raise :class:`CursorError`."""
    if not isinstance(cursor, str):
        raise CursorError("invalid cursor")
    text = cursor.strip()
    if not text:
        raise CursorError("invalid cursor")
    padding = "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(text + padding)
    except (binascii.Error, ValueError):
        raise CursorError("invalid cursor")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise CursorError("invalid cursor")
    if not isinstance(decoded, list) or not decoded:
        raise CursorError("invalid cursor")
    if not all(isinstance(part, str) for part in decoded):
        raise CursorError("invalid cursor")
    return tuple(decoded)


def paginate(
    items: List[Any],
    cursor: Optional[str],
    limit: Any,
    key: Callable[[Any], Sequence[str]],
) -> Tuple[List[Any], Optional[str]]:
    """Return ``(page, next_cursor)`` for an oldest-first ``items`` list.

    ``items`` must already be in the order the caller wants to present (oldest
    first) and ``key`` must return a stable, unique sort key for an item. A
    ``cursor`` of ``None`` returns the most recent ``limit`` items (the existing
    default). A present cursor locates the anchor item whose key it encodes and
    returns the ``limit`` items immediately older than it. ``next_cursor`` is
    the opaque cursor of the oldest item on the returned page, or ``None`` when
    the page reaches the oldest item (or no anchor matches).

    A cursor string that is not well-formed raises :class:`CursorError`. A
    well-formed cursor that matches no item returns an empty page with
    ``next_cursor`` of ``None``.
    """
    bound = clamp_limit(limit)
    if not isinstance(items, list):
        items = list(items)
    if cursor is None or (isinstance(cursor, str) and not cursor.strip()):
        end = len(items)
    else:
        anchor = decode_cursor(cursor)
        end = None
        for index, item in enumerate(items):
            if tuple(str(part) for part in key(item)) == anchor:
                end = index
                break
        if end is None:
            return [], None
    start = max(0, end - bound)
    page = items[start:end]
    next_cursor: Optional[str] = None
    if page and start > 0:
        next_cursor = encode_cursor(key(page[0]))
    return page, next_cursor
