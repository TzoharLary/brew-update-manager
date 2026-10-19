# Multi-provider update architecture: source audit and implementation plan

Date: 2026-10-19. Scope: design only against repository version 1.1.18. No packages were installed, updated, removed, or repaired for this document; no live application endpoints or tests were invoked. File references below are repository-relative, with one-based line numbers at audit time. Host measurements and provider availability belong to the companion inventory/routes audits; proposed examples here are contracts, not verified available releases.

## Decision

Keep the Python service, Electron renderer, existing isolated environments, serial execution, and conservative binary-only default. Evolve the current engine into a small provider coordinator. First make discovery, ownership, planning, health, and recovery correct; then expand provider coverage. Adding more names to `PIXI_TOOLS` does not solve these systemic gaps.

The useful promise is: **every discovered installation has an owner, every update has an explainable compatible route, and unsupported cases remain visible with an honest reason**. It is not possible to promise automatic safe updates of every arbitrary future program from its command name alone.

## Existing foundations worth retaining

- `update_engine.Engine` already separates plan/install/run, stages managed tools, probes them before activation, and persists deferred plans and batch outcomes (`backend/update_engine.py:82-131,133-219,221-300`).
- `bottle_blockers()` walks runtime dependencies, rejects missing metadata/options/source requirements, and respects exclusions (`backend/update_routes.py:131-171`).
- Homebrew upgrades recheck exclusions and binary dependencies immediately before execution; unexpected build output stops the process (`backend/brew-updates-service.py:2353-2410`).
- Batch failure stops later roots, cancellation has process-group control, and restart restores prior results (`backend/update_engine.py:247-300`; `backend/brew-updates-service.py:1081-1148,3013-3031`).
- Previous environments and Brew kegs are retained; the current design intentionally avoids library and database replacement (`README.md:58-89`). Preserve that separation.
- Tests already cover exclusion persistence, dependency closure, batch fail-stop, canceled planning, candidate rejection, live output, timeout and cancellation (`tests/test_update_policy.py:39-142`; `tests/test_upgrade_activity.py:21-90`). These are useful starting fixtures, not proof of a complete provider architecture.

## Prioritized correctness gaps

“Blocker” below means a gate before claiming general multi-provider management, not that an incident has been reproduced on this host.

