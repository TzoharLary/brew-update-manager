import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import macports_provider as mp
import mac_manager as mm
import update_routes as routes
from test_update_policy import service

class MacTests(unittest.TestCase):
    def test_inventory_identity_revision_and_variants(self):
        responses = [service.CommandResult(0, '  wget @1.0_2+ssl (active)\n  foo @2.0_0 (active)', ''),
                     service.CommandResult(0, 'wget 1.0_2 < 1.0_3', '')]
        with patch.object(service, 'run_cmd', side_effect=responses), patch.object(Path, 'is_file', return_value=True):
            rows=mp.inventory(service)
        self.assertEqual(rows[0]['kind'], 'port')
        self.assertEqual(rows[0]['installed_variants'], '+ssl')
        self.assertEqual(rows[0]['latest_version'], '1.0_3')
        self.assertTrue(rows[0]['outdated'])
        self.assertFalse(rows[1]['outdated'])

    def test_privileged_validation_precedes_authorization(self):
        with patch.object(mp, 'applescript', side_effect=AssertionError('Unauthorized command reached admin')):
            for action, names in [('uninstall',['wget']), ('upgrade',['ngrok']), ('install',['x;whoami']), ('install',[])]:
                with self.assertRaises(ValueError): mp.privileged(service,action,names)

    def test_exclusion_and_absent_mapping(self):
        with patch.object(service, 'is_excluded', return_value=False):
            with self.assertRaises(ValueError): mm.migration_plan(service,'ngrok')
            with self.assertRaises(ValueError): mm.migration_plan(service,'unmapped')

    def test_selection_accepts_explicit_port_and_preserves_old_requests(self):
        self.assertEqual(service.normalize_selected_packages([{'kind':'port','name':'wget'}])[0]['kind'],'port')
        self.assertEqual(service.normalize_selected_packages([{'kind':'formula','name':'wget'}])[0]['kind'],'formula')

    def test_failed_brew_scan_preserves_macports(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(service,'read_json',return_value={}), \
             patch.object(service,'STATE_FILE',Path(tmp)/'state.json'), patch.object(service,'DATA_DIR',Path(tmp)), \
             patch.object(mp,'inventory',return_value=[dict(name='wget',kind='port',provider='macports',outdated=False)]), \
             patch.object(service,'is_excluded',return_value=False),patch.object(routes,'read_records',return_value={}):
            state=mm.combined(service,lambda *a,**k: (_ for _ in ()).throw(RuntimeError('Brew missing')),False,False,False)
        self.assertTrue(state['partial'])
        self.assertEqual(state['packages'][0]['provider'],'macports')

    def test_expired_and_complex_migration_cannot_execute(self):
        with patch.object(service,'read_json',return_value={'id':'old','created_at':0}):
            with self.assertRaises(ValueError): mm.approve(service,'old')
        with patch.object(service,'read_json',return_value={'id':'x','created_at':__import__('time').time(),'executable':False}):
            with self.assertRaises(ValueError): mm.approve(service,'x')

    def test_signature_or_install_failure_is_not_success(self):
        with patch.object(mp,'plan',return_value={'ready':True}), \
             patch.object(mp,'privileged',return_value=service.CommandResult(1,'bad signature','bad signature')), \
             patch.object(mp,'inventory',return_value=[]):
            result=mp.upgrade(service,{'name':'wget','latest_version':'1'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'],1)

    def test_binary_preflight_blocks_missing_dependencies_and_variants(self):
        import csv
        def resolve(args,**kw):
            out=Path(args[2])
            (out/'root-results.tsv').write_text('root\tstatus\terror\nwget\t0\t\n')
            (out/'nodes.tsv').write_text('name\tversion\trevision\tvariants\tarch\tarchive\trequires\tporturl\n'
                'wget\t1\t0\t\tx86_64\twget-1_0.darwin_25.x86_64.tbz2\t\tfile:///fixture\n')
            return service.CommandResult(0,'','')
        fake=type('Probe',(),{'check_node':staticmethod(lambda n:{**n,'archive_status':'missing'})})
        with patch.object(service,'run_cmd',side_effect=resolve),patch.object(mp,'load_probe',return_value=fake), \
             patch.object(service,'is_excluded',return_value=False):
            with self.assertRaisesRegex(RuntimeError,'Ready archives'):
                mp.preflight(service,['wget'],[{'name':'wget','installed_variants':''}])
            with self.assertRaisesRegex(RuntimeError,'variants'):
                mp.preflight(service,['wget'],[{'name':'wget','installed_variants':'+ssl'}])

    def test_both_copies_remain_separate(self):
        pkg=dict(name='node',kind='formula',provider='native',installed_version='26.12',
            latest_version='26.12',brew_installed_version='26.0',outdated=False)
        with tempfile.TemporaryDirectory() as tmp,patch.object(service,'read_json',return_value={}), \
             patch.object(service,'STATE_FILE',Path(tmp)/'state.json'),patch.object(service,'DATA_DIR',Path(tmp)), \
             patch.object(mp,'inventory',return_value=[]),patch.object(routes,'read_records',return_value={}), \
             patch.object(service,'is_excluded',return_value=False):
            state=mm.combined(service,lambda *a,**k:{'packages':[pkg]},False,False,False)
        rows=state['packages']
        self.assertEqual({p['kind'] for p in rows},{'formula','tool'})
        self.assertEqual(len({p['installation_id'] for p in rows}),2)

    def test_cached_policy_keeps_macports_and_tool_ownership(self):
        pkgs=[dict(name='wget',kind='port',provider='macports',outdated=True,installed_version='1',latest_version='2',installation_id='macports:port:wget'),
              dict(name='node',kind='tool',provider='native',outdated=False,installed_version='26',latest_version='26',installation_id='native:tool:node'),
              dict(name='node',kind='formula',provider='homebrew',outdated=True,installed_version='25',latest_version='26',installation_id='homebrew:formula:node')]
        with patch.object(routes,'read_records',return_value={'node':{'version':'26','provider':'native','verified_at':1}}), \
             patch.object(service,'load_settings',return_value={'excluded_packages':[]}):
            service.apply_update_policy(pkgs)
        self.assertEqual([p['provider'] for p in pkgs],['macports','native','homebrew'])
        self.assertEqual(pkgs[2]['installed_version'],'25')

    def test_admin_prompt_rejection_is_propagated(self):
        with patch.object(service,'run_cmd',return_value=service.CommandResult(1,'','User canceled (-128)')):
            with self.assertRaisesRegex(RuntimeError,'canceled'): mp.applescript(service,'fixed command')

    def test_script_replay_and_restore_preserve_later_edits(self):
        spec=importlib.util.spec_from_file_location('implementation',Path(__file__).resolve().parents[1]/'tools/implement-mac-manager.py')
        impl=importlib.util.module_from_spec(spec);spec.loader.exec_module(impl)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'repo';root.mkdir();(root/'dirty.txt').write_text('original dirty content')
            impl.ROOT=root;impl.RECOVERY=Path(tmp)/'recovery'
            with patch.object(impl,'transforms',return_value={'dirty.txt':'new content','created.txt':'new file'}):
                impl.check_or_apply(True);impl.check_or_apply(True)
                (root/'dirty.txt').write_text('later edit')
                with self.assertRaises(RuntimeError): impl.check_or_apply(True)
                with self.assertRaises(RuntimeError): impl.restore()
                self.assertEqual((root/'dirty.txt').read_text(),'later edit')
                (root/'dirty.txt').write_text('new content');impl.restore()
                self.assertEqual((root/'dirty.txt').read_text(),'original dirty content')
                self.assertFalse((root/'created.txt').exists())

    def test_migration_http_plan_and_origin_guard(self):
        import threading
        import urllib.request
        import urllib.error
        server=service.ThreadingHTTPServer(('127.0.0.1',0),service.BrewUpdatesHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_address[1]}/api/migrations/plan'
        try:
            with patch.object(mm,'migration_plan',return_value={'id':'review-only'}), \
                 patch.object(service,'append_service_log'):
                request=urllib.request.Request(url,data=b'{"name":"terminal-notifier"}',headers={'Content-Type':'application/json'})
                with urllib.request.urlopen(request) as response: result=json.load(response)
                self.assertEqual(result['plan']['id'],'review-only')
                request=urllib.request.Request(url,data=b'{}',headers={'Content-Type':'application/json','Origin':'https://example.com'})
                with self.assertRaises(urllib.error.HTTPError) as caught: urllib.request.urlopen(request)
                self.assertEqual(caught.exception.code,403)
                caught.exception.close()
        finally: server.shutdown();server.server_close();thread.join()

    def test_cancel_before_admin_prompt(self):
        with patch.object(service,'is_cancel_requested',return_value=True), \
             patch.object(mp,'applescript',side_effect=AssertionError('Prompt opened after cancel')):
            with self.assertRaises(service.OperationCancelledError): mp.privileged(service,'upgrade',['wget'])

    def test_privileged_worker_cannot_silently_disappear(self):
        import shutil, subprocess
        directory=Path(subprocess.check_output(['/usr/bin/mktemp','-d','/private/tmp/mac-manager.XXXXXXXX'],text=True).strip())
        try:
            with patch.object(mp,'applescript',side_effect=[str(directory),'']), \
                 patch.object(service,'is_cancel_requested',return_value=False):
                with self.assertRaisesRegex(RuntimeError,'without a completion receipt'):
                    mp.privileged(service,'install',['terminal-notifier'])
        finally: shutil.rmtree(directory)

    def test_privileged_shell_waits_and_returns_real_exit(self):
        import shutil, subprocess
        directory=Path(subprocess.check_output(['/usr/bin/mktemp','-d','/private/tmp/mac-manager.XXXXXXXX'],text=True).strip())
        calls=[]
        def authorized(s,command):
            calls.append(command)
            if len(calls)==1: return str(directory)
            self.assertNotIn('nohup',command)
            self.assertIn('wait',command)
            (directory/'output').write_text('signature verification failed')
            (directory/'status').write_text('1')
            return ''
        try:
            with patch.object(mp,'applescript',side_effect=authorized), \
                 patch.object(service,'is_cancel_requested',return_value=False):
                result=mp.privileged(service,'install',['terminal-notifier'])
            self.assertEqual(result.code,1)
            self.assertIn('signature',result.stderr)
        finally: shutil.rmtree(directory)

    def test_resume_reuses_installed_port_and_preserves_app_notification_identity(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=root/'data';data.mkdir()
            cellar=root/'Cellar';keg=cellar/'terminal-notifier/2.0';keg.mkdir(parents=True)
            (keg/'receipt').write_text('original')
            plan={'id':'review','created_at':time.time(),'executable':True,'name':'terminal-notifier',
                'installed_version':'2.0','target':'3.1.0','consumers':[],'remove_brew':True,
                'graph':{'nodes':[{'name':'terminal-notifier','version':'3.1.0','revision':'0'}]}}
            helper=root/'existing-app-helper';binary=root/'macports-cli'
            def command(s,args,**kw):
                if '--prefix' in args: return str(keg)
                if '--cellar' in args: return str(cellar)
                if '-version' in args: return 'terminal-notifier 3.1.0.'
                if 'uninstall' in args:
                    self.assertEqual(kw['env_override']['HOMEBREW_NO_AUTOREMOVE'],'1')
                return ''
            with patch.object(service,'read_json',return_value=plan),patch.object(service,'DATA_DIR',data), \
                 patch.object(service,'TOOLS_ROOT',root/'tools'),patch.object(service,'brew_bin',return_value='/fixture/brew'), \
                 patch.object(service,'is_cancel_requested',return_value=False),patch.object(mm,'migration_plan',return_value=plan), \
                 patch.object(mp,'checked',side_effect=command),patch.object(mm,'notifier_path',return_value=binary), \
                 patch.object(mm,'app_notification_path',return_value=helper), \
                 patch.object(mp,'inventory',return_value=[{'name':'terminal-notifier','installed_version':'3.1.0_0','outdated':False}]), \
                 patch.object(mp,'privileged',side_effect=AssertionError('An already installed port was reinstalled')), \
                 patch.object(service,'send_notification') as notify:
                result=mm.approve(service,'review')
            self.assertEqual(result['status'],'completed')
            self.assertTrue(result['installation_reused'])
            self.assertEqual(notify.call_args.kwargs['notifier_override'],str(helper))
            route=json.loads((data/'notifier-route.json').read_text())
            self.assertEqual(route['provider'],'application_helper')
            self.assertEqual(route['cli_provider'],'macports')

if __name__=='__main__': unittest.main()
