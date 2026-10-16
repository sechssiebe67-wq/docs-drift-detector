"""Shared, dependency-free date-window parsing and filtering.

The drift trend surface lets a documentation lead ask "how bad was drift during
the 4.2 release window?" by adding an explicit, inclusive ``from``/``to`` window
to the four read-only trend endpoints. This module owns the parsing rules so
every endpoint (and every builder) agrees on them:

- a *missing* or empty bound is unbounded on that side;
- a plain date (``2026-01-05``) is interpreted as ``00:00:00+00:00``;
- a full ISO-8601 timestamp (``2026-01-05T12:00:00+00:00``) keeps its offset,
  and a naive value is treated as UTC;
- anything else is malformed and raises :class:`WindowError`, letting the HTTP
  layer answer 400.

The module is standard-library only and never mutates state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, Tuple


class WindowError(ValueError):
    """Raised when a ``from``/``to`` bound cannot be parsed."""


def parse_stored(value: Any) -> Optional[datetime]:
    """Return an aware UTC datetime for a stored ``created_at``, or ``None``.

    Stored timestamps are the values written by
    :meth:`docs_drift_detector.store.Store.create_scan` (UTC ISO-8601). A
    malformed value returns ``None`` so callers can skip it rather than raise.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_bound(value: Any, field: str = "value") -> Optional[datetime]:
    """Parse one optional bound into an aware UTC datetime.

    ``None`` or a blank string means unbounded (``None``). A plain date is
    midnight UTC and a full ISO-8601 timestamp keeps its offset (naive values
    are treated as UTC). Raises :class:`WindowError` for a malformed value.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise WindowError("invalid %s" % field)
    if not value.strip():
        return None
    moment = parse_stored(value)
    if moment is None:
        raise WindowError("invalid %s" % field)
    return moment


def parse_window(
    from_value: Any = None, to_value: Any = None
) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Parse the optional ``from``/``to`` values into an inclusive window.

    Returns ``(start, end)`` where either side may be ``None`` (unbounded).
    Raises :class:`WindowError` when a present value is malformed.
    """
    return parse_bound(from_value, "from"), parse_bound(to_value, "to")


def in_window(
    moment: datetime,
    start: Optional[datetime],
    end: Optional[datetime],
) -> bool:
    """Return whether ``moment`` falls in the inclusive ``[start, end]`` window.

    ``moment`` is expected to be timezone-aware (as returned by
    :func:`parse_stored`); ``start``/``end`` may each be ``None`` to leave that
    side unbounded.
    """
    if start is not None and moment < start:
        return False
    if end is not None and moment > end:
        return False
    return True
