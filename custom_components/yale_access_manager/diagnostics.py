"""Allowlisted diagnostics without account identities, guest names or secrets."""

from .const import DOMAIN


async def async_get_config_entry_diagnostics(hass, entry):
    manager = entry.runtime_data
    return {"integration": DOMAIN, "version": "0.3.0", "cloud_polling": True,
            "last_sync": manager.last_sync.isoformat() if manager.last_sync else None,
            "last_error_code": manager.last_error, "history_error_code": manager.history_error,
            "history_gap": manager.history_gap, "supports_schedules": manager.supports_schedules,
            "external_link_count": len(manager.external_links),
            "managed_accesses": [{"operation": record["operation"], "linked": bool(record.get("person_unique_id")),
                                  "temporary_lease": bool(record.get("lease")), "notification_status": record.get("notification_status"),
                                  "phase": record.get("operation_info", {}).get("phase"),
                                  "started_at": record.get("operation_info", {}).get("started_at"),
                                  "error_code": record.get("operation_info", {}).get("error_code")}
                                 for record in manager.accesses.values()]}
