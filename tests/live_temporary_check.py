"""Explicitly authorized, in-memory temporary test against an existing HA lock.

This module is excluded from automatic test discovery. It is never an installer.
Requires exactly one active August entry and one lock; never operates the motor.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import secrets
from types import SimpleNamespace

from aiohttp import ClientSession
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session

from yale_access_manager.api import YaleAPI
from yale_access_manager.const import AccessError
from yale_access_manager.manager import AccessManager
from yale_access_manager.models import records
from test_integration import MemoryStore


FIELDS = ("_id", "userID", "pin", "slot", "state", "accessType", "firstName", "lastName", "accessTimes", "accessRecurrence", "schedule")


def snapshot(items):
    return {item["_id"]: {key: item.get(key) for key in FIELDS} for item in items}


async def run():
    entries = json.loads(Path("/config/.storage/core.config_entries").read_text())["data"]["entries"]
    active = [entry for entry in entries if entry["domain"] == "august" and not entry.get("disabled_by")]
    if len(active) != 1:
        raise RuntimeError("Ambiguous source; test aborted")
    hass = HomeAssistant("/__yale_live_memory_check__")
    source = SimpleNamespace(entry_id=active[0]["entry_id"], state=ConfigEntryState.LOADED,
                             data=active[0]["data"], async_start_reauth=lambda _: None)
    hass.config_entries = SimpleNamespace(async_get_entry=lambda key: source if key == source.entry_id else None)

    async def no_refresh(token):
        raise RuntimeError("Live diagnostic requires an already-valid OAuth token")

    oauth = OAuth2Session(hass, source, SimpleNamespace(async_refresh_token=no_refresh))
    api = YaleAPI(ClientSession(), oauth, source, hass)
    manager = None
    baseline = {}
    try:
        locks = await api.locks()
        if len(locks) != 1:
            raise RuntimeError("Ambiguous lock; test aborted")
        lock_id = next(iter(locks))
        existing = records(await api.pins(lock_id))
        if any(item.get("state") != "loaded" for item in existing):
            raise RuntimeError("Existing operation is pending; test aborted")
        baseline = snapshot(existing)
        entry = SimpleNamespace(entry_id="live-memory-test", unique_id=lock_id,
                                 data={"lock_id": lock_id, "device_id": "in-memory-device"},
                                 async_on_unload=lambda callback: None)
        manager = AccessManager(hass, entry, api, store=MemoryStore())
        await manager.load()
        used = {item["pin"] for item in existing}
        codes = []
        while len(codes) < 2:
            code = str(secrets.randbelow(900000) + 100000)
            if code not in used and code not in codes:
                codes.append(code)
        start = datetime.now(timezone.utc)
        data = {"name": "HA Viability Test", "pin": codes[0], "access_type": "temporary",
                "starts_at": start.isoformat(), "ends_at": (start + timedelta(minutes=15)).isoformat()}
        access_id = await manager.create(data)
        print(json.dumps({"stage": "candidate_create", "loaded": True}), flush=True)
        items = await manager.raw()
        before = manager.owned(items, access_id)
        identity_fields = ("_id", "userID", "partnerUserID")
        if before is None or any(not isinstance(before.get(key), str) or not before[key] for key in identity_fields):
            raise RuntimeError("Created access lacks an identity needed for comparison")
        before_identity = {key: before[key] for key in identity_fields}
        before_partner = manager.accesses[access_id]["partner_id"]
        current = snapshot(items)
        assert all(current.get(key) == value for key, value in baseline.items())
        await manager.update(access_id, {**data, "pin": codes[1],
                                        "starts_at": (start + timedelta(minutes=1)).isoformat(),
                                        "ends_at": (start + timedelta(minutes=13)).isoformat()})
        print(json.dumps({"stage": "candidate_replace", "loaded": True}), flush=True)
        items = await manager.raw()
        after = manager.owned(items, access_id)
        if after is None or any(not isinstance(after.get(key), str) or not after[key] for key in identity_fields):
            raise RuntimeError("Replaced access lacks an identity needed for comparison")
        print(json.dumps({
            "stage": "identity_comparison",
            "yale_user_id_preserved": before_identity["userID"] == after["userID"],
            "pin_record_id_preserved": before_identity["_id"] == after["_id"],
            "cloud_partner_id_preserved": before_identity["partnerUserID"] == after["partnerUserID"],
            "managed_partner_id_preserved": before_partner == manager.accesses[access_id]["partner_id"],
            "managed_access_id_preserved": access_id in manager.accesses,
            "pin_changed": before["pin"] != after["pin"],
            "schedule_changed": before.get("accessTimes") != after.get("accessTimes"),
        }), flush=True)
        current = snapshot(items)
        assert all(current.get(key) == value for key, value in baseline.items())
        await manager.delete(access_id)
        print(json.dumps({"stage": "candidate_delete", "removed": True}), flush=True)
    except AccessError as exc:
        print(json.dumps({"stage": "candidate_error", "code": exc.code, "uncertain": exc.uncertain}), flush=True)
        raise RuntimeError("Candidate live check failed") from None
    finally:
        if manager:
            for access_id in list(manager.accesses):
                # Cleanup only journal entries created by this disposable manager.
                try:
                    await manager.delete(access_id)
                except AccessError:
                    print(json.dumps({"stage": "cleanup_unconfirmed", "test_partner_id": manager.accesses[access_id]["partner_id"]}), flush=True)
            final = snapshot(await manager.raw())
            print(json.dumps({"stage": "candidate_final", "existing_unchanged": final == baseline,
                              "entry_count": len(final), "journal_empty": not manager.accesses}), flush=True)
            if final != baseline or manager.accesses:
                raise RuntimeError("Candidate cleanup or baseline comparison failed")
            await manager.async_shutdown()
        await api.close()


if __name__ == "__main__":
    import sys
    if "--write-authorized" not in sys.argv:
        raise SystemExit("Explicit --write-authorized is required for this temporary test")
    asyncio.run(run())
