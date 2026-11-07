"""Clock and time helpers. All stored timestamps are UTC ISO strings with milliseconds."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from zoneinfo import ZoneInfo

UTC = timezone.utc


class Clock:
    """Wall clock for schedules and event stamps, monotonic clock for short delays and freshness."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock(Clock):
    def __init__(self, start: datetime):
        self._now = start.astimezone(UTC)
        self._mono = 1000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


def parse(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s).astimezone(UTC)


def parse_hhmm(s: str) -> dtime:
    hh, mm = s.strip().split(":")
    t = dtime(int(hh), int(mm))
    return t


def resolve_local(d: date, t: dtime, tz: ZoneInfo) -> tuple[datetime, str | None]:
    """Resolve a local wall time to UTC. Returns (utc, issue) where issue is
    None, 'NONEXISTENT' (spring-forward gap) or 'AMBIGUOUS' (fall-back repeat).
    Callers must surface issues for explicit review instead of guessing."""
    naive = datetime.combine(d, t)
    first = naive.replace(tzinfo=tz, fold=0).astimezone(UTC)
    second = naive.replace(tzinfo=tz, fold=1).astimezone(UTC)
    if first.astimezone(tz).replace(tzinfo=None) != naive:
        return first, "NONEXISTENT"
    if first != second:
        return first, "AMBIGUOUS"
    return first, None


def local_date(dt: datetime, tz: ZoneInfo) -> date:
    return dt.astimezone(tz).date()


def fmt_local(dt: datetime | None, tz: ZoneInfo) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(tz).strftime("%-I:%M %p")


def upcoming_dst_issues(times: list[str], tz: ZoneInfo, start: date, days: int = 60) -> list[dict]:
    """Preview local times in the next `days` that fall in a DST gap or overlap."""
    issues = []
    for offset in range(days):
        d = start + timedelta(days=offset)
        for hhmm in times:
            _, issue = resolve_local(d, parse_hhmm(hhmm), tz)
            if issue:
                issues.append({"date": d.isoformat(), "time": hhmm, "issue": issue})
    return issues
