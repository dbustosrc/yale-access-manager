"""Per-lock operations, metadata polling and a durable non-secret journal."""

import asyncio
from datetime import timedelta
from hashlib import sha256
import re
import uuid

from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, NAME, AccessError
from .models import command, matches, metadata, records, saved


class AccessManager(DataUpdateCoordinator[dict]):
    """One config entry and one operation lock per physical Yale lock."""

    def __init__(self, hass, entry, api, *, store=None) -> None:
        import logging
        super().__init__(hass, logging.getLogger(__name__), name=NAME,
                         update_interval=timedelta(minutes=5), config_entry=entry)
        self.api, self.entry = api, entry
        self.lock_id = entry.data["lock_id"]
        self.device_id = entry.data["device_id"]
        # Physical lock key survives removing/re-adding a config entry; metadata contains no PIN.
        self.store = store or Store(hass, 1, f"{DOMAIN}.{sha256(self.lock_id.encode()).hexdigest()}",
                                   private=True, atomic_writes=True)
        self.accesses: dict[str, dict] = {}
        self.operation_lock = asyncio.Lock()
        self.poll_interval = 5
        self.poll_timeout = 180
        self.supports_schedules = False

    async def load(self) -> None:
        data = await self.store.async_load()
        if data is not None:
            if not isinstance(data, dict) or not isinstance(data.get("accesses"), dict):
                raise AccessError("journal_invalid")
            self.accesses = data["accesses"]
            for key, value in self.accesses.items():
                if (not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key) or not isinstance(value, dict)
                        or set(value) != {"partner_id", "metadata", "operation"}
                        or value.get("partner_id") != f"yam-{key}"
                        or not isinstance(value.get("metadata"), dict)
                        or set(value["metadata"]) - {"firstName", "lastName", "accessType", "accessTimes", "accessRecurrence"}
                        or value.get("operation") not in ("ready", "creating", "updating", "deleting", "unknown")):
                    raise AccessError("journal_invalid")
        locks = await self.api.locks()
        if self.lock_id not in locks or locks[self.lock_id].get("UserType") != "superuser":
            raise AccessError("access_denied")
        detail = await self.api.detail(self.lock_id)
        if not detail.get("supportsEntryCodes"):
            raise AccessError("unsupported_lock")
        self.supports_schedules = bool(detail.get("accessSchedulesAllowed"))

    async def save(self) -> None:
        await self.store.async_save({"accesses": self.accesses})

    async def raw(self) -> list[dict]:
        items = records(await self.api.pins(self.lock_id))
        for item in items:
            metadata(item)
        return items

    def owned(self, items: list[dict], access_id: str) -> dict | None:
        record = self.accesses.get(access_id)
        if record is None:
            raise AccessError("not_managed")
        found = [pin for pin in items if pin.get("partnerUserID") == record["partner_id"]]
        if len(found) > 1:
            raise AccessError("outcome_unknown", uncertain=True)
        return found[0] if found else None

    async def list_accesses(self) -> dict:
        items = await self.raw()
        known = {record["partner_id"]: key for key, record in self.accesses.items()}
        result = []
        present = set()
        for pin in items:
            key = known.get(pin.get("partnerUserID"))
            if key:
                present.add(key)
            result.append({**metadata(pin), "access_id": key or f"external:{pin.get('_id', '')}",
                           "managed": key is not None,
                           "operation": self.accesses[key]["operation"] if key else "read_only"})
        for key, record in self.accesses.items():
            if key not in present:
                result.append({**metadata(record["metadata"]), "access_id": key, "managed": True,
                               "state": "not_found", "operation": record["operation"]})
        return {"accesses": result, "supports_schedules": self.supports_schedules,
                "recurring_timezone": "lock_timezone"}

    async def _async_update_data(self) -> dict:
        try:
            listing = await self.list_accesses()
        except AccessError as exc:
            raise UpdateFailed(exc.code) from None
        items = listing["accesses"]
        return {"total": sum(item["state"] != "not_found" for item in items),
                "managed": sum(item["managed"] for item in items),
                "pending": sum(record["operation"] != "ready" for record in self.accesses.values())}

    async def wait(self, access_id: str, predicate) -> None:
        deadline = asyncio.get_running_loop().time() + self.poll_timeout
        while True:
            pin = self.owned(await self.raw(), access_id)
            if predicate(pin):
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise AccessError("outcome_unknown", uncertain=True)
            await asyncio.sleep(self.poll_interval)

    def validate(self, data: dict, items: list[dict], existing: dict | None = None) -> dict:
        desired = command(data, timezone_name=self.hass.config.time_zone)
        if desired["accessType"] != "always" and not self.supports_schedules:
            raise AccessError("unsupported_schedule")
        if any(pin.get("pin") == desired["pin"] and pin is not existing for pin in items):
            raise AccessError("duplicate_pin")
        return desired

    async def load_pin(self, access_id: str, desired: dict) -> None:
        await self.api.write(self.lock_id, {**desired, "partnerUserID": self.accesses[access_id]["partner_id"]})
        try:
            await self.wait(access_id, lambda pin: pin is not None and matches(pin, desired))
        except AccessError as exc:
            raise AccessError(exc.code, uncertain=True) from None

    async def remove_pin(self, access_id: str, pin: dict) -> None:
        if not isinstance(pin.get("pin"), str):
            raise AccessError("invalid_response")
        await self.api.write(self.lock_id, {"partnerUserID": self.accesses[access_id]["partner_id"],
                             "pin": pin["pin"], "accessType": pin["accessType"],
                             "action": "delete", "retry": False})
        try:
            await self.wait(access_id, lambda value: value is None)
        except AccessError as exc:
            raise AccessError(exc.code, uncertain=True) from None

    async def failed(self, access_id: str) -> None:
        self.accesses[access_id]["operation"] = "unknown"
        await self.save()

    def ensure_idle(self) -> None:
        if any(record["operation"] != "ready" for record in self.accesses.values()):
            raise AccessError("operation_pending")

    async def create(self, data: dict) -> str:
        async with self.operation_lock:
            self.ensure_idle()
            desired = self.validate(data, await self.raw())
            access_id = uuid.uuid4().hex
            self.accesses[access_id] = {"partner_id": f"yam-{access_id}",
                                        "metadata": saved(desired), "operation": "creating"}
            await self.save()  # Journal first: an accepted request must survive a restart.
            try:
                await self.load_pin(access_id, desired)
            except asyncio.CancelledError:
                # The saved creating phase remains recoverable without storing the PIN.
                raise
            except AccessError as exc:
                if exc.uncertain:
                    await self.failed(access_id)
                else:
                    del self.accesses[access_id]
                    await self.save()
                raise
            self.accesses[access_id]["operation"] = "ready"
            await self.save()
        await self.async_request_refresh()
        return access_id

    async def update(self, access_id: str, data: dict) -> None:
        async with self.operation_lock:
            self.ensure_idle()
            items = await self.raw()
            pin = self.owned(items, access_id)
            if pin is None or pin.get("state") != "loaded" or self.accesses[access_id]["operation"] != "ready":
                raise AccessError("operation_pending")
            desired = self.validate(data, items, pin)
            previous = {**saved(pin), "pin": pin["pin"], "action": "load", "retry": False}
            old_metadata = self.accesses[access_id]["metadata"]
            self.accesses[access_id].update(metadata=saved(desired), operation="updating")
            await self.save()
            try:
                await self.remove_pin(access_id, pin)
                try:
                    await self.load_pin(access_id, desired)
                except AccessError as exc:
                    if not exc.uncertain and exc.code == "write_rejected":
                        # Restore only after a definitive rejection; never race an ambiguous write.
                        await self.load_pin(access_id, previous)
                        self.accesses[access_id].update(metadata=old_metadata, operation="ready")
                        await self.save()
                        raise AccessError("update_rolled_back") from None
                    raise
            except AccessError as exc:
                if exc.code != "update_rolled_back":
                    await self.failed(access_id)
                raise
            self.accesses[access_id]["operation"] = "ready"
            await self.save()
        await self.async_request_refresh()

    async def reconcile(self, access_id: str, data: dict) -> None:
        """Confirm an interrupted operation by reading; never repeat a write."""
        async with self.operation_lock:
            items = await self.raw()
            pin = self.owned(items, access_id)
            desired = self.validate(data, items, pin)
            if pin is None or not matches(pin, desired):
                raise AccessError("operation_pending")
            self.accesses[access_id].update(metadata=saved(desired), operation="ready")
            await self.save()
        await self.async_request_refresh()

    async def delete(self, access_id: str) -> None:
        async with self.operation_lock:
            pin = self.owned(await self.raw(), access_id)
            if pin is None and self.accesses[access_id]["operation"] != "ready":
                raise AccessError("operation_pending")
            self.accesses[access_id]["operation"] = "deleting"
            await self.save()
            try:
                if pin is not None:
                    await self.remove_pin(access_id, pin)
            except AccessError:
                await self.failed(access_id)
                raise
            del self.accesses[access_id]
            await self.save()
        await self.async_request_refresh()

    async def close(self) -> None:
        await self.api.close()
