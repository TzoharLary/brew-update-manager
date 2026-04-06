# Brew Update Manager (Desktop macOS App)

Electron desktop app for managing Homebrew updates locally on macOS.

## Distribution goal

The project is prepared to be published as a GitHub repository with downloadable macOS installer artifacts (`.dmg`) from Releases.

## Highlights

- Native desktop app window (no browser tab)
- Bilingual UI: English + Hebrew (with RTL support)
- Installed tab + Outdated tab
- Current version + latest version + both release-date columns:
	- Current version date
	- Latest version date
- `Check Now`, per-package `Update`, and `Update All`
- Automatic checks with user-configurable frequency/time (managed from app settings)
- In-app Homebrew path settings (auto-detect + manual override)

## Consolidated Project Layout

- `backend/brew-updates-service.py` — Python backend API + brew orchestration
- `tools/check-brew-outdated.sh` — launchd daily entrypoint
- `tools/open-brew-updates-app.sh` — opens/starts desktop app
- `tools/open-brew-updates-ui.sh` — compatibility alias to app launcher
- `renderer/` — UI files (`index.html`, `app.js`, `i18n.js`)
- `data/` — state, logs, and caches (created automatically)

## Runtime dependencies

- Python 3.14 (auto-detected from common macOS paths or `PATH`)
- Homebrew
- Node + npm (Electron runtime)

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
2. `npm run build`

Artifacts are created under `dist/` (for example, `.dmg` and `.zip`) for:

- `x64` (Intel Macs)
- `arm64` (Apple Silicon: M1/M2/M3)

> Note: current build configuration is for MVP unsigned distribution. For broad public distribution, add Apple signing + notarization in the release pipeline.

## Install from GitHub Release (for end users)

1. Download the latest `.dmg` from the repository Releases page.
2. Open the `.dmg` and drag **Homebrew Update Manager** into **Applications**.
3. Launch from **Applications**.

If macOS blocks the first launch (unsigned build), open:

- **System Settings → Privacy & Security**
- Allow app launch for the downloaded app

## Update the app from inside the app

The app includes an **App updates** section under **Automation & Environment**:

1. The app is already connected to the official release repo (`tzoharlary/brew-update-manager`).
2. On startup, the app automatically checks for a newer app version.
3. While the app is open, it re-checks periodically.
4. You can manually trigger the check using **Check for app update**.
5. If a newer release exists, click **Download app installer**.
6. macOS opens the downloaded `.dmg`; replace the app in Applications.

Notes:

- This updates the **desktop app itself** (from GitHub Releases).
- The existing **Check Homebrew packages now** / **Update all outdated Homebrew packages** buttons update Homebrew packages only (formulae/casks), not the app binary.
- Release assets must include architecture-specific DMG files (for `x64` and/or `arm64`).

## Security and legal notice

- This app is a third-party UI/automation layer for the Homebrew CLI.
- Homebrew is not bundled as this project's codebase.
- See `THIRD_PARTY_NOTICES.md` for third-party licensing and responsibility notes.

## Data and logs

- Development mode: `./data/`
- Packaged app mode: `~/Library/Application Support/Homebrew Update Manager/`
