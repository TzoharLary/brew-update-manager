"""Read-only, bounded local inventory. Writes only into this audit directory."""
import collections
import datetime
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess

OUT = Path(__file__).resolve().parent
HOME = Path.home()
APP = HOME / 'Library/Application Support/Homebrew Update Manager'
MANAGED = HOME / '.local/share/brew-update-manager'
ENV = dict(os.environ, HOMEBREW_NO_AUTO_UPDATE='1', HOMEBREW_NO_ANALYTICS='1')
ENV['PATH'] = str(MANAGED / 'bin') + ':' + ENV.get('PATH', '')

def write(name, obj):
    (OUT / name).write_text(json.dumps(obj, indent=2, ensure_ascii=False) + '\n')

def read(path):
    return json.loads(Path(path).read_text())

def run(args, timeout=35):
    try:
        p = subprocess.run([str(a) for a in args], env=ENV, capture_output=True,
                           text=True, timeout=timeout)
        output = p.stdout.strip()
        try:
            output = json.loads(output)
        except (json.JSONDecodeError, ValueError):
            pass
        return {'command': [str(a) for a in args], 'exit_code': p.returncode,
                'output': output, 'stderr': p.stderr.strip()[:3000]}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {'command': [str(a) for a in args], 'error': str(e)}

now = datetime.datetime.now(datetime.timezone.utc).isoformat()
host = {'captured_at': now, 'uname': run(['/usr/bin/uname', '-smr']),
        'macos': run(['/usr/bin/sw_vers']),
        'cpu': run(['/usr/sbin/sysctl', '-n', 'machdep.cpu.brand_string']),
        'logical_cpus': run(['/usr/sbin/sysctl', '-n', 'hw.logicalcpu']),
        'memory_bytes': run(['/usr/sbin/sysctl', '-n', 'hw.memsize'])}
write('host.json', host)

raw = read(OUT / 'homebrew-installed-metadata.json')
excluded = run(['/usr/local/bin/brew', 'info', '--json=v2', '--cask', 'ngrok'])
if excluded.get('exit_code') == 0:
    rows = excluded['output'].get('casks', [])
    for x in rows:
        x['audit_excluded_from_actions'] = True
    raw['casks'].extend(x for x in rows if x['token'] not in {c['token'] for c in raw['casks']})
write('homebrew-installed-metadata.json', raw)

formulae = raw['formulae']
byname = {f['name']: f for f in formulae}
aliases = {f.get('full_name', f['name']): f['name'] for f in formulae}
def short(name):
    return aliases.get(name, name.rsplit('/', 1)[-1])

receipts = []
edges = []
rows = []
for f in formulae:
    n = f['name']
    installs = []
    for i in f.get('installed', []):
        path = Path('/usr/local/Cellar') / n / i['version'] / 'INSTALL_RECEIPT.json'
        receipt = read(path) if path.exists() else {}
        source = receipt or i
        r = {k: source.get(k) for k in ['installed_on_request', 'installed_as_dependency',
             'built_as_bottle', 'poured_from_bottle', 'time', 'arch', 'compiler', 'homebrew_version']}
        r.update({'name': n, 'version': i['version'], 'receipt_path': str(path),
                  'receipt_exists': path.exists(), 'runtime_dependencies': source.get('runtime_dependencies') or [],
                  'runtime_dependencies_recorded': source.get('runtime_dependencies') is not None})
        receipts.append(r)
        installs.append({k: r[k] for k in ['version', 'installed_on_request', 'installed_as_dependency',
                                          'built_as_bottle', 'poured_from_bottle']})
        for dep in r['runtime_dependencies']:
            d = short(dep['full_name'])
            edges.append({'from': n, 'from_version': i['version'], 'to': d,
                'required_version': dep.get('pkg_version', dep.get('version')),
                'declared_directly': dep.get('declared_directly'), 'target_installed': d in byname,
                'source': 'installed_receipt_runtime_dependencies'})
    on_request = any(i.get('installed_on_request') is True for i in installs)
    dependency = any(i.get('installed_as_dependency') is True for i in installs)
    rows.append({'id': 'formula:' + n, 'name': n, 'kind': 'formula', 'tap': f.get('tap'),
        'installed': installs, 'linked_keg': f.get('linked_keg'), 'keg_only': f.get('keg_only'),
        'installed_on_request': on_request, 'installed_as_dependency': dependency,
        'role': 'requested_root' if on_request else ('dependency' if dependency else 'unknown_or_legacy'),
        'current_metadata_dependencies': f.get('dependencies', []),
        'current_metadata_build_dependencies': f.get('build_dependencies', []),
        'outdated': f.get('outdated'), 'pinned': f.get('pinned'),
        'excluded_from_actions': n == 'ngrok'})
