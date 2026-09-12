from __future__ import annotations

import re
from datetime import timedelta
from urllib.parse import urlparse

_DURATION = re.compile(r"^(?P<number>[1-9]\d*)\s*(?P<unit>[mhd])$", re.I)


def parse_duration(value: str) -> timedelta | None:
    match = _DURATION.fullmatch(value.strip())
    if not match:
        return None
    number = int(match["number"])
    unit = match["unit"].lower()
    if unit == "m":
        return timedelta(minutes=number)
    if unit == "h":
        return timedelta(hours=number)
    return timedelta(days=number)


def is_reasonable_url(value: str) -> bool:
    candidate = value.strip()
    if candidate.startswith("t.me/"):
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def normalise_url(value: str) -> str:
    value = value.strip()
    return "https://" + value if value.startswith("t.me/") else value


def normalise_telegram_url(value: str) -> str | None:
    candidate = value.strip()
    if candidate.startswith('@'):
        candidate = 'https://t.me/' + candidate[1:]
    elif candidate.startswith('t.me/'):
        candidate = 'https://' + candidate
    parsed = urlparse(candidate)
    if parsed.scheme not in {'http', 'https'} or parsed.netloc.lower() not in {'t.me', 'www.t.me', 'telegram.me', 'www.telegram.me'} or not parsed.path.strip('/'):
        return None
    return candidate
