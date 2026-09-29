# Changelog

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
