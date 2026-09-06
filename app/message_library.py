from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any


class MessageLibraryError(ValueError):
    pass


def load_message_library(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MessageLibraryError(f"消息库文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise MessageLibraryError(f"消息库文件格式错误: {path}") from exc
    if not isinstance(raw, list):
        raise MessageLibraryError("消息库必须是 JSON 数组")
    return raw


def _match_date_range(date_str: str, today: date) -> bool:
    today_str = today.isoformat()
    if date_str == today_str:
        return True
    range_match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", date_str)
    if range_match:
        start, end = range_match.groups()
        return start <= today_str <= end
    return False


def _match_weekday(days: Any, today: date) -> bool:
    if not isinstance(days, list):
        return False
    wd = today.weekday()
    return wd in days


def _match_holiday(holiday_id: str, today: date) -> bool:
    holidays = {
        "new_year": (1, 1),
        "spring_festival": None,
        "labour": (5, 1),
        "national": (10, 1),
    }
    if holiday_id not in holidays:
        return False
    month, day = holidays[holiday_id]
    return (today.month, today.day) == (month, day)


def select_messages(library: list[dict[str, Any]], today: date | None = None) -> list[str]:
    if today is None:
        today = date.today()
    messages = []
    for entry in library:
        if not isinstance(entry, dict):
            continue
        dates = entry.get("date") or entry.get("dates")
        weekday = entry.get("weekday")
        holiday = entry.get("holiday")
        categories = entry.get("categories")
        content = entry.get("content") or entry.get("message")
        if content is None:
            continue
        if not _match_date_range(dates, today) and dates is not None:
            continue
        if weekday is not None and not _match_weekday(weekday, today):
            continue
        if holiday is not None and not _match_holiday(holiday, today):
            continue
        if categories is not None:
            msg_cats = entry.get("message_categories") or []
            if not any(c in msg_cats for c in categories):
                continue
        messages.append(content)
    return messages
