from datetime import UTC, datetime


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    # Radarr/Sonarr serialize DateTime as ISO 8601. Normalize "Z" for fromisoformat.
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def age_days(value: str | None, now: datetime) -> float | None:
    parsed = parse_iso(value)
    if parsed is None:
        return None
    delta = now - parsed
    return delta.total_seconds() / 86400
