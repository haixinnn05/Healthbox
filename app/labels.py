"""Label directions to suggested times. Deterministic rules, no AI.

suggest_schedule() reads the directions printed on a label (the frequency_text field) and
suggests plan times. Label text is data: it is matched against fixed patterns and only
fixed times and small numbers come out, so nothing on a label can act as an instruction.
It only suggests; the person still reviews and confirms every entry in Setup.

Anything unclear returns None, and the person types the times themselves."""
from __future__ import annotations

import re

TIMES = {1: ["08:00"], 2: ["08:00", "20:00"], 3: ["08:00", "14:00", "20:00"], 4: ["08:00", "12:00", "16:00", "20:00"]}
HINT_TIMES = {"morning": "08:00", "evening": "20:00", "bedtime": "21:00"}
EVERY_TO_COUNT = {24: 1, 12: 2, 8: 3, 6: 4}
DEFAULT_MIN_INTERVAL, DEFAULT_MAX_PER_DAY = 4, 3

_NUMBER_WORDS = {"twenty four": "24", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                 "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12"}

_NON_DAILY = re.compile(r"\b(other day|alternate days?|qod|weeks?|weekly|wk|months?|monthly|years?|yearly|"
                        r"every \d+ days|(?:mon|tues|wednes|thurs|fri|satur|sun)days?)\b")
_CLOCK_TIME = re.compile(r"\b\d{1,2}(?::\d{2})? ?(?:am|pm)\b|\b\d{1,2}:\d{2}\b|\bnoon\b|\bmidday\b")
_PRN = re.compile(r"\b(as needed|when needed|if needed|as required|prn)\b")
_MULT = re.compile(r"\b(once|twice|thrice|(\d+) times?)\b(?: (?:a|per|each|every|in a|in))?"
                   r"(?: (day|daily|24 hours|24 hrs|week|weeks|month|hour|hours|hr|hrs))?")
_TIMES_RANGE = re.compile(r"\b(\d+ (?:to|or) \d+ times?|once or twice|1 or 2 times)\b")
_EVERY_H = re.compile(r"\b(?:every|q) ?(\d+)(?: to (\d+))? ?(?:hours?|hrs?|h)\b")
_ABBR = {"qd": 1, "bid": 2, "bd": 2, "tid": 3, "tds": 3, "qid": 4, "qds": 4}
_ABBR_RE = re.compile(r"\b(qd|bid|bd|tid|tds|qid|qds)\b")
_DAILY = re.compile(r"\b(daily|every day|each day|everyday)\b")
_HINTS = {
    "morning": re.compile(r"\b(mornings?|qam)\b"),
    "evening": re.compile(r"\b(evenings?|qpm)\b"),
    "bedtime": re.compile(r"\b(bedtime|bed time|before bed|at night|every night|each night|nightly|q?hs)\b"),
}
_MAX = re.compile(r"\b(?:not more than|no more than|not to exceed|do not exceed|maximum of|maximum|max of|max|"
                  r"up to|at most)\s+(\d+)(?![.\d])(?: (tablets?|tabs?|capsules?|caps?|pills?|caplets?|doses?|times?|mg|mcg|g|ml))?")
_UNITS = r"(?:tablets?|tabs?|capsules?|caps?|pills?|caplets?)"
_PER_DOSE = re.compile(r"\b(?:take|use) (\d+)(?: to (\d+))? " + _UNITS + r"\b")


def _normalize(text: str) -> str:
    s = text.lower()[:300]
    s = re.sub(r"(?<=[a-z])\.(?=[a-z])", "", s)  # b.i.d. -> bid. , q.d. -> qd.
    s = re.sub(r"(\d)\s*[-–—]\s*(\d)", r"\1 to \2", s)  # 4-6 -> 4 to 6
    s = re.sub(r"(\d)\s*x\b", r"\1 times", s)  # 2x, 3x/day
    s = re.sub(r"[^a-z0-9:. ]+", " ", s)  # punctuation, slashes, hyphens between words
    s = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", s)  # keep only decimal points
    for word, digit in _NUMBER_WORDS.items():
        s = re.sub(rf"\b{word}\b", digit, s)
    return re.sub(r"\s+", " ", s).strip()


def _basis(phrases: list[str]) -> str:
    seen = []
    for p in phrases:
        if p and p not in seen:
            seen.append(p)
    return "; ".join(seen)[:80]


