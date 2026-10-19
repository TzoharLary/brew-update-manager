#!/usr/bin/env python3
"""HEAD-only checks of exact MacPorts archives for an already resolved binary graph."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import datetime as dt
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


def read_tsv(path):
    with path.open() as stream:
        return list(csv.DictReader(stream, delimiter='\t'))


def probe(url):
    try:
        with urlopen(Request(url, method='HEAD', headers={'User-Agent':'BrewUpdateManager-BinaryAudit/1'}), timeout=20) as response:
            kind = response.headers.get('Content-Type', '')
            size = response.headers.get('Content-Length')
            okay = response.status == 200 and 'text/html' not in kind and (size is None or int(size) > 0)
            return {'url':url,'status':response.status,'available':okay,'content_type':kind,'size':size,
                    'final_url':response.url,'method':'HEAD'}
    except HTTPError as error:
        return {'url':url,'status':error.code,'available':False,'method':'HEAD'}
    except (URLError, TimeoutError, OSError, ValueError) as error:
        return {'url':url,'status':None,'available':False,'error':str(error),'method':'HEAD'}


def check_node(row):
    attempts = []
    # Official primary and two HTTPS mirrors from this host's archive_sites.tcl.
    sites = ['https://packages.macports.org','https://nue.de.packages.macports.org','https://mse.uk.packages.macports.org']
    for site in sites:
        url = f"{site}/{quote(row['name'],safe='')}/{quote(row['archive'],safe='+._-')}"
        archive = probe(url)
        signature = probe(url+'.sig') if archive['available'] else None
        attempts.append({'archive':archive,'signature':signature})
        if archive['available'] and signature and signature['available']:
            return {**row,'archive_status':'available','attempts':attempts,
                    'integrity_verified':False,'runtime_verified':False}
    uncertain = any(p['archive']['status'] not in (200,404,410) or
                    (p['signature'] and p['signature']['status'] not in (200,404,410)) for p in attempts)
    return {**row,'archive_status':'unknown' if uncertain else 'missing_from_checked_sites',
            'attempts':attempts,'integrity_verified':False,'runtime_verified':False}


def version(value):
    raw = str(value).split('_')[0]
    if not re.fullmatch(r'\d+(?:\.\d+)*',raw):
        return None
    parts = [int(n) for n in raw.split('.')]
    while len(parts)>1 and parts[-1]==0:
        parts.pop()
    return tuple(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resolved',required=True,type=Path)
    parser.add_argument('--root-map',required=True,type=Path)
    parser.add_argument('--brew-report',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--workers',type=int,default=6)
    parser.add_argument('--resume',action='store_true',help='Reuse successful exact-archive HEAD observations from the same output file')
    args=parser.parse_args()
    nodes=read_tsv(args.resolved/'nodes.tsv')
    for row in nodes:
        if not re.fullmatch(r'[A-Za-z0-9_.+-]+',row['name']) or '/' in row['archive']:
            raise ValueError('Unsafe resolved archive identity')
    root_results={r['root']:r for r in read_tsv(args.resolved/'root-results.tsv')}
    closures={}
    for row in read_tsv(args.resolved/'root-nodes.tsv'):
        closures.setdefault(row['root'],set()).add(row['archive'])
    mapping=json.loads(args.root_map.read_text())
    brew=json.loads(args.brew_report.read_text())
    prior={}
    if args.resume and args.output.exists():
        saved=json.loads(args.output.read_text())
        for row in saved.get('nodes',[]):
            if row['archive_status']=='available':prior[row['archive']]=row
    checked=[]
    pending=[]
    for row in nodes:
        if row['archive'] in prior:checked.append(prior[row['archive']])
        else:pending.append(row)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    started=time.monotonic()

    def save_nodes():
        args.output.write_text(json.dumps({'checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),
            'status':'probing','nodes':sorted(checked,key=lambda r:r['archive'])},indent=2)+'\n')

    with ThreadPoolExecutor(max_workers=max(1,min(args.workers,8))) as pool:
        futures=[pool.submit(check_node,row) for row in pending]
        for future in as_completed(futures):
            checked.append(future.result())
            if len(checked)%10==0:
                save_nodes()
                print(f'Checked {len(checked)}/{len(nodes)} exact archives',flush=True)
    by_archive={row['archive']:row for row in checked}
    package_rows=[]
    for item in brew['items']:
        if item['status']!='source_required':continue
        name=item['name'];roots=mapping.get(name,[])
        result={'homebrew_name':name,'homebrew_target':item['target_version'],'macports_roots':roots,
                'archive_closure_available':False,'migration_verified':False,'runtime_verified':False}
        failed=[r for r in roots if root_results.get(r,{}).get('status')!='0']
        archives=set().union(*(closures.get(r,set()) for r in roots)) if roots else set()
        missing=[by_archive[a] for a in sorted(archives) if by_archive[a]['archive_status']!='available']
        wrong_arch=[by_archive[a]['name'] for a in sorted(archives)
                    if not ({'x86_64','noarch'} & set(by_archive[a]['arch'].split(',')))]
        primary=next((by_archive[a] for a in closures.get(roots[0],set()) if by_archive[a]['name']==roots[0]),None) if roots else None
        installed_version=primary['version'] if primary else None
        target_version=version(item['target_version']);candidate_version=version(installed_version)
        compatible_version=target_version is not None and candidate_version is not None and candidate_version>=target_version
        if not roots:status='unmapped'
        elif failed:status='resolution_failed'
        elif wrong_arch:status='architecture_not_verified'
        elif not compatible_version:status='version_gap_or_unknown'
        elif missing:status='dependency_archives_unavailable_or_unknown'
        elif not archives:status='resolution_failed'
        else:status='all_runtime_archives_available'
        result.update(status=status,macports_version=installed_version,
                      upstream_version_meets_target=compatible_version,failed_roots=failed,
                      archive_count=len(archives),archive_closure_available=status=='all_runtime_archives_available',
                      blockers=[{'name':r['name'],'archive':r['archive'],'status':r['archive_status']} for r in missing],
                      incompatible_architectures=wrong_arch)
        package_rows.append(result)
    summary={'checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),'status':'complete',
        'elapsed_seconds':round(time.monotonic()-started,1),'host':brew['host'],
        'policy':'No install, archive download, source build or privileged modification. HEAD metadata only.',
        'signature_policy':'Required .sig availability; signature type from installed MacPorts official archive_sites.tcl. No downloaded artifact/signature integrity verification.',
        'scope':'Official resolved default-variant binary runtime closures in an empty private registry, using existing /opt/local prefix and current host.',
        'limitations':['All-runtime-archives-available is metadata evidence, not runtime, signature or migration verification.',
                       'Homebrew revisions and MacPorts revisions are not interchangeable; only upstream versions are compared.',
                       'Missing is limited to the three checked official HTTPS sites. Network failures remain unknown.',
                       'MacPorts package equivalence and application features/consumers need review before migration.'],
        'counts':{status:sum(r['status']==status for r in package_rows) for status in sorted({r['status'] for r in package_rows})},
        'items':package_rows,'nodes':sorted(checked,key=lambda r:r['archive'])}
    args.output.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'counts':summary['counts'],'unique_archives':len(checked),'report':str(args.output)},ensure_ascii=False))


if __name__=='__main__':main()
