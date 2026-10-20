# Mac Update Manager — implementation and product direction

Updated 2026-10-20. Source version: 1.3.3. This documentation-only review did not
query a live service or rerun product tests. The installed build and package state
must not be inferred from package.json alone.

Read [the product goal](PRODUCT_GOAL.md) for the user's current requirements and
[the task record](MAC_MANAGER_TASKS.md) for status and acceptance criteria. Those
decisions replace earlier designs requiring a separate approval for each migration
or full test runs by the coding agent before handing the user a test script.

## Required user flow — not yet complete

Start Check discovers updates and compares supported providers for native Intel
macOS. Select the newest suitable upstream version with a ready binary runtime
closure. Retain the existing owner on an otherwise equal route. Homebrew, MacPorts
and Pixi participate as their execution routes are implemented and verified.

Show installed/target versions, current/executing provider and the reason. A source
provider change is visible before the user clicks Update selected/all. That click
authorizes the displayed actions; there is no separate per-package migration
wizard. macOS may still require administrator credentials. Revalidate before any
change; a materially changed proposal returns for updated selection.

After successful migration, persist provider ownership and discover future versions
from that provider even without the Homebrew copy. Unknown metadata is unverified,
not absent. Unsupported migration blocks the affected package with a reason.
Never silently build from source, downgrade, or discard exclusions and user data.

## Existing inventory and update paths

Homebrew formulae/casks, active MacPorts ports and existing isolated tools share
an inventory. `kind=port` identifies MacPorts in the selection API; legacy
formula/cask requests remain Homebrew identities. Existing Pixi-managed tools
do not establish general Pixi alternative discovery or migration support.

MacPorts preflight resolves runtime archives for the current OS/architecture and
checks archive/signature availability. Installation uses MacPorts' binary mode
and signature verification. Availability alone does not prove a working migration.
Catalog refresh and cache handling exist; provider freshness and error behavior
still need regression coverage when integrated into the common selection policy.

Combined scans report provider stages and preserve partial errors. Version 1.3.2
introduced final progress in operation responses, retained terminal results,
protection against old responses and unverified status after communication loss.
Inventory evidence separates manager versions, active keg, receipt, PATH and
upstream/revision. Only explicitly configured commands, initially LLVM clang,
are probed. Evidence conflicts block affected decisions.

T02 classifies candidate roles using receipts, reverse dependencies, files,
interfaces, services and bounded linkage checks. Private projects are not scanned.
Evidence is stored in `migration-evidence` under the application data directory.
A standalone classification does not itself authorize execution.

## Existing migration APIs — internal/legacy path

- `POST /api/migrations/plan {name}` creates a proposal.
- `POST /api/migrations/approve {id}` executes a supported proposal.
- `POST /api/migrations/recovery-plan {transaction_id, action}` creates a recovery
  proposal; the current engine does not execute resume/rollback through approve.
- `POST /api/macports/sync` requests catalog refresh with macOS authorization.

The old terminal-notifier implementation remains separate. The 1.3.3 additions
delegate other names to `migration_engine.py`; `migration_profiles.py` defines
terminal-notifier and include-what-you-use profiles. This is partial groundwork,
not a completed common transaction engine. Current update-all does not perform
general cross-provider migrations. See [the T03 review](T03_VALIDATION.md).

The current engine writes plans and transactions under `DATA_DIR/migrations/`
and a route record under `TOOLS_ROOT/migrations/routes.json`. A written route
does not prove shims were activated or future provider upgrades are integrated.
Do not put MacPorts ownership into the legacy installed.json tool registry and
thereby manufacture a duplicate installed tool.

The existing notification helper, permission identity, app/data/profile identity,
launch agent, exclusions (including ngrok), settings and history must remain stable.

## Validation and delivery

The coding agent implements code/tests and performs only focused lightweight
checks appropriate to the edit. The user runs the full suite and acceptance
script; inspect its saved report and avoid rerunning unchanged suites needlessly.
Full unrun checks must be reported as pending. Isolated checks must not mutate
real packages; real acceptance shows the actions and asks for one overall approval.

`tools/verify-migration.py --checks` now calls `verify-current-code.py` directly,
independent of old bundle manifests, and saves per-step logs and a summary.
The full suite awaits the user run. The helper also has an IWYU-only acceptance mode. It is not yet the complete script for the required product flow. Its
limitations and the distinction between historical runs and future validation
are recorded in T03_VALIDATION.md. Do not recommend a real migration until its
preflight, activation, rollback and acceptance gaps are resolved.

[The 1.3.2 validation report](T01_T02_VALIDATION.md) preserves historical evidence;
it does not certify later edits. Local delivery only; no push or release is implied.

## Generator and recovery

Product, tests and documentation are generated through
`tools/implement-mac-manager.py`; edit that script with apply_patch and run terminal
commands interactively. Preserve dirty work. Never replay legacy apply or
document-tasks over a newer bundle, reset README, or overwrite recovery manifests
to silence a precondition failure.

The 2026-10-20 direction update uses `direction-docs-check`, `direction-docs-apply`
and `direction-docs-restore`. These touch documentation only, with a separate
backup and manifest under `recovery/documentation-direction-2026-10-20/`.
Restore refuses subsequent independent edits. Existing bundles keep their old
manifests; their hash checks may correctly reject these newer documentation
files. Adapt the next implementation/verification bundle to the current baseline.