for c in raw['casks']:
    n = c['token']
    rows.append({'id': 'cask:' + n, 'name': n, 'kind': 'cask', 'tap': c.get('tap'),
                 'installed': c.get('installed'), 'role': 'cask_root',
                 'outdated': c.get('outdated'), 'excluded_from_actions': n == 'ngrok',
                 'depends_on': c.get('depends_on', {}), 'artifacts': c.get('artifacts', [])})
    for kind in ['formula', 'cask']:
        deps = c.get('depends_on', {}).get(kind, [])
        if isinstance(deps, str): deps = [deps]
        for dep in deps:
            edges.append({'from': n, 'from_kind': 'cask', 'to': dep, 'to_kind': kind,
                          'source': 'current_cask_metadata', 'declared_directly': True})

reverse = collections.defaultdict(set)
for e in edges:
    if e['source'] == 'installed_receipt_runtime_dependencies':
        reverse[e['to']].add(e['from'])
for r in rows:
    r['installed_reverse_dependents'] = sorted(reverse[r['name']]) if r['kind']=='formula' else []
    r['installed_reverse_dependent_count'] = len(r['installed_reverse_dependents'])
write('homebrew-receipts.json', receipts)
write('homebrew-rows.json', {'captured_at': now, 'rows': rows})
write('homebrew-edges.json', {'captured_at': now, 'edge_semantics':
    'Installed formula receipt runtime_dependencies, including recorded transitive edges; declared_directly distinguishes direct dependencies. Current formula metadata edges are separate in rows, not asserted as installed receipt truth. Cask edges use current metadata.', 'edges': edges})

app = {}
for name in ['state.json', 'settings.json', 'update-plan.json', 'last-update-run.json']:
    d = read(APP / name)
    if name == 'last-update-run.json':
        d['results'] = [{k: v for k,v in r.items() if k not in ['stdout','stderr']} for r in d.get('results', [])]
    app[name] = d
registry = read(MANAGED / 'installed.json')
app['managed_registry'] = {n: {k:v for k,v in r.items() if k!='probe_output'} for n,r in registry.items()}
write('app-snapshot.json', app)

manager = {'scope': 'Bounded global-tool/env discovery only. No project traversal or package installation.',
           'path_resolution': {n: shutil.which(n, path=ENV['PATH']) for n in
            ['uv','pipx','pixi','conda','mise','pyenv','nvm','fnm','bun','npm','node','cargo','rustup','gem','pnpm']},
           'commands': [], 'known_directories': {}}
def add(*args, timeout=35):
    result = run(args, timeout)
    manager['commands'].append(result)
    return result

uvs = [MANAGED/'bin/uv', APP/'runtime/bin/uv', HOME/'.local/bin/uv', Path('/usr/local/bin/uv')]
seen = set()
for uv in uvs:
    if uv.exists() and str(uv.resolve()) not in seen:
        seen.add(str(uv.resolve()))
        add(uv, '--version')
        if uv == MANAGED/'bin/uv':
            add(uv, 'tool', 'list')
add(MANAGED/'bin/node', '--version')
add(MANAGED/'bin/npm', 'ls', '-g', '--depth=0', '--json')
add(MANAGED/'bin/npm', 'config', 'get', 'prefix')
global_node_modules = Path('/usr/local/lib/node_modules')
global_pkgs=[]
if global_node_modules.exists():
    candidates = list(global_node_modules.glob('*/package.json')) + list(global_node_modules.glob('@*/*/package.json'))
    for path in candidates:
        try:
            pkg=read(path)
            global_pkgs.append({'name':pkg.get('name'), 'version':pkg.get('version'), 'path':str(path.parent)})
        except (OSError, ValueError): pass
manager['legacy_usr_local_npm_global_packages'] = global_pkgs

for name, args in [('pipx',['list','--json']), ('pixi',['global','list','--json']),
                   ('conda',['env','list','--json']), ('mise',['ls','--json']), ('pyenv',['versions','--bare']),
                   ('fnm',['list']), ('cargo',['install','--list']),
                   ('rustup',['toolchain','list']), ('gem',['list','--local']), ('pnpm',['list','-g','--depth=0','--json'])]:
    path = shutil.which(name, path=ENV['PATH'])
    if not path and name=='pixi' and (APP/'runtime/bin/pixi').exists(): path=str(APP/'runtime/bin/pixi')
    if path: add(path, *args)
for rel in ['.cargo/bin','.rustup/toolchains','.pixi/bin','.pixi/envs','.conda/envs',
            'miniconda3/envs','anaconda3/envs','.nvm/versions/node','.fnm/node-versions',
            '.pyenv/versions','.local/share/mise/installs','.local/share/uv/tools',
            '.local/share/pipx/venvs','.local/pipx/venvs','.bun/install/global/node_modules',
            'Library/pnpm','Library/Application Support/pypoetry/venv']:
    p=HOME/rel
    manager['known_directories'][rel] = {'exists':p.exists(), 'children': sorted(x.name for x in p.iterdir())[:100] if p.is_dir() else []}