| Priority | Gap and consequence | Current source evidence | Required correction |
| --- | --- | --- | --- |
| P0 | Discovery is Brew-owned. Managed tools without a Brew row disappear; a Brew outage prevents the entire snapshot. | Service `compute_snapshot():2028,2064-2077`; `apply_update_policy():683-703` only overlays supplied rows. | Union independent provider inventories. Keep provider failure/staleness explicit; preserve known installations when one provider fails. |
| P0 | One row conflates the active CLI with a retained Brew installation. A managed version suppresses the Brew row's outdated status even if Brew dependents still use that keg. | Service `696-702`; Brew dependent checks explicitly disabled by engine `24-25`; retained-keg limitation documented in README `80-83`. | Separate installation IDs, consumer edges and active command bindings. CLI success cannot mark a Brew keg or its dependents healthy. |
| P0 | “Latest” and comparisons are Brew-derived even for Pixi/native/uv; numeric digit tuples drop Brew revision and prerelease semantics. | Service `1331-1338,698-702`; routes `32-39`; engine `93-94,195-196`. | Each provider owns release/version semantics. Compare cross-provider upstream versions only through a verified mapping; retain revision/build/channel separately. Unknown ordering is `unknown`, never “current.” |
| P0 | Activation is only partly atomic. It swaps the tool symlink, then writes shims one by one, then registry. Partial executable sets are accepted; command collisions overwrite existing shims. | Routes `94-127`, particularly `99-109,110-127`. Existing test only rejects a wholly absent candidate before switching (`tests/test_update_policy.py:110-120`). | Prevalidate complete export set/collisions, journal old/new generation, use one publication point for command mappings, verify after switch, and recover or rollback on failures. |
| P0 | Plan is not a frozen authorization of exact mutations. No input digest, dependency action list, ownership constraints, artifact-lock hash, or stale-plan rejection. Brew upgrades can change dependencies after preflight. | Engine `82-131,240-261`; service `2371-2402`; plan contains name/provider/version/readiness. | Record exact resolved artifacts and effects; revalidate installed fingerprints, policy, runtime, lock and metadata before each step. Report/stop on unexpected mutations. Use provider-native locking/transaction semantics where available; do not claim Brew supports an exact replay it cannot enforce. |
| P1 | Binary readiness is inconsistent. uv is “ready” when its executable exists; its dependency wheel resolution occurs during installation. Pixi uses `>=` the Brew version, so it can move beyond an intended major. | Engine `121-123,184-191`; routes `63-75`; engine `108-116`. | Resolve before ready; use exact resolved versions/artifacts and declared channel/major constraints. uv candidate requires full binary-only resolution including the actual Python ABI and dependency prerelease policy. |
| P1 | Binary compatibility is mostly a process architecture label and a launch probe, without OS/deployment target/runtime ABI evidence. Internal Brew API cache path/codename is assumed. | Engine `23,34-41,56-80,198-215`; routes `78-91`. | Capture OS build/version, machine and process arch, translation state if relevant, provider subdir, Python ABI and linked runtime requirements. Candidate compatibility can be proven, rejected, or unknown. A URL/filename is insufficient. |
| P1 | The journal records only completed package results. Crash after installing/activating but before result write has no durable transaction stage; restart restores presentation, not reconciliation. | Engine `240-241,256-273`; service `3015-3031`. | Per-step durable prepare/install/verify/activate/commit records and explicit restart reconciliation. Never blindly rerun an interrupted mutation. |
| P1 | Runtime dependencies of the manager are not lifecycle-managed. Pixi/uv are checked by file existence, Python by minor version. Repository scheduled script and Electron use different Python selection; this is not a claim about the currently installed LaunchAgent. | Engine `104-105,121-123,184`; Electron `main.js:72-105`; `tools/check-brew-outdated.sh:13-35`. | One shared runtime manifest/resolver, pinned compatible helper versions/digests, runtime health and a separate staged update path with rollback. Updating user `uv` does not update app-owned `runtime/bin/uv`. |
| P1 | Ownership and exclusion IDs use short Brew names; full tap identity is display metadata. Settings do not express pin/channel/major/migration policy. | Service `159,661-676,1325,1342-1344,1360`; routes `50-60`. | Namespaced provider IDs and separate policy for installation, logical software, channel and permitted mutations; preserve existing ngrok exclusions through migration. |
| P1 | “Command runs” does not establish consumer/linkage health. Most exports have no individual probe, no baseline or post-upgrade Brew linkage verification. | Engine `197-217`; routes `99-101`; service `2407` uses exit code; engine `24-25` disables dependent checks. | Health facets for CLI, exports, dynamic linkage, services/data and consumers; preexisting broken linkage must remain a distinct issue even after CLI migration. |
| P2 | UI counts represent merged rows and readiness reasons without stable installation identity; deferred matching uses name only. | Service `summarize_counts():1981-1982`; renderer `app.js:2033-2035,2089-2113`. | Show software plus expandable installations; count actionable updates, deferred updates, exclusions and health issues separately; match plan items by installation ID and plan ID. |

## Minimal domain model

Use versioned JSON files and Python dataclasses/typed dictionaries initially. No daemon cluster, arbitrary plugin loader, database server, new language, or central solver is needed. A provider is a trusted built-in adapter that speaks a uniform contract; each ecosystem's own resolver remains responsible for its dependency semantics.

1. **Software identity**: optional verified equivalence such as “Node.js.” Give it an opaque stable ID. It groups installations for display; matching names alone never establishes equivalence.
2. **Provider package identity**: e.g. `homebrew:formula:homebrew/core/node`, `conda:conda-forge/nodejs`, or a configured publisher product ID. It includes repository/channel namespace and package kind. Preserve provider aliases and rename lineage.
3. **Installation**: provider package plus owner and location/scope. Two Homebrew prefixes or two uv tool environments are separate installations. Keep raw provider version, revision/build, architecture, receipt fingerprint and health independently.
4. **Command binding**: which installation supplies `node` in a declared scope, along with export set, shim owner, effective PATH resolution and collisions. A shell CLI activation is not a system-wide replacement.
5. **Dependency/consumer edge**: `runtime_link`, `build_only`, `python_runtime`, `service_data`, `command_invocation` or `unknown`. Record evidence source and freshness. Provider package dependencies and actual installed consumer links are different evidence sets.
6. **Candidate and operation**: exact provider release/build/artifacts plus a compatibility result, policy decisions, affected installations/commands/data, expected health checks and rollback level.

