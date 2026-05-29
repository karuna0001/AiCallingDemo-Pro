import re
from datetime import datetime, timedelta, time as dt_time
from typing import Optional
from zoneinfo import ZoneInfo


DAYPART_TIMES = {
    "morning": dt_time(9, 0),
    "afternoon": dt_time(14, 0),
    "evening": dt_time(18, 0),
    "night": dt_time(20, 0),
}

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _now(timezone: str, now: Optional[datetime] = None) -> datetime:
    if now is not None:
        if now.tzinfo is None:
            return now.replace(tzinfo=ZoneInfo(timezone))
        return now.astimezone(ZoneInfo(timezone))
    return datetime.now(ZoneInfo(timezone))


def _combine(base: datetime, clock: dt_time) -> datetime:
    return base.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)


TIME_CONTEXT_WORDS = (
    "today", "tomorrow", "morning", "afternoon", "evening", "night",
    "call", "message", "whatsapp", "meet", "meeting", "demo",
)
QUANTITY_WORDS = (
    "cabinet", "cabinets", "lakh", "lakhs", "lac", "lacs", "rupee", "rupees",
    "rs", "budget", "unit", "units", "piece", "pieces", "pcs", "sqft",
    "square", "doors", "windows",
)


def _clock_match(text: str) -> Optional[re.Match]:
    patterns = (
        r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
        r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
        r"\b(\d{1,2}):(\d{2})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match

    for match in re.finditer(r"\b(\d{1,2})(?::(\d{2}))?\b", text):
        start, end = match.span()
        before = text[max(0, start - 18):start]
        after = text[end:end + 18]
        nearby = f"{before} {after}"
        next_word = re.match(r"\s*([a-z]+)", after)
        if next_word and next_word.group(1) in QUANTITY_WORDS:
            continue
        if any(word in nearby for word in TIME_CONTEXT_WORDS):
            return match
    return None


def _parse_clock(text: str) -> Optional[dt_time]:
    match = _clock_match(text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    suffix = (match.group(3) or "").lower()
    if suffix == "pm" and hour < 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    if not suffix and 1 <= hour <= 7:
        hour += 12
    if hour > 23 or minute > 59:
        return None
    return dt_time(hour, minute)


def parse_followup_time(text: str, timezone: str = "Asia/Kolkata", now: Optional[datetime] = None) -> datetime:
    """Parse common Indian-English follow-up phrases into a timezone-aware datetime."""
    raw = (text or "").strip().lower()
    current = _now(timezone or "Asia/Kolkata", now)
    if not raw:
        return _combine(current + timedelta(days=1), dt_time(10, 0))

    rel = re.search(r"\b(?:after|in|within)\s+(\d+)\s*(minute|minutes|min|mins|hour|hours|hr|hrs)\b", raw)
    if rel:
        qty = int(rel.group(1))
        unit = rel.group(2)
        return current + (timedelta(hours=qty) if unit.startswith(("hour", "hr")) else timedelta(minutes=qty))

    if "call later" in raw or raw == "later":
        return current + timedelta(hours=2)

    clock = _parse_clock(raw)
    part = next((name for name in DAYPART_TIMES if name in raw), "")
    part_clock = DAYPART_TIMES.get(part)

    if "tomorrow" in raw:
        target = current + timedelta(days=1)
        return _combine(target, clock or part_clock or dt_time(10, 0))

    if "today" in raw:
        target = _combine(current, clock or part_clock or dt_time(18, 0))
        return target if target > current else target + timedelta(days=1)

    for weekday, idx in WEEKDAYS.items():
        if weekday in raw:
            days = (idx - current.weekday()) % 7
            if "next" in raw or days == 0:
                days = days or 7
            target = current + timedelta(days=days)
            return _combine(target, clock or part_clock or dt_time(10, 0))

    if clock:
        target = _combine(current, clock)
        return target if target > current else target + timedelta(days=1)

    if part_clock:
        target = _combine(current, part_clock)
        return target if target > current else target + timedelta(days=1)

    return current + timedelta(hours=2)


def detect_followup_intent(text: str) -> str:
    lower = (text or "").strip().lower()
    if not lower:
        return ""
    if any(p in lower for p in ("wrong number", "wrong no", "incorrect number")):
        return "wrong_number"
    if any(p in lower for p in ("not interested", "no need", "dont call", "don't call", "stop", "remove me")):
        return "not_interested"
    if any(p in lower for p in ("reschedule", "change time", "change the time")):
        return "reschedule_request"
    if any(p in lower for p in ("send details", "price", "package", "brochure", "catalog", "catalogue", "details")):
        return "details_request"
    if any(p in lower for p in ("book demo", "demo", "meeting", "google meet", "appointment")):
        return "demo_request"
    if "message" in lower and (any(p in lower for p in ("tomorrow", "later", "after", "morning", "evening")) or re.search(r"\bin\s+\d+\s*(minute|minutes|min|mins|hour|hours|hr|hrs)\b", lower)):
        return "message_later"
    if "call" in lower and (any(p in lower for p in ("later", "after", "tomorrow", "morning", "evening", "busy")) or re.search(r"\bin\s+\d+\s*(minute|minutes|min|mins|hour|hours|hr|hrs)\b", lower)):
        return "callback_request"
    if "busy" in lower or lower == "later":
        return "busy"
    if any(p in lower for p in ("yes", "ok", "okay", "interested", "tell me")):
        return "positive_interest"
    return ""


def iso_utcish(value: datetime) -> str:
    return value.isoformat()


FOLLOWUP_TIME_PARSER_SMOKE_EXAMPLES = [
    ("call me after 30 minutes", "relative +30 minutes"),
    ("call me in 30 minutes", "relative +30 minutes"),
    ("message me tomorrow morning", "tomorrow 09:00"),
    ("call tomorrow at 11 am", "tomorrow 11:00"),
    ("I need 10 cabinets", "no clock parse; fallback applies"),
    ("budget 5 lakh", "no clock parse; fallback applies"),
]