manager['managed_environment_locks'] = []
for n,r in registry.items():
    workspace=Path(r['workspace'])
    lock=workspace/'pixi.lock'
    manifest=workspace/'pixi.toml'
    if lock.exists():
        target = OUT/('managed-' + n + '-pixi.lock')
        target.write_text(lock.read_text())
        manager['managed_environment_locks'].append({'name':n,'source':str(lock),'audit_copy':target.name})
    if manifest.exists(): (OUT/('managed-' + n + '-pixi.toml')).write_text(manifest.read_text())
write('other-managers.json', manager)

services = {'brew_services':run(['/usr/local/bin/brew','services','list','--json']), 'launch_plists':[], 'formula_service_metadata':{}}
targets=['postgresql@17','mongodb-community','mongodb-database-tools','unbound','net-snmp','php']
for n in targets:
    if n in byname: services['formula_service_metadata'][n] = byname[n].get('service')
for directory in [HOME/'Library/LaunchAgents',Path('/Library/LaunchAgents'),Path('/Library/LaunchDaemons')]:
    for p in sorted(directory.glob('*.plist')):
        if any(x in p.name.lower() for x in ['postgres','mongo','unbound','snmp','php']):
            try:
                d=plistlib.loads(p.read_bytes())
                safe={k:d[k] for k in ['Label','Program','ProgramArguments','WorkingDirectory',
                    'StandardOutPath','StandardErrorPath','RunAtLoad','KeepAlive'] if k in d}
                safe['plist_path']=str(p)
                services['launch_plists'].append(safe)
            except Exception as e: services['launch_plists'].append({'plist_path':str(p),'error':str(e)})
services['data_path_metadata'] = [{'path':str(p),'exists':p.exists(),'is_directory':p.is_dir()}
    for p in [Path('/usr/local/var/postgresql@17'),Path('/usr/local/var/mongodb'),Path('/usr/local/etc/mongod.conf'),
              Path('/usr/local/etc/unbound'),Path('/usr/local/etc/snmp'),Path('/usr/local/etc/php')]]
write('services.json', services)

apps=[]
for directory in [Path('/Applications'),HOME/'Applications']:
    for p in sorted(directory.glob('*.app')):
        try:
            d=plistlib.loads((p/'Contents/Info.plist').read_bytes())
            apps.append({'name':p.name,'path':str(p),'bundle_id':d.get('CFBundleIdentifier'),
                         'version':d.get('CFBundleShortVersionString'), 'build':d.get('CFBundleVersion')})
        except Exception: apps.append({'name':p.name,'path':str(p),'metadata_unreadable':True})
write('native-apps.json', {'scope':'One directory level in /Applications and ~/Applications; no system apps or nested app bundles.', 'apps':apps})
receipt_ids=run(['/usr/sbin/pkgutil','--pkgs'])
if isinstance(receipt_ids.get('output'),str): receipt_ids['output']=receipt_ids['output'].splitlines()
write('installer-receipt-ids.json', receipt_ids)

direct_edges=[e for e in edges if e.get('declared_directly') is True and e['source']=='installed_receipt_runtime_dependencies']
deferred=[p for p in app['last-update-run.json']['plan'] if not p.get('ready')]
summary = {'captured_at':now,'formula_count':len(formulae),'cask_count':len(raw['casks']),
 'homebrew_physical_package_count':len(rows),'excluded_installed_count':sum(r['excluded_from_actions'] for r in rows),
 'formula_roles':dict(collections.Counter(r['role'] for r in rows if r['kind']=='formula')),
 'installed_formula_receipt_count':len(receipts), 'runtime_dependency_edge_count':sum(e['source']=='installed_receipt_runtime_dependencies' for e in edges),
 'direct_runtime_dependency_edge_count':len(direct_edges),
 'unknown_directness_runtime_edge_count':sum(e.get('declared_directly') is None for e in edges if e['source']=='installed_receipt_runtime_dependencies'),
 'missing_installed_targets':sorted({e['to'] for e in edges if e.get('target_installed') is False}),
 'managed_registry_count':len(registry),'managed_homebrew_overlap':sorted(set(registry)&set(byname)),
 'current_app_counts':app['state.json']['counts'],'last_run_deferred_count':app['last-update-run.json'].get('deferred_count'),
 'last_run_deferred_names':sorted(p['name'] for p in deferred),
 'top_reverse_dependents':sorted([{'name':n,'count':len(v)} for n,v in reverse.items()],key=lambda x:(-x['count'],x['name']))[:25],
 'native_app_count':len(apps),'legacy_npm_global_count':len(global_pkgs)}
write('summary.json',summary)
print(json.dumps(summary,indent=2))
