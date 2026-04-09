# Update Reliability + History Accuracy — Refined Execution Plan

## Scope
Fix two production issues end-to-end:
1. App self-update may rollback unexpectedly (example: `1.1.1 -> 1.1.3`).
2. "Recently updated packages" can be missing/inaccurate.

Target release for this fix set: **`1.1.4`** (patch).

---

## 5-Angle Review (Refined)

### 1) Functional Correctness
- Risk: version parsing/comparison is too permissive around tag/manifest formats.
- Risk: history entries can be skipped if snapshot refresh fails after update.
- Action:
  - Harden semantic version normalization/comparison.
  - Persist history entries even when post-update snapshot partially fails.

### 2) Reliability & Observability
- Risk: update verification relies on a fragile process-start check and can falsely mark rollback.
- Risk: history endpoint/UI failures are swallowed silently.
- Action:
  - Improve update-apply verification and marker writing path.
  - Add explicit logging + user-visible fallback states for history loading failure.

### 3) Architecture & Lifecycle
- Risk: updater flow has multiple fallbacks (manifest/tag/legacy DMG) with ambiguous priority.
- Risk: write/read history is read-modify-write without lock discipline.
- Action:
  - Keep source-of-truth order explicit and validated.
  - Add lock around history file mutation.

### 4) Security & Safety
- Risk: malformed manifest or delta metadata can produce undefined update behavior.
- Risk: corrupted history JSON can break UX.
- Action:
  - Validate manifest schema essentials before use.
  - Add defensive recovery for history read failures.

### 5) Release & Versioning Discipline
- Risk: incomplete version propagation/verification causes mismatch between runtime/build/release assets.
- Action:
  - Bump `package.json` to `1.1.4`.
  - Verify manifest, tag naming, asset names, checksums.

---

## Implementation Sequence (Concrete)

### Phase A — Updater correctness (Electron)
**Files:**
- `electron/app-updater.js`

**Changes:**
1. Harden `normalizeVersion/parseSemver/compareVersions` behavior for real-world tag strings.
2. Strengthen update apply verification path (avoid false-negative startup checks).
3. Guard manifest usage with stricter shape checks and safer fallback logic.

**Acceptance:**
- Update check consistently identifies newer version when available.
- No false rollback on successful app restart.

### Phase B — Backend history durability (Python)
**Files:**
- `backend/brew-updates-service.py`

**Changes:**
1. Add lock discipline around update-history read/append/write.
2. Ensure history append happens even if snapshot refresh partially fails.
3. Improve error handling for load/save corruption scenarios.

**Acceptance:**
- Concurrent updates do not lose history entries.
- Failed snapshot does not erase/skip update record.

### Phase C — UI resilience (Renderer)
**Files:**
- `renderer/app.js`

**Changes:**
1. Improve history loading UX: explicit loading/error/retry handling.
2. Avoid silent empty state when endpoint fails.

**Acceptance:**
- User can distinguish “no history” vs “history failed to load”.

### Phase D — Release/version alignment
**Files:**
- `package.json`

**Changes:**
1. Bump version `1.1.3 -> 1.1.4`.
2. Rebuild release artifacts and verify update-manifest/checksums.

**Acceptance:**
- Produced artifacts + manifest all reference `1.1.4` consistently.

---

## Validation Matrix (Must Pass)

1. **Happy-path app update:** installed app updates to `1.1.4`, stays on new version.
2. **Rollback safety:** simulated startup failure causes rollback with clear marker.
3. **Concurrent history writes:** parallel update operations keep all entries.
4. **History persistence:** entries survive app restart.
5. **Manifest integrity:** malformed/missing fields fail safely to deterministic fallback.
6. **UI state correctness:** loading, success, and error history states are distinguishable.

---

## Non-Goals (for this patch)
- No protocol/schema breaking changes to external APIs.
- No major redesign of updater transport.
- No Python runtime requirement change.

---

## Risk Mitigation
- Keep changes small and localized to updater/history paths.
- Validate after each phase with focused checks.
- Preserve existing fallback flows while making them explicit and deterministic.

---

## Done Definition
- Code changes applied in updater + backend + UI.
- Version bumped to `1.1.4`.
- End-to-end checks completed successfully.
- Findings and release notes ready for tag/release.
