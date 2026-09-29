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

Permanent, temporary and weekly recurring access are supported. Temporary dates
use Home Assistant's timezone. Recurring times use the lock's timezone in the
Yale app; a weekly interval must start and end within the same day.

**App-created accesses are read-only.** Only accesses created by this integration
can be changed or deleted. App invitations, owner roles, one-time PINs and
fingerprint/RFID credentials are not supported.

Replacement uses **delete then load**, with a brief access interruption. A
definitively rejected replacement attempts to restore the previous PIN. An
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

For `temporary`, provide `starts_at` and `ends_at`. For `recurring`, provide
`weekdays` (MO, TU, WE, TH, FR, SA, SU), `start_time` and `end_time`.
For `always`, schedule fields are not needed. PINs contain 4–8 ASCII digits;
leading zeros are preserved.

The lock's Yale Access Manager device receives three diagnostic count sensors:
**Access entries**, **Managed accesses** and **Pending access operations**.
Home Assistant can represent the same lock separately for each integration.
Actions accept its configured August or Yale Access Manager device and verify
the lock identity and account association.
Counts refresh every five minutes and after management operations. Requests wait
up to three minutes for Yale's reported loading/removal state.

## Privacy and persistence

- PIN values are not stored in entity state, attributes, options, responses,
  integration logs or the access journal. They are held temporarily in memory.
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