Avoid inventing a universal graph of every script/project on disk. Start with provider receipts/dependency metadata, Brew installed dependent/linkage evidence, command exports and known runtime/service relationships. Unknown external consumers are an explicit limitation on migration, not grounds to mark every ordinary CLI update unsupported.

An illustrative installation record (IDs and version strings are schematic):

```json
{
  "schema_version": 2,
  "installation_id": "inst-managed-node-01",
  "software_id": "software-nodejs",
  "package_id": "publisher:nodejs/node",
  "provider": "native-node",
  "scope": {"kind": "user_cli", "owner": "current-user"},
  "location": "/example/managed/environments/node/generation-01/prefix",
  "version": {"raw": "24.x.y", "scheme": "provider-semver", "build": null},
  "channel": "stable",
  "origin_receipt": "receipt-sha256-placeholder",
  "exported_commands": ["node", "npm", "npx"],
  "health": {"commands": "verified", "consumer_linkage": "not_applicable"},
  "discovered_at": "2026-10-19T00:00:00Z"
}
```

The retained Brew installation gets a different installation ID, its own version/outdated/linkage state and consumer edges. The group may say “Node command current; Homebrew dependency installation needs attention.”

## Provider contract

Extract adapters incrementally from existing code, not by rewriting all service endpoints. Keep `Engine` as coordinator; move Brew commands/catalog parsing, Pixi lock handling, native Node/Deno assets, and uv resolution behind adapters. A small interface is enough:

```python
class Provider:
    id: str
    capabilities: Capabilities

    def probe_runtime(self, host) -> RuntimeHealth: ...
    def discover(self, context) -> DiscoveryResult: ...
    def releases(self, package_ref, policy, context) -> ReleaseResult: ...
    def compare_versions(self, installed, candidate) -> VersionRelation: ...
    def resolve(self, installation, candidate, host, policy) -> Resolution: ...
    def revalidate(self, operation, context) -> RevalidationResult: ...
    def stage(self, operation, journal, cancellation) -> StagedInstall: ...
    def verify(self, target, checks) -> HealthReport: ...
    def activate(self, staged, bindings, journal) -> ActivationResult: ...
    def rollback(self, transaction, journal) -> RollbackResult: ...
    def reconcile(self, interrupted_transaction, context) -> RecoveryResult: ...
```

`discover()` reports complete/partial/failed with timestamp and source; a provider failure must not be converted into an empty installation list. `releases()` reports unavailable/stale/unsupported distinctly from no update. `resolve()` is observational except for private cache/workspace creation; it cannot switch commands or mutate an installed environment. Cancellation is passed through metadata requests/resolution/downloads and child processes.

Capabilities advertise only implemented behaviors: `discover`, `list_releases`, `binary_resolve`, `stage_isolated`, `activate_commands`, `in_place_update`, `rollback_activation`, `rollback_package`, `service_or_data_migration`. Brew in-place update has weaker rollback than a managed CLI generation; the UI must reflect that.

Initially expose four adapters corresponding to existing behavior: Homebrew, Pixi/conda, publisher Node/Deno, and uv binary-wheel tools. A common native download helper can serve explicit publisher manifests. Do not turn unknown GitHub repositories into executable adapters based only on matching names.

## Deterministic route selection

The default operation updates the installation through its existing owner. Changing owners is a migration operation with additional checks. This avoids repeatedly switching a working tool because another provider reports a newer release first.

For each installation:

