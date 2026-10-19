#!/usr/bin/env python3
"""Check whether MacPorts can resolve an input list. Never claims archive closure from a dry run."""
import argparse
import datetime as dt
import json
from pathlib import Path
import re
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path, help='One actual MacPorts port name per line')
    parser.add_argument('--port', default='/opt/local/bin/port')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    names = [s.strip() for s in args.input.read_text().splitlines() if s.strip() and not s.startswith('#')]
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]*', n) for n in names):
        parser.error('Use MacPorts names, not Homebrew names/version selectors, shell expressions or options')
    try:
        probe = subprocess.run([args.port, 'version'], capture_output=True, text=True, timeout=30)
        detail = (probe.stdout+'\n'+probe.stderr).strip()
        state = 'platform_mismatch' if 'platform mismatch' in detail.lower() else 'ready_for_planning' if probe.returncode==0 else 'initialization_failed'
    except (OSError, subprocess.TimeoutExpired) as error:
        state, detail = 'unavailable', str(error)
    rows = []
    for name in dict.fromkeys(names):
        row = {'port':name,'status':state,'binary_dependency_closure_verified':False}
        if state == 'ready_for_planning':
            try:
                plan = subprocess.run([args.port,'-N','-b','-y','install',name],capture_output=True,text=True,timeout=120)
                row.update(status='dependency_plan_only' if plan.returncode==0 else 'planning_failed',
                           output=plan.stdout, error=plan.stderr, code=plan.returncode)
            except (OSError,subprocess.TimeoutExpired) as error:
                row.update(status='planning_failed',error=str(error))
        rows.append(row)
    report = {'checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),'environment_status':state,
              'environment_detail':detail,'installation_performed':False,
              'limitation':'This is environment/dependency planning only. -y does not prove archives exist. Full verification requires exact versions/variants/platform and all runtime archives plus integrity verification.',
              'items':rows}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'environment_status':state,'ports':len(rows),'verified_binary_closures':0,'report':str(args.output)}))


if __name__ == '__main__':
    main()
