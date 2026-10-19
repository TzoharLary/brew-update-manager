# Mac Update Manager (Desktop macOS App)

Electron desktop app for Homebrew, MacPorts and managed tool updates on macOS.

## Distribution goal

The project is prepared to be published as a GitHub repository with downloadable macOS installer artifacts from Releases, including:

- macOS installers (`.dmg`) for manual install
- full in-app updater packages (`-full.tar.gz`)
- optional differential updater packages (`-delta.tar.gz`)
- `update-manifest.json` + `update-checksums.txt`

## Highlights

- Native desktop app window (no browser tab)
- Bilingual UI: English + Hebrew (with RTL support)
- Installed tab + Outdated tab
- Advanced table controls: column sorting + filters (type/status) + search and quick reset
- Current version + latest version + both release-date columns:
	- Current version date
	- Latest version date
- `Check Now`, per-package `Update`, and `Update All`
- Automatic checks with user-configurable frequency/time (managed from app settings)
- In-app Homebrew path settings (auto-detect + manual override)
- Recent update history with search, filter, and sorting controls
- Live update explanations in English/Hebrew: downloads, required packages,
  source builds, and installation, with elapsed times and optional technical details.
  Stages follow Homebrew output; silence is reported without claiming a stall or
  inventing a completion percentage. Homebrew build logs can supply a task count
  or compiler percentage; this is labelled separately from time remaining.
- Persisted exclusions apply to every update entry point, counts and notifications.
  Excluded software remains installed, labelled in the installed inventory.
- Preflight checks the runtime dependency tree before using Homebrew bottles.
  Binary-only mode defers unavailable/source builds with a visible reason.
- Optional Pixi/conda-forge routes for selected CLI tools, official Intel/ARM
  Node and Deno downloads with publisher SHA-256 verification, and a uv route
  for Azure CLI. Isolated installations are activated only after runtime probes.
- A durable run journal preserves completed results. A failed command stops the
  batch before later packages can repeat the same failed shared build.

## Consolidated Project Layout

- `backend/brew-updates-service.py` — Python backend API + brew orchestration
- `tools/check-brew-outdated.sh` — launchd daily entrypoint
- `tools/open-brew-updates-app.sh` — opens/starts desktop app
- `tools/open-brew-updates-ui.sh` — compatibility alias to app launcher
- `renderer/` — UI files (`index.html`, `app.js`, `i18n.js`)
- `data/` — state, logs, and caches (created automatically)

## Runtime dependencies

- Python 3.14 (prefers the app-owned `runtime/python/current/bin/python3.14`
  under its Application Support directory, then common macOS paths or `PATH`)
- Homebrew
- Node + npm for development/builds; the installed app contains Electron.

## Update routes and recovery

The Update policy panel controls binary-only mode and explicit exclusions such
as `cask:ngrok`. Missing binary routes remain outdated; deferral is not success.
The app does not replace Homebrew libraries, migrate database data, remove
formulae, or globally substitute compilers. Binary tool ownership is configured
in `settings.json` (`binary_tools`); supported tool names live in
`backend/update_routes.py`. The locally provisioned Pixi and uv executables live
in the app's `runtime/bin/`, independently of Homebrew.

Tools are staged under `~/.local/share/brew-update-manager/environments/`;
verified commands are exposed through `~/.local/share/brew-update-manager/bin`.
The local setup adds this directory to the user's zsh PATH and preserves prior
startup files in its recovery receipt. Existing Homebrew installations remain.
Ansible environments pin Python 3.14, avoiding an observed Python 3.15 startup
failure. Azure CLI uses binary wheels with an explicit prerelease policy for its
SDK dependencies; no PyPI source builds are allowed.

Upgrades have no arbitrary two-hour limit by default. An optional
`upgrade_timeout_seconds` setting accepts 60–86400 seconds (0 means unlimited).
Cancellation still terminates the active process group. Homebrew automatic
metadata refresh and install cleanup are disabled per command; Check Now
explicitly refreshes metadata. Automatic dependent upgrades are disabled for
planned Homebrew upgrades so that they cannot launch unplanned source builds.
Tools with broken Homebrew linkage require separate diagnosis; an isolated CLI
replacement does not repair the retained Homebrew keg.

Managed tools remain in the inventory after their redundant Homebrew keg is
removed. Their catalog metadata is currently still queried from Homebrew;
independent provider release discovery remains part of the architecture plan.
An adopted standalone uv installation updates in its existing user directory
using `uv self update`, rather than creating another Pixi environment.
See `docs/tool-installation-reconciliation-2026-10-19/README.md` for the measured
local consolidation, preserved backups, and the retained Homebrew Node dependency.
The subsequent `docs/macports-recovery-2026-10-19/README.md` records the consumer
checks and removal of that redundant Node keg, its compatibility command link,
and recovery of MacPorts 2.12.7 with binary-only installation policy. No Homebrew
packages have been migrated to MacPorts as part of that recovery.