1. Discover owner, identity, scope and healthy baseline. Apply exclusions/pins before resolution.
2. Ask its provider for candidates inside configured channel/major/version policy. Use that provider's version ordering, not a shared digit regex.
3. Resolve complete candidate dependencies/artifacts against the measured host/runtime. Reject source-required, incompatible, untrusted or policy-violating mutations. Return exact reasons for unknown/unavailable data.
4. Prefer an eligible candidate owned by the current provider. Within that provider select the newest eligible candidate in policy, with deterministic build preference and artifact ID tie-breaks.
5. If no current-owner candidate is eligible, enumerate configured alternative-provider mappings. Only independently installable user CLI software is automatically eligible for an already authorized migration policy. Libraries, compilers, services, shared runtimes and data stores require explicit impact handling.
6. Rank migration candidates by a fixed policy tuple: verified software mapping, platform compatibility, channel/major match, complete binary closure, trust policy, activation rollback support, provider preference, release/build ordering. Never compare unrelated provider raw versions lexicographically; unresolved equivalence is `mapping_required`.
7. Freeze the chosen resolution and its evidence in the plan, including alternative rejection reasons. No silent source-build or second-provider fallback during execution.

Most future packages become supported through provider discovery and metadata without adding a per-name route. Per-product mappings remain necessary where ecosystems use different names/export sets/version schemes or migration semantics. Store them as small declarative, reviewed records, with provenance. Provider-owned metadata can populate mappings where it actually establishes upstream identity; fuzzy names may suggest a review but cannot authorize mutation.

Unknown installation behavior: retain it as `owner_unknown` or `provider_read_only`, display observed path/version/evidence, and offer a concrete next step. Do not execute arbitrary `--version`, install scripts or self-update commands against every executable discovered on PATH. Start discovery from configured manager receipts, then optional bounded PATH/application identification.

## Host compatibility and policy

The current Intel host must choose artifacts compatible with the measured x86_64 machine, its actual macOS version and the active interpreter/runtime. Persist both hardware and process architecture so a later Apple Silicon/Rosetta setup can be distinguished. Avoid assuming that every non-arm64 process is supported Intel or that an architecture match proves OS support.

Candidate evidence includes provider's selected platform/build, minimum OS where available, required runtime ABI, binary-only dependency resolution, artifact digest/source, and isolated launch/smoke results. Native assets without compatibility metadata remain unproven until verified in staging; verification must occur before command activation. For Python tools, resolve using the actual selected Python version/ABI and wheel platform tags. Do not promote `noarch`/pure-Python metadata into proof that its native dependencies have compatible wheels.

Policy fields should cover:

- Exclusion selector scope: logical software, provider package or installation; specify whether it prevents root updates and dependency mutations. Preserve existing formula/cask exclusions when upgrading schema.
- Channel and prerelease rule, optional allowed major/version range or exact pin; provider-specific revision/build identity; automatic migration permission and preferred providers.
- Source builds default disabled; optional separately chosen source policy, resource budget and user-visible impact, rather than a silent fallback.
- Trusted repositories/publishers, binary integrity requirement, rollback/health requirements, and discovery scopes.

An excluded dependency already installed in a satisfying version may be consumed without mutation if policy allows; an operation that would change it is blocked. Current `bottle_blockers()` rejects an excluded dependency before checking whether it already satisfies the desired version (`routes:144-157`); refine this using the resolved mutation set without weakening exclusions. Pins constrain dependencies too; the planner must not allow a root to bypass them.

## Plan, migration and recovery

Persist `plan_id`, `schema_version`, `created_at`, host fingerprint, policy revision/hash, discovery revisions, runtime fingerprints, resolved artifact/lock hashes, installation before-state, explicit dependency/command/service/data effects, verification checks and rollback level. Never expose credentials or full inherited environment in the plan.

Each root owns a DAG of provider operations. Deduplicate the same provider installation/dependency action across roots. Do not merge dependencies across isolated environments merely because their names match. A failed shared action blocks its downstream roots. Retain current whole-batch fail-stop initially; later safe independent continuation can be added only with tested dependency boundaries.

Revalidate immediately before mutation. If policy, host/runtime, receipt, dependency graph or locked artifacts changed, mark `plan_stale` and regenerate/review changed effects. A fetch failure is not permission to use different artifacts. Brew may not guarantee exact snapshot execution; document that limitation, use its supported controls, recalculate its dependency eligibility at the mutation boundary and compare actual effects afterward. No unsupported promise of all-provider atomicity.

