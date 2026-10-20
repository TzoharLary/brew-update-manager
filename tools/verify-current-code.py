#!/usr/bin/env python3
"""Run existing isolated source checks and save reports; never migrate packages."""
from __future__ import annotations
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
REPORTS = Path.home() / '.local/share/brew-update-manager/recovery/t03-1.3.3'

def syntax():
    files = sorted(p for folder in ('backend', 'tools', 'tests')
                   for p in (ROOT / folder).rglob('*.py') if '__pycache__' not in p.parts)
    for path in files:
        ast.parse(path.read_bytes(), filename=str(path))
    print(f'Python syntax: {len(files)} files OK')

def main():
    out = REPORTS / ('checks-' + str(time.time_ns()))
    out.mkdir(parents=True)
    sources = sorted(p for folder in ('backend', 'tools', 'tests', 'renderer', 'electron')
                     for p in (ROOT / folder).rglob('*')
                     if p.is_file() and '__pycache__' not in p.parts
                     and p.suffix in {'.py', '.js', '.cjs', '.html', '.css'})
    sources += [ROOT / 'package.json', ROOT / 'package-lock.json']
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    report = {'mode': 'isolated_code_checks', 'real_migration': False,
              'started_at': time.time(), 'code_sha256': hashes, 'steps': [],
              'scope': 'Existing source suites only; no installed-app or real-migration acceptance'}
    (out / 'code-sha256.json').write_text(json.dumps(hashes, indent=2) + '\n')
    def save():
        (out / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        lines = ['# דוח בדיקות קוד מבודדות', '', 'תוצאה: ' + report.get('result', 'running'),
                 '', 'לא בוצעו מעבר, עדכון או הסרה של חבילות.',
                 'הדוח אינו בדיקת קבלה לאפליקציה המותקנת או למעבר ספק אמיתי.', '']
        lines += [f"- {s['name']}: {s['status']} ({s.get('log', '')})" for s in report['steps']]
        if report.get('error'): lines += ['', report['error']]
        (out / 'summary.md').write_text('\n'.join(lines) + '\n')
    save()
    print('דוח ולוגים:', out, flush=True)
    node = shutil.which('node')
    electron = ROOT / 'node_modules/electron/dist/Electron.app/Contents/MacOS/Electron'
    if not node or not electron.is_file():
        report.update(result='prerequisite_missing', error='Node or local Electron is missing; no dependency installation was attempted')
        save()
        return 2
    js = sorted(p for folder in ('electron', 'renderer', 'tools', 'tests')
                for p in (ROOT / folder).rglob('*') if p.suffix in {'.js', '.cjs'})
    commands = [('python-syntax', [sys.executable, str(Path(__file__).resolve()), '--syntax-only'], 60),
                ('python-tests', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py'], 600)]
    commands += [('syntax-' + str(p.relative_to(ROOT)).replace('/', '-'), [node, '--check', str(p)], 30) for p in js]
    commands += [('http', [node, str(ROOT / 'tests/service-http.cjs')], 90),
                 ('progress-ui', [str(electron), str(ROOT / 'tests/progress-ui.cjs')], 120),
                 ('operation-results-ui', [str(electron), str(ROOT / 'tests/operation-results-ui.cjs')], 120),
                 ('whitespace', ['git', 'diff', '--check'], 30)]
    code = 0
    with tempfile.TemporaryDirectory(prefix='mac-manager-checks-') as temporary:
        env = {**os.environ, 'BREW_UPDATE_DATA_DIR': temporary, 'PYTHONDONTWRITEBYTECODE': '1'}
        for name, argv, timeout in commands:
            step = {'name': name, 'argv': argv, 'status': 'running', 'log': name + '.log'}
            report['steps'].append(step)
            save()
            print('בודק:', name, flush=True)
            process = None
            started = time.monotonic()
            try:
                with (out / step['log']).open('wb') as log:
                    process = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log,
                                               stderr=subprocess.STDOUT, start_new_session=True)
                    while True:
                        remaining = timeout - (time.monotonic() - started)
                        if remaining <= 0: raise subprocess.TimeoutExpired(argv, timeout)
                        try:
                            result = process.wait(timeout=min(20, remaining))
                            break
                        except subprocess.TimeoutExpired:
                            print(f'עדיין בודק: {name} ({int(time.monotonic() - started)} שניות)', flush=True)
                step.update(returncode=result, status='pass' if result == 0 else 'fail')
                if result != 0: code = 1
            except (subprocess.TimeoutExpired, KeyboardInterrupt, OSError) as error:
                if process is not None and process.poll() is None:
                    # Only the isolated subprocess group created above belongs to us.
                    os.killpg(process.pid, signal.SIGTERM)
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                step.update(status='unverified', error=str(error) or 'Interrupted by user')
                code = 2
            step['elapsed_seconds'] = round(time.monotonic() - started, 2)
            save()
            if code == 2: break
        completed = {s['name'] for s in report['steps']}
        for name, argv, _ in commands:
            if name not in completed:
                report['steps'].append({'name': name, 'argv': argv, 'status': 'not_run'})
    changed = [rel for rel, sha in hashes.items()
               if not (ROOT / rel).is_file() or hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() != sha]
    if changed:
        report['error'] = 'Source files changed during checks: ' + ', '.join(changed)
        code = 2
    report.update(finished_at=time.time(), exit_code=code,
                  result={0: 'pass', 1: 'fail', 2: 'unverified'}[code])
    save()
    print('הבדיקות הסתיימו:', report['result'], '\nדוח:', out, flush=True)
    return code

if __name__ == '__main__':
    if sys.argv[1:] == ['--syntax-only']: syntax()
    elif sys.argv[1:]: raise SystemExit('This runner accepts no arguments')
    else: raise SystemExit(main())
