"""Tolerant timestamp parsing and comparison for the memory store.

Every timestamp in the store is an ISO-8601 string. Callers, however, naturally
ask questions such as "where did I live in 2025?" or pass ``as_of=2025`` / ``as_of=2025-06``.
Comparing those strings lexicographically (the previous behaviour) is wrong in
three ways:

* ``"2025" < "2025-01-01T00:00:00+00:00"`` so an as-of year silently matched nothing;
* a date-only ``as_of`` excluded facts that became valid on that very day;
* a naive timestamp raised ``TypeError`` when compared with an aware one.

This module normalises wall-clock input to the **end of the requested period**
(so ``2025`` means "at the end of 2025") and compares real ``datetime`` values in
UTC.
"""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timezone

# Year, optional month, optional day, accepting 2025 / 2025-06 / 2025/6/2 /
# 2025年6月2日 so both API callers and natural-language queries work.
_PERIOD = re.compile(
    r"(?P<year>(?:19|20)\d{2})"
    r"(?:\s*[-/年]\s*(?P<month>\d{1,2}))?"
    r"(?:\s*[-/月]\s*(?P<day>\d{1,2}))?"
    r"\s*日?"
)
_FULL_PERIOD = re.compile(_PERIOD.pattern + r"$")


def _end_of_period(match: re.Match[str]) -> datetime | None:
    year = int(match.group("year"))
    month_raw = match.group("month")
    day_raw = match.group("day")
    try:
        if month_raw is None:
            return datetime(year, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
        month = int(month_raw)
        if day_raw is None:
            last_day = calendar.monthrange(year, month)[1]
            return datetime(year, month, last_day, 23, 59, 59, 999999, tzinfo=timezone.utc)
        return datetime(year, month, int(day_raw), 23, 59, 59, 999999, tzinfo=timezone.utc)
    except ValueError:  # e.g. 2025-02-31
        return None


def parse_instant(value: str | None) -> datetime | None:
    """Parse an ISO-ish timestamp into an aware UTC datetime, or ``None``.

    Naive values are interpreted as UTC. Date-only values resolve to the end of
    the day/month/year so "as of 2025-06-01" includes facts valid on that day.
    """
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    period = _FULL_PERIOD.match(text)
    if period is not None:
        return _end_of_period(period)
    candidate = text[:-1] + "+00:00" if text[-1] in "Zz" else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def find_period(text: str) -> str | None:
    """Return the first year/month/day expression in ``text``, if any.

    Used by the query planner to turn "2025年我住在哪" into an ``as_of`` filter.
    """
    match = _PERIOD.search(text or "")
    return match.group(0).strip() if match else None


def visible_at(valid_from: str | None, valid_to: str | None, as_of: str | None) -> bool:
    """Whether a fact bounded by ``valid_from``/``valid_to`` holds at ``as_of``.

    Unparsable bounds are treated as "unknown" and do not filter the fact out;
    only an explicit, parseable bound can exclude it.
    """
    if as_of is None:
        return True
    moment = parse_instant(as_of)
    if moment is None:
        return True
    start = parse_instant(valid_from)
    if start is not None and start > moment:
        return False
    end = parse_instant(valid_to)
    if end is not None and end <= moment:
        return False
    return True


def expired(valid_to: str | None, now: str | None) -> bool:
    """Whether ``valid_to`` is already in the past relative to ``now``."""
    if not valid_to:
        return False
    moment = parse_instant(now) or datetime.now(timezone.utc)
    end = parse_instant(valid_to)
    return end is not None and end <= moment


def overlaps(valid_from: str | None, valid_to: str | None,
             range_from: str | None, range_to: str | None) -> bool:
    """Whether two half-open validity intervals overlap."""
    start = parse_instant(valid_from)
    end = parse_instant(valid_to)
    requested_start = parse_instant(range_from)
    requested_end = parse_instant(range_to)
    if requested_end is not None and start is not None and start >= requested_end:
        return False
    if requested_start is not None and end is not None and end <= requested_start:
        return False
    return True


def require_instant(value: str | None, field: str) -> str | None:
    """Return ``value`` unchanged, raising ``ValueError`` when it is unparsable.

    Silently ignoring a malformed ``as_of``/``now`` would answer a different
    question than the caller asked, so callers surface it as a client error.
    """
    if value is None or str(value).strip() == "":
        return None
    if parse_instant(value) is None:
        raise ValueError(f"{field} is not a valid timestamp: {value!r}")
    return value
