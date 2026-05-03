"""Time helpers.

The autotrader runs against US markets; all user-visible day boundaries
(daily loss cap, daily trade count, daily LLM budget) are measured in
America/Los_Angeles so "today" matches the user's wall clock regardless
of host timezone or DST. DB rows still store UTC — we just convert the
Pacific-day start/end back to UTC for queries.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
NYSE = ZoneInfo("America/New_York")

# US equity regular session in Eastern.
_REGULAR_OPEN = time(9, 30)
_REGULAR_CLOSE = time(16, 0)


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_pacific() -> datetime:
    return datetime.now(PACIFIC)


def pacific_day_bounds_utc(ref: datetime | None = None) -> tuple[datetime, datetime]:
    """Return [start, end) of the Pacific-local day, expressed in UTC.

    Use to query UTC-stored rows by "today" in the user's timezone.
    """
    ref_pacific = (ref or datetime.now(UTC)).astimezone(PACIFIC)
    start_local = ref_pacific.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def is_us_equities_regular_session(ref: datetime | None = None) -> bool:
    """Cheap best-effort check — weekday + 9:30–16:00 ET.

    Does NOT consult the US market holiday calendar. Brokers (e.g. Alpaca)
    provide an authoritative `is_market_open()` clock endpoint; prefer
    that when a broker is available. This helper is for components that
    don't have a broker handle (e.g. the scout loop).
    """
    now = (ref or datetime.now(UTC)).astimezone(NYSE)
    if now.weekday() >= 5:  # Saturday / Sunday
        return False
    t = now.time()
    return _REGULAR_OPEN <= t < _REGULAR_CLOSE


def five_business_days_ago_ny_start_utc(
    ref: datetime | None = None,
) -> datetime:
    """Return the UTC instant at NY-midnight 5 business days before ``ref``.

    The FINRA PDT rule counts same-day round trips over a rolling 5 NYSE
    trading-day window. Without a full holiday calendar we approximate by
    skipping weekends only — an off-by-one on half-days / holidays biases
    *stricter* (we forget a holiday and count one fewer day), which is
    the safe direction for a safety rail.
    """
    ref = ref or datetime.now(UTC)
    ny_today: date = ref.astimezone(NYSE).date()
    count = 0
    d = ny_today
    while count < 5:
        d = d - timedelta(days=1)
        if d.weekday() < 5:
            count += 1
    dt_ny = datetime.combine(d, time(0, 0), tzinfo=NYSE)
    return dt_ny.astimezone(UTC)


def ny_session_date(ts: datetime) -> date:
    """Map a UTC/aware datetime to its New York calendar date.

    Used to compare open vs close dates for PDT (a 'day trade' is an
    open and close on the same NY session date)."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(NYSE).date()


def pacific_session_date(ts: datetime) -> date:
    """Map a UTC/aware datetime to its Pacific calendar date.

    Analytics charts and day-bucketing anchor on the user's Pacific wall
    clock. A host in UTC would otherwise bucket an 8pm-Pacific close into
    "tomorrow", surfacing ghost dates on the analytics page near midnight.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(PACIFIC).date()


def ny_today() -> date:
    """Today's calendar date in New York.

    Option expirations are US-Eastern dates. Using ``date.today()`` on a
    host in Pacific/UTC can flip the expiry-DTE math near midnight —
    anchoring to NY removes that source of off-by-one rejections and
    watchdog closes.
    """
    return datetime.now(UTC).astimezone(NYSE).date()


__all__ = [
    "NYSE",
    "PACIFIC",
    "five_business_days_ago_ny_start_utc",
    "is_us_equities_regular_session",
    "now_pacific",
    "now_utc",
    "ny_session_date",
    "ny_today",
    "pacific_day_bounds_utc",
    "pacific_session_date",
]
