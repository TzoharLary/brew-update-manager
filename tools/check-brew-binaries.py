#!/usr/bin/env python3
"""Inspect outdated Brew formulae without updating metadata or installing anything."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from update_routes import bottle_blockers


def command(args):
    env = dict(os.environ, HOMEBREW_NO_AUTO_UPDATE='1', HOMEBREW_NO_ANALYTICS='1')
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError(f'{args[1]} failed: {result.stderr.strip()}')
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--brew', default='/usr/local/bin/brew')
    parser.add_argument('--catalog', type=Path, help='Platform-selected Homebrew cache payload')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    outdated = command([args.brew, 'outdated', '--json=v2'])
    info = command([args.brew, 'info', '--json=v2', '--installed'])
    os_version = platform.mac_ver()[0]
    codename = {11:'big_sur',12:'monterey',13:'ventura',14:'sonoma',15:'sequoia',26:'tahoe',27:'golden_gate'}.get(int(os_version.split('.')[0]))
    if not codename and not args.catalog:
        raise RuntimeError('Unknown macOS: supply a verified platform-selected --catalog')
    tag = ('arm64_' if platform.machine() == 'arm64' else '') + str(codename)
    path = args.catalog or Path.home() / f'Library/Caches/Homebrew/api/internal/packages.{tag}.jws.json.payload'
    catalog_error = None
    try:
        with path.open() as stream:
            next(stream)
            catalog = json.load(stream)['formulae']
    except (OSError, ValueError, KeyError, StopIteration) as error:
        catalog, catalog_error = {}, str(error)
    settings_path = Path.home() / 'Library/Application Support/Homebrew Update Manager/settings.json'
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    excluded = set(settings.get('excluded_packages', [])) | {'formula:ngrok', 'cask:ngrok'}
    installed = {row['name']: {'versions':[x['version'] for x in row.get('installed', [])],
                             'options':[v for x in row.get('installed', []) for v in x.get('used_options', [])]}
                 for row in info.get('formulae', [])}
    rows = []
    for item in outdated.get('formulae', []):
        name = item['name']
        blockers = bottle_blockers(name, catalog, installed, sorted(excluded))
        if f'formula:{name}' in excluded:
            status = 'excluded'
        elif any(x['reason'] in ('source_required', 'custom_build_options') for x in blockers):
            status = 'source_required'
        elif blockers:
            status = 'not_verified'
        else:
            status = 'binary_candidate'
        rows.append({'name':name, 'installed_versions':item.get('installed_versions', []),
                     'target_version':item.get('current_version'), 'status':status, 'blockers':blockers})
    report = {'checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),
              'host':{'macos':os_version,'architecture':platform.machine()},
              'scope':'Physical outdated Homebrew formulae, including retained kegs hidden by active CLI overlays. Not all software on the Mac.',
              'catalog':{'path':str(path),'mtime':path.stat().st_mtime if path.exists() else None,'error':catalog_error},
              'metadata_refreshed':False, 'installation_performed':False,
              'limitations':['Uses current local Brew metadata and the existing conservative bottle closure policy.',
                             'binary_candidate is not an installed/runtime-verified result; exclusions and missing metadata remain distinct.',
                             'Casks are reported separately; their application/runtime migration is not checked here.'],
              'counts':{status:sum(r['status']==status for r in rows) for status in ['source_required','binary_candidate','not_verified','excluded']},
              'items':rows,
              'outdated_casks':[{'name':r.get('name') or r.get('token'),
                                'excluded':f"cask:{r.get('name') or r.get('token')}" in excluded}
                               for r in outdated.get('casks', [])]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    names = [r['name'] for r in rows if r['status']=='source_required']
    args.output.with_suffix('.source-required.txt').write_text('\n'.join(names)+'\n')
    print(json.dumps({'counts':report['counts'],'source_required':names,'report':str(args.output)}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
