# UI Refactor Plan — Settings Window + Inline Progress + Refresh

Date: 2026-04-12  
Project: brew-update-manager  
Goal: Move automation/settings controls to a dedicated settings window (gear), convert operation progress from popup/modal to inline panel, and add explicit refresh/status controls for clarity.

---

## 1) Scope

### Requested outcomes

1. Add a gear button for settings in the main header actions.
2. Gear hover should show localized tooltip (`Settings` / `הגדרות`).
3. Move all current Automation & Environment sections into this settings window (stacked vertically).
4. Progress UI for `check` / `update all` should no longer be a popup overlay.
5. Progress UI should appear inline in main page, between summary cards and the table area.
6. Add explicit refresh/status action so users can force-refresh and understand if an operation is running.

### In-scope files

- `renderer/index.html`
- `renderer/app.js`
- `renderer/i18n.js`

### Out-of-scope

- Backend API contract changes (unless strictly required)
- Data model/schema changes

---

## 2) Current-state findings (from code research)

1. Settings content is currently in `.settings-panel` under an accordion in main page flow.
2. Loader/progress is currently `.loader` with fixed overlay behavior (`position: fixed; inset: 0; z-index: 999`).
3. Live update package details panel already exists (`#updateLivePanel`) inside loader.
4. No explicit manual “refresh status” control in header.
5. Progress polling is event-driven by operation calls (`startProgressPolling`), not by user refresh.

---

## 3) Target UX and structure

### Header actions

- Keep existing buttons.
- Add:
  - `Refresh` button (localized) — manual status/state refresh.
  - `Settings` gear button (icon-only visual + localized tooltip).

### Settings window

- New centered modal window with backdrop.
- Move existing settings sections into this window body:
  - App version updates
  - Automatic checks
  - Homebrew path
  - Recent package updates
- Render sections stacked vertically (one under another), not in grid/accordion summary flow.

### Progress panel

- Keep existing progress internals and IDs where possible.
- Stop using fullscreen overlay mode.
- Render loader as inline panel in main content, between cards and tabs/table.
- Keep cancel button and package live details visible there.

---

## 4) Implementation sequence

### Phase A — Settings modal shell

1. Add `#refreshStatusBtn` and `#settingsBtn` to header actions.
2. Add settings modal DOM (`#settingsModal`, backdrop, content, close button).
3. Move existing settings panel markup into modal content (preserve existing element IDs to minimize JS churn).
4. Add CSS for modal/backdrop and vertical settings layout.
5. Add JS toggle handlers:
   - Open settings modal
   - Close modal (X button, backdrop click, Escape)

### Phase B — Inline progress panel

1. Reposition loader DOM so it sits after `#cards`.
2. Replace overlay CSS behavior with inline panel style:
   - Remove fixed positioning effect for operations
   - Keep panel hidden when idle, shown during operation
3. Update `setLoading()` checks from `display: flex` assumptions to a visibility/class-based approach.
4. Ensure `renderCheckProgress()` works when loader is inline.
5. Keep cancel flow unchanged (`cancelCurrentOperation`).

### Phase C — Refresh/status clarity

1. Add `handleRefreshStatus()` action:
   - Poll operation progress once
   - Reload state/settings/history safely
   - Show concise message if operation running
2. On app init, probe progress once and show inline panel if operation already running.
3. Keep auto polling for active operation; manual refresh acts as explicit user control.

### Phase D — Localization

1. Add EN/HE i18n keys for:
   - Settings button tooltip
   - Refresh button label and tooltip
   - Settings modal title/close label
   - Refresh status messages

### Phase E — Validation + release

1. Syntax checks (JS/Python files touched).
2. Manual smoke pass for:
   - settings open/close
   - check now
   - update all
   - inline progress visibility
   - refresh button behavior
3. Version bump.
4. Build release artifacts.
5. Commit/push/tag/release.

---

## 5) Risks and mitigations

1. **Risk:** Existing code assumes settings panel is always in-page.
   - **Mitigation:** Keep all original IDs; move markup without renaming IDs.

2. **Risk:** Progress rendering tied to `loaderEl.style.display === 'flex'`.
   - **Mitigation:** Update condition to use boolean visibility helper/class.

3. **Risk:** Modal + inline panel interaction conflicts with busy state.
   - **Mitigation:** Keep `busyLoading` as source of truth; avoid duplicating state machines.

4. **Risk:** Localization regressions for new controls.
   - **Mitigation:** Add complete EN/HE keys and verify `refreshStaticText()` covers new attributes.

---

## 6) Acceptance checklist

- [ ] Gear settings button exists and shows localized tooltip on hover.
- [ ] Clicking gear opens settings modal; sections are stacked vertically.
- [ ] Existing settings features continue working unchanged (scheduler, brew path, app update, history).
- [ ] Progress is inline (not popup overlay) for check/update operations.
- [ ] Inline progress appears between cards and table area.
- [ ] Refresh/status control exists and updates visible operation/state.
- [ ] EN/HE translations present and rendered correctly.
- [ ] Release build succeeds; version bumped and published.
