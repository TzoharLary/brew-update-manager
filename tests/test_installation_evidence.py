import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_update_policy import service as s
import installation_evidence as e
import mac_manager as mm


class InstallationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.cellar=self.root/'Cellar';self.cellar.mkdir()
        self.data=self.root/'data'
        self.patches=[]
        for key,value in [('DATA_DIR',self.data),('STATE_FILE',self.data/'state.json'),('SERVICE_LOG_FILE',self.data/'service.log')]:
            p=patch.object(s,key,value);p.start();self.patches.append(p)
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patches)])
        p=patch.object(s,'brew_bin',return_value='/fixture/brew');p.start();self.patches.append(p)
        e.reset();s.reset_cancel_state();self.addCleanup(s.reset_cancel_state)

    def keg(self,name,version='1.0',deps=None,receipt=True,active=True,formula='class Tool < Formula\nend\n'):
        root=self.cellar/name/version;root.mkdir(parents=True,exist_ok=True)
        if receipt:(root/'INSTALL_RECEIPT.json').write_text(json.dumps({'runtime_dependencies':deps or [],'used_options':[]}))
        (root/'.brew').mkdir(exist_ok=True);(root/'.brew'/f'{name}.rb').write_text(formula)
        if active:
            opt=self.root/'opt'/name;opt.parent.mkdir(exist_ok=True);opt.unlink(missing_ok=True);opt.symlink_to(root)
        return root

    def row(self,name,versions=('1.0',),linked=None):
        return dict(name=name,installed=[dict(version=v) for v in versions],linked_keg=linked)

    def command(self,args,**kw):
        self.assertTrue(kw.get('cancellable'))
        if args[-1]=='--prefix':return s.CommandResult(0,str(self.root),'')
        if args[-1]=='--cellar':return s.CommandResult(0,str(self.cellar),'')
        if 'services' in args:return s.CommandResult(0,'[]','')
        return s.CommandResult(0,'','')

    def test_opt_receipt_selects_active_not_last_manager_entry(self):
        self.keg('llvm','22.1.5');self.keg('llvm','23.1.3_2',receipt=False,active=False)
        audit=e.inspect(s,self.row('llvm',('23.1.3_2','22.1.5')),self.root,self.cellar)
        self.assertEqual(audit['package_version'],'22.1.5')
        self.assertEqual(audit['status'],'verified')
        self.assertIn('inactive_receipt_missing:23.1.3_2',audit['warnings'])

    def test_missing_active_receipt_and_broken_link_are_unverified(self):
        root=self.keg('tool',receipt=False)
        audit=e.inspect(s,self.row('tool'),self.root,self.cellar)
        self.assertEqual(audit['status'],'unverified')
        (self.root/'opt/tool').unlink();(self.root/'opt/tool').symlink_to(root/'missing')
        audit=e.inspect(s,self.row('tool'),self.root,self.cellar)
        self.assertIn('active_link_missing_or_outside_installation',audit['issues'])

    def test_revision_and_llvm_probe_and_foreign_path_are_separate(self):
        root=self.keg('llvm','22.1.5_2');(root/'bin').mkdir();(root/'bin/clang').write_text('fixture')
        with patch.object(e.shutil,'which',return_value='/usr/bin/clang'),patch.object(e,'command',return_value=s.CommandResult(0,'clang version 22.1.5','')):
            audit=e.inspect(s,self.row('llvm',('22.1.5_2',)),self.root,self.cellar)
        self.assertEqual(audit['upstream_version'],'22.1.5');self.assertEqual(audit['revision'],2)
        self.assertEqual(audit['status'],'verified');self.assertEqual(audit['command_resolution']['path'],'/usr/bin/clang')
        with patch.object(e,'command',return_value=s.CommandResult(0,'clang version 21.0.0','')):
            audit=e.inspect(s,self.row('llvm',('22.1.5_2',)),self.root,self.cellar)
        self.assertEqual(audit['status'],'conflict')

    def test_failed_version_probe_retains_installation_but_blocks_dependent_action(self):
        root=self.keg('llvm','22.1.5');(root/'bin').mkdir();(root/'bin/clang').write_text('fixture')
        with patch.object(e,'command',return_value=s.CommandResult(-6,'','dyld: library missing')):
            audit=e.inspect(s,self.row('llvm',('22.1.5',)),self.root,self.cellar)
        self.assertEqual(audit['package_version'],'22.1.5');self.assertEqual(audit['functional_status'],'failed')
        fields=e.inventory_fields(s,self.row('llvm',('22.1.5',)),dict(audits={'llvm':audit}))
        self.assertTrue(fields['inventory_stale']);self.assertEqual(fields['installed_version'],'22.1.5')

    def test_incomplete_keg_is_not_a_satisfied_binary_dependency(self):
        self.keg('llvm','22.1.5');self.keg('llvm','23.1.3_2',receipt=False,active=False)
        with patch.object(s,'run_cmd',side_effect=self.command):e.collect(s,dict(formulae=[self.row('llvm',('22.1.5','23.1.3_2'))]))
        result=e.verified_dependencies(s,{})
        self.assertEqual(result['llvm']['versions'],['22.1.5'])

    def test_receipts_reconcile_omitted_install_and_retained_consumer(self):
        self.keg('tool');self.keg('consumer','1',deps=[dict(full_name='tool',version='1.0')],active=False)
        self.keg('consumer','2',active=True)
        with patch.object(s,'run_cmd',side_effect=self.command):ctx=e.collect(s,dict(formulae=[self.row('tool')]))
        self.assertEqual(ctx['omitted'][0]['name'],'consumer')
        self.assertEqual(ctx['reverse']['tool'][0]['installed_version'],'1')

    def evidence(self,**kw):
        return dict(inventory={'status':'verified'},gaps=[],consumers=[],files=dict(commands=['tool'],gaps=[],**kw))

    def test_categories_and_role_precedence_and_no_private_script_requirement(self):
        self.assertEqual(e.classify(self.evidence())['category'],'standalone_candidate')
        self.assertEqual(e.classify(self.evidence(libraries=['lib/tool.dylib']))['category'],'shared_environment')
        self.assertEqual(e.classify(self.evidence(development=['bin/llvm-config']))['category'],'shared_environment')
        result=e.classify(self.evidence(service_definition=True,libraries=['lib/tool.dylib']))
        self.assertEqual(result['category'],'service');self.assertEqual(result['roles'],['service','shared_environment'])
        result=e.classify({**self.evidence(),'gaps':['otool_failed']})
        self.assertEqual(result['category'],'insufficient_information');self.assertFalse(result['automation_candidate'])
        result=e.classify({**self.evidence(service_definition=True),'gaps':['receipt_gap']})
        self.assertEqual(result['category'],'service');self.assertIn('receipt_gap',result['gaps'])

    def test_stopped_service_remains_service(self):
        result=e.classify({**self.evidence(),'service':dict(name='tool',status='none')})
        self.assertEqual(result['category'],'service')

    def test_compiler_cli_is_a_development_environment(self):
        root=self.keg('go');(root/'bin').mkdir();p=root/'bin/go';p.write_text('fixture');p.chmod(0o755)
        files=e.file_evidence(s,root)
        result=e.classify(dict(inventory={'status':'verified'},gaps=[],files=files))
        self.assertEqual(result['category'],'shared_environment')

    def test_external_command_target_prevents_standalone_classification(self):
        root=self.keg('tool');(root/'bin').mkdir();(root/'bin/tool').symlink_to('/usr/bin/true')
        files=e.file_evidence(s,root)
        self.assertTrue(any(x.startswith('external_command_target:') for x in files['gaps']))
        self.assertEqual(e.classify(dict(inventory={'status':'verified'},files=files,gaps=[]))['category'],'insufficient_information')

    def test_file_inspection_is_installation_scoped_and_cancellable(self):
        root=self.keg('tool',formula='class Tool < Formula\n service do\n run opt_bin/"tool"\n end\nend')
        (root/'bin').mkdir();p=root/'bin/tool';p.write_text('#!/bin/sh\n');p.chmod(0o755)
        external=self.root/'private';external.mkdir();(external/'secret.h').write_text('not inspected')
        (root/'include').symlink_to(external)
        files=e.file_evidence(s,root)
        self.assertTrue(files['service_definition']);self.assertEqual(files['headers'],[])
        s.mark_cancel_requested()
        with self.assertRaises(s.OperationCancelledError):e.file_evidence(s,root)

    def test_annotation_preserves_execution_policy_and_exclusion(self):
        root=self.keg('tool');(root/'bin').mkdir();p=root/'bin/tool';p.write_text('fixture');p.chmod(0o755)
        with patch.object(s,'run_cmd',side_effect=self.command):ctx=e.collect(s,dict(formulae=[self.row('tool')]))
        pkg=dict(name='tool',provider='homebrew',kind='formula',installation_id='homebrew:formula:tool',inventory_verification=ctx['audits']['tool'])
        offers=[dict(name='tool',executable=False,status='archive_unknown'),dict(name='ngrok',executable=False)]
        with patch.object(s,'run_cmd',side_effect=self.command):e.annotate(s,offers,[pkg],True)
        c=offers[0]['classification'];self.assertEqual(c['category'],'standalone_candidate')
        self.assertFalse(offers[0]['executable']);self.assertNotIn('classification',offers[1])
        self.assertTrue(Path(c['evidence_refs'][0]).is_file())
        s.atomic_write_json(s.STATE_FILE,dict(migrations=offers))
        pkg['inventory_verification']['checked_at']='new timestamp'
        result=e.annotate(s,[dict(name='tool')],[pkg],False)
        self.assertEqual(result[0]['classification']['category'],'standalone_candidate')
        pkg['inventory_verification']['package_version']='changed'
        self.assertEqual(e.annotate(s,[dict(name='tool')],[pkg],False)[0]['classification']['category'],'unclassified')

    def test_one_candidate_failure_does_not_erase_other_classification(self):
        offers=[dict(name='first',executable=False),dict(name='second',executable=False)]
        audits={n:dict(status='verified',active_keg=str(self.keg(n)),package_version='1.0') for n in ('first','second')}
        e.CONTEXT.inventory=dict(rows={},reverse={},gaps=[],prefix=str(self.root))
        packages=[dict(name=n,provider='homebrew',kind='formula',inventory_verification=a) for n,a in audits.items()]
        with patch.object(e,'file_evidence',side_effect=[OSError('unreadable'),dict(commands=['second'],gaps=[])]),patch.object(s,'run_cmd',side_effect=self.command):
            e.annotate(s,offers,packages,True)
        self.assertEqual(offers[0]['classification']['category'],'insufficient_information')
        self.assertEqual(offers[1]['classification']['category'],'standalone_candidate')

    def test_real_update_engine_zero_installs_keeps_deferred_reasons(self):
        from update_engine import Engine
        packages=[dict(name=n,kind='formula',provider='homebrew',outdated=True,inventory_stale=True,
                       installed_version='1',latest_version='2') for n in ['first','second']]
        state=dict(packages=packages)
        with patch.object(s,'RUN_JOURNAL_FILE',self.data/'run.json'),patch.object(s,'UPDATE_PLAN_FILE',self.data/'plan.json'), \
             patch.object(s,'compute_snapshot',return_value=state),patch.object(s,'safe_refresh_snapshot',return_value=(state,None)), \
             patch.object(s,'append_update_history_entries'),patch.object(s,'maybe_schedule_deferred_check'), \
             patch.object(Engine,'catalog',return_value={}),patch.object(Engine,'installed',return_value={}), \
             patch.object(s,'run_upgrade',side_effect=AssertionError('Unexpected installation')) as upgrade:
            result=s.run_with_progress('update_all',lambda:Engine(s).run(track_progress=True))
        upgrade.assert_not_called()
        self.assertEqual(result['updated_count'],0);self.assertEqual(result['deferred_count'],2)
        self.assertFalse(result['progress']['running']);self.assertEqual(result['progress']['phase'],'completed')
        self.assertTrue(all(x['reason']=='inventory_unavailable' for x in result['progress']['deferred_packages']))


