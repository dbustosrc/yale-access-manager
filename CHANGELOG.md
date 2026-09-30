# Changelog

## 0.3.0

- Preserve the final disabled state when replacing a suspended guest and validate the saved Yale identity consistently across management and reconciliation.
- Run expiry cleanup independently of activity-history failures and entity subscriptions; retain ambiguous writes without replaying them.
- Add one administrator-controlled switch and state/expiry sensors per managed guest, plus lock-level problem and last-successful-sync diagnostics.
- Add native Repairs alerts and downloadable allowlisted diagnostics without PINs, guest names or account identifiers.
- Record operation phases, start times, transaction identifiers and sanitized errors; honor API rate-limit backoff and avoid redundant refreshes and unchanged journal writes.
- Add unlinking and phone removal, prefilled editing forms, eligible-action filtering and manual visit reset.
- Distinguish consumed and expired codes; recover up to 100 recent activities and report unconfirmed history continuity.
- Add explicit code-receipt confirmation, configurable 5–30 minute validity and 5–120 minute question cooldowns, and English/Spanish notifications.
- Make the blueprint detection entity selectable, recover unanswered questions without extra helpers and validate the blueprint against Home Assistant's automation schema.
- Test against the minimum supported Home Assistant version and the current stable container.

## 0.2.5

- Allow linking app-created and owner accesses to a Home Assistant person without granting PIN-management permissions or writing to Yale.
- Make the Companion phone optional for identity linkage; validate account ownership whenever a phone is selected.
- Retain external credential and Yale user identifiers separately from managed accesses, rejecting ambiguous or changed identities.
- Report linked keypad activity for external and persistent managed guests even outside a temporary code lease.

## 0.2.4

- Add an explicit pending-access cancellation action and native form that use the original PIN and managed partner ID, including when Yale no longer lists the attempted load.
- Remove a pending journal entry only after Yale accepts its deletion request and a fresh read confirms absence; preserve it on rejection or uncertainty.
- Show the pending access ID in blocked-operation errors and log confirmation timeouts with the last observed Yale state.
- Log accepted write transaction IDs without logging PINs or request bodies.
- Preserve failure/conflict response bodies even when the HTTP request itself succeeds; never treat them as an accepted cancellation.

## 0.2.3

- Fix Yale rejecting guests with a single-word name: omit the optional surname instead of sending an empty field.
- Apply the same handling to replacement and rollback, while verifying that the loaded guest has the expected name.

## 0.2.2

- Display Yale's original HTTP error response in management forms and action errors instead of replacing it with a generic rejection message.
- Log the complete error response and HTTP status while redacting PINs and authentication material; successful access responses remain unlogged.
- Preserve Yale error details through replacement rollback, activation and temporary issuance failures without retrying writes.

## 0.2.1

- Add a reusable Home Assistant blueprint for on-demand guest access with explicit face, phone, lock and door checks.
- Keep per-guest visit and 30-minute question cooldown state in the private access journal, avoiding separate helpers for each guest.
- Route the question to the Companion phone already linked to the managed guest.
- Require a disabled managed access before asking; keep code issuance conditional on a fresh detection and confirmed notification response.

## 0.2.0

- Link each managed guest to a Home Assistant person and a Companion phone using stable account and Yale user identifiers.
- Add disable and enable actions that keep the managed guest and its Yale identity.
- Add a ten-minute code issuance action that renews a managed guest PIN and sends it to the linked phone without returning the PIN to callers.
- Revoke issued access when its Yale activity is attributed to the managed guest, or when its Yale schedule expires; reconcile unconfirmed revocations after restarts without repeating writes blindly.
- Expose PIN-free activity through a native event entity on the lock device.

Yale API state transitions and user-ID persistence were checked on a temporary
managed guest. Physical keypad rejection while disabled remains unverified.

## 0.1.1

- Fix all access-management actions rejecting a correctly selected lock when Home Assistant assigns separate device IDs to August and Yale Access Manager.
- Validate the selected device's Yale lock identity and configuration account; unrelated devices and unloaded entries remain rejected.

## 0.1.0

- Add direct Yale/August cloud access management using an existing August OAuth account.
- Provide native configuration and management forms in English and Spanish.
- List existing access metadata without exposing PIN values; app-created entries are read-only.
- Create, replace and delete managed entries, with permanent, temporary and weekly schedule validation.
- Keep managed identifiers and interrupted-operation metadata across restarts without persisting PIN values.
- Require administrator permissions for access-management actions and validate the selected lock device.
- Confirm server-reported loading/removal and preserve uncertain operations instead of retrying writes blindly.
- Expose only access counts as diagnostic sensors on the existing lock device.
- Package the integration for installation as a HACS custom repository.

Temporary entry creation, replacement and removal have been verified through the
Yale API. Permanent and weekly schedule construction is covered by automated
checks; physical keypad verification and broader hardware coverage remain open.
