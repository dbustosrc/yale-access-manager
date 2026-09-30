"""Public integration constants and safe errors."""

DOMAIN = "yale_access_manager"
NAME = "Yale Access Manager"
CONF_SOURCE = "august_entry_id"
CONF_DEVICE = "device_id"
CONF_LOCK = "lock_id"
ACCESS_TYPES = ("always", "temporary", "recurring")
DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
EVENT_TYPES = ("created", "updated", "disabled", "enabled", "keypad_unlock", "issued", "consumed", "expired", "deleted", "delivery_confirmed")


class AccessError(Exception):
    """Internal code plus an optional sanitized original Yale error."""

    def __init__(self, code: str, *, uncertain: bool = False, detail: str | None = None) -> None:
        super().__init__(detail or code)
        self.code = code
        self.uncertain = uncertain
        self.detail = detail
