# Yale Access Manager

Manage Yale Assure Lock 2 keypad access from Home Assistant using the existing
August OAuth account and the direct Yale/August cloud API. No Seam account is
required. Existing August and Yale Access Bluetooth lock control stays in place.

## Requirements

- Home Assistant **2026.9.4 or newer** and HACS.
- The official **August** integration, configured and loaded with owner access.
- A Yale/August US API lock that supports entry codes. Schedule-based access also
  requires the lock to support schedules and its Wi-Fi connection to be online.

This integration uses the cloud to administer accesses. It does not program PINs
over Bluetooth and does not add Matter or Z-Wave support.

## Install with HACS

1. In **HACS → Custom repositories**, add
   `https://github.com/dbustosrc/yale-access-manager` with category **Integration**.
2. Download **Yale Access Manager**, then restart Home Assistant.
3. In **Settings → Devices & services → Add integration**, select
   **Yale Access Manager**.
4. Choose your existing August account and its Yale lock.

The repository is installed as a HACS custom repository; inclusion in HACS's
default catalog is separate.

## Manage accesses with native forms

Open the integration's **Configure/options** button in Devices & services:

- **View accesses** shows existing guests and their access metadata without PINs.
- **Create access** asks for a guest name, PIN and access type.
- **Replace access** changes a managed guest's name, PIN and schedule.
- **Resolve pending operation** verifies the expected PIN and metadata by reading
  Yale, without repeating a write.
- **Delete access** removes the selected managed entry and waits for confirmation.
- **Cancel pending access** sends an explicit deletion for the unresolved managed
  ID using its original PIN, even if the attempted load is absent in Yale.
  A rejection keeps the journal pending; it never just erases the record.
- **Disable/enable access** changes keypad availability while retaining the guest
  and its Yale user ID. Do not enable an expired temporary schedule.
- **Link person** connects a managed or app-created access, including an owner,
  to a Home Assistant person by credential and Yale user IDs. A Companion
  phone is optional for linkage and must belong to that person's HA account
  when selected. Temporary code delivery still requires a linked phone.
- **Unlink person** removes the local association and phone without changing
  the Yale credential. Link person can also remove only the existing phone.
- **End visit** releases a stopped visit while retaining its question cooldown.
- Editing forms suggest the existing name and schedule, never the existing PIN.
  Action menus filter accesses according to their current operation and state.

Permanent, temporary and weekly recurring access are supported. Temporary dates
use Home Assistant's timezone. Recurring times use the lock's timezone in the
Yale app; a weekly interval must start and end within the same day.

**App-created accesses are read-only.** Only accesses created by this integration
can be changed or deleted. App invitations, owner roles, one-time PINs and
fingerprint/RFID credentials are not supported.

Read-only accesses can be linked to a person without becoming managed. Their
linkage is stored separately in Home Assistant; no Yale PIN, schedule or role
is changed. Keypad activity uses the Yale user ID, never a display-name match.
If the external credential identity changes, explicitly review and relink it.

Replacement uses **delete then load**, with a brief access interruption. A
successful manual replacement restores the previous enabled/disabled state.
Yale loads the replacement before it can be disabled, so editing a disabled
credential is not atomic and can briefly activate the new PIN. A definitively
rejected replacement attempts to restore the previous PIN. An
ambiguous result is retained as pending instead of replaying writes. Resolve or
delete pending entries before creating or replacing another access.

## Native actions

All access-management actions require an administrator or an authorized system
automation, and an explicit configured lock `device_id`.

| Action | Inputs | Response |
| --- | --- | --- |
| `yale_access_manager.list_accesses` | `device_id` | Access metadata, IDs, management status and schedules; no PINs |
| `yale_access_manager.create_access` | `device_id`, `name`, `pin`, `access_type`, schedule fields | Optional managed `access_id` after Yale reports loaded |
| `yale_access_manager.update_access` | Same fields plus a managed `access_id` | Confirms replacement or raises a clear error |
| `yale_access_manager.reconcile_access` | Same fields plus a managed `access_id` | Confirms an existing loaded entry by reading only |
| `yale_access_manager.delete_access` | `device_id`, managed `access_id` | Confirms removal |
| `yale_access_manager.cancel_pending_access` | `device_id`, pending `access_id`, original `pin` | Requests deletion for the original partner and clears the journal only after Yale accepts it and a fresh read confirms absence |
| `yale_access_manager.disable_access` | `device_id`, managed `access_id` | Confirms the PIN is disabled in Yale's API |
| `yale_access_manager.enable_access` | `device_id`, managed `access_id` | Confirms an unexpired PIN is loaded |
| `yale_access_manager.bind_person` | `device_id`, managed or external `access_id`, `person_entity_id`, optional `notification_entry_id`, `clear_notification` | Links stable identifiers; phone removal cannot be combined with selecting a new phone |
| `yale_access_manager.unbind_person` | `device_id`, linked managed or external `access_id` | Removes local person/phone linkage without modifying Yale; refuses an active issued lease |
| `yale_access_manager.issue_access` | `device_id`, linked `person_entity_id`, optional `validity_minutes` (5–30), `language` (en/es) | Renews a temporary PIN and requests phone delivery; returns access ID, expiry, opaque receipt action and notification status, never the PIN |
| `yale_access_manager.confirm_delivery` | `device_id`, managed `access_id`, current `receipt_action` | Records the recipient's explicit notification-button response; rejects another issuance's correlation ID |
| `yale_access_manager.begin_visit` | `device_id`, linked `person_entity_id`, optional `cooldown_minutes` (5–120) | Reserves one question per visit and returns the verified Companion notify service, or `allowed: false` |
| `yale_access_manager.end_visit` | `device_id`, linked `person_entity_id` | Clears the visit after departure; preserves the 30-minute cooldown |

