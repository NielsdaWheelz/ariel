from __future__ import annotations

import re
from typing import Any


_SECRET_LIKE_PATTERN = re.compile(
    (
        r"(sk-[A-Za-z0-9_\-]{8,}"
        r"|api[_-]?key"
        r"|secret(?:[_-]?(?:key|value))?"
        r"|authorization"
        r"|bearer\s+[A-Za-z0-9\-_.]+"
        r"|token\s*[:=]\s*[A-Za-z0-9\-_.]+)"
    ),
    re.IGNORECASE,
)


def safe_failure_reason(raw_message: str, *, fallback: str) -> str:
    candidate = raw_message.strip()
    if not candidate:
        return fallback
    if _SECRET_LIKE_PATTERN.search(candidate):
        return fallback
    return candidate[:500]


def redact_text(value: str) -> str:
    return _SECRET_LIKE_PATTERN.sub("[REDACTED]", value)


def redact_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(key): redact_json_value(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [redact_json_value(item) for item in value]
    return value
