"""Validate input and expose only explicitly selected access metadata."""

from datetime import datetime, time, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .const import ACCESS_TYPES, DAYS, AccessError


def utc(value: str, timezone_name: str = "UTC") -> str:
    """Native local date inputs use HA's timezone; ambiguous DST needs an offset."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            local = parsed.replace(tzinfo=ZoneInfo(timezone_name))
            if (local.utcoffset() != local.replace(fold=1).utcoffset()
                    or local.astimezone(timezone.utc).astimezone(local.tzinfo).replace(tzinfo=None) != parsed):
                raise ValueError
            parsed = local
        return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, TypeError, AttributeError, ZoneInfoNotFoundError):
        raise AccessError("invalid_schedule") from None


def command(data: dict, *, now: datetime | None = None, timezone_name: str = "UTC") -> dict:
    """Build a validated load command before any existing access is removed."""
    pin, name = data.get("pin", ""), data.get("name", "")
    kind = data.get("access_type", "temporary")
    if not isinstance(pin, str) or not re.fullmatch(r"[0-9]{4,8}", pin):
        raise AccessError("invalid_pin")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80 or any(ord(char) < 32 for char in name):
        raise AccessError("invalid_name")
    name = name.strip()
    if kind not in ACCESS_TYPES:
        raise AccessError("invalid_schedule")
    first, _, last = name.partition(" ")
    result = {"pin": pin, "firstName": first, "lastName": last,
              "action": "load", "accessType": kind, "retry": False}
    if kind == "temporary":
        start, end = utc(data.get("starts_at", ""), timezone_name), utc(data.get("ends_at", ""), timezone_name)
        if start >= end or datetime.fromisoformat(end.replace("Z", "+00:00")) <= (now or datetime.now(timezone.utc)):
            raise AccessError("invalid_schedule")
        result["accessTimes"] = f"DTSTART={start};DTEND={end}"
    elif kind == "recurring":
        try:
            start = time.fromisoformat(data.get("start_time", ""))
            end = time.fromisoformat(data.get("end_time", ""))
            days = data.get("weekdays", [])
            if (start.tzinfo or end.tzinfo or start >= end or start.second or end.second
                    or start.microsecond or end.microsecond or not days or not isinstance(days, list)
                    or any(day not in DAYS for day in days)):
                raise ValueError
        except (ValueError, TypeError):
            raise AccessError("invalid_schedule") from None
        result["accessTimes"] = f"STARTSEC={start.hour * 3600 + start.minute * 60};ENDSEC={end.hour * 3600 + end.minute * 60}"
        result["accessRecurrence"] = "FREQ=WEEKLY;INTERVAL=1;BYDAY=" + ",".join(day for day in DAYS if day in days)
    return result


def records(groups: dict) -> list[dict]:
    """Include every lifecycle group, not just loaded entries."""
    if not isinstance(groups, dict):
        raise AccessError("invalid_response")
    result = []
    for group in ("created", "loaded", "disabled", "disabling", "enabling", "deleting", "updating"):
        values = groups.get(group, [])
        if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
            raise AccessError("invalid_response")
        result.extend(values)
    return result


def metadata(pin: dict) -> dict:
    """Allowlist public response fields; no PIN, contact details or account IDs."""
    if any(value is not None and not isinstance(value, str) for value in
           (pin.get(key) for key in ("firstName", "lastName", "accessType", "state", "accessTimes", "accessRecurrence"))):
        raise AccessError("invalid_response")
    return {"name": " ".join(filter(None, (pin.get("firstName"), pin.get("lastName")))),
            "access_type": pin.get("accessType"), "state": pin.get("state"),
            "schedule": pin.get("accessTimes"), "recurrence": pin.get("accessRecurrence")}


def saved(command_data: dict) -> dict:
    """Only non-secret metadata is persisted in the operation journal."""
    return {key: command_data[key] for key in
            ("firstName", "lastName", "accessType", "accessTimes", "accessRecurrence")
            if key in command_data and command_data[key] is not None}


def matches(pin: dict, desired: dict) -> bool:
    return (pin.get("state") == "loaded" and pin.get("pin") == desired["pin"]
            and all((pin.get(key) or "") == value if key in ("firstName", "lastName") else pin.get(key) == value
                    for key, value in saved(desired).items()))
