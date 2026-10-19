# Bounded tool installation discovery

Captured: 2026-10-19T10:05:50.027289+00:00

Read-only discovery; only the two discovery artifacts were written. No installs, updates, removals, manager state changes, or tool-list commands.

## Findings

| Export | Location | Resolved file | Version probe | Evidence |
|---|---|---|---|---|
| node | /Users/tzoharlary/.local/share/brew-update-manager/bin/node | /Users/tzoharlary/.local/share/brew-update-manager/bin/node | v26.12.0 | managed_registry |
| node | /usr/local/Cellar/node/26.0.0/bin/node | /usr/local/Cellar/node/26.0.0/bin/node | dyld[3236]: Library not loaded: /usr/local/opt/ada-url/lib/libada.3.dylib | homebrew_cellar |
| node | /usr/local/bin/node | /usr/local/Cellar/node/26.0.0/bin/node | dyld[3236]: Library not loaded: /usr/local/opt/ada-url/lib/libada.3.dylib | homebrew_cellar |
| node | /usr/local/opt/node/bin/node | /usr/local/Cellar/node/26.0.0/bin/node | dyld[3236]: Library not loaded: /usr/local/opt/ada-url/lib/libada.3.dylib | homebrew_cellar |
| npm | /Users/tzoharlary/.local/share/brew-update-manager/bin/npm | /Users/tzoharlary/.local/share/brew-update-manager/bin/npm | 11.20.0 | managed_registry |
| npm | /usr/local/Cellar/node/26.0.0/bin/npm | /usr/local/lib/node_modules/npm/bin/npm-cli.js | 11.17.0 | independent_location_candidate |
| npm | /usr/local/bin/npm | /usr/local/lib/node_modules/npm/bin/npm-cli.js | 11.17.0 | independent_location_candidate |
| npm | /usr/local/opt/node/bin/npm | /usr/local/lib/node_modules/npm/bin/npm-cli.js | 11.17.0 | independent_location_candidate |
| deno | /Users/tzoharlary/.local/share/brew-update-manager/bin/deno | /Users/tzoharlary/.local/share/brew-update-manager/bin/deno | deno 2.9.7 (stable, release, x86_64-apple-darwin) | managed_registry |
| deno | /usr/local/Cellar/deno/2.7.14/bin/deno | /usr/local/Cellar/deno/2.7.14/bin/deno | deno 2.7.14 (stable, release, x86_64-apple-darwin) | homebrew_cellar |
| deno | /usr/local/opt/deno/bin/deno | /usr/local/Cellar/deno/2.7.14/bin/deno | deno 2.7.14 (stable, release, x86_64-apple-darwin) | homebrew_cellar |
| ansible | /Users/tzoharlary/.local/share/brew-update-manager/bin/ansible | /Users/tzoharlary/.local/share/brew-update-manager/bin/ansible | ansible [core 2.21.5] | managed_registry |
| ansible | /usr/local/Cellar/ansible/13.6.0_1/bin/ansible | /usr/local/Cellar/ansible/13.6.0_1/libexec/bin/ansible | ansible [core 2.20.5] | homebrew_cellar |
| ansible | /usr/local/opt/ansible/bin/ansible | /usr/local/Cellar/ansible/13.6.0_1/libexec/bin/ansible | ansible [core 2.20.5] | homebrew_cellar |
| uv | /Users/tzoharlary/.local/bin/uv | /Users/tzoharlary/.local/bin/uv | uv 0.9.7 (0adb44480 2025-10-30) | independent_location_candidate |
| uv | /Users/tzoharlary/.local/share/brew-update-manager/bin/uv | /Users/tzoharlary/.local/share/brew-update-manager/bin/uv | uv 0.13.1 (x86_64-apple-darwin) | managed_registry |
| uv | /usr/local/Cellar/uv/0.11.18/bin/uv | /usr/local/Cellar/uv/0.11.18/bin/uv | uv 0.11.18 (Homebrew 2026-06-01 x86_64-apple-darwin) | homebrew_cellar |
| uv | /usr/local/bin/uv | /usr/local/Cellar/uv/0.11.18/bin/uv | uv 0.11.18 (Homebrew 2026-06-01 x86_64-apple-darwin) | homebrew_cellar |
| uv | /usr/local/opt/uv/bin/uv | /usr/local/Cellar/uv/0.11.18/bin/uv | uv 0.11.18 (Homebrew 2026-06-01 x86_64-apple-darwin) | homebrew_cellar |
| uvx | /Users/tzoharlary/.local/bin/uvx | /Users/tzoharlary/.local/bin/uvx | uvx 0.9.7 (0adb44480 2025-10-30) | independent_location_candidate |
| uvx | /Users/tzoharlary/.local/share/brew-update-manager/bin/uvx | /Users/tzoharlary/.local/share/brew-update-manager/bin/uvx | uvx 0.13.1 (x86_64-apple-darwin) | managed_registry |
| uvx | /usr/local/Cellar/uv/0.11.18/bin/uvx | /usr/local/Cellar/uv/0.11.18/bin/uvx | uvx 0.11.18 (Homebrew 2026-06-01 x86_64-apple-darwin) | homebrew_cellar |
| uvx | /usr/local/bin/uvx | /usr/local/Cellar/uv/0.11.18/bin/uvx | uvx 0.11.18 (Homebrew 2026-06-01 x86_64-apple-darwin) | homebrew_cellar |
| uvx | /usr/local/opt/uv/bin/uvx | /usr/local/Cellar/uv/0.11.18/bin/uvx | uvx 0.11.18 (Homebrew 2026-06-01 x86_64-apple-darwin) | homebrew_cellar |
| pandoc | /Users/tzoharlary/.local/share/brew-update-manager/bin/pandoc | /Users/tzoharlary/.local/share/brew-update-manager/bin/pandoc | pandoc 3.12.1 | managed_registry |
| pandoc | /usr/local/Cellar/pandoc/3.9.0.2/bin/pandoc | /usr/local/Cellar/pandoc/3.9.0.2/bin/pandoc | pandoc 3.9.0.2 | homebrew_cellar |
| pandoc | /usr/local/bin/pandoc | /usr/local/Cellar/pandoc/3.9.0.2/bin/pandoc | pandoc 3.9.0.2 | homebrew_cellar |
| pandoc | /usr/local/opt/pandoc/bin/pandoc | /usr/local/Cellar/pandoc/3.9.0.2/bin/pandoc | pandoc 3.9.0.2 | homebrew_cellar |

