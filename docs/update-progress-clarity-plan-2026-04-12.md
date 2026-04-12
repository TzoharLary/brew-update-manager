# Plan — Update Flow Clarity, Concurrent Check Queue, and Progress UX

Date: 2026-04-12  
Project: brew-update-manager  

---

## Context

User requested improvements in five areas:

1. Clarify behavior of **Refresh Status** and what happens during an active update.
2. Allow **version check request while package update is running**.
3. Fix confusing phase labeling where UI may show "Running brew update" while current item is package-specific.
4. Improve ETA credibility and make its basis explicit.
5. Improve progress list readability:
   - show full lists for completed and remaining (no “...and more” truncation)
   - for failures, render expandable cards with package name + reason

---

## Implementation Goals

### Goal A — Safe check request during updates

- Keep update operation exclusive.
- When user requests check during an update, queue deferred check instead of returning busy conflict.
- Return `202` with `queued: true` and a clear message.
- Execute deferred check automatically after update operation completes.

### Goal B — Phase semantics and ETA trust

- Separate phase naming:
  - `brew_update` = actual `brew update` command only
  - `updating_packages` = package upgrade loop
- Improve ETA for package updates using completed package durations (rolling/average based), not only simplistic operation-start ratio.

### Goal C — Progress UI readability

- Completed and remaining lists show all entries (with scroll area), no truncation summary.
- Failure section becomes expandable card list:
  - summary = package name
  - details = normalized failure reason and raw hint text
- Typography and spacing tuned for readability.

### Goal D — Refresh status behavior clarity

- `Refresh Status` should:
  - fetch live `/api/progress`
  - refresh settings + history
  - refresh package state only when no active operation is running
- During active update: never interrupt operation; only re-sync UI state.

---

## Files To Change

- `backend/brew-updates-service.py`
- `renderer/app.js`
- `renderer/index.html`
- `renderer/i18n.js`

---

## Ordered Steps

### Step 1 — Backend queue support for check during update

1. Add deferred-check state + lock.
2. Update `POST /api/check`:
   - if update operation running: queue check and return `202` payload.
   - otherwise keep immediate check behavior.
3. Trigger deferred check execution after update operation end.

### Step 2 — Backend phase and ETA improvements

1. Replace update loop phase from `brew_update` to `updating_packages`.
2. Add ETA helper based on completed package durations for update loops.
3. Keep existing ETA behavior for general check flow where appropriate.

### Step 3 — Frontend check behavior during update

1. Keep `Start check` actionable during active update.
2. Handle queued response (`202`, `queued=true`) with user-facing message.
3. Ensure check action does not stop live polling of running update.

### Step 4 — Frontend progress panel UX improvements

1. Replace completed/remaining text blocks with full readable item lists.
2. Replace failed text block with expandable failure cards.
3. Add styles for list readability and failure details hierarchy.

### Step 5 — Refresh/status text and localization

1. Add/update EN/HE strings:
   - queued check message
   - phase label `updating_packages`
   - readable list/failure labels
   - ETA explanation copy

### Step 6 — Validate and release

1. Run syntax/lint checks for changed files.
2. Build release artifacts and validate delta generation.
3. Bump version and publish release.

---

## Acceptance Criteria

- [ ] Clicking check during update returns queued behavior (not blocking error).
- [ ] Deferred check runs automatically after update completes.
- [ ] Phase text is semantically correct (`brew_update` vs `updating_packages`).
- [ ] ETA behavior is derived from actual completed package timing in update mode.
- [ ] Completed and remaining show full list (scrollable), not truncated.
- [ ] Failed list is expandable cards with clear reasons.
- [ ] Refresh status during update does not interrupt update and accurately re-syncs UI.

---

## Risk Notes

- Deferred checks are in-memory queued state; service restart clears queue (acceptable for local desktop operation).
- Very large package lists can increase DOM size; use scrollable container and efficient rendering.
- ETA is still an estimate; with per-package timing it is meaningfully closer to reality than global linear approximation.
