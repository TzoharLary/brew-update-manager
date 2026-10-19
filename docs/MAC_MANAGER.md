# Mac Update Manager

Homebrew formulae/casks, MacPorts active ports and existing isolated tool installs
share one inventory. `kind=port` unambiguously identifies MacPorts in the existing
selection API; legacy formula/cask requests remain Homebrew identities.

MacPorts executes only binary installs/upgrades. Every action resolves exact
runtime archives on the current OS/architecture and checks archive plus signature
availability. MacPorts verifies downloaded signatures. Missing archives, unknown
variants and uncertain metadata block the action. A scan uses the local catalog;
the explicit MacPorts refresh button requests macOS admin authorization to sync.

POST /api/migrations/plan {name} returns a 15-minute, exact-graph approval plan.
POST /api/migrations/approve {id} revalidates and executes terminal-notifier only.
Other mapped tools require project/consumer migration; update-all never migrates.
POST /api/macports/sync refreshes the catalog with administrator authorization.
All operations share the existing exclusive-operation lock and cancellation state.

The application keeps its existing dedicated notification helper and notification
permission identity. MacPorts owns the terminal-notifier CLI; its default bundle
ID is not substituted for the application's already authorized delivery helper.
Retrying a failed verification reuses an exact, already installed MacPorts port.

The app ID, legacy data/profile paths, updater repository, notification group and
launch-agent identity remain stable. The visible product name is Mac Update Manager.
ngrok stays excluded. User settings, data and unmanaged paths are preserved.

Run tools/implement-mac-manager.py check|apply|verify|pack|install|pilot|restore.
All source changes are embedded in that script. Its recovery manifest preserves
the exact previous dirty files, and restore refuses files edited after application.
The install and pilot stages require successful verification in the same process.
No system package or service is migrated by apply/verify/pack.

Root operations create receipts and logs in root-owned /private/tmp/mac-manager.*
directories. A canceled admin prompt fails without installation. Cancellation of
an active root port command uses a separate macOS authorization to stop its tree.
Incomplete migration journals remain visible for review; they never auto-resume.


## 1.2.4 check progress

Each combined scan resets old update results and reports Homebrew, MacPorts, and route checking separately. Cancellation stops tracked commands and preserves the previous complete snapshot. Provider errors remain visible as partial results; failure of both providers is reported as an error. Finished operations show no current item.


## 1.3 automatic alternative checks

Start Check refreshes the MacPorts catalog when its last verified sync or local index is older than 24 hours. Foreground scans request macOS authorization only when needed; scheduled checks report awaiting authorization and never open an unattended prompt. Failed or declined refresh keeps local inventory visible as unverified. All mapped Homebrew binary blockers are checked together, including full runtime closures. Shared exact archives are checked once; available results expire after six hours and missing observations after thirty minutes. Unknown network results are not reused. Availability is not installation/signature verification or migration approval. Only the supported terminal-notifier pilot can execute a reviewed migration; other available alternatives explicitly require consumer migration plans. Unknown mappings are displayed as unverified, not absent. Regular update actions never migrate packages.


## 1.3.1 installed-version baseline

Alternative updates compare the mapped MacPorts upstream component versions with the actual Homebrew installation, not with Homebrew latest. All mapped components must avoid a downgrade and at least one must be newer. A version below Homebrew latest can qualify after complete binary closure checks; it is never described as the last Intel-supported release without upstream evidence. Equal upstream versions and unknown comparisons remain distinct from updates. Cached recommendations are invalidated on policy or installed-version changes. Every approved migration repeats the same policy with a fresh inventory. The primary action is labeled Update all / עדכן הכול.