Managed CLI transaction:

1. Journal `prepared` with before binding/registry hashes and candidate destination.
2. Download and verify digest/trust metadata; install into a new private generation using frozen resolution, then journal `installed`.
3. Verify required exports, executable format/permissions, representative behavior and declared linkage/runtime requirements; journal `verified`.
4. Prepare complete command map/shims and reject unmanaged/existing owner collisions. Publish through one atomic generation pointer. Stable launcher stubs should resolve the same committed generation map; no independently writable per-command state during cutover.
5. Journal `activated`, verify actual public command resolution and smoke behavior, atomically commit the registry generation, journal `committed`.
6. On verification/commit failure switch to the previous pointer, verify rollback and record both original failure and rollback outcome. A crash during cutover is reconciled from pointer/receipt hashes at startup.

Keep prior generations until a separate explicit retention/cleanup policy. Staging may leave removable candidate directories; cleanup is not part of package update success. Rollback of a command binding is not rollback of user data, globally installed npm packages/native addons, services, external consumers, or Brew dependency changes. Migration records must call these effects out.

Homebrew keeps ownership of its kegs/libraries. Before treating an update as healthy, distinguish baseline linkage failures from newly introduced failures using bounded provider health checks and receipts. If a retained keg is broken, expose a separate repair plan listing affected consumers and eligible reinstall/relink actions; do not attempt generic replacement of `.dylib` files from another ecosystem. Databases and persistent services need product-specific migration/backup/rollback semantics before automating ownership changes.

Cancellation is cooperative before destructive/activation boundaries. A canceled download or stage leaves old bindings active; cancel during an atomic activation is deferred until commit or rollback is stable. For in-place Brew operations, terminate as currently implemented but mark state `needs_reconcile`, rediscover actual versions and health before retry. Successful prior steps remain committed; interrupted steps are neither successes nor automatic retries.

Startup reconciles unfinished journals before accepting new mutations. It can restore a known managed generation, adopt a verified already-committed effect, or block the affected installation with `recovery_required`. Keep read-only inventory available. Use a process-wide file lock scoped to shared tools/registry, because current `OP_LOCK` is only in-process (`service:1227-1248`) while `TOOLS_ROOT` is shared (`service:57-75`); a second backend/development or scheduled process must not race activation or journal writes.

## Manager runtime and scope boundaries

The app runtime is a separately owned installation group: Python 3.14 patch, Pixi, uv, Electron/app version and provider API capability versions. It needs a manifest containing version, artifact origin/digest, platform and executable paths. Both scheduler and Electron should consume the same runtime receipt and pass the same data/tools roots. Verify helper output/version/capabilities, not just existence. Store user CLI uv independently from the app's helper uv. The source-script mismatch does not establish a live scheduler defect: the installed LaunchAgent may already invoke the isolated Python/backend directly, and must be checked against the companion host inventory before proposing a repair.

Helper updates stage alongside current versions, run provider contract/smoke checks, then switch the runtime pointer. The currently running Python service cannot reliably replace its own live environment; activate its candidate only on a controlled next start, with an Electron-side startup/fallback health check. Retain a bootstrap/repair path independent of the Python being replaced. First milestone only aligns runtime resolution and reports missing/outdated helpers; autonomous self-updating can follow once recovery is tested.

Scope is explicit:

| Scope | Intended behavior |
| --- | --- |
| User/global CLI installations | Discover and update through their owner; consider reversible isolated CLI migrations with evidence. |
| Global package-manager tools | A later adapter can discover npm global/uv tool/pipx-style environments; keep their runtime and prefix ownership explicit. This table is a design slot, not a verified provider list. |
| Project dependencies/virtual environments | Detect only configured scopes; do not recursively upgrade lockfiles, environments, transitive libraries or native addons as a side effect of a global runtime update. Project maintenance needs its own test/lock policy. |
| Shared libraries, compilers, databases/services | Update inside the owning manager with consumer/health checks; ownership migration requires dedicated recipes. A replacement CLI does not satisfy their runtime consumers. |
| Desktop applications | Homebrew casks remain an existing provider scope. Self-updaters, app-store installations and other application installers need independent adapters and application identity; do not double-own them. |
| macOS/firmware | Separate OS workflow, reboot/admin policy and compatibility decision; do not include in a generic package batch. |

