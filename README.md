# Yale Access Manager

Feasibility work for a Home Assistant custom integration using `yalexs` and the
Yale/August API to manage keypad access on Yale Assure Lock 2.

This repository currently contains research and a diagnostic probe. It is not
yet an installable HACS integration.

## Confirmed foundation

- Reuse the existing August OAuth account with the `yale_august` brand.
- Read keypad entries and their synchronization states from Yale/August.
- Create a temporary PIN-only guest, replace its PIN and schedule, and remove
  its entry through the direct cloud API.
- Poll until Yale reports the entry loaded or removed. HTTP acceptance alone
  does not confirm completion.
- No Seam account or additional OAuth client was needed for these tests.

Offline PIN administration is not implemented by `yalexs-ble`. Existing local
lock control can continue through Home Assistant's Yale Access Bluetooth
integration.

Management of app-created entries, recurring writes, permanent writes and app
invitations are not established by the temporary guest test. They must not be
advertised as verified capabilities.

The user-ID route accepted an update intent but did not report the replacement
PIN loaded during the observation period. A client-side commit was deliberately
not sent. Server-reported `loaded` is not an independent physical keypad test.

## Probe

`tools/viability_probe.py` runs in memory inside an existing Home Assistant
container. It reads the installed configuration there and never exports tokens,
PIN values or raw entry records. It does not operate the lock.

Default execution is read-only. `--self-check` checks time formatting and access
comparison without network calls. `--write-authorized` creates a short-lived
test entry and removes it afterward; it requires the lock owner's specific
authorization. `--check-user-endpoint` additionally probes the user-ID route
on that same disposable entry. These flags are diagnostic tools, not a license
to test against a third party's installation.

Writes are sent once. Cleanup reconciles the current entry list after errors.
Deletion must be confirmed; an expiry timestamp alone is not proof of deletion.

## Development constraints

- Keep this integration separate from the built-in August and Bluetooth code.
- Use Home Assistant's OAuth session APIs for refresh; do not copy account tokens
  into a second configuration entry.
- Preserve the installed `yalexs` dependency instead of replacing or patching it.
- Implement the missing PIN transport separately using its public brand/header
  helpers. Do not pass schedules through `Pin.access_times`, which treats schedule
  expressions as datetime strings.
- Keep PIN values out of entity state, attributes, logs and diagnostics. Design
  the admin interface so it does not put PIN payloads into automation traces.
- Serialize modifications per lock and reconcile ambiguous network failures.
- Preserve stable partner identifiers so access entries remain manageable after
  a restart. Never adopt app-created entries without a validated method.

When development begins, HACS requires one integration under
`custom_components/yale_access_manager/`, a versioned manifest, documentation
and repository metadata. A manifest will only be added when there is working
integration code.

## References

- [yalexs](https://github.com/Yale-Libs/yalexs)
- [yalexs-ble](https://github.com/Yale-Libs/yalexs-ble)
- [Home Assistant August](https://www.home-assistant.io/integrations/august/)
- [Home Assistant Yale Access Bluetooth](https://www.home-assistant.io/integrations/yalexs_ble/)
- [OAuth integration guidance](https://developers.home-assistant.io/docs/api_lib_auth/)
- [HACS integration requirements](https://www.hacs.xyz/docs/publish/integration/)
- [August Access Codes, an existing Seam-based alternative](https://github.com/iluvdata/august_access_codes)
