import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
from test_update_policy import service as s
import mac_manager as mm
import update_routes as routes
import macports_provider as mp


class CheckLifecycle(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        for name, value in [('STATE_FILE',Path(tmp)/'state.json'),('DATA_DIR',Path(tmp))]:
            self.stack.enter_context(patch.object(s,name,value))
        self.stack.enter_context(patch.object(s,'read_json',return_value={}))
        self.stack.enter_context(patch.object(routes,'read_records',return_value={}))
        self.stack.enter_context(patch.object(mp,'inventory',return_value=[]))
        from update_engine import Engine
        self.stack.enter_context(patch.object(Engine,'catalog',return_value={}))
        self.stack.enter_context(patch.object(Engine,'installed',return_value={}))
        s.reset_cancel_state()
        self.addCleanup(s.reset_cancel_state)

    def scan(self, brew=None):
        return mm.combined(s,brew or (lambda *a,**k: {'packages':[]}),False,True,False)

    def test_new_scan_clears_old_results_and_finishes_all_stages(self):
        s.set_check_progress(running=False,phase='completed',completed_packages=[{'name':'old'}])
        observed=[]
        def brew(*a,**k):
            observed.append(s.read_check_progress())
            return {'packages':[]}
        def ports(*a):
            observed.append(s.read_check_progress())
            return []
        with patch.object(mp,'inventory',side_effect=ports): self.scan(brew)
        self.assertTrue(all(p['running'] for p in observed))
        self.assertEqual(observed[0]['completed_packages'],[])
        self.assertEqual(observed[1]['phase'],'collecting_macports')
        end=s.read_check_progress()
        self.assertEqual(end['phase'],'completed')
        self.assertFalse(end['running'])
        self.assertEqual(end['percent'],100)
        self.assertIsNone(end['current_package'])

    def test_both_provider_failures_are_not_success(self):
        def fail(*a,**k): raise RuntimeError('offline')
        with patch.object(mp,'inventory',side_effect=fail): state=self.scan(fail)
        self.assertTrue(state['partial'])
        self.assertEqual(s.read_check_progress()['phase'],'error')

    def test_cancel_stops_before_next_provider_and_does_not_publish(self):
        def cancel(*a,**k):
            s.mark_cancel_requested()
            raise s.OperationCancelledError('cancel')
        with patch.object(mp,'inventory') as ports, patch.object(s,'atomic_write_json') as save:
            with self.assertRaises(s.OperationCancelledError): self.scan(cancel)
        ports.assert_not_called()
        save.assert_not_called()
        self.assertEqual(s.read_check_progress()['phase'],'canceled')
        self.assertFalse(getattr(mm.SCAN_CONTEXT,'active',False))

    def test_unexpected_save_failure_finishes_progress(self):
        with patch.object(s,'atomic_write_json',side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.scan()
        self.assertEqual(s.read_check_progress()['phase'],'error')
        self.assertFalse(s.read_check_progress()['running'])

    def test_scan_commands_are_cancellable_and_stop_after_cancel(self):
        mm.SCAN_CONTEXT.active=True
        try:
            def command(*a,**kw):
                self.assertTrue(kw['cancellable'])
                s.mark_cancel_requested()
                return s.CommandResult(0,'','')
            with patch.object(s,'_run_cmd_impl',side_effect=command):
                with self.assertRaises(s.OperationCancelledError): s.run_cmd(['/fixture/brew'])
        finally: mm.SCAN_CONTEXT.active=False

    def test_untracked_background_scan_does_not_replace_update_progress(self):
        s.set_check_progress(running=True,phase='updating_packages',current_package='formula:live')
        mm.combined(s,lambda *a,**k: {'packages':[]},False,False,False)
        self.assertEqual(s.read_check_progress()['current_package'],'formula:live')

    def test_planning_commands_honor_cancel_and_release_operation(self):
        for operation in ['update_plan','migration']:
            def command(*a,**kw):
                self.assertTrue(kw['cancellable'])
                s.mark_cancel_requested()
                return s.CommandResult(0,'','')
            with patch.object(s,'_run_cmd_impl',side_effect=command):
                with self.assertRaises(s.OperationCancelledError):
                    s.run_exclusive(operation,lambda: s.run_cmd(['/fixture/tool']))
            self.assertEqual(s.read_check_progress()['phase'],'canceled')
            self.assertFalse(s.read_current_operation()['running'])
            self.assertFalse(getattr(mm.SCAN_CONTEXT,'active',False))

    def test_one_provider_failure_is_partial_not_total_failure(self):
        def fail(*a,**kw): raise RuntimeError('Brew unavailable')
        state=self.scan(fail)
        self.assertTrue(state['partial'])
        self.assertEqual(s.read_check_progress()['phase'],'completed')
        self.assertIn('warnings',s.read_check_progress()['message'])

    def test_real_brew_scanner_does_not_finish_combined_scan_early(self):
        observed=[]
        def ports(*a):
            observed.append(s.read_check_progress())
            return []
        with patch.object(s,'brew_bin',return_value='/fixture/brew'), \
             patch.object(s,'fetch_outdated_index',return_value={}), \
             patch.object(s,'fetch_installed_inventory',return_value=[]), \
             patch.object(s,'append_check_log'),patch.object(mp,'inventory',side_effect=ports):
            self.scan(s.compute_homebrew_snapshot)
        self.assertTrue(observed[0]['running'])
        self.assertIsNone(observed[0]['completed_at'])
        self.assertEqual(observed[0]['phase'],'collecting_macports')

    def test_metadata_enrichment_checks_cancel_between_packages(self):
        def cancel(packages, progress_callback=None):
            s.mark_cancel_requested()
            progress_callback(1,1,{'name':'fixture','kind':'formula'})
        with patch.object(s,'brew_bin',return_value='/fixture/brew'), \
             patch.object(s,'fetch_outdated_index',return_value={}), \
             patch.object(s,'fetch_installed_inventory',return_value=[]), \
             patch.object(s,'append_check_log'),patch.object(s,'enrich_release_dates',side_effect=cancel), \
             patch.object(mp,'inventory') as ports:
            with self.assertRaises(s.OperationCancelledError):
                mm.combined(s,s.compute_homebrew_snapshot,False,True,True)
        ports.assert_not_called()
        self.assertEqual(s.read_check_progress()['phase'],'canceled')

    def test_normal_check_batches_all_alternatives_without_per_package_requests(self):
        packages=[dict(name=name,kind='formula',provider='homebrew',outdated=True,
                       installed_version='1',latest_version='2') for name in ['first','second']]
        with patch.object(mm.catalog_refresh,'ensure',return_value={'fresh':True,'status':'fresh'}) as catalog, \
             patch.object(mm.routes,'bottle_blockers',return_value=[{'reason':'source_required'}]), \
             patch.object(mm.automatic_alternatives,'check',side_effect=lambda s,offers,*args,**kw: offers) as batch:
            mm.combined(s,lambda *a,**kw:{'packages':packages},True,True,False)
        batch.assert_called_once()
        self.assertEqual({o['name'] for o in batch.call_args.args[1]},{'first','second'})
        self.assertTrue(catalog.call_args.kwargs['refresh'])
        self.assertTrue(all(p['update_block_reason']=='source_required' for p in packages))