def suggest_schedule(frequency_text: str | None, units_per_dose: int | None = None) -> dict | None:
    """{"schedule": "SCHEDULED"|"AS_NEEDED", "times": [...], "min_interval_hours", "max_per_day", "basis"} or None.

    units_per_dose (from the label fields, optional) converts a limit stated in tablets into doses."""
    if not frequency_text or not isinstance(frequency_text, str):
        return None
    s = _normalize(frequency_text)
    if not s or _NON_DAILY.search(s) or _CLOCK_TIME.search(s) or _TIMES_RANGE.search(s):
        return None
    if re.search(r"\d\.\d+ ?(?:hours?|hrs?|h|times?|" + _UNITS[3:-1] + r")\b", s):
        return None  # decimal hours, counts or tablets: not clear enough

    phrases: list[str] = []
    counts: set[int] = set()
    for m in _MULT.finditer(s):
        unit = m.group(3)
        if unit in ("week", "weeks", "month", "hour", "hours", "hr", "hrs"):
            return None
        n = {"once": 1, "twice": 2, "thrice": 3}.get(m.group(1)) or int(m.group(2))
        counts.add(n)
        phrases.append(m.group(0))
    for m in _ABBR_RE.finditer(s):
        counts.add(_ABBR[m.group(1)])
        phrases.append(m.group(1))
    intervals: list[tuple[int, int | None, str]] = [
        (int(m.group(1)), int(m.group(2)) if m.group(2) else None, m.group(0)) for m in _EVERY_H.finditer(s)]
    hints = {name for name, rx in _HINTS.items() if rx.search(s)}
    hint_phrases = [rx.search(s).group(0) for name, rx in _HINTS.items() if name in hints]
    maxes = [(int(m.group(1)), m.group(2), m.group(0)) for m in _MAX.finditer(s)]

    if _PRN.search(s):
        return _as_needed(s, phrases, counts, intervals, hints, maxes, units_per_dose)

    # Scheduled. Limits ("up to", "no more than") without "as needed" are unclear.
    if maxes:
        return None
    for n, m, text in intervals:
        if m is not None or n not in EVERY_TO_COUNT:
            return None
        counts.add(EVERY_TO_COUNT[n])
        phrases.append(text)
    if not counts:
        d = _DAILY.search(s)
        if d:
            counts.add(1)
            phrases.append(d.group(0))
    if len(counts) > 1:
        return None
    count = next(iter(counts), None)
    if count is not None and count not in TIMES:
        return None
    if hints:
        if "evening" in hints and "bedtime" in hints:
            return None
        if count is not None and count != len(hints):
            return None  # e.g. "twice daily at bedtime"
        times = sorted(HINT_TIMES[h] for h in hints)
        phrases += hint_phrases
    elif count is not None:
        times = TIMES[count]
    else:
        return None
    return {"schedule": "SCHEDULED", "times": times, "min_interval_hours": None, "max_per_day": None,
            "basis": _basis(phrases)}


def _as_needed(s, phrases, counts, intervals, hints, maxes, units_per_dose) -> dict | None:
    prn = _PRN.search(s).group(0)
    basis = [prn]

    # Minimum gap: "every N hours", or "every N to M hours" -> M, the safer gap.
    min_interval = None
    if intervals:
        min_interval = max(m if m is not None else n for n, m, _ in intervals)
        basis += [text for *_, text in intervals]
        min_interval = min(24, max(1, min_interval))

    # Daily maximum, in doses.
    max_per_day = None
    for n, unit, text in maxes:
        if unit in ("mg", "mcg", "g", "ml"):
            return None  # an amount, not a count of doses
        if unit and unit.rstrip("s") in ("tablet", "tab", "capsule", "cap", "pill", "caplet"):
            per = units_per_dose
            m = _PER_DOSE.search(s)
            if m:
                per = int(m.group(2) or m.group(1))  # "1 to 2 tablets" -> 2, the safer conversion
            if not per or per < 1:
                return None  # a limit in tablets needs the tablets per dose
            n = n // per
            if n < 1:
                return None
        if max_per_day is not None and n != max_per_day:
            return None
        max_per_day = n
        basis.append(text)
    # A stated daily frequency ("twice daily as needed", "at bedtime as needed") is a stated maximum too.
    stated = set(counts)
    if hints:
        stated.add(len(hints))
    if not stated and not intervals and not maxes:
        d = _DAILY.search(s)
        if d:
            stated.add(1)
            basis.append(d.group(0))
    if stated:
        if len(stated) > 1:
            return None
        n = next(iter(stated))
        if max_per_day is not None and n != max_per_day:
            return None
        max_per_day = n
        basis += [p for p in phrases if p not in basis]
    if max_per_day is not None:
        max_per_day = min(8, max(1, max_per_day))

    if min_interval is None:
        min_interval = DEFAULT_MIN_INTERVAL
    if max_per_day is None:
        # Not stated: the default, but never more than the gap allows in 24 hours.
        max_per_day = min(DEFAULT_MAX_PER_DAY, max(1, 24 // min_interval))
    return {"schedule": "AS_NEEDED", "times": [], "min_interval_hours": min_interval, "max_per_day": max_per_day,
            "basis": _basis(basis)}