## Trust and failure isolation without overengineering

Keep adapters built-in and reviewed; accept data manifests, never downloaded executable route code. Reuse existing argument arrays and private prefixes. Record trusted source, integrity evidence and lock digest per candidate. Publisher checksums downloaded from the same publisher protect integrity but should not be described as independent publisher authentication. Verify signatures/attestations where an adapter explicitly supports them; otherwise state the actual evidence.

Bound HTTP timeouts, redirects, archive paths/size, external metadata and logs. Current tar extraction uses the Python data filter (`engine:167-168`); preserve that protection and check extracted export paths remain in the staging prefix. Do not let provider metadata select arbitrary shell commands, absolute export paths or external executables.

The localhost API currently accepts mutation requests with no visible request authentication (`service:2570-2594,2699-2708`; `electron/main.js:128-149`). Before broader provider execution, bind requests to the app session (e.g. random local token passed by main process), validate requests and bound body size. This is a local authorization boundary, not a cloud account/OAuth project. New catalog data rendered in package names/descriptions must use text nodes or explicit safe markup; current `renderer/app.js:2093-2096` interpolates metadata into `innerHTML`. These are source findings, not demonstrated exploits.

## Hebrew UI model

Keep one software row for scanning and expand it to show installations. Suggested columns: **תוכנה | גרסה פעילה | עדכון זמין | מצב | פעולה**. A detail panel shows **מי מנהל את ההתקנה**, **מה ישתנה**, **למה נבחר המסלול**, **מה תלוי בה**, and **אפשרות חזרה**. Use provider names in details, with concise Hebrew consequences in the main view.

States must remain separate: **מעודכן**, **מוכן לעדכון**, **נדחה — אין חבילה מוכנה תואמת**, **מוחרג**, **מוצמד לגרסה**, **לא ניתן לבדוק כרגע**, **נדרשת הגדרה**, **תקלה בהתקנה**, **נדרשת התאוששות**. Unknown metadata is not current; deferred is not success; an excluded update is not deleted inventory.

Example: “Node פעיל דרך התקנה נפרדת. ההתקנה של Homebrew עדיין בשימוש של שתי חבילות.” Only show the number when dependency evidence measured it. For linkage: “הפקודה פועלת; נמצאה תקלה נפרדת בספרייה של Homebrew.” A plan summary says “יעודכנו 4 התקנות; 2 יישארו ללא שינוי בגלל החרגה; 3 נדחו” with each denominator sourced from the current plan. The update button is enabled by ready operations, not raw outdated rows.

Migration copy: “תותקן גרסה מוכנה בהתקנה נפרדת, ולאחר בדיקה הפקודות יעברו אליה. ההתקנה הקודמת תישמר. תלות של Homebrew לא תועבר.” Show exact exported commands and impacts in expanded details; the common flow should not require the user to understand Conda solver terminology.

## Phased implementation and acceptance gates

Each phase can be delivered as a reviewable incremental change. Do not expand automatic migrations before phases 1–4 pass.

### Phase 1 — Honest discovery and identity

Extract Brew discovery; union managed registry entries independently. Migrate settings/registry to namespaced package and installation IDs with schema version and backups. Preserve legacy API fields as a renderer compatibility projection, not the authoritative model. Keep old Brew installation/outdated/health facts independent from active CLI state. Report provider freshness/errors and app-owned helper runtime inventory.

Acceptance: managed-only installation survives removed Brew row; Brew discovery failure retains last-known Brew records marked stale while other providers succeed; identical names across tap/prefix/provider do not merge; broken/missing active prefix is not reported verified; existing formula/cask ngrok exclusions migrate without changing their effect; duplicate app processes cannot mutate the shared registry concurrently.

### Phase 2 — Policy and provider-owned candidates

Implement uniform releases/compare/resolve for the four existing route types. Remove Brew latest as the authority for managed tools. Exact Pixi locks and binary uv resolution become readiness evidence. Explicit channel/major/pin rules replace implicit `>=` and global prerelease allowance. Host/runtime evidence is saved in each resolution; unknown mappings remain deferred.