For `temporary`, provide `starts_at` and `ends_at`. For `recurring`, provide
`weekdays` (MO, TU, WE, TH, FR, SA, SU), `start_time` and `end_time`.
For `always`, schedule fields are not needed. PINs contain 4–8 ASCII digits;
leading zeros are preserved.

The lock's Yale Access Manager device receives three diagnostic count sensors:
**Access entries**, **Managed accesses** and **Pending access operations**.
Home Assistant can represent the same lock separately for each integration.
Actions accept its configured August or Yale Access Manager device and verify
the lock identity and account association.
Counts refresh every minute (15 seconds during an issued access) and after management operations. Requests wait
up to three minutes for Yale's reported loading/removal state.

Each managed guest also receives an **Access state** sensor, an **Access expiry**
timestamp sensor and an individual **Access** switch. The switch uses the same
administrator-only actions as manual access management; it does not control the
physical bolt. External and owner accesses do not receive management switches.
The state/switch reflects Yale's reported enabled state; programmed schedules
also determine whether a loaded PIN can be used. A permanent guest has no expiry.
These guest entities include names and linkage metadata, which can appear in
Home Assistant history; they never include PINs. Deleted managed guests' entities
are removed without affecting unrelated entities.

Lock-level **Access problem** and **Last successful synchronization** entities
remain useful during an outage. Activity becomes unavailable if cloud refresh
fails. A separate native maintenance timer continues even when the count sensors
are disabled. Rate-limited requests honor `Retry-After` without replaying writes.

`event.*_access_activity` records guest activity without PIN values. Its events
include the stable `access_id`, Yale `userID`, linked person entity ID, timestamp
and event type. `consumed` identifies revocation following observed credential
use; `expired` identifies expiry cleanup. It is an activity entity, not a presence tracker; keypad activity
identifies the credential that operated the lock, not the human holding it.

## On-demand access

Create one managed guest with a temporary PIN, disable it and link it to a person
and Companion phone in **Configure**. The issuance action replaces the old PIN
with a random six-digit code valid for ten minutes by default, then requests its
delivery to that phone. Validity can be set to 5–30 minutes.
It refuses a second simultaneous issuance. The Yale user ID and managed
`access_id` remain stable across replacements. A confirmed Yale keypad unlock
for that user starts revocation; the expiry is also programmed in Yale and
an unconfirmed revocation is reconciled after Home Assistant restarts without
blindly repeating a write. Yale cloud activity polling
can lag, so this is **best-effort single use**, not a hardware-enforced one-time
credential.

The integration itself does not decide when a visitor may request access.
The reusable blueprint at
[`blueprints/automation/yale_access_manager/on_demand_guest.yaml`](blueprints/automation/yale_access_manager/on_demand_guest.yaml)
combines a fresh recognized face at the entrance with phone proximity, Bluetooth
lock state and door contact. It asks the linked Companion phone once per visit,
waits two minutes for a Yes/No answer, and rechecks presence and door state before
issuing. It keeps a 30-minute question cooldown and the visit state in the
private access journal; no input booleans or timers are needed. Both the guest
and phone must be away for five minutes to end a visit. A periodic check
recovers departure after Home Assistant restarts.

Install the blueprint in Home Assistant's `blueprints/automation` directory,
then create **one UI automation from it per guest**. Select the managed lock,
linked person, canonical Presence Engine face ID, entrance camera and area,
phone proximity tracker, Bluetooth lock, door contact and camera occupancy.
Each instance is editable through Home Assistant's automation editor. Keep it
disabled until the guest linkage, notification delivery and physical keypad
behavior have been verified. Existing app-created permanent codes must be
retired separately through Yale.

