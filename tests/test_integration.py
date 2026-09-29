"""Runnable native HA checks; all credentials and API responses are synthetic."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import time

import yaml

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session

from yale_access_manager import async_setup, async_setup_entry
from yale_access_manager.api import YaleAPI, redact_error
from yale_access_manager.config_flow import AccessOptionsFlow, YaleAccessManagerConfigFlow
from yale_access_manager.const import DOMAIN, AccessError
from yale_access_manager.manager import AccessManager
from yale_access_manager.models import command, matches, records, saved, utc
from yale_access_manager.sensor import AccessCount
from yale_access_manager.event import AccessActivity


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
TEMP = {"name": "Test Guest", "pin": "012345", "access_type": "temporary",
        "starts_at": "2099-01-01T10:00:00-05:00", "ends_at": "2099-01-01T11:00:00-05:00"}


class MemoryStore:
    def __init__(self):
        self.data = None

    async def async_load(self):
        return deepcopy(self.data)

    async def async_save(self, data):
        # No raw PIN value or field can reach persistence.
        if '"pin"' in json.dumps(data) or TEMP["pin"] in json.dumps(data):
            raise AssertionError("A PIN reached persistence")
        self.data = deepcopy(data)


class SimulatedYale:
    def __init__(self):
        self.entries = [{"_id": "external", "userID": "external-user", "pin": "876543",
                         "firstName": "Existing", "lastName": "Guest", "accessType": "always", "state": "loaded"}]
        self.writes = []
        self.next_failure = None
        self.pending = False
        self.closed = False
        self.reject_next_load = False
        self.next_user_id = None
        self.events = []

    async def locks(self):
        return {"lock": {"UserType": "superuser"}}

    async def detail(self, lock_id):
        return {"supportsEntryCodes": True, "accessSchedulesAllowed": True, "HouseID": "test-house"}

    async def activities(self, house_id):
        return deepcopy(self.events)

    async def pins(self, lock_id):
        return {"loaded": deepcopy([pin for pin in self.entries if pin["state"] == "loaded"]),
                "created": deepcopy([pin for pin in self.entries if pin["state"] == "created"]),
                "disabled": deepcopy([pin for pin in self.entries if pin["state"] == "disabled"])}

    async def write(self, lock_id, payload):
        self.writes.append(deepcopy(payload))
        if self.next_failure:
            exc, self.next_failure = self.next_failure, None
            raise exc
        partner_id = payload["partnerUserID"]
        if payload["action"] == "load" and self.reject_next_load:
            self.reject_next_load = False
            raise AccessError("write_rejected")
        if payload["action"] == "delete":
            self.entries = [pin for pin in self.entries if pin.get("partnerUserID") != partner_id]
        elif payload["action"] in ("enable", "disable"):
            for pin in self.entries:
                if pin.get("partnerUserID") == partner_id:
                    pin["state"] = "loaded" if payload["action"] == "enable" else "disabled"
        else:
            user_id = self.next_user_id or "user-" + partner_id
            self.next_user_id = None
            self.entries.append({**payload, "_id": partner_id, "userID": user_id,
                                 "state": "created" if self.pending else "loaded"})
        return {"transactionID": "synthetic-transaction"}

    async def close(self):
        self.closed = True


class ModelChecks(unittest.TestCase):
    def test_schedules_and_secret_boundary(self):
        desired = command(TEMP, now=NOW)
        self.assertEqual(desired["pin"], "012345")
        self.assertEqual(desired["accessTimes"], "DTSTART=2099-01-01T15:00:00.000Z;DTEND=2099-01-01T16:00:00.000Z")
        self.assertNotIn("pin", saved(desired))
        self.assertEqual(utc("2099-01-01T10:00", "America/Guayaquil"), "2099-01-01T15:00:00.000Z")
        for value in ("2026-03-08T02:30", "2026-11-01T01:30"):
            with self.assertRaises(AccessError):
                utc(value, "America/New_York")
        self.assertEqual(utc("2026-11-01T01:30-05:00", "America/New_York"), "2026-11-01T06:30:00.000Z")
        for changes in ({"pin": "１２３４"}, {"pin": "123"}, {"pin": 123456},
                        {"name": ""}, {"name": "Bad\nName"}, {"ends_at": TEMP["starts_at"]},
                        {"ends_at": "2000-01-01T00:00:00Z"}):
            with self.assertRaises(AccessError):
                command({**TEMP, **changes}, now=NOW)
        recurring = command({"name": "Weekly Guest", "pin": "123456", "access_type": "recurring",
                             "weekdays": ["FR", "MO", "MO"], "start_time": "08:30", "end_time": "17:15"}, now=NOW)
        self.assertEqual(recurring["accessTimes"], "STARTSEC=30600;ENDSEC=62100")
        self.assertEqual(recurring["accessRecurrence"], "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,FR")
        with self.assertRaises(AccessError):
            command({"name": "Overnight", "pin": "123456", "access_type": "recurring",
                     "weekdays": ["MO"], "start_time": "23:00", "end_time": "06:00"}, now=NOW)
        self.assertNotIn("accessTimes", command({"name": "Permanent", "pin": "123456", "access_type": "always"}, now=NOW))
        single_name = command({"name": "Guest", "pin": "234567", "access_type": "always"}, now=NOW)
        self.assertNotIn("lastName", single_name)
        self.assertNotIn("lastName", saved({**single_name, "lastName": ""}))
        self.assertTrue(matches({**single_name, "state": "loaded", "lastName": None}, single_name))
        self.assertFalse(matches({**single_name, "state": "loaded", "lastName": "Different"}, single_name))
        self.assertEqual(saved({"firstName": "Guest", "lastName": None, "accessType": "always", "accessTimes": None}),
                         {"firstName": "Guest", "accessType": "always"})
        self.assertTrue(matches({"state": "loaded", "pin": "123456", "firstName": "Guest", "lastName": None,
                                 "accessType": "always"}, command({"name": "Guest", "pin": "123456", "access_type": "always"}, now=NOW)))
        self.assertEqual(len(records({"loaded": [{}], "updating": [{}]})), 2)


class NativeChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # This path is never created. The only storage used below is MemoryStore.
        self.hass = HomeAssistant("/__yale_memory_checks__")
        self.hass.config.time_zone = "America/Guayaquil"
        self.entry = SimpleNamespace(entry_id="manager", unique_id="lock", domain=DOMAIN,
                                     data={"lock_id": "lock", "device_id": "device", "august_entry_id": "source"},
                                     state=ConfigEntryState.LOADED, async_on_unload=lambda callback: None)
        self.store = MemoryStore()
        self.api = SimulatedYale()
        self.manager = AccessManager(self.hass, self.entry, self.api, store=self.store)
        self.entry.runtime_data = self.manager
        self.manager.poll_interval = 0
        self.manager.poll_timeout = 0.05
        self.entries = {"manager": self.entry}
        self.hass.config_entries = SimpleNamespace(
            async_entries=lambda domain: [entry for entry in self.entries.values() if entry.domain == domain],
            async_get_entry=lambda key: self.entries.get(key), async_get_known_entry=lambda key: self.entries[key])
        self.devices = {
            "device": dr.DeviceEntry(id="device", config_entry_id="source", identifiers={("august", "lock")}),
            "manager-device": dr.DeviceEntry(id="manager-device", config_entry_id="manager", identifiers={("august", "lock")}),
            "other-account": dr.DeviceEntry(id="other-account", config_entry_id="other-source", identifiers={("august", "lock")}),
            "other-lock": dr.DeviceEntry(id="other-lock", config_entry_id="manager", identifiers={("august", "other-lock")}),
        }
        self.hass.data[dr.DATA_REGISTRY] = SimpleNamespace(async_get=self.devices.get)
        await self.manager.load()

    async def asyncTearDown(self):
        await self.manager.async_shutdown()
        await self.manager.close()

    async def test_crud_restart_and_app_access_protection(self):
        baseline = deepcopy(self.api.entries[0])
        access_id = await self.manager.create(TEMP)
        listing = await self.manager.list_accesses()
        self.assertEqual(len(listing["accesses"]), 2)
        self.assertNotIn(TEMP["pin"], json.dumps(listing))
        self.assertNotIn("876543", json.dumps(listing))
        self.assertFalse(listing["accesses"][0]["managed"])
        with self.assertRaises(AccessError):
            await self.manager.delete("external:external")
        writes = len(self.api.writes)
        with self.assertRaises(AccessError):
            await self.manager.update(access_id, {**TEMP, "pin": "876543"})
        self.assertEqual(len(self.api.writes), writes)
        await self.manager.update(access_id, {**TEMP, "pin": "234567", "name": "Updated Guest"})
        self.assertEqual(self.api.writes[-2]["action"], "delete")
        self.assertEqual(self.api.writes[-1]["action"], "load")
        self.assertNotIn("userID", self.api.writes[-2])
        self.entry.entry_id = "readded-entry"
        restarted = AccessManager(self.hass, self.entry, self.api, store=self.store)
        restarted.poll_interval = 0
        await restarted.load()
        self.assertEqual(restarted.accesses[access_id]["partner_id"], "yam-" + access_id)
        await restarted.delete(access_id)
        await restarted.async_shutdown()
        self.assertEqual(self.api.entries, [baseline])

    async def test_single_word_name_create_and_replace_never_send_empty_surname(self):
        data = {"name": "Guest", "pin": "234567", "access_type": "always"}
        access_id = await self.manager.create(data)
        await self.manager.update(access_id, {**data, "name": "Renamed", "pin": "345678"})
        self.api.reject_next_load = True
        with self.assertRaises(AccessError) as result:
            await self.manager.update(access_id, {**data, "pin": "456789"})
        self.assertEqual(result.exception.code, "update_rolled_back")
        for payload in self.api.writes:
            if payload["action"] == "load":
                self.assertNotIn("lastName", payload)
        self.assertEqual(self.manager.owned(await self.manager.raw(), access_id)["firstName"], "Renamed")

    async def test_interruption_and_definitive_rejection(self):
        self.api.pending = True
        with self.assertRaises(AccessError) as result:
            await self.manager.create(TEMP)
        self.assertTrue(result.exception.uncertain)
        self.assertEqual(len(self.api.writes), 1)
        access_id = next(iter(self.manager.accesses))
        self.assertEqual(self.store.data["accesses"][access_id]["operation"], "unknown")
        with self.assertRaises(AccessError):
            await self.manager.create({**TEMP, "pin": "345678"})
        self.assertEqual(len(self.api.writes), 1)
        self.api.entries[-1]["state"] = "loaded"
        await self.manager.reconcile(access_id, TEMP)
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(self.manager.accesses[access_id]["operation"], "ready")
        await self.manager.delete(access_id)
        self.assertEqual(len(self.api.entries), 1)
        self.api.pending = False
        self.api.next_failure = AccessError("write_rejected")
        with self.assertRaises(AccessError):
            await self.manager.create(TEMP)
        self.assertEqual(self.manager.accesses, {})

    async def test_rejected_replacement_restores_previous_access(self):
        access_id = await self.manager.create(TEMP)
        self.api.reject_next_load = True
        with self.assertRaises(AccessError) as result:
            await self.manager.update(access_id, {**TEMP, "pin": "234567"})
        self.assertEqual(result.exception.code, "update_rolled_back")
        self.assertEqual(self.api.entries[-1]["pin"], TEMP["pin"])
        self.assertEqual(self.manager.accesses[access_id]["operation"], "ready")

    async def test_persistent_guest_issue_use_and_revocation(self):
        access_id = await self.manager.create(TEMP)
        yale_id = self.manager.accesses[access_id]["user_id"]
        person_entry = SimpleNamespace(platform="person", unique_id="person-1")
        registry = SimpleNamespace(async_get=lambda entity_id: person_entry if entity_id == "person.guest" else None,
                                   async_get_entity_id=lambda domain, platform, unique_id:
                                   "person.guest" if (domain, platform, unique_id) == ("person", "person", "person-1") else None)
        self.hass.states.async_set("person.guest", "home", {"user_id": "owner-1"})
        phone = SimpleNamespace(entry_id="phone-1", domain="mobile_app", title="Test Phone", state=ConfigEntryState.LOADED,
                                data={"user_id": "owner-1", "device_name": "Test Phone"})
        wrong_phone = SimpleNamespace(entry_id="phone-2", domain="mobile_app", title="Other Phone", state=ConfigEntryState.LOADED,
                                      data={"user_id": "other-user", "device_name": "Other Phone"})
        self.entries.update({"phone-1": phone, "phone-2": wrong_phone})
        delivered = []

        async def notify(call):
            delivered.append(call.data)

        self.hass.services.async_register("notify", "mobile_app_test_phone", notify)
        with patch("yale_access_manager.manager.er.async_get", return_value=registry):
            flow = AccessOptionsFlow()
            flow.hass = self.hass
            flow.flow_id = "person-link-flow"
            flow.handler = "manager"
            flow._access_id = access_id
            link_form = await flow.async_step_person()
            self.assertEqual(link_form["step_id"], "person")
            link_form["data_schema"]({"person_entity_id": "person.guest", "notification_entry_id": "phone-1"})
            with self.assertRaises(AccessError):
                await self.manager.bind_person(access_id, "person.guest", "phone-2")
            await self.manager.bind_person(access_id, "person.guest", "phone-1")
            self.assertEqual(self.manager.accesses[access_id]["user_id"], yale_id)
            await self.manager.set_enabled(access_id, False)
            self.assertEqual(self.manager.owned(await self.manager.raw(), access_id)["state"], "disabled")
            self.assertEqual(self.manager.accesses[access_id]["user_id"], yale_id)
            visit = await self.manager.begin_visit("person.guest")
            self.assertEqual(visit, {"allowed": True, "notify_service": "notify.mobile_app_test_phone"})
            self.assertEqual(await self.manager.begin_visit("person.guest"), {"allowed": False})
            await self.manager.end_visit("person.guest")
            self.assertEqual(await self.manager.begin_visit("person.guest"), {"allowed": False})
            restarted = AccessManager(self.hass, self.entry, self.api, store=self.store)
            restarted.poll_interval = 0
            await restarted.load()
            self.assertEqual(await restarted.begin_visit("person.guest"), {"allowed": False})
            await restarted.async_shutdown()
            self.assertNotIn("pin", json.dumps(self.store.data))
            issued = await self.manager.issue_access("person.guest")
            self.assertEqual(issued["access_id"], access_id)
            self.assertNotIn("pin", json.dumps(issued))
            self.assertEqual(len(delivered), 1)
            self.assertIn("código", delivered[0]["message"])
            self.assertEqual(self.manager.accesses[access_id]["user_id"], yale_id)
            self.assertEqual(self.manager.owned(await self.manager.raw(), access_id)["state"], "loaded")
            with self.assertRaises(AccessError):
                await self.manager.issue_access("person.guest")
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000) + 1000
            self.api.events = [{"id": "synthetic-activity", "timestamp": now_ms,
                                "deviceID": "lock", "action": "pin_unlock", "user": {"UserID": yale_id}}]
            await self.manager.maintain()
            self.assertIn("synthetic-activity", self.manager.seen_activities)
            self.assertIsNone(self.manager.accesses[access_id].get("lease"), self.manager.accesses[access_id])
            self.assertEqual(self.manager.owned(await self.manager.raw(), access_id)["state"], "disabled")
            self.assertEqual(self.manager.accesses[access_id]["user_id"], yale_id)
            self.assertEqual(len(delivered), 2)  # PIN delivery and notification clearing.
            self.assertNotIn("pin", json.dumps(self.store.data))
            await self.manager.maintain()
            self.assertEqual(len(delivered), 2)
            self.api.next_user_id = "unexpected-user"
            with self.assertRaises(AccessError):
                await self.manager.issue_access("person.guest")
            self.assertEqual(len(delivered), 2)
            self.assertEqual(self.manager.owned(await self.manager.raw(), access_id)["state"], "disabled")
            self.assertEqual(self.manager.accesses[access_id]["operation"], "unknown")

    async def test_expired_lease_recovered_after_restart_without_replaying_write(self):
        access_id = await self.manager.create(TEMP)
        before = self.manager.accesses[access_id]["user_id"]
        started = datetime.now(timezone.utc) - timedelta(minutes=2)
        self.manager.accesses[access_id]["lease"] = {
            "started_at": started.isoformat(), "expires_at": (started + timedelta(minutes=1)).isoformat(), "revoke": False}
        self.manager.seen_activities = None
        await self.manager.save()
        restarted = AccessManager(self.hass, self.entry, self.api, store=self.store)
        restarted.poll_interval = 0
        await restarted.load()
        writes = len(self.api.writes)
        await restarted.maintain()
        self.assertEqual(len(self.api.writes), writes + 1)
        self.assertEqual(self.api.writes[-1]["action"], "disable")
        self.assertEqual(restarted.owned(await restarted.raw(), access_id)["state"], "disabled")
        self.assertIsNone(restarted.accesses[access_id].get("lease"))
        self.assertEqual(restarted.accesses[access_id]["user_id"], before)
        await restarted.async_shutdown()

    async def test_uncertain_disable_is_reconciled_without_a_second_post(self):
        access_id = await self.manager.create(TEMP)
        started = datetime.now(timezone.utc) - timedelta(minutes=2)
        self.manager.accesses[access_id]["lease"] = {
            "started_at": started.isoformat(), "expires_at": (started + timedelta(minutes=1)).isoformat(),
            "revoke": True, "disable_requested": False}
        await self.manager.save()
        self.api.next_failure = AccessError("outcome_unknown", uncertain=True)
        writes = len(self.api.writes)
        await self.manager.maintain()
        self.assertEqual(len(self.api.writes), writes + 1)
        self.assertTrue(self.manager.accesses[access_id]["lease"]["disable_requested"])
        await self.manager.maintain()
        self.assertEqual(len(self.api.writes), writes + 1)
        self.assertEqual(self.manager.owned(await self.manager.raw(), access_id)["state"], "loaded")
        self.api.entries[-1]["state"] = "disabled"
        await self.manager.maintain()
        self.assertIsNone(self.manager.accesses[access_id].get("lease"))
        self.assertEqual(len(self.api.writes), writes + 1)

    async def test_event_entity_is_not_presence_tracker(self):
        activity = AccessActivity(self.entry)
        self.assertEqual(activity.event_types[-1], "deleted")
        self.assertEqual(activity.device_info["identifiers"], {("august", "lock")})

    async def test_native_admin_services_and_target_validation(self):
        await async_setup(self.hass, {})
        self.hass.auth = SimpleNamespace(async_get_user=self._get_user)
        with self.assertRaises(Unauthorized):
            await self.hass.services.async_call(DOMAIN, "list_accesses", {"device_id": "device"},
                                                blocking=True, return_response=True, context=Context(user_id="guest"))
        self.assertEqual(self.api.writes, [])
        listing = await self.hass.services.async_call(DOMAIN, "list_accesses", {"device_id": "device"},
                                                     blocking=True, return_response=True, context=Context(user_id="admin"))
        self.assertNotIn("876543", json.dumps(listing))
        with self.assertRaises(Exception):
            await self.hass.services.async_call(DOMAIN, "delete_access", {"device_id": "another-device", "access_id": "external"},
                                                blocking=True, context=Context(user_id="admin"))
        self.assertEqual(self.api.writes, [])

    async def _get_user(self, user_id):
        return SimpleNamespace(is_admin=user_id == "admin")

    async def test_actions_accept_per_integration_device_ids(self):
        # Native DeviceEntry objects: same lock identity, distinct August/manager IDs.
        self.assertNotEqual(self.devices["device"].id, self.devices["manager-device"].id)
        await async_setup(self.hass, {})
        self.hass.auth = SimpleNamespace(async_get_user=self._get_user)

        async def invoke(action, data):
            return await self.hass.services.async_call(
                DOMAIN, action, data, blocking=True,
                return_response=action in ("list_accesses", "create_access"),
                context=Context(user_id="admin"))

        for device_id in ("device", "manager-device"):
            listing = await invoke("list_accesses", {"device_id": device_id})
            self.assertEqual(len(listing["accesses"]), 1)
        selected = {"device_id": "manager-device"}
        result = await invoke("create_access", {**selected, **TEMP})
        selected["access_id"] = result["access_id"]
        changed = {**TEMP, "pin": "234567", "name": "Updated Guest"}
        await invoke("update_access", {**selected, **changed})
        await invoke("reconcile_access", {**selected, **changed})
        await invoke("delete_access", selected)
        self.assertEqual(len(self.api.entries), 1)

        writes = len(self.api.writes)
        for device_id in ("missing", "other-account", "other-lock"):
            with self.assertRaises(ServiceValidationError):
                await invoke("create_access", {"device_id": device_id, **TEMP})
        self.entry.state = ConfigEntryState.NOT_LOADED
        with self.assertRaises(ServiceValidationError):
            await invoke("create_access", {"device_id": "manager-device", **TEMP})
        self.assertEqual(len(self.api.writes), writes)

    async def test_native_options_menu_does_not_save_pin(self):
        flow = AccessOptionsFlow()
        flow.hass = self.hass
        flow.flow_id = "synthetic-flow"
        flow.handler = "manager"
        menu = await flow.async_step_init()
        self.assertIn("create_access", menu["menu_options"])
        form = await flow.async_step_create_access()
        self.assertEqual(form["step_id"], "details")
        form = await flow.async_step_details({key: TEMP[key] for key in ("name", "pin", "access_type")})
        self.assertEqual(form["step_id"], "schedule")
        result = await flow.async_step_schedule({key: TEMP[key] for key in ("starts_at", "ends_at")})
        self.assertEqual(result["data"], {})
        self.assertEqual(flow._draft, {})
        self.assertNotIn(TEMP["pin"], json.dumps(self.store.data))

    async def test_original_yale_error_reaches_action_and_form(self):
        detail = 'Yale HTTP 409: {"errorName": "ValidationError", "message": "Invalid lastName"}'
        await async_setup(self.hass, {})
        self.api.next_failure = AccessError("write_rejected", detail=detail)
        with self.assertRaises(HomeAssistantError) as result:
            await self.hass.services.async_call(DOMAIN, "create_access", {"device_id": "device", **TEMP}, blocking=True)
        self.assertEqual(str(result.exception), detail)
        self.api.next_failure = AccessError("write_rejected", detail=detail)
        flow = AccessOptionsFlow()
        flow.hass, flow.flow_id, flow.handler = self.hass, "error-flow", "manager"
        form = await flow.async_step_create_access({"name": "Guest", "pin": "234567", "access_type": "always"})
        self.assertEqual(form["errors"], {"base": "yale_error"})
        self.assertEqual(form["description_placeholders"], {"error": detail})
        self.assertEqual(self.manager.accesses, {})

    async def test_config_flow_and_no_source(self):
        flow = YaleAccessManagerConfigFlow()
        flow.hass = self.hass
        flow.flow_id = "setup-flow"
        flow.handler = DOMAIN
        flow.context = {"source": "user"}
        result = await flow.async_step_user()
        self.assertEqual(result["reason"], "no_august")
        self.entries["source"] = SimpleNamespace(domain="august", entry_id="source", title="Test account", state=ConfigEntryState.LOADED)
        self.hass.data[dr.DATA_REGISTRY] = SimpleNamespace(async_get=lambda key: None)
        form = await flow.async_step_user()
        form["data_schema"]({"august_entry_id": "source", "device_id": "device"})
        result = await flow.async_step_user({"august_entry_id": "source", "device_id": "device"})
        self.assertEqual(result["errors"], {"base": "unknown_device"})

    async def test_setup_entry_and_native_count_entities(self):
        self.entry.state = ConfigEntryState.SETUP_IN_PROGRESS
        self.entries["source"] = SimpleNamespace(domain="august", entry_id="source", state=ConfigEntryState.LOADED)
        device = SimpleNamespace(id="device", identifiers={("august", "lock")}, config_entries={"source"})
        attached = []
        self.hass.data[dr.DATA_REGISTRY] = SimpleNamespace(
            async_get=lambda key: device,
            async_get_or_create=lambda **kwargs: attached.append(kwargs))
        self.hass.config_entries.async_forward_entry_setups = AsyncMock()
        # Only our integration's bindings are substituted. HA code is untouched.
        with patch("yale_access_manager.create_api", AsyncMock(return_value=self.api)), \
                patch("yale_access_manager.AccessManager", return_value=self.manager):
            self.assertTrue(await async_setup_entry(self.hass, self.entry))
        self.assertEqual(attached[0]["identifiers"], {("august", "lock")})
        sensor = AccessCount(self.entry, "total")
        self.assertEqual(sensor.native_value, 1)
        self.assertIsNone(sensor.extra_state_attributes)
        self.assertEqual(sensor.device_info["identifiers"], {("august", "lock")})

    async def test_native_oauth_refresh_updates_only_source_entry(self):
        source = SimpleNamespace(entry_id="source", state=ConfigEntryState.LOADED,
                                 data={"auth_implementation": "synthetic", "token": {
                                     "access_token": "expired-synthetic", "refresh_token": "synthetic-refresh", "expires_at": 0}})
        self.entries["source"] = source
        updates = []

        def update(entry, *, data):
            updates.append(entry.entry_id)
            entry.data = data

        self.hass.config_entries.async_update_entry = update
        provider = SimpleNamespace(async_refresh_token=AsyncMock(return_value={
            "access_token": "new-synthetic-token", "refresh_token": "synthetic-refresh",
            "expires_in": 3600, "expires_at": time.time() + 3600}))
        oauth = OAuth2Session(self.hass, source, provider)
        await oauth.async_ensure_token_valid()
        self.assertEqual(oauth.token["access_token"], "new-synthetic-token")
        self.assertEqual(updates, ["source"])
        self.assertNotIn("token", self.entry.data)


class TransportChecks(unittest.IsolatedAsyncioTestCase):
    async def test_http_error_details_and_logs_redact_secrets_without_retry(self):
        class OAuth:
            token = {"access_token": "synthetic-secret-token", "refresh_token": "synthetic-refresh"}

            async def async_ensure_token_valid(self):
                pass

        class Response:
            status = 409

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def text(self):
                return json.dumps({"errorName": "ValidationError", "message": "Invalid lastName; PIN 234567; synthetic-secret-token",
                                   "pin": "234567", "nested": {"refresh_token": "other-secret"}, "transactionID": "trace-id"})

        response = Response()
        session = SimpleNamespace(request=lambda *args, **kwargs: response)
        source = SimpleNamespace(entry_id="source", state=ConfigEntryState.LOADED, async_start_reauth=lambda hass: None)
        hass = SimpleNamespace(config_entries=SimpleNamespace(async_get_entry=lambda key: source))
        api = YaleAPI(session, OAuth(), source, hass)
        for status, code, uncertain in ((409, "write_rejected", False), (500, "write_rejected", True),
                                        (401, "source_auth", False), (403, "access_denied", False), (429, "rate_limited", False)):
            response.status = status
            with self.assertLogs("yale_access_manager.api", level="ERROR") as logs:
                with self.assertRaises(AccessError) as result:
                    await api.write("private-lock", {"pin": "234567"})
            error = result.exception
            self.assertEqual((error.code, error.uncertain), (code, uncertain))
            self.assertIn(f"Yale HTTP {status}", error.detail)
            self.assertIn("Invalid lastName", error.detail)
            self.assertIn("trace-id", error.detail)
            exposed = error.detail + str(logs.output)
            for secret in ("234567", "synthetic-secret-token", "other-secret", "private-lock"):
                self.assertNotIn(secret, exposed)
        plain = redact_error("Invalid PIN 234567; token=unknown-secret Bearer another-secret", [])
        for secret in ("234567", "unknown-secret", "another-secret"):
            self.assertNotIn(secret, plain)

    async def test_no_write_retry_or_credential_exception(self):
        class OAuth:
            token = {"access_token": "synthetic-secret-token"}

            async def async_ensure_token_valid(self):
                pass

        class Session:
            calls = 0

            def request(self, *args, **kwargs):
                self.calls += 1
                raise TimeoutError("synthetic-secret-token")

        source = SimpleNamespace(entry_id="source", state=ConfigEntryState.LOADED)
        hass = SimpleNamespace(config_entries=SimpleNamespace(async_get_entry=lambda key: source))
        session = Session()
        api = YaleAPI(session, OAuth(), source, hass)
        with self.assertRaises(AccessError) as result:
            await api.write("lock", {"pin": "123456"})
        self.assertTrue(result.exception.uncertain)
        self.assertEqual(session.calls, 1)
        self.assertNotIn("synthetic-secret-token", str(result.exception))


class PackageChecks(unittest.TestCase):
    def test_metadata_and_translation_contract(self):
        assets = globals().get("TEST_ASSETS")
        if assets is None:
            root = Path(__file__).resolve().parents[1]
            assets = {str(path.relative_to(root)).replace("\\", "/"): path.read_text(encoding="utf-8")
                      for path in (root / "custom_components" / DOMAIN).rglob("*")
                      if path.suffix in (".json", ".yaml")}
            assets["hacs.json"] = (root / "hacs.json").read_text()
        prefix = f"custom_components/{DOMAIN}/"
        manifest = json.loads(assets[prefix + "manifest.json"])
        self.assertEqual(manifest["domain"], DOMAIN)
        self.assertEqual(manifest["dependencies"], ["august"])
        self.assertEqual(manifest["requirements"], [])
        self.assertEqual(json.loads(assets["hacs.json"])["homeassistant"], "2026.9.4")
        services = yaml.safe_load(assets[prefix + "services.yaml"])
        english = json.loads(assets[prefix + "strings.json"])
        spanish = json.loads(assets[prefix + "translations/es.json"])
        self.assertEqual(set(services), set(english["services"]))
        self.assertEqual(set(services), set(spanish["services"]))
        for name, definition in services.items():
            self.assertTrue(definition["fields"]["device_id"]["required"])
            self.assertEqual(set(definition["fields"]), set(english["services"][name]["fields"]))
            self.assertEqual(set(definition["fields"]), set(spanish["services"][name]["fields"]))
        self.assertEqual(services["create_access"]["fields"]["pin"]["selector"], {"text": {"type": "password"}})


if __name__ == "__main__":
    unittest.main()