Acceptance: provider-newer-than-Brew releases are visible; Brew-newer-than-provider produces an honest provider availability result; prerelease/stable, epoch/revision/build fixtures use provider semantics; pinned major never moves unexpectedly; missing Intel/OS/ABI-compatible artifacts defer before installation; missing uv transitive wheel is caught during plan; no unconfigured prerelease is accepted.

### Phase 3 — Explicit effects and revalidation

Introduce plan IDs, dependency actions, input hashes, command collision checks, immutable artifact/lock references and per-step revalidation. Continue serial execution and whole-batch fail-stop. Separate update from migration; retain kegs and validate health/consumers. Extend single, selected, all and scheduled entry points through the same policy and planner.

Acceptance: excluded/pinned dependency mutation blocks all affected roots; using an already satisfying immutable dependency is allowed only by policy; policy/receipt/runtime/lock change invalidates a plan; unrelated newly installed software cannot be accidentally selected; shared failure runs once and blocks downstream roots; active managed CLI does not hide outdated/broken retained Brew keg; preexisting linkage failure is not labeled introduced by a successful update.

### Phase 4 — Transactional activation and recovery

Add versioned generation registry, one activation publication point, pre/post probes, durable step journal, process file lock and startup reconciliation. Align Electron/scheduler runtime selection. Add runtime health status and a tested manual runtime recovery path before autonomous helper updates.

Acceptance: fault injection before/after every journal and pointer write yields old verified generation, new verified generation or explicit `recovery_required`; never unrecorded mixed command ownership. Missing one required export or shim collision fails before activation. Post-switch failure rolls back and probes old commands. Cancel during stage keeps old commands; cancel during cutover reaches stable commit/rollback. Interrupted Brew install is rediscovered, not blindly replayed. Two simultaneous backends cannot both acquire mutation ownership. Scheduler still starts if only the valid app-owned Python exists.

### Phase 5 — Broader provider discovery and migration coverage

Add providers guided by measured host inventory, not a speculative checklist. Prefer provider-level discovery/candidate metadata, declarative verified software mappings and reusable artifact/activation helpers. Add read-only adapters first; enable mutation only when provider lifecycle/health tests pass. Add product-specific service/data recipes only when needed and separately reviewed.

Acceptance: a newly installed supported manager package appears without editing a package-name allowlist; an unknown manager remains visible with concrete next action; same-name unrelated products never migrate automatically; desktop self-updating apps are not concurrently owned by two adapters; project lockfiles/environments are untouched by global CLI batches; fresh installation on the current Intel/OS host selects actual compatible binaries or explains why it cannot.

### Phase 6 — UI completion and runtime lifecycle

Render installation details, clear status counts, route explanations, plan effects/rollback and provider errors in Hebrew/English; reuse the fixture-based Electron UI testing approach. Finish staged helper runtime updates and startup fallback only after phase 4 recovery gates are proven.

Acceptance: installation/outdated/deferred/excluded/unknown/health counts reconcile to their displayed denominator; independent CLI and Brew keg states remain understandable; stale plans cannot show as executable; Hebrew/English mixed provider names/paths render correctly; public command health reflects actual effective resolution; helper rollback restores manager startup; no upgrade is marked successful based only on its exit code or a registry version string.

## Verification plan for the implementation, not this audit

Use offline provider JSON/release/artifact/ABI fixtures, a temporary filesystem, mock subprocess/network clients and deterministic fault injection. Extend existing policy/progress tests at the coordinator boundaries. Keep a small contract suite each adapter must satisfy (discovery failures, version ordering, exact resolution, policy propagation, cancellation, transaction recovery). Use representative staging integration checks only after read-only planning is stable, with temporary roots and explicit approved packages; do not turn tests into real global updates.

The first implementation milestone should be phases 1–2 for the existing providers. It produces a truthful current-machine inventory and route plan without committing to additional installations. Expansion then has a measurable denominator: discovered installations, independently checkable installations, eligible updates, deferred reasons, successful verified actions, and unresolved health issues.
