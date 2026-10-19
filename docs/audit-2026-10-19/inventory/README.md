# Current local inventory, 2026-10-19

Captured around 05:49 UTC. This is an architecture-planning inventory. No package installation, removal, update, trust, service restart, or application migration was performed. Read-style commands had two incidental behaviors described below; therefore this is not a claim that every command was filesystem-pure.

## Host and denominators

- macOS 26.7.1, build 25G241; Darwin 25.6.0 x86_64.
- Intel Core i9-9980HK, 16 logical CPUs, 16 GiB RAM.
- **233 canonical Homebrew formula identities + 7 casks = 240 package identities physically represented locally**, including ngrok, which is explicitly excluded from all action planning.
- `brew info --installed` and the app each report only 231 formulae + 7 casks = 238. Local Cellar receipts additionally prove `mongodb-community` 8.2.9 and `mongodb-database-tools` 100.16.1. Their receipts identify `mongodb/brew`; the last app run separately warned this tap is untrusted. The causal reason for omission was not proved by this audit.
- 234 Cellar directory names resolve to 233 canonical directories: `whisper-cpp` is a symlink to `whisper.cpp`, not a second installation.
- 284 formula version records include 283 actual receipt files. `llvm/23.1.3_2` is an **empty directory without a receipt** reported by brew metadata; its installation completeness is unverified. The active opt target is LLVM 22.1.5.
- Five managed alternative environments overlap existing Homebrew identities: ansible, deno, node, pandoc, uv. Count these as separate installation records/owners, not five additional logical package names.

## Formula intent and graph

51 formula identities have at least one receipt marked installed on request. Of the remaining 182, 101 have an explicit installed-as-dependency flag and 81 are not requested but lack that older optional dependency flag. Do not silently classify those 81 as removable. Requested intent also does not prove current use.

The graph contains 1,186 installed-receipt runtime requirements across retained kegs: **480 direct edges and 706 recorded transitive requirements**. Restricting direct edges to the current opt-target parent kegs gives 438 edges. `homebrew-direct-edges.json` and separate reverse-dependent arrays in `homebrew-rows.json` preserve these distinctions.

These are recorded requirements, not measured dylib linkage or executable health. 296 recorded version requirements have no same-version target keg directory; newer installed versions, ABI compatibility, and retained historical receipts mean this number is not a broken-package count. MongoDB's receipt directly requires `mongosh` 2.8.3, which has no local Cellar identity or `/usr/local/bin/mongosh`; this is an unresolved dependency gap. The missing LLVM receipt is a separate coverage limitation. No global linkage scan was run.

Retain Homebrew libraries while any Homebrew direct or transitive consumer still needs them. Choosing a managed alternative for a root executable does not satisfy the old Homebrew receipt or rewrite its linked consumers.

## Current application state

`app-snapshot.json` contains state/settings/plan and the minimized last-run results plus managed registry. Last run finished 2026-10-19 04:22:29 UTC and reports **27 deferred items**, matching the cached app outdated count. This confirms saved current application state, not a newly refreshed remote availability check.

The 27 are cryptography, gnupg, gpgme, gpgmepp, include-what-you-use, libtensorflow, llvm, merve, net-snmp, nmap, nss, open-mpi, openjpeg, openldap, p11-kit, pango, php, poppler, postgresql@17, py3cairo, simdjson, socat, terminal-notifier, tesseract, unbound, wget, whisper.cpp.

## Other bounded discovery

- Managed Node 26.12.0 works. npm's effective global prefix remains `/usr/local`, with **10 global package rows**, independently corroborated by `/usr/local/lib/node_modules/*/package.json`. Global npm ownership is shared with the prior Node installation rather than isolated in the managed Node prefix.
- uv tool inventory has **notebooklm-mcp-cli 0.9.11**. Multiple uv installations coexist: managed 0.13.1, app/Homebrew 0.11.18, and standalone `~/.local/bin/uv` 0.9.7.
- The pipx command is absent from PATH, but retained `~/.local/pipx/venvs/imagesorcery-mcp/pipx_metadata.json` proves **imagesorcery-mcp 0.11.4**, using Python 3.12.0. This is why command-only discovery is incomplete.
- App-bundled Pixi exists and reports zero global environments. The three managed Pixi environments have copied TOML manifests and lockfiles in this directory; their internal dependencies must remain separately owned, not added to a flat top-level count.
- `/usr/bin/gem list --local` enumerates **48 gem identities**, including macOS/system defaults. They are discovery evidence, not automatic migration targets.
- **63 top-level native app bundles** were read from `/Applications` and `~/Applications`; app-store/vendor/self-update ownership was not proved for each. Installer receipt IDs are recorded separately; they are historical installation evidence, not a list of currently runnable apps.
- No PATH commands or checked standard install directories established active cargo/rustup, conda, mise, pyenv, nvm or fnm inventories. This is bounded absence evidence, not a machine-wide absence claim. pnpm's list failed because its configured bin directory is outside PATH; checked global directories do not exist.
- Bun is installed, but `bun pm ls -g` unexpectedly enumerated the **home project**, showing 284 total/6 top-level packages. This output is explicitly wrong scope and is not a global Bun inventory. Standard Bun global package paths were absent. No project directories were traversed by the audit code.

`other-package-rows.json` contains the 10 npm, 1 uv tool, 1 pipx, and 48 system Ruby rows. Native app rows are in their own file. Their scopes overlap or differ; do not advertise their sum as all software on the machine.

## Service preservation

`services.json` captures Homebrew service status, formula executable/data/log metadata, targeted LaunchAgent/LaunchDaemon metadata, and path existence only. Homebrew reports php, postgresql@17 and unbound as `none`, and no matching registered launch plists were found. This does not exclude manually started daemons. PostgreSQL and MongoDB data directories and MongoDB/unbound/php configuration locations exist; contents and actual databases were not read. Any future migration needs explicit service ownership, data backup, major-version compatibility and rollback checks.

## Discovery requirements for future packages

1. Reconcile manager metadata with physical receipts and aliases, including untrusted or unavailable tap metadata; preserve excluded installed rows.
2. Use installation records keyed by provider + package + version + prefix, and logical identities above them. Keep dependencies, executable resolution, service/data ownership and update intent as separate dimensions.
3. Support Homebrew, managed native/Pixi/uv, npm globals, pipx/uv tools, Ruby gems, app bundles and installer receipts as adapters. Discover missing commands via bounded standard inventory roots.
4. Keep project lockfiles/environments opt-in and separately scoped. Never infer global ownership from whichever project a manager happens to inspect.
5. Preserve `unknown` and `unavailable`; do not turn a failed metadata request into removal or source-build permission. New packages should be discovered on subsequent inventory scans without a fixed name allowlist.

## Incidental effects and reproducibility

Old uv `tool list` warned that it evicted malformed interpreter-cache entries. Those entries cannot be reconstructed; no package changes were requested. The Bun list command reported reading `.env` and migrating npm lock metadata. No secret values were emitted or retained. Follow-up checks found no `bun.lock` or `bun.lockb` in home, repository, or the standard Bun global directory; the home package and npm lock retain January 2026 modification times. **No persisted Bun lock change was confirmed; no user file was deleted or restored.**

The saved collector no longer invokes Bun listing or old uv tool listing. `finalize_inventory.py` invokes no package-manager commands and reproduces the canonical Homebrew rows from saved metadata plus local receipts. `summary.json`, `homebrew-cellar-reconciliation.json`, `homebrew-rows.json`, `homebrew-receipts.json` and the two edge files are the machine-readable primary planning artifacts.
