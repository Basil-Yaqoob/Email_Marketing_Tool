"""Out-of-office return date parsing.

Handles absolute ("until 2026-09-01"), relative ("back Monday"), and
duration ("on leave for two weeks") phrasings.
Relative dates resolve against the received date, not "now".
Sanity clamp rejects anything >90 days out or in the past.
"""

# ruff: noqa: SIM102, SIM103

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def parse_return_date(body: str, received: datetime) -> date | None:
    """Extract return date from OOO message.

    Args:
        body: The email body
        received: When the message was received (used for relative date resolution)

    Returns:
        Parsed return date or None if unrecognizable or invalid
    """
    # Try absolute date first (ISO, YYYY-MM-DD, Month DD)
    absolute = _parse_absolute_date(body)
    if absolute:
        if _is_valid_return_date(absolute, received):
            return absolute

    # Try relative date ("back Monday", "back on the 15th")
    relative = _parse_relative_date(body, received)
    if relative:
        if _is_valid_return_date(relative, received):
            return relative

    # Try duration ("on leave for two weeks", "away for 3 days")
    duration = _parse_duration_date(body, received)
    if duration:
        if _is_valid_return_date(duration, received):
            return duration

    return None


def _parse_absolute_date(body: str) -> date | None:
    """Parse absolute dates like '2026-09-01' or 'September 1, 2026'."""
    # ISO format first (YYYY-MM-DD) - must come before DD patterns to avoid conflict
    iso_match = re.search(
        r"back\s+on\s+(\d{4})-(\d{1,2})-(\d{1,2})|(\d{4})-(\d{1,2})-(\d{1,2})", body
    )
    if iso_match:
        try:
            # Either group 1-3 (with "back on" prefix) or group 4-6 (standalone)
            if iso_match.group(1):
                year, month, day = (
                    int(iso_match.group(1)),
                    int(iso_match.group(2)),
                    int(iso_match.group(3)),
                )
            else:
                year, month, day = (
                    int(iso_match.group(4)),
                    int(iso_match.group(5)),
                    int(iso_match.group(6)),
                )
            return date(year, month, day)
        except (ValueError, TypeError):
            pass

    # Month DD[,] YYYY or DD Month YYYY
    month_names = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "sept": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }

    for month_name, month_num in month_names.items():
        # "September 1, 2026" or "1 September 2026"
        pattern = rf"(?:until\s+)?({month_name})\s+(\d{{1,2}}),?\s+(\d{{4}})"
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            try:
                day, year = int(match.group(2)), int(match.group(3))
                return date(year, month_num, day)
            except ValueError:
                pass

    return None


def _parse_relative_date(body: str, received: datetime) -> date | None:
    """Parse relative dates like 'back Monday' or 'back on the 15th'."""
    today = received.date()
    days_ahead = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }

    # "back Monday" or "back on Monday"
    for day_name, day_offset in days_ahead.items():
        pattern = rf"(?:back|returning)\s+(?:on\s+)?{day_name}"
        if re.search(pattern, body, re.IGNORECASE):
            # Find the next occurrence of that weekday
            current_weekday = today.weekday()
            days_until = (day_offset - current_weekday) % 7
            if days_until == 0:
                days_until = 7  # If today is that day, next week
            return today + timedelta(days=days_until)

    # "back on the 15th" or "back the 15th"
    day_match = re.search(
        r"back\s+(?:on\s+)?(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?", body, re.IGNORECASE
    )
    if day_match:
        day = int(day_match.group(1))
        # Assume current month unless the day is before today
        month, year = today.month, today.year
        if day < today.day:
            month += 1
            if month > 12:
                month = 1
                year += 1
        try:
            return date(year, month, day)
        except ValueError:
            pass

    return None


def _parse_duration_date(body: str, received: datetime) -> date | None:
    """Parse durations like 'on leave for two weeks' or 'away for 3 days'."""
    today = received.date()

    # "for N days/weeks/etc"
    duration_match = re.search(
        r"(?:for|the\s+next)\s+(\d+)\s+(day|week|month)", body, re.IGNORECASE
    )
    if duration_match:
        count = int(duration_match.group(1))
        unit = duration_match.group(2).lower()

        if unit.startswith("day"):
            return today + timedelta(days=count)
        elif unit.startswith("week"):
            return today + timedelta(weeks=count)
        elif unit.startswith("month"):
            # Approximate: add 30 days per month
            return today + timedelta(days=count * 30)

    return None


def _is_valid_return_date(parsed_date: date, received: datetime) -> bool:
    """Sanity check: reject dates >90 days out or in the past."""
    today = received.date()

    # Reject dates in the past
    if parsed_date < today:
        return False

    # Reject dates more than 90 days in the future
    max_future = today + timedelta(days=90)
    if parsed_date > max_future:
        return False

    return True


def next_business_day_after(target: date) -> date:
    """Return the next business day after the target date.

    Skips weekends (Saturday=5, Sunday=6).
    """
    day = target + timedelta(days=1)
    while day.weekday() >= 5:  # Saturday or Sunday
        day += timedelta(days=1)
    return day


__all__ = ["next_business_day_after", "parse_return_date"]