`update-plan.json` records ready/deferred routes. `last-update-run.json` is
written after every result, and completed results remain in update history.
After a batch, inventory refresh reuses cached descriptions/dates rather than
performing network enrichment. No previous successful update is reinstalled
merely because the earlier batch was canceled.

## Run locally after clone (exact order)

1. `npm install`
2. `npm run start-app`

The app entrypoint is defined in `package.json` via `start-app`, so users do not need to choose a file manually.

## Configure automatic checks from inside the app

Use the **Automation & Environment** panel in the app:

1. Enable automatic checks
2. Choose frequency:
	- Daily
	- Weekly (with weekday)
	- Every X hours
3. Choose time/interval
4. Save schedule

The app writes and manages a per-user LaunchAgent (`~/Library/LaunchAgents/com.local.brew-outdated-check.plist`).

## Configure Homebrew path from inside the app

Use **Homebrew path** in the same panel:

- **Auto-detect path**: app searches common brew locations and proposes one
- **Save path**: user confirms and saves a custom brew binary path
- **Use automatic only**: clears custom path and falls back to auto-detection

No installation-time prompt is required; this is configurable later from inside the app UI.

> Important: scans and updates are blocked until a Homebrew path is explicitly saved in app settings.

## Build installer artifacts (macOS)

1. `npm install`
2. `npm run build` (quick local build, current Mac architecture only, DMG)

For a full release build (both architectures + updater assets):

- `npm run build:release`

Both build commands automatically clean `dist/` first, so older version artifacts are removed before new ones are created.

Release artifacts are created under `dist/` for:

- `x64` (Intel Macs)
- `arm64` (Apple Silicon: M1/M2/M3)

Artifacts include:

- `Mac Update Manager-<version>-<arch>.dmg`
- `Mac Update Manager-<version>-<arch>-full.tar.gz`
- `Mac Update Manager-<prev>-to-<version>-<arch>-delta.tar.gz` (when previous release assets are available — manifest full package or DMG fallback)
- `update-manifest.json`
- `update-checksums.txt`

Release builds now run strict delta round-trip validation by default (`STRICT_DELTA_VALIDATION=true`):

- each generated delta is applied to the previous app bundle in a staging area
- the resulting app bundle tree is compared to the new full bundle
- if any architecture fails validation, the release build fails (instead of publishing a risky delta)

> Note: current build configuration is for MVP unsigned distribution. For broad public distribution, add Apple signing + notarization in the release pipeline.

## Install from GitHub Release (for end users)

1. Download the latest `.dmg` from the repository Releases page.
2. Open the `.dmg` and drag **Mac Update Manager** into **Applications**.
3. Launch from **Applications**.

If macOS blocks the first launch (unsigned build), open:

- **System Settings → Privacy & Security**
- Allow app launch for the downloaded app

## Update the app from inside the app

The app includes an **App updates** section under **Automation & Environment**:

1. The app is already connected to the official release repo (`tzoharlary/brew-update-manager`).
2. On startup, the app automatically checks for a newer app version.
3. While the app is open, it re-checks periodically.
4. You can manually trigger metadata-only check using **Check version update**.
5. If a newer release exists, click **Update version** to explicitly start download/install.
6. The updater prefers a differential package when available for your current version, and automatically falls back to full package if needed.
7. The app shows live update progress (percent, speed, ETA), verifies SHA-256 checksums, stages the update, then restarts with apply-on-quit.

Notes:

- This updates the **desktop app itself** (from GitHub Releases).
- **Start Check** and **Update all** operate on Homebrew packages, installed MacPorts ports and managed tools, subject to update policy and exclusions. They do not update the app itself or migrate packages between providers.
- Differential updates are architecture-aware (`x64` vs `arm64`) and include rollback marker handling if restart validation fails.

## Release workflow expectations (for updater)

When publishing a new tag release:

1. Run `npm run build:release`
2. Verify `dist/` contains DMG/full/manifest artifacts (and delta artifacts when available)
3. Publish all updater assets to the GitHub Release:
	- `dist/*.dmg`
	- `dist/*-full.tar.gz`
	- `dist/*-delta.tar.gz` (if generated)
	- `dist/update-manifest.json`
	- `dist/update-checksums.txt`

The release CI workflow already uploads these assets automatically.

## Security and legal notice

- This app is a third-party UI/automation layer for the Homebrew CLI.
- Homebrew is not bundled as this project's codebase.
- See `THIRD_PARTY_NOTICES.md` for third-party licensing and responsibility notes.

## Data and logs

- Development mode: `./data/`
- Packaged app mode: `~/Library/Application Support/Homebrew Update Manager/`

Package upgrades also append both output streams to `logs/brew-upgrades.log`.
The live panel keeps only the latest 12 lines in its collapsed technical details.

Offline progress checks (do not run Homebrew):
`python3 -m unittest discover -s tests -v`

UI fixture checks in Hebrew and English (mock IPC, no backend):
`node_modules/.bin/electron tests/progress-ui.cjs`


## Mac Update Manager 1.3.1

See [combined provider behavior and local rollout](docs/MAC_MANAGER.md).