class FinalProgressTests(unittest.TestCase):
    def setUp(self):
        s.reset_cancel_state();s.reset_check_progress_for_run()
        self.addCleanup(s.reset_cancel_state)

    def test_final_copy_captured_under_lock_and_not_mutated_by_next_run(self):
        def operation():
            self.assertTrue(s.OP_LOCK.locked())
            s.set_check_progress(running=False,phase='completed',completed_packages=[dict(name='first',ok=True)])
            return dict(partial=True,packages=[])
        with patch.object(s,'maybe_schedule_deferred_check'):
            payload=s.run_with_progress('check_now',operation,snapshot=True)
        s.reset_check_progress_for_run()
        self.assertEqual(payload['progress']['phase'],'completed');self.assertTrue(payload['progress']['partial'])
        self.assertEqual(payload['progress']['completed_packages'][0]['name'],'first')
        self.assertFalse(s.OP_LOCK.locked())

    def test_stale_caller_snapshot_cannot_finish_a_new_operation(self):
        with patch.dict(s.CURRENT_OPERATION,dict(running=True,name='check_now',started_at=s.now_iso())):
            s.set_check_progress(running=True,phase='preflight',current_package='new-operation')
            result=s.normalize_stale_progress_state({'running':False})
            self.assertTrue(result['running']);self.assertEqual(result['current_package'],'new-operation')
            payload=s.runtime_status_payload()
            self.assertTrue(payload['operation']['running']);self.assertTrue(payload['progress']['running'])

    def test_orphaned_progress_is_an_error_without_invented_user_cancellation(self):
        with patch.dict(s.CURRENT_OPERATION,dict(running=False,name=None)),patch.object(s,'append_service_log'):
            s.set_check_progress(running=True,phase='preflight',done=0,total=3,current_package='stale')
            result=s.normalize_stale_progress_state()
        self.assertEqual(result['phase'],'error');self.assertFalse(result['canceled']);self.assertIsNone(result['current_package'])

    def test_unexpected_update_failure_finishes_progress_before_unlock(self):
        def fail():raise OSError('fixture failure')
        with patch.object(s,'maybe_schedule_deferred_check'):
            with self.assertRaises(OSError) as caught:s.run_with_progress('update_all',fail)
        self.assertEqual(caught.exception.operation_progress['phase'],'error')
        self.assertFalse(s.read_check_progress()['running']);self.assertEqual(s.read_check_progress()['phase'],'error')
        self.assertFalse(s.OP_LOCK.locked())

    def test_cancellation_captures_terminal_state(self):
        def cancel():raise s.OperationCancelledError('fixture cancel')
        with patch.object(s,'maybe_schedule_deferred_check'):
            result=s.run_with_progress('check_now',cancel,snapshot=True)
        self.assertTrue(result['canceled']);self.assertEqual(result['progress']['phase'],'canceled')
