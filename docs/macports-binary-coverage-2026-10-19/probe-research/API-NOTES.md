# Installed MacPorts 2.12.7 metadata probe

`resolve.tcl OUTPUT_DIR PORT ...` uses the installed MacPorts Tcl interpreter,
installed base, and current local Portfiles. It creates an empty private registry
and private cache under `/tmp/macports-metadata-PID`; it never executes a target.
No sudo, registry mutation under `/opt/local`, sync, install, archive fetch, or
archive download is performed. Portfile evaluation itself is executable Tcl, so
the script is intended for the existing trusted MacPorts tree.

The source tree remains the local `/opt/local/var/macports/sources/rsync.macports.org/macports/release/tarballs/ports`.
The system and user configuration are read by `mportinit`; the temporary final
config overrides only `portdbpath`, `sources_conf`, and `buildfromsource never`.
Default variants and host settings are resolved by base, including `variants.conf`.

Exact APIs, in `/opt/local/libexec/macports/lib`:

- `macports1.0/macports.tcl:2531`: `mportopen URL OPTIONS VARIATIONS` evaluates
  Portfile, universal setup, requested/default variants, and callbacks.
- `macports1.0/macports.tcl:4373`: `mportinfo HANDLE` returns the evaluated
  `PortInfo` dictionary; worker variables give version/revision/portvariants.
- `package1.0/portutil.tcl:2694`: worker `get_portimage_name` returns the exact
  archive filename; do not synthesize variants or platform suffixes.
- `package1.0/portutil.tcl:840`: `_get_archive_platform` handles
  `platforms any`, including `darwin_any`.
- `package1.0/portutil.tcl:3327`: worker `get_canonical_archs` handles noarch,
  universal, and configured native architecture.
- `macports1.0/macports.tcl:4443`: `mportdepends HANDLE install 1 0 0 deps`
  resolves recursively with no installed/satisfied dependency pruning.
- `macports1.0/macports.tcl:4857`: `ports_binary_only=yes` makes the dependency
  types exactly `depends_lib` and `depends_run`; excludes build/fetch/extract/
  patch/test dependencies.
- `macports1.0/macports.tcl:4650`: `ditem_key HANDLE requires` contains direct
  resolved dependency names. Dependencies may be promoted to universal by the
  official architecture logic before their worker metadata is emitted.

One deliberate interception removes `mport_hint_install` from options passed to
`mportopen`, because base passes that hint for dependencies and it starts
speculative asynchronous archive availability checks (`portutil.tcl:3472`).
This leaves Portfile/variant/dependency resolution intact while keeping remote
archive checks under the caller's explicit HEAD-only procedure.

Files: `nodes.tsv` deduplicates by exact archive filename; `root-nodes.tsv`
records each root's complete resolved node set; `root-results.tsv` records base
resolution return status. Only status 0 represents successful metadata closure.
This is metadata evidence, not proof of installed runtime behavior or archive
availability. HEAD checks must cover all nodes and signature URLs separately.

Verified local example (`example/`): zlib, py312-setuptools, curl all resolve
status 0. curl includes its default `+brotli+http2+idn+psl+ssl+zstd` variants;
py312-setuptools produces `darwin_any.noarch`; zlib produces
`darwin_25.x86_64`. No archive was downloaded.
