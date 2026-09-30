"""Per-lock operations, metadata polling and a durable non-secret journal."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import re
import uuid
import secrets
from copy import deepcopy
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import slugify

from .const import DOMAIN, NAME, AccessError
from .models import command, matches, metadata, records, saved

_LOGGER = logging.getLogger(__name__)


class AccessManager(DataUpdateCoordinator[dict]):
    """One config entry and one operation lock per physical Yale lock."""

    def __init__(self, hass, entry, api, *, store=None) -> None:
        super().__init__(hass, _LOGGER, name=NAME, update_interval=None, config_entry=entry)
        self.api, self.entry = api, entry
        self.lock_id = entry.data["lock_id"]
        self.device_id = entry.data["device_id"]
        # Physical lock key survives removing/re-adding a config entry; metadata contains no PIN.
        self.store = store or Store(hass, 1, f"{DOMAIN}.{sha256(self.lock_id.encode()).hexdigest()}",
                                   private=True, atomic_writes=True)
        self.accesses: dict[str, dict] = {}
        self.external_links: dict[str, dict] = {}
        self.operation_lock = asyncio.Lock()
        self.poll_interval = 5
        self.poll_timeout = 180
        self.supports_schedules = False
        self.house_id = None
        self.seen_activities = None
        self.last_sync = None
        self.last_attempt = None
        self.last_error = None
        self.history_error = None
        self.history_gap = False
        self.identity_problem = False
        self.problem = False
        self._saved_data = None
        self._cancel_timer = None
        self._issues = set()

    def start(self) -> None:
        """Maintenance must keep running even when diagnostic entities are disabled."""
        self._cancel_timer = async_track_time_interval(self.hass, self._tick, timedelta(seconds=15))

    async def _tick(self, now) -> None:
        interval = 15 if any(record.get("lease") for record in self.accesses.values()) else 60
        if not self.operation_lock.locked() and (self.last_attempt is None or (now - self.last_attempt).total_seconds() >= interval):
            await self.async_refresh()

    async def load(self) -> None:
        data = await self.store.async_load()
        if data is not None:
            if not isinstance(data, dict) or not isinstance(data.get("accesses"), dict):
                raise AccessError("journal_invalid")
            self.accesses = data["accesses"]
            self._saved_data = deepcopy(data)
            self.external_links = data.get("external_links", {})
            if not isinstance(self.external_links, dict):
                raise AccessError("journal_invalid")
            for key, value in self.external_links.items():
                if (not isinstance(key, str) or not key.startswith("external:") or not key[9:]
                        or not isinstance(value, dict) or not {"user_id", "person_unique_id"} <= set(value)
                        or set(value) - {"user_id", "person_unique_id", "notification_entry_id"}
                        or any(not isinstance(field, str) or not field for field in value.values())):
                    raise AccessError("journal_invalid")
            self.seen_activities = data.get("seen_activities")
            if self.seen_activities is not None and (not isinstance(self.seen_activities, list)
                    or any(not isinstance(value, str) for value in self.seen_activities)):
                raise AccessError("journal_invalid")
            for key, value in self.accesses.items():
                if (not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key) or not isinstance(value, dict)
                        or not {"partner_id", "metadata", "operation"} <= set(value)
                        or set(value) - {"partner_id", "metadata", "operation", "user_id", "person_unique_id", "notification_entry_id", "lease", "visit", "operation_info", "notification_status"}
                        or value.get("partner_id") != f"yam-{key}"
                        or not isinstance(value.get("metadata"), dict)
                        or set(value["metadata"]) - {"firstName", "lastName", "accessType", "accessTimes", "accessRecurrence"}
                        or value.get("operation") not in ("ready", "creating", "updating", "deleting", "unknown", "disabling", "enabling")):
                    raise AccessError("journal_invalid")
                for field in ("user_id", "person_unique_id", "notification_entry_id"):
                    if field in value and (not isinstance(value[field], str) or not value[field]):
                        raise AccessError("journal_invalid")
                if value.get("notification_status") not in (None, "requested", "confirmed", "unconfirmed", "failed"):
                    raise AccessError("journal_invalid")
                info = value.get("operation_info")
                if info is not None:
                    if (not isinstance(info, dict) or not {"phase", "started_at"} <= set(info)
                            or set(info) - {"phase", "started_at", "expected_state", "transaction_id", "error_code", "error_detail"}
                            or any(not isinstance(field, str) for field in info.values())
                            or info.get("expected_state") not in (None, "loaded", "disabled")):
                        raise AccessError("journal_invalid")
                    try:
                        if not datetime.fromisoformat(info["started_at"]).tzinfo:
                            raise ValueError
                    except (ValueError, TypeError):
                        raise AccessError("journal_invalid") from None
                if value.get("lease") is not None:
                    lease = value["lease"]
                    if (not isinstance(lease, dict) or not {"started_at", "expires_at", "revoke"} <= set(lease)
                            or set(lease) - {"started_at", "expires_at", "revoke", "disable_requested", "receipt_action", "language"}
                            or type(lease["revoke"]) is not bool
                            or type(lease.get("disable_requested", False)) is not bool):
                        raise AccessError("journal_invalid")
                    if ("receipt_action" in lease and (not isinstance(lease["receipt_action"], str)
                            or not re.fullmatch(r"YALE_RECEIVED_[0-9a-f]{32}", lease["receipt_action"]))) or lease.get("language") not in (None, "es", "en"):
                        raise AccessError("journal_invalid")
                    try:
                        if not all(datetime.fromisoformat(lease[field]).tzinfo for field in ("started_at", "expires_at")):
                            raise ValueError
                    except (ValueError, TypeError, KeyError):
                        raise AccessError("journal_invalid") from None
                if value.get("visit") is not None:
                    visit = value["visit"]
                    if (not isinstance(visit, dict) or set(visit) != {"open", "block_until"}
                            or type(visit["open"]) is not bool):
                        raise AccessError("journal_invalid")
                    try:
                        if not datetime.fromisoformat(visit["block_until"]).tzinfo:
                            raise ValueError
                    except (ValueError, TypeError):
                        raise AccessError("journal_invalid") from None
        locks = await self.api.locks()
        if self.lock_id not in locks or locks[self.lock_id].get("UserType") != "superuser":
            raise AccessError("access_denied")
        detail = await self.api.detail(self.lock_id)
        if not detail.get("supportsEntryCodes"):
            raise AccessError("unsupported_lock")
        self.supports_schedules = bool(detail.get("accessSchedulesAllowed"))
        self.house_id = detail.get("HouseID")
        if not isinstance(self.house_id, str) or not self.house_id:
            raise AccessError("invalid_response")

    async def save(self) -> None:
        data = {"accesses": self.accesses, "external_links": self.external_links, "seen_activities": self.seen_activities}
        if data != self._saved_data:
            snapshot = deepcopy(data)
            await self.store.async_save(snapshot)
            self._saved_data = snapshot

    def sync_issues(self) -> None:
        """Use native Repairs without exposing PINs or replaying ambiguous writes."""
        links = {**self.accesses, **self.external_links}
        invalid_links = False
        for key, record in links.items():
            if not record.get("person_unique_id"):
                continue
            person_id = self.person_entity(key)
            state = self.hass.states.get(person_id) if person_id else None
            invalid_links |= state is None
            if record.get("notification_entry_id"):
                phone = self.hass.config_entries.async_get_entry(record["notification_entry_id"])
                invalid_links |= (phone is None or phone.domain != "mobile_app" or phone.state is not ConfigEntryState.LOADED
                                  or state is None or not state.attributes.get("user_id") or phone.data.get("user_id") != state.attributes["user_id"])
        flags = {"pending_access": any(record["operation"] != "ready" for record in self.accesses.values()),
                 "identity_changed": self.identity_problem, "invalid_link": invalid_links,
                 "history_unavailable": bool(self.history_error), "history_gap": self.history_gap, "cloud_unavailable": bool(self.last_error),
                 "notification_unconfirmed": any(record.get("lease") and record.get("notification_status") in ("unconfirmed", "failed") for record in self.accesses.values())}
        for kind, active in flags.items():
            issue_id = f"{self.entry.entry_id}_{kind}"
            if active:
                self._issues.add(issue_id)
                ir.async_create_issue(self.hass, DOMAIN, issue_id, is_fixable=False, is_persistent=False,
                                      severity=ir.IssueSeverity.ERROR, translation_key=kind,
                                      translation_placeholders={"lock": getattr(self.entry, "title", NAME)},
                                      learn_more_url="https://github.com/dbustosrc/yale-access-manager#troubleshooting")
            else:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                self._issues.discard(issue_id)
        self.problem = any(flags.values())

    def start_operation(self, access_id: str, phase: str, expected_state: str = "loaded") -> None:
        self.accesses[access_id]["operation_info"] = {"phase": phase, "started_at": datetime.now(timezone.utc).isoformat(), "expected_state": expected_state}

    async def write(self, access_id: str, payload: dict) -> None:
        record = self.accesses[access_id]
        if "operation_info" not in record:
            self.start_operation(access_id, payload["action"])
        info = record["operation_info"]
        info["phase"] = payload["action"]
        await self.save()
        try:
            result = await self.api.write(self.lock_id, payload)
        except AccessError as exc:
            info["error_code"] = exc.code
            if exc.detail:
                info["error_detail"] = exc.detail
            await self.save()
            raise
        transaction = result.get("transactionID")
        if isinstance(transaction, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", transaction):
            info["transaction_id"] = transaction
        info.pop("error_code", None)
        info.pop("error_detail", None)
        await self.save()

    def person_entity(self, access_id: str) -> str | None:
        record = self.accesses.get(access_id, self.external_links.get(access_id, {}))
        unique_id = record.get("person_unique_id")
        return er.async_get(self.hass).async_get_entity_id("person", "person", unique_id) if unique_id else None

    def emit(self, access_id: str, kind: str, **extra) -> None:
        record = self.accesses.get(access_id, self.external_links.get(access_id, {}))
        async_dispatcher_send(self.hass, f"{DOMAIN}_{self.lock_id}_activity", {
            "event_type": kind, "access_id": access_id, "yale_user_id": record.get("user_id"),
            "person_entity_id": self.person_entity(access_id),
            "occurred_at": datetime.now(timezone.utc).isoformat(), **extra,
        })

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
        pin = found[0] if found else None
        if pin is not None:
            user_id = pin.get("userID")
            if not isinstance(user_id, str) or not user_id:
                raise AccessError("invalid_response")
            if record.get("user_id", user_id) != user_id:
                raise AccessError("identity_changed", uncertain=True)
        return pin

    async def list_accesses(self) -> dict:
        items = await self.raw()
        known = {record["partner_id"]: key for key, record in self.accesses.items()}
        result = []
        present = set()
        self.identity_problem = False
        for pin in items:
            key = known.get(pin.get("partnerUserID"))
            external_id = f"external:{pin.get('_id', '')}"
            external_link = self.external_links.get(external_id, {})
            identity_valid = (self.accesses[key].get("user_id", pin.get("userID")) == pin.get("userID")) if key else not external_link or external_link.get("user_id") == pin.get("userID")
            self.identity_problem |= not identity_valid
            linked_id = (key or external_id) if identity_valid else None
            if key:
                present.add(key)
            result.append({**metadata(pin), "access_id": key or external_id,
                           "managed": key is not None,
                           "operation": self.accesses[key]["operation"] if key else "read_only",
                           "person_entity_id": self.person_entity(linked_id) if linked_id else None,
                           "expires_at": (self.accesses[key].get("lease") or {}).get("expires_at") if key else None,
                           "identity_valid": identity_valid,
                           "notification_status": self.accesses[key].get("notification_status") if key else None,
                           "operation_info": dict(self.accesses[key].get("operation_info", {})) if key else {}})
        for key, record in self.accesses.items():
            if key not in present:
                result.append({**metadata(record["metadata"]), "access_id": key, "managed": True,
                               "state": "not_found", "operation": record["operation"], "identity_valid": True,
                               "operation_info": dict(record.get("operation_info", {})), "expires_at": (record.get("lease") or {}).get("expires_at"),
                               "person_entity_id": self.person_entity(key), "notification_status": record.get("notification_status")})
        return {"accesses": result, "supports_schedules": self.supports_schedules,
                "recurring_timezone": "lock_timezone"}

    async def _async_update_data(self) -> dict:
        self.last_attempt = datetime.now(timezone.utc)
        try:
            if not self.operation_lock.locked():
                await self.maintain()
            listing = await self.list_accesses()
        except AccessError as exc:
            self.last_error = exc.code
            self.sync_issues()
            raise UpdateFailed(str(exc)) from None
        items = listing["accesses"]
        self.last_error = None
        self.last_sync = datetime.now(timezone.utc)
        self.sync_issues()
        return {"total": sum(item["state"] != "not_found" for item in items),
                "managed": sum(item["managed"] for item in items),
                "pending": sum(record["operation"] != "ready" for record in self.accesses.values()),
                "accesses": {item["access_id"]: item for item in items}, "last_sync": self.last_sync, "problem": self.problem}

    async def wait(self, access_id: str, predicate) -> dict | None:
        deadline = asyncio.get_running_loop().time() + self.poll_timeout
        while True:
            pin = self.owned(await self.raw(), access_id)
            if predicate(pin):
                return pin
            if asyncio.get_running_loop().time() >= deadline:
                detail = (f"Yale completion unconfirmed after {self.poll_timeout} seconds. "
                          f"Access ID: {access_id}; last Yale state: {pin.get('state') if pin else 'not_found'}. "
                          "Inspect this access before another write; do not repeat creation.")
                _LOGGER.error("%s", detail)
                raise AccessError("outcome_unknown", uncertain=True, detail=detail)
            await asyncio.sleep(self.poll_interval)

    def validate(self, data: dict, items: list[dict], existing: dict | None = None) -> dict:
        desired = command(data, timezone_name=self.hass.config.time_zone)
        if desired["accessType"] != "always" and not self.supports_schedules:
            raise AccessError("unsupported_schedule")
        if any(pin.get("pin") == desired["pin"] and pin is not existing for pin in items):
            raise AccessError("duplicate_pin")
        return desired

    async def load_pin(self, access_id: str, desired: dict) -> None:
        await self.write(access_id, {**desired, "partnerUserID": self.accesses[access_id]["partner_id"]})
        try:
            pin = await self.wait(access_id, lambda pin: pin is not None and matches(pin, desired))
        except AccessError as exc:
            raise AccessError(exc.code, uncertain=True, detail=exc.detail) from None
        user_id = pin.get("userID") if pin else None
        if not isinstance(user_id, str) or not user_id:
            raise AccessError("invalid_response", uncertain=True)
        self.accesses[access_id]["user_id"] = user_id

    async def remove_pin(self, access_id: str, pin: dict) -> None:
        if not isinstance(pin.get("pin"), str):
            raise AccessError("invalid_response")
        await self.write(access_id, {"partnerUserID": self.accesses[access_id]["partner_id"],
                             "pin": pin["pin"], "accessType": pin["accessType"],
                             "action": "delete", "retry": False})
        try:
            await self.wait(access_id, lambda value: value is None)
        except AccessError as exc:
            raise AccessError(exc.code, uncertain=True, detail=exc.detail) from None

    async def failed(self, access_id: str, error: AccessError | None = None) -> None:
        self.accesses[access_id]["operation"] = "unknown"
        if error is not None and self.accesses[access_id].get("operation_info"):
            info = self.accesses[access_id]["operation_info"]
            info["error_code"] = error.code
            if error.detail:
                info["error_detail"] = error.detail
        await self.save()

    def ensure_idle(self) -> None:
        pending = [f"{key} ({record['operation']})" for key, record in self.accesses.items() if record["operation"] != "ready"]
        if pending:
            raise AccessError("operation_pending", detail="Unresolved Yale operation: " + ", ".join(pending)
                              + ". Reconcile a loaded access or explicitly cancel the pending access before creating another.")

    async def create(self, data: dict) -> str:
        async with self.operation_lock:
            self.ensure_idle()
            desired = self.validate(data, await self.raw())
            access_id = uuid.uuid4().hex
            self.accesses[access_id] = {"partner_id": f"yam-{access_id}",
                                        "metadata": saved(desired), "operation": "creating"}
            self.start_operation(access_id, "creating")
            await self.save()  # Journal first: an accepted request must survive a restart.
            try:
                await self.load_pin(access_id, desired)
            except asyncio.CancelledError:
                # The saved creating phase remains recoverable without storing the PIN.
                raise
            except AccessError as exc:
                if exc.uncertain:
                    await self.failed(access_id, exc)
                else:
                    del self.accesses[access_id]
                    await self.save()
                raise
            self.accesses[access_id]["operation"] = "ready"
            await self.save()
            self.emit(access_id, "created")
        await self.async_request_refresh()
        return access_id

    async def update(self, access_id: str, data: dict) -> None:
        async with self.operation_lock:
            if self.accesses.get(access_id, {}).get("lease"):
                raise AccessError("operation_pending")
            await self._replace(access_id, data)
        await self.async_request_refresh()

    async def _replace(self, access_id: str, data: dict, *, activate: bool = False) -> None:
        self.ensure_idle()
        items = await self.raw()
        pin = self.owned(items, access_id)
        if pin is None or pin.get("state") not in ("loaded", "disabled"):
            raise AccessError("operation_pending")
        desired = self.validate(data, items, pin)
        previous = {**saved(pin), "pin": pin["pin"], "action": "load", "retry": False}
        old_metadata = self.accesses[access_id]["metadata"]
        self.accesses[access_id].update(metadata=saved(desired), operation="updating")
        self.start_operation(access_id, "updating", "loaded" if activate else pin["state"])
        await self.save()
        try:
            await self.remove_pin(access_id, pin)
            try:
                await self.load_pin(access_id, desired)
            except AccessError as exc:
                if not exc.uncertain and exc.code == "write_rejected":
                    await self.load_pin(access_id, previous)
                    self.accesses[access_id].update(metadata=old_metadata, operation="ready")
                    if pin.get("state") == "disabled":
                        await self._activation(access_id, False)
                    await self.save()
                    raise AccessError("update_rolled_back", detail=(f"{exc.detail}\nPrevious access restored." if exc.detail else None)) from None
                raise
        except AccessError as exc:
            if exc.code != "update_rolled_back":
                await self.failed(access_id, exc)
            raise
        if pin["state"] == "disabled" and not activate:
            try:
                await self._activation(access_id, False)
            except AccessError as exc:
                await self.failed(access_id, exc)
                raise
        self.accesses[access_id]["operation"] = "ready"
        await self.save()
        self.emit(access_id, "updated")

    async def bind_person(self, access_id: str, person_entity_id: str, notification_entry_id: str | None = None, *, clear_notification: bool = False) -> None:
        async with self.operation_lock:
            record = self.accesses.get(access_id)
            items = await self.raw()
            if record is not None:
                pin = self.owned(items, access_id)
            else:
                found = [item for item in items if f"external:{item.get('_id', '')}" == access_id]
                if not access_id.startswith("external:") or len(found) != 1:
                    raise AccessError("invalid_response")
                pin = found[0]
                if any(value["partner_id"] == pin.get("partnerUserID") for value in self.accesses.values()):
                    raise AccessError("not_managed")
                record = self.external_links.get(access_id, {})
            person = er.async_get(self.hass).async_get(person_entity_id)
            state = self.hass.states.get(person_entity_id)
            phone = self.hass.config_entries.async_get_entry(notification_entry_id) if notification_entry_id else None
            if (person is None or person.platform != "person" or not person_entity_id.startswith("person.")
                    or state is None):
                raise AccessError("invalid_person")
            if notification_entry_id and (clear_notification or phone is None or phone.domain != "mobile_app"
                    or phone.state is not ConfigEntryState.LOADED or not state.attributes.get("user_id")
                    or phone.data.get("user_id") != state.attributes["user_id"]):
                raise AccessError("invalid_person")
            links = {**self.accesses, **self.external_links}
            if access_id in self.accesses and any(key != access_id and value.get("person_unique_id") == person.unique_id
                                                 for key, value in self.accesses.items()):
                raise AccessError("ambiguous_person")
            user_id = pin.get("userID") if pin else None
            if not isinstance(user_id, str) or not user_id:
                raise AccessError("invalid_response")
            if any(key != access_id and value.get("user_id") == user_id for key, value in links.items()):
                raise AccessError("ambiguous_person")
            if record.get("user_id", user_id) != user_id:
                raise AccessError("identity_changed")
            if record.get("lease"):
                raise AccessError("operation_pending")
            if record.get("person_unique_id") != person.unique_id:
                record.pop("visit", None)
                record.pop("notification_entry_id", None)
            record.update(user_id=user_id, person_unique_id=person.unique_id)
            if phone is not None:
                record["notification_entry_id"] = phone.entry_id
            elif clear_notification:
                record.pop("notification_entry_id", None)
            if access_id not in self.accesses:
                self.external_links[access_id] = record
            await self.save()
        await self.async_request_refresh()

    async def unbind_person(self, access_id: str) -> None:
        async with self.operation_lock:
            record = self.accesses.get(access_id, self.external_links.get(access_id))
            if record is None:
                raise AccessError("invalid_response")
            if record.get("lease"):
                raise AccessError("operation_pending")
            if access_id in self.external_links:
                del self.external_links[access_id]
            else:
                for field in ("person_unique_id", "notification_entry_id", "visit"):
                    record.pop(field, None)
            await self.save()
        await self.async_request_refresh()

    def notification_service(self, access_id: str) -> str:
        person_id = self.person_entity(access_id)
        state = self.hass.states.get(person_id) if person_id else None
        phone = self.hass.config_entries.async_get_entry(self.accesses[access_id].get("notification_entry_id"))
        if (state is None or not state.attributes.get("user_id") or phone is None
                or phone.domain != "mobile_app" or phone.state is not ConfigEntryState.LOADED
                or phone.data.get("user_id") != state.attributes["user_id"]):
            raise AccessError("invalid_person")
        service = "mobile_app_" + slugify(phone.data.get("device_name", ""))
        if not self.hass.services.has_service("notify", service):
            raise AccessError("invalid_person")
        return service

    def bound_guest(self, person_entity_id: str) -> str:
        keys = [key for key in self.accesses if self.person_entity(key) == person_entity_id]
        if len(keys) != 1:
            raise AccessError("ambiguous_person")
        return keys[0]

    async def begin_visit(self, person_entity_id: str, *, cooldown_minutes: int = 30) -> dict:
        """Reserve one question per visit without creating Home Assistant helpers."""
        async with self.operation_lock:
            if type(cooldown_minutes) is not int or not 5 <= cooldown_minutes <= 120:
                raise AccessError("invalid_schedule")
            key = self.bound_guest(person_entity_id)
            service = self.notification_service(key)
            record = self.accesses[key]
            visit = record.get("visit")
            now = datetime.now(timezone.utc)
            if (visit and (visit["open"] or now < datetime.fromisoformat(visit["block_until"]))) or record.get("lease"):
                return {"allowed": False}
            self.ensure_idle()
            pin = self.owned(await self.raw(), key)
            if pin is None or pin.get("state") != "disabled":
                raise AccessError("operation_pending")
            record["visit"] = {"open": True, "block_until": (now + timedelta(minutes=cooldown_minutes)).isoformat()}
            await self.save()
            return {"allowed": True, "notify_service": f"notify.{service}"}

    async def end_visit(self, person_entity_id: str) -> None:
        async with self.operation_lock:
            key = self.bound_guest(person_entity_id)
            visit = self.accesses[key].get("visit")
            if visit and visit["open"]:
                visit["open"] = False
                await self.save()

    async def set_enabled(self, access_id: str, enabled: bool) -> None:
        async with self.operation_lock:
            if enabled:
                self.ensure_idle()
                if self.accesses.get(access_id, {}).get("lease"):
                    raise AccessError("operation_pending")
            await self._activation(access_id, enabled)
        await self.async_request_refresh()

    async def _activation(self, access_id: str, enabled: bool) -> None:
        record = self.accesses.get(access_id)
        if record is None:
            raise AccessError("not_managed")
        pin = self.owned(await self.raw(), access_id)
        target = "loaded" if enabled else "disabled"
        if pin is None or pin.get("state") not in ("loaded", "disabled"):
            raise AccessError("operation_pending")
        changed = pin.get("state") != target or record["operation"] != "ready"
        if enabled and pin.get("accessType") == "temporary":
            try:
                expiry = pin["accessTimes"].split("DTEND=", 1)[1].split(";", 1)[0]
                if datetime.fromisoformat(expiry.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                    raise ValueError
            except (KeyError, IndexError, ValueError, TypeError):
                raise AccessError("invalid_schedule") from None
        if pin.get("state") != target:
            record["operation"] = "enabling" if enabled else "disabling"
            self.start_operation(access_id, record["operation"], target)
            if not enabled and record.get("lease"):
                record["lease"]["disable_requested"] = True
            await self.save()
            sent = False
            try:
                payload = {key: pin[key] for key in ("pin", "accessType", "accessTimes", "accessRecurrence") if pin.get(key) is not None}
                await self.write(access_id, {**payload, "partnerUserID": record["partner_id"],
                                                    "action": "enable" if enabled else "disable", "retry": False})
                sent = True
                await self.wait(access_id, lambda current: current is not None and current.get("state") == target)
            except AccessError as exc:
                # Only a confirmed rejection permits a later retry. Unknown writes are read back.
                if not exc.uncertain and not sent:
                    record["operation"] = "ready"
                    if record.get("lease"):
                        record["lease"]["disable_requested"] = False
                    await self.save()
                else:
                    await self.failed(access_id, exc)
                raise AccessError(exc.code, uncertain=exc.uncertain or sent, detail=exc.detail) from None
        record["operation"] = "ready"
        if not enabled:
            record.pop("lease", None)
        await self.save()
        if changed:
            self.emit(access_id, "enabled" if enabled else "disabled")

    async def issue_access(self, person_entity_id: str, *, validity_minutes: int = 10, language: str | None = None) -> dict:
        async with self.operation_lock:
            if type(validity_minutes) is not int or not 5 <= validity_minutes <= 30 or language not in (None, "es", "en"):
                raise AccessError("invalid_schedule")
            language = language or ("es" if self.hass.config.language == "es" else "en")
            key = self.bound_guest(person_entity_id)
            service = self.notification_service(key)
            record = self.accesses[key]
            if record.get("lease"):
                raise AccessError("operation_pending")
            self.ensure_idle()
            items = await self.raw()
            existing = self.owned(items, key)
            if existing is None or existing.get("state") != "disabled":
                raise AccessError("operation_pending")
            name = metadata(record["metadata"])["name"]
            used = {item.get("pin") for item in items}
            while True:
                pin = f"{secrets.randbelow(1000000):06d}"
                if pin not in used:
                    break
            start = datetime.now(timezone.utc)
            end = start + timedelta(minutes=validity_minutes)
            receipt_action = f"YALE_RECEIVED_{uuid.uuid4().hex}"
            record["lease"] = {"started_at": start.isoformat(), "expires_at": end.isoformat(),
                               "revoke": False, "disable_requested": False, "receipt_action": receipt_action, "language": language}
            record["notification_status"] = "unconfirmed"
            await self.save()
            try:
                await self._replace(key, {"name": name, "pin": pin, "access_type": "temporary",
                                          "starts_at": start.isoformat(), "ends_at": end.isoformat()}, activate=True)
                if datetime.now(timezone.utc) >= end - timedelta(minutes=2):
                    raise AccessError("invalid_schedule")
                expiry = end.astimezone(ZoneInfo(self.hass.config.time_zone)).strftime('%H:%M')
                await self.hass.services.async_call("notify", service, {
                    "title": "Acceso temporal a la puerta" if language == "es" else "Temporary door access",
                    "message": f"Tu código es {pin}. Vence a las {expiry}." if language == "es" else f"Your code is {pin}. Expires at {expiry}.",
                    "data": {"tag": f"yale-access-{key}", "visibility": "secret", "timeout": max(1, int((end - datetime.now(timezone.utc)).total_seconds())),
                             "actions": [{"action": receipt_action, "title": "He recibido el código" if language == "es" else "I received the code", "authenticationRequired": True}]},
                }, blocking=True)
                record["notification_status"] = "requested"
                await self.save()
            except (Exception, asyncio.CancelledError) as exc:
                record["notification_status"] = "failed"
                if record.get("lease"):
                    record["lease"]["revoke"] = True
                await self.save()
                try:
                    await self._activation(key, False)
                except AccessError:
                    pass
                if isinstance(exc, AccessError) and exc.code == "identity_changed":
                    await self.failed(key)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise AccessError("issue_failed", detail=exc.detail if isinstance(exc, AccessError) else None) from None
            self.emit(key, "issued", expires_at=end.isoformat())
        await self.async_request_refresh()
        return {"access_id": key, "expires_at": end.isoformat(), "receipt_action": receipt_action, "notification_status": "requested"}

    async def confirm_delivery(self, access_id: str, receipt_action: str) -> dict:
        """An explicit recipient response is stronger evidence than a notify service return."""
        async with self.operation_lock:
            record = self.accesses.get(access_id)
            if record is None:
                raise AccessError("not_managed")
            lease = record.get("lease")
            if not lease or datetime.now(timezone.utc) >= datetime.fromisoformat(lease["expires_at"]):
                return {"confirmed": False}
            if not isinstance(receipt_action, str) or not secrets.compare_digest(receipt_action, lease.get("receipt_action", "")):
                raise AccessError("invalid_response")
            self.notification_service(access_id)
            pin = self.owned(await self.raw(), access_id)
            if pin is None or pin.get("state") != "loaded" or lease["revoke"]:
                return {"confirmed": False}
            if record.get("notification_status") != "confirmed":
                record["notification_status"] = "confirmed"
                await self.save()
                self.emit(access_id, "delivery_confirmed")
        await self.async_request_refresh()
        return {"confirmed": True}

    async def maintain(self) -> None:
        async with self.operation_lock:
            if not self.accesses and not self.external_links:
                return
            # Expiry cleanup must run even when the activity endpoint is unavailable.
            await self.cleanup_leases()
            try:
                events = await self.api.activities(self.house_id)
                ids = [event.get("id") for event in events if isinstance(event.get("id"), str)]
                if self.seen_activities and len(events) == 50 and not set(ids).intersection(self.seen_activities):
                    # ponytail: Yale returned at most 100 events; report gaps instead of inventing cursor support.
                    events = await self.api.activities(self.house_id, limit=100)
                    ids = [event.get("id") for event in events if isinstance(event.get("id"), str)]
                    self.history_gap = not set(ids).intersection(self.seen_activities)
                self.history_error = None
            except AccessError as exc:
                if not self.history_error:
                    _LOGGER.warning("Yale activity history unavailable: %s; expiry cleanup remains independent", exc.code)
                self.history_error = exc.code
                return
            if self.seen_activities is None:
                # A restart during a lease must still see its keypad event.
                self.seen_activities = [] if any(record.get("lease") for record in self.accesses.values()) else ids
            for event in reversed(events):
                identifier = event.get("id")
                if not isinstance(identifier, str) or identifier in self.seen_activities:
                    continue
                if event.get("deviceID") != self.lock_id or event.get("action") != "pin_unlock":
                    continue
                user = event.get("callingUser", event.get("user"))
                actor = user.get("UserID") if isinstance(user, dict) else None
                keys = [key for key, value in {**self.accesses, **self.external_links}.items()
                        if actor and value.get("user_id") == actor]
                if len(keys) != 1:
                    continue
                key = keys[0]
                stamp = event.get("timestamp")
                if type(stamp) not in (int, float):
                    continue
                try:
                    observed = datetime.fromtimestamp(stamp / 1000 if stamp > 1e12 else stamp, timezone.utc)
                except (ValueError, OverflowError, OSError):
                    continue
                lease = self.accesses.get(key, {}).get("lease")
                if not lease:
                    self.emit(key, "keypad_unlock", activity_id=identifier, occurred_at=observed.isoformat())
                    continue
                if datetime.fromisoformat(lease["started_at"]) <= observed <= datetime.fromisoformat(lease["expires_at"]):
                    self.emit(key, "keypad_unlock", activity_id=identifier, occurred_at=observed.isoformat())
                    lease["revoke"] = True
                    lease.pop("receipt_action", None)
            # Retain the catch-up window to deduplicate its events on subsequent normal polls.
            self.seen_activities = list(dict.fromkeys(ids + (self.seen_activities or [])))[:200]
            await self.save()
            await self.cleanup_leases()

    async def cleanup_leases(self) -> None:
        now = datetime.now(timezone.utc)
        for key, record in self.accesses.items():
            lease = record.get("lease")
            if lease and record.get("notification_status") == "requested" and now >= datetime.fromisoformat(lease["started_at"]) + timedelta(minutes=2):
                record["notification_status"] = "unconfirmed"
                await self.save()
            if not lease or not (lease["revoke"] or datetime.fromisoformat(lease["expires_at"]) <= now):
                continue
            try:
                if lease.get("disable_requested"):
                    current = self.owned(await self.raw(), key)
                    if current is None or current.get("state") != "disabled":
                        continue
                    record["operation"] = "ready"
                    record.pop("lease")
                    await self.save()
                    self.emit(key, "disabled")
                else:
                    await self._activation(key, False)
            except AccessError as exc:
                _LOGGER.debug("Yale revocation remains unconfirmed for %s: %s", key, exc.code)
                continue
            self.emit(key, "consumed" if lease["revoke"] else "expired")
            try:
                service = self.notification_service(key)
                await self.hass.services.async_call("notify", service, {"message": "clear_notification", "data": {"tag": f"yale-access-{key}"}}, blocking=True)
            except Exception:
                _LOGGER.debug("Yale notification clearing failed for %s", key)

    async def reconcile(self, access_id: str, data: dict) -> None:
        """Confirm an interrupted operation by reading; never repeat a write."""
        async with self.operation_lock:
            items = await self.raw()
            pin = self.owned(items, access_id)
            desired = self.validate(data, items, pin)
            state = self.accesses[access_id].get("operation_info", {}).get("expected_state", "loaded")
            if pin is None or not matches(pin, desired, state=state):
                raise AccessError("operation_pending")
            self.accesses[access_id]["user_id"] = pin["userID"]
            self.accesses[access_id].update(metadata=saved(desired), operation="ready")
            await self.save()
        await self.async_request_refresh()

    async def delete(self, access_id: str, *, cancellation_pin: str | None = None) -> None:
        async with self.operation_lock:
            record = self.accesses.get(access_id)
            if record is None:
                raise AccessError("not_managed")
            pin = self.owned(await self.raw(), access_id)
            if cancellation_pin is not None:
                if not isinstance(cancellation_pin, str) or not re.fullmatch(r"[0-9]{4,8}", cancellation_pin):
                    raise AccessError("invalid_pin")
                if record["operation"] == "ready" or record.get("lease"):
                    raise AccessError("operation_pending")
                if pin is not None and pin.get("pin") != cancellation_pin:
                    raise AccessError("identity_changed")
            if pin is None and record["operation"] != "ready" and cancellation_pin is None:
                raise AccessError("operation_pending")
            record["operation"] = "deleting"
            self.start_operation(access_id, "deleting")
            await self.save()
            try:
                if pin is not None:
                    await self.remove_pin(access_id, pin)
                elif cancellation_pin is not None:
                    # Explicit cancellation targets the original partner, even if its load is absent.
                    await self.write(access_id, {
                        "partnerUserID": record["partner_id"], "pin": cancellation_pin,
                        "accessType": record["metadata"]["accessType"], "action": "delete", "retry": False})
                    await self.wait(access_id, lambda current: current is None)
            except AccessError:
                await self.failed(access_id)
                raise
            self.emit(access_id, "deleted")
            del self.accesses[access_id]
            await self.save()
        await self.async_request_refresh()

    async def close(self) -> None:
        if self._cancel_timer:
            self._cancel_timer()
            self._cancel_timer = None
        for issue_id in self._issues:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        self._issues.clear()
        await self.api.close()
