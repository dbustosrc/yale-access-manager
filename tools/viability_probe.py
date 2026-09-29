"""Yale access feasibility probe: run in HA via SSH/stdin; never save credentials.

Read-only by default. --write-authorized is only for the user-approved temporary
test. No lock/unlock, BLE operations, token refresh, files, or automatic POST retries.
"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import re
import secrets
import time
import uuid

from aiohttp import ClientSession, ClientTimeout
from homeassistant.components.august.const import DEFAULT_AUGUST_BRAND
from yalexs.api_async import ApiAsync
from yalexs.api_common import api_auth_headers


TEST_NAME = ("HA", "Viability Test")
FIELDS = (
    "_id", "userID", "pin", "slot", "state", "accessType", "accessTimes",
    "accessRecurrence", "schedule", "firstName", "lastName", "type", "unverified",
)


def emit(stage, **values):
    print(json.dumps({"stage": stage, **values}), flush=True)


def records(groups):
    return [pin for items in groups.values() if isinstance(items, list)
            for pin in items if isinstance(pin, dict) and "pin" in pin]


def snapshot(groups):
    return {pin["_id"]: {key: pin.get(key) for key in FIELDS}
            for pin in records(groups)}


def window(start, end):
    def stamp(value):
        return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return f"DTSTART={stamp(start)};DTEND={stamp(end)}"


def self_check():
    start = datetime(2026, 1, 1, 8, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert window(start, start + timedelta(minutes=15)) == (
        "DTSTART=2026-01-01T13:00:00.000Z;DTEND=2026-01-01T13:15:00.000Z"
    )
    baseline = {"loaded": [{"_id": "sample", "pin": "123456", "state": "loaded"}], "created": []}
    pending = {"loaded": baseline["loaded"], "created": [{"_id": "new", "pin": "234567"}]}
    assert len(records(pending)) == 2
    assert snapshot(pending)["sample"] == snapshot(baseline)["sample"]
    changed = {"loaded": [{"_id": "sample", "pin": "345678", "state": "loaded"}]}
    assert snapshot(changed)["sample"] != snapshot(baseline)["sample"]
    emit("self_check", passed=True)


async def run(write_authorized, check_user_endpoint=False):
    # Library debug logging includes raw PIN responses; silence this process only.
    logging.disable(logging.CRITICAL)
    entries = json.loads(Path("/config/.storage/core.config_entries").read_text())["data"]["entries"]
    active = [entry for entry in entries if entry["domain"] == "august" and not entry.get("disabled_by")]
    if len(active) != 1:
        raise RuntimeError("Expected one active August account")
    credentials = active[0]["data"]["token"]
    if credentials.get("expires_at", 0) <= time.time() + 900:
        raise RuntimeError("OAuth token must remain valid throughout test")
    token = credentials["access_token"]
    headers = api_auth_headers(token, DEFAULT_AUGUST_BRAND)
    headers.update({"Accept-Version": "0.0.1", "Content-Type": "application/json"})

    async with ClientSession(timeout=ClientTimeout(total=25)) as session:
        api = ApiAsync(session, timeout=20, brand=DEFAULT_AUGUST_BRAND)

        async def get(path):
            async with session.get(api.get_brand_url(path), headers=headers, allow_redirects=False) as response:
                if response.status != 200:
                    emit("get_failed", http_status=response.status)
                    raise RuntimeError("Read failed")
                return await response.json()

        locks = await get("/users/locks/mine")
        if len(locks) != 1:
            raise RuntimeError("Expected one lock; ambiguous target")
        lock_id, lock_summary = next(iter(locks.items()))
        if lock_summary.get("UserType") != "superuser":
            raise RuntimeError("Owner access required")
        detail = await get(f"/locks/{lock_id}")
        if not detail.get("supportsEntryCodes") or not detail.get("accessSchedulesAllowed"):
            raise RuntimeError("PINs and schedules required")
        path = f"/locks/{lock_id}/pins"
        baseline_groups = await get(path)
        baseline = snapshot(baseline_groups)
        baseline_users = detail.get("users", {})
        emit("baseline", brand=str(DEFAULT_AUGUST_BRAND), owner=True,
             groups={key: len(value) for key, value in baseline_groups.items() if isinstance(value, list)},
             user_count=len(baseline_users))
        parsed = await api.async_get_pins(token, lock_id)
        schedule_errors = 0
        for pin in parsed:
            try:
                pin.access_times
            except Exception:
                schedule_errors += 1
        emit("yalexs_read", loaded_count=len(parsed), schedule_parser_errors=schedule_errors)
        if not write_authorized:
            return
        if any(value for key, value in baseline_groups.items() if key != "loaded"):
            raise RuntimeError("Existing pending or disabled PINs; test aborted")
        if any((pin.get("firstName"), pin.get("lastName")) == TEST_NAME for pin in records(baseline_groups)):
            raise RuntimeError("Existing test name; refusing to touch it")

        partner_id = "ha-viability-" + uuid.uuid4().hex
        used_codes = {pin.get("pin") for pin in records(baseline_groups)}
        codes = []
        while len(codes) != 2:
            code = str(secrets.randbelow(900000) + 100000)
            if code not in used_codes and code not in codes:
                codes.append(code)
        started = datetime.now(timezone.utc)
        expiry = started + timedelta(minutes=15)

        def ours(groups):
            return [pin for pin in records(groups) if pin.get("_id") not in baseline
                    and (pin.get("pin") in codes or pin.get("partnerUserID") == partner_id)
                    and (pin.get("firstName"), pin.get("lastName")) == TEST_NAME]

        async def post(command, label, method="POST", request_path=None):
            # POST is intentionally sent once: retrying ambiguous writes can duplicate access.
            payload = {"commands": [command]} if method == "POST" else command
            async with session.request(method, api.get_brand_url(request_path or path), headers=headers,
                                       json=payload, allow_redirects=False) as response:
                try:
                    body = await response.json()
                except Exception:
                    body = None
                text = json.dumps(body).lower()
                hints = [word for word in ("webhook", "permission", "scope", "unauthorized",
                         "unsupported", "invalid", "commands", "partner", "not found") if word in text]
                error_message = ""
                if response.status >= 400 and isinstance(body, dict):
                    error_message = str(body.get("message", ""))
                    for sensitive in (token, lock_id, partner_id, *codes):
                        error_message = error_message.replace(sensitive, "<redacted>")
                    error_message = re.sub(r"https?://\S+|[\w.+-]+@[\w.-]+|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}|\d{4,}", "<redacted>", error_message)[:300]
                emit(label, http_status=response.status,
                     response_fields=sorted(body) if isinstance(body, dict) else [],
                     transaction_present=isinstance(body, dict) and bool(body.get("transactionID")),
                     error_hints=hints, error_message=error_message)
                return response.status in (200, 201, 202, 204)

        async def wait_for(predicate, label):
            deadline = time.monotonic() + 150
            while True:
                groups = await get(path)
                current = snapshot(groups)
                if any(current.get(key) != value for key, value in baseline.items()):
                    raise RuntimeError("Existing access changed; stopping test")
                if predicate(groups):
                    emit(label, confirmed=True,
                         groups={key: len(value) for key, value in groups.items() if isinstance(value, list)})
                    return groups
                if time.monotonic() >= deadline:
                    raise TimeoutError("PIN synchronization not confirmed")
                await asyncio.sleep(5)

        def load(code, access_times):
            return {"partnerUserID": partner_id, "pin": code, "action": "load",
                    "accessType": "temporary", "firstName": TEST_NAME[0],
                    "lastName": TEST_NAME[1], "accessTimes": access_times, "retry": False}

        def delete(pin):
            if pin["_id"] in baseline or pin not in ours({"loaded": [pin]}):
                raise RuntimeError("Delete target is not our temporary test")
            return {"partnerUserID": partner_id, "pin": pin["pin"],
                    "action": "delete", "accessType": "temporary", "retry": False}

        try:
            emit("test_window", expires_at=expiry.isoformat(), writes_authorized=True)
            first_window = window(started, expiry)
            if not await post(load(codes[0], first_window), "create_request"):
                return
            groups = await wait_for(lambda group: any(pin.get("pin") == codes[0]
                                   and pin.get("state") == "loaded" for pin in ours(group)), "create_loaded")
            pin = ours(groups)[0]
            emit("create_schedule", exact_match=pin.get("accessTimes") == first_window,
                 temporary=pin.get("accessType") == "temporary")
            if check_user_endpoint:
                accepted = await post(
                    {"pin": codes[1], "state": "update", "action": "intent",
                     "accessType": "temporary", "accessTimes": first_window},
                    "legacy_user_pin_request", "PUT", f"/locks/{lock_id}/users/{pin['userID']}/pin",
                )
                if accepted:
                    groups = await wait_for(lambda group: any(value.get("pin") == codes[1]
                                           and value.get("state") == "loaded" for value in ours(group)),
                                            "legacy_user_pin_loaded")
                    pin = ours(groups)[0]
            if not await post(delete(pin), "replace_delete_request"):
                raise RuntimeError("Cannot remove test PIN before replacement")
            await wait_for(lambda group: not ours(group), "replace_delete_complete")
            second_window = window(started + timedelta(minutes=2), expiry - timedelta(minutes=2))
            if not await post(load(codes[1], second_window), "replace_load_request"):
                raise RuntimeError("Cannot load replacement")
            groups = await wait_for(lambda group: any(pin.get("pin") == codes[1]
                                   and pin.get("state") == "loaded" for pin in ours(group)), "replacement_loaded")
            pin = ours(groups)[0]
            emit("replacement_schedule", exact_match=pin.get("accessTimes") == second_window,
                 old_pin_absent=all(value.get("pin") != codes[0] for value in records(groups)))
        except Exception as exc:
            emit("test_error", error_type=type(exc).__name__)
        finally:
            # Reconcile after an ambiguous POST before deciding which test credential to delete.
            try:
                groups = await get(path)
                for pin in ours(groups):
                    await post(delete(pin), "cleanup_delete_request")
                final = await wait_for(lambda group: not ours(group), "cleanup_complete")
                final_detail = await get(f"/locks/{lock_id}")
                emit("final", existing_unchanged=all(snapshot(final).get(key) == value for key, value in baseline.items()),
                     total_pins=len(records(final)), test_absent=not ours(final),
                     registered_users_unchanged=final_detail.get("users", {}) == baseline_users)
            except Exception as exc:
                emit("cleanup_unconfirmed", error_type=type(exc).__name__, expires_at=expiry.isoformat())
    headers.clear()
    token = None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-authorized", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--check-user-endpoint", action="store_true")
    args = parser.parse_args()
    try:
        if args.self_check:
            self_check()
        else:
            asyncio.run(run(args.write_authorized, args.check_user_endpoint))
    except Exception as exc:
        emit("probe_failed", error_type=type(exc).__name__)
        raise SystemExit(1) from None