The blueprint supports a selectable Presence Engine detection event entity,
English/Spanish notifications and a 5–120 minute minimum question cooldown
(30 by default). Face identity, confidence, freshness, entrance, phone, closed
door and Bluetooth locked-state checks remain required. No new helpers are needed.
An unanswered question releases the visit reservation after two minutes while
preserving the cooldown; an explicit No keeps the visit silent until departure.

The code notification includes **I received the code**. The blueprint matches
its opaque action against the current issuance and calls `confirm_delivery`.
`requested` means the notify service returned; it does not prove delivery.
`confirmed` means an explicit button response was recorded; `unconfirmed` means
no response was recorded after two minutes, and `failed` means an exception was
reported. Lack of confirmation does not establish that delivery failed and does
not extend the code's validity or disable an otherwise valid code early.

## Troubleshooting

Settings → System → Repairs shows pending operations, changed identities,
invalid links, cloud/history failures, history continuity gaps and unconfirmed
code receipt. Repairs gives instructions; it never automatically cancels an
access or repeats an uncertain write. Use the listing action to inspect operation
phase, start time, transaction ID and sanitized error details.
Unlink/relink external identities only after reviewing the credential.

Download diagnostics from the integration menu. Diagnostics include version,
connectivity, phase/error codes and aggregate linkage information, without PINs,
guest names, account identities or raw cloud responses.

Activity polling normally reads 50 events. If continuity is missing, it requests
up to 100; the Yale endpoint returned at most 100 even when larger limits were
requested during verification. Missing continuity raises a Repairs alert.
This is bounded recovery, not a guarantee of complete history after a long outage.
Expiry cleanup runs independently of the activity endpoint. Yale's programmed
expiry remains the fallback when cloud connectivity or Home Assistant is down.

## Privacy and persistence

Yale HTTP failures show the original response text and status in forms and
action errors. The integration logs the complete sanitized error response at
ERROR level, including server error names and transaction details when Yale
returns them. PINs, tokens and other authentication material are redacted;
successful PIN responses and request bodies are never logged. Non-HTTP errors
retain the integration's local validation messages.

- PIN values are not stored in entity state, attributes, options, responses,
  integration logs or the access journal. They are held temporarily in memory.
- PINs necessarily pass through the Companion notification service and may be
  visible on the recipient's phone or in Home Assistant service traces.
- Native password fields hide the input visually. **A PIN passed in a script or
  automation can still appear in that script's configuration or execution trace.**
  Prefer the native management forms for manual access administration.
- The source August integration owns and refreshes OAuth credentials; this
  integration does not copy them into another configuration entry.
- Managed identifiers and non-secret pending-operation metadata are stored using
  Home Assistant's private atomic storage. Keep them in Home Assistant backups.
  Losing this journal makes those entries read-only; the integration will not
  guess ownership.
- Removing this integration does **not** revoke lock accesses. Delete unwanted
  entries before removal. The non-secret journal is retained so re-adding the
  same lock can recover management, subject to the same OAuth client identity.

## Scope of verification

Temporary creation, PIN/schedule replacement and deletion were verified through
the Yale API on Yale Assure Lock 2. Server-reported `loaded` is not an independent
physical keypad test. Permanent and weekly schedule validation, permissions,
privacy, restart recovery and failure handling are covered by automated checks;
broader real-device verification is still needed.

The same temporary guest was disabled and re-enabled through Yale's API while
retaining its `userID`, PIN record ID, partner ID, PIN and schedule. Physical
rejection of its PIN while disabled has not yet been checked.

The native Matter credential manager is bound to Matter services and does not
provide a documented general hook for Yale. This integration uses native
configuration/options forms, action selectors, device registration and
administrator permissions without modifying Home Assistant.

## Development checks

Run `python -B -m unittest discover -s tests -v` inside a Home Assistant 2026.9.4
environment with its August library installed. CI runs these checks in that
Home Assistant container and validates the HACS package.

`tools/test_ha.ps1` can send public sources and synthetic checks into an existing
HA container through SSH/stdin without installing the integration or creating
remote files. It requires an explicit target and SSH identity. The separate
`tools/viability_probe.py` is read-only by default; live write tests require the
lock owner's specific authorization.

## References

- [Home Assistant August](https://www.home-assistant.io/integrations/august/)
- [Yale Access Bluetooth](https://www.home-assistant.io/integrations/yalexs_ble/)
- [yalexs](https://github.com/Yale-Libs/yalexs)
- [Native integration actions](https://developers.home-assistant.io/docs/dev_101_services/)
- [HACS requirements](https://www.hacs.xyz/docs/publish/integration/)

MIT licensed. Community integration; not affiliated with Yale, August or Assa Abloy.