## Runtime caveat

The current /usr/local/bin/node resolves to Brew node 26.0.0 and aborts on --version because /usr/local/opt/ada-url/lib/libada.3.dylib is missing. The successful npm 11.17.0 probe uses currently managed Node through PATH; it does not prove that Brew Node works. Both /usr/local/bin/npm and Brew node/bin/npm resolve to the same /usr/local/lib/node_modules/npm/bin/npm-cli.js file.

## PATH and origin interpretation

The clean-environment login interactive zsh places the newly managed bin first, then later ~/.local/bin before /usr/local/bin. The inherited Codex environment places /usr/local/bin before ~/.local/bin. Therefore removing only the managed prefix would expose uv 0.9.7 in a clean login interactive shell, while the inherited agent shell would expose Brew uv 0.11.18. Both full captures are in discovery.json.

Only uv/uvx were found at the enumerated independent user locations. No independent Node/Deno/Ansible/Pandoc executable was found in those locations. This bounded absence does not establish that none exists elsewhere.

Node at /usr/local/bin currently resolves into the Brew Cellar. The historic org.nodejs.node.pkg receipt records v22.15.0; it is not evidence that the current node executable still belongs to that installer. npm resolves outside the Cellar at /usr/local/lib/node_modules/npm/bin/npm-cli.js; org.nodejs.npm.pkg records v10.9.2, but the current npm file origin requires additional evidence.

Ansible output reports core version. The registry and prior Brew audit report community distribution versions separately. Do not compare these as the same version series.

## Scope and evidence

Checked ~/.local/bin, ~/.deno/bin, ~/.nvm, ~/.fnm, ~/.local/share/fnm, ~/.volta, ~/.asdf, ~/.local/share/mise, ~/bin, /usr/local/bin, /opt/local/bin and the standard Node.framework bin patterns. Also checked only these packages in /usr/local/Cellar and /usr/local/opt to explain unlinked Brew copies. Most enumerated user manager roots are absent; JSON records each root.

Inspected only PATH/manager-related startup lines from .zprofile, .zshrc, .profile, .bash_profile and sourced ~/.local/bin/env. JSON records exact line numbers, link targets, UTC mtimes, successful/failed version outputs, installer receipt IDs and metadata. User preference and maintenance intent are not proven by these paths.
