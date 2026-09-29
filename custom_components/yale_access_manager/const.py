"""Public integration constants and safe errors."""

DOMAIN = "yale_access_manager"
NAME = "Yale Access Manager"
CONF_SOURCE = "august_entry_id"
CONF_DEVICE = "device_id"
CONF_LOCK = "lock_id"
ACCESS_TYPES = ("always", "temporary", "recurring")
DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


class AccessError(Exception):
    """An error code safe to display; never attach a server response or PIN."""

    def __init__(self, code: str, *, uncertain: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain
