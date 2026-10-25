"""Recovery through real journals, routing publication and local HTTP handlers."""
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer

import test_migration_recovery as fixture
import migration_engine as engine
import unified_updates as unified


class RecoveryIntegration(unittest.TestCase):
    def setUp(self):
        self.case = fixture.RecoveryTransactions()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.s = self.case.s

    def request(self):
        self.s.row = {'name': 'jq', 'kind': 'formula', 'provider': 'homebrew',
                      'installation_id': 'homebrew:formula:jq', 'installed_version': '1.0'}
        decision = {'decision_id': 'c'*32, 'material_fingerprint': 'material',
                    'status': 'ready', 'action': 'migrate', 'logical_id': 'jq',
                    'source_installation_id': self.s.row['installation_id'],
                    'installed_package_version': '1.0', 'installed_version': '1.0',
                    'current_provider': 'homebrew', 'target_provider': 'pixi',
                    'selected_candidate': {'migration_plan_id': self.case.plan['id'],
                                           'migration_fingerprint': 'fingerprint'}}
        engine._write(engine._plan_path(self.s, self.case.plan['id']), self.case.plan)
        engine._write(self.s.DATA_DIR/'update-plans'/('d'*32+'.json'), {
            'id': 'd'*32, 'expires_at': time.time()+600, 'decisions': [decision]})
        return {'request_id': 'e'*32, 'plan_id': 'd'*32,
                'decisions': [{'decision_id': 'c'*32, 'material_fingerprint': 'material'}]}

    def fail_request(self):
        body = self.request()
        self.case.postcheck.side_effect = RuntimeError('postcheck failed')
        with patch.object(unified, '_revalidate_migration'), \
             patch.object(engine, '_material', return_value=({}, 'fingerprint')):
            with self.assertRaisesRegex(RuntimeError, 'postcheck failed'):
                unified.execute(self.s, body)
        self.case.postcheck.side_effect = None
        journals = list((self.s.DATA_DIR/'migrations/transactions').glob('*/journal.json'))
        txn = self.s.read_json(journals[0])
        return body, txn

    def test_resume_updates_batch_and_replay_returns_same_result(self):
        body, txn = self.fail_request()
        plan = engine.recovery_plan(self.s, txn['transaction_id'], 'resume')
        engine.approve(self.s, plan['id'])
        batch = unified.request_status(self.s, 'e'*32)
        self.assertEqual(batch['status'], 'completed')
        self.assertTrue(batch['result']['ok'])
        self.assertEqual(batch['result']['migrated_count'], 1)
        self.assertEqual(unified.execute(self.s, body), batch['result'])
        self.case.install.assert_called_once()
        self.assertEqual([cmd[0][1] for cmd in self.s.commands], ['uninstall'])

    def test_rollback_updates_batch_without_reporting_a_successful_update(self):
        body, txn = self.fail_request()
        plan = engine.recovery_plan(self.s, txn['transaction_id'], 'rollback')
        engine.approve(self.s, plan['id'])
        batch = unified.request_status(self.s, 'e'*32)
        self.assertEqual(batch['status'], 'recovered')
        self.assertFalse(batch['result']['ok'])
        self.assertEqual(batch['result']['rolled_back_count'], 1)
        self.assertEqual(batch['result']['migrated_count'], 0)
        self.assertEqual(unified.execute(self.s, body), batch['result'])
        self.case.install.assert_called_once()

    def test_crash_before_batch_reconciliation_is_projected_without_writes(self):
        _, txn = self.fail_request()
        plan = engine.recovery_plan(self.s, txn['transaction_id'], 'resume')
        with patch.object(engine, '_reconcile_request'):
            engine.approve(self.s, plan['id'])
        path = unified._request_path(self.s, 'e'*32)
        before = path.read_bytes()
        self.assertEqual(self.s.read_json(path)['status'], 'needs_recovery')
        self.assertEqual(unified.request_status(self.s, 'e'*32)['status'], 'completed')
        self.assertEqual(path.read_bytes(), before)

    def test_unexecuted_batch_items_are_deferred_not_automatically_run(self):
        _, txn = self.fail_request()
        path = unified._request_path(self.s, 'e'*32)
        batch = self.s.read_json(path)
        batch['approved_decisions'].append({'decision_id': 'f'*32})
        engine._write(path, batch)
        plan = engine.recovery_plan(self.s, txn['transaction_id'], 'resume')
        engine.approve(self.s, plan['id'])
        result = unified.request_status(self.s, 'e'*32)['result']
        self.assertFalse(result['ok'])
        self.assertEqual(result['deferred_count'], 1)
        self.case.install.assert_called_once()

    def test_running_batch_is_not_prematurely_projected_by_status_query(self):
        _, txn = self.fail_request()
        plan = engine.recovery_plan(self.s, txn['transaction_id'], 'resume')
        with patch.object(engine, '_reconcile_request'):
            engine.approve(self.s, plan['id'])
        path = unified._request_path(self.s, 'e'*32)
        batch = self.s.read_json(path)
        batch['status'] = 'running'
        engine._write(path, batch)
        self.s.read_current_operation = lambda: {'running': True}
        self.assertEqual(unified.request_status(self.s, 'e'*32)['status'], 'running')

    def test_compensating_link_ignores_scan_cancellation(self):
        import mac_manager
        self.s.failure = 'partial'
        with self.assertRaises(engine.MigrationConflict):
            self.case.run_transaction()
        real_command = self.s.run_cmd
        def guarded(argv, **kwargs):
            if getattr(mac_manager.SCAN_CONTEXT, 'active', False):
                raise RuntimeError('scan-wide cancel would interrupt compensation')
            return real_command(argv, **kwargs)
        mac_manager.SCAN_CONTEXT.active = True
        try:
            with patch.object(self.s, 'run_cmd', side_effect=guarded):
                self.assertEqual(self.case.recover('rollback')['status'], 'rolled_back')
            self.assertTrue(mac_manager.SCAN_CONTEXT.active)
        finally:
            mac_manager.SCAN_CONTEXT.active = False

    @unittest.skipUnless(hasattr(os, 'fork'), 'requires POSIX process isolation')
    def test_all_routing_publication_crash_windows_roll_back(self):
        for window in ('generations_ready', 'shim_write_pending', 'shim_written', 'pointer_switch_pending'):
            with self.subTest(window=window):
                case = fixture.RecoveryTransactions()
                case.setUp()
                try:
                    active, shim = case.prepare_real_routing()
                    pid = os.fork()
                    if pid == 0:
                        def activate(*args, **kwargs):
                            original_progress = kwargs['progress']
                            def crash(state):
                                original_progress(state)
                                if state['state'] == window:
                                    os._exit(23)
                            kwargs['progress'] = crash
                            return fixture.REAL_ACTIVATE(*args, **kwargs)
                        try:
                            with patch.object(engine.routing, 'activate', side_effect=activate):
                                case.run_transaction()
                        except BaseException:
                            os._exit(99)
                        os._exit(98)
                    _, status = os.waitpid(pid, 0)
                    self.assertEqual(os.waitstatus_to_exitcode(status), 23)
                    with patch.object(engine.routing, 'restore', wraps=fixture.REAL_RESTORE):
                        self.assertEqual(case.recover('rollback')['status'], 'rolled_back')
                    self.assertFalse(os.path.lexists(active))
                    self.assertFalse(os.path.lexists(shim))
                    self.assertEqual(case.s.commands, [])
                finally:
                    case.doCleanups()

    def start_http(self):
        from test_update_policy import service
        # Use the real request handler, operation lock and progress wrapper.
        # Every mutable path and every provider call is redirected to fixtures.
        for key, value in {
                'DATA_DIR': self.s.DATA_DIR, 'TOOLS_ROOT': self.s.TOOLS_ROOT,
                'STATE_FILE': self.s.DATA_DIR/'state.json',
                'LOG_DIR': self.s.DATA_DIR/'logs', 'CACHE_DIR': self.s.DATA_DIR/'cache',
                'CHECK_LOG_FILE': self.s.DATA_DIR/'logs/check.log',
                'SERVICE_LOG_FILE': self.s.DATA_DIR/'logs/service.log',
                'SETTINGS_FILE': self.s.DATA_DIR/'settings.json',
                'brew_bin': self.s.brew_bin, 'run_cmd': self.s.run_cmd,
                'compute_snapshot': self.s.compute_snapshot,
                'maybe_schedule_deferred_check': lambda *args: None}.items():
            self.case.stack.enter_context(patch.object(service, key, value))
        server = ThreadingHTTPServer(('127.0.0.1', 0), service.BrewUpdatesHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), worker.join(2)))
        return service, server.server_address[1]

    def http(self, port, path, body=None):
        request = urllib.request.Request('http://127.0.0.1:'+str(port)+path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_real_command_timeout_reaps_fixture_process(self):
        service, _ = self.start_http()
        pidfile = self.case.root/'timeout-pid'
        script = "import os,time;from pathlib import Path;Path("+repr(str(pidfile))+").write_text(str(os.getpid()));time.sleep(60)"
        with patch.object(service, 'process_env', return_value=dict(os.environ)):
            result = service._run_cmd_impl([sys.executable, '-c', script], timeout=0.2, cancellable=True)
        self.assertEqual(result.code, 124)
        pid = int(pidfile.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertIsNone(service.ACTIVE_COMMAND['process'])

    def test_real_cancel_reaps_command_before_transaction_recovery(self):
        service, _ = self.start_http()
        pidfile = self.case.root/'cancel-pid'
        script = ("import os,time;from pathlib import Path;Path("+repr(str(self.s.keg/'bin/jq'))+").unlink();"
                  "Path("+repr(str(pidfile))+").write_text(str(os.getpid()));time.sleep(60)")
        def command(argv, **kwargs):
            if argv[1] == 'uninstall':
                result = service._run_cmd_impl([sys.executable, '-c', script], timeout=10, cancellable=True)
                service.raise_if_cancel_requested()
                return result
            return self.s.run_cmd(argv, **kwargs)
        outcome = []
        with patch.object(service, 'run_cmd', side_effect=command), \
             patch.object(service, 'process_env', return_value=dict(os.environ)):
            worker = threading.Thread(target=lambda: outcome.append(service.run_with_progress(
                'migration', lambda: engine._run(service, self.case.journal, self.case.directory))))
            worker.start()
            try:
                deadline = time.monotonic()+4
                while not pidfile.exists() and time.monotonic()<deadline:
                    time.sleep(0.01)
                self.assertTrue(pidfile.exists(), 'fixture process did not start')
                service.request_cancel_current_operation()
                worker.join(5)
                self.assertFalse(worker.is_alive())
            finally:
                if worker.is_alive():
                    service.request_cancel_current_operation()
                    worker.join(5)
        self.assertTrue(outcome[0]['canceled'])
        self.assertEqual(self.case.stored()['status'], 'needs_attention')
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)
        self.assertEqual(self.case.recover('rollback')['status'], 'rolled_back')

    def test_rollback_preserves_unrelated_provider_ownership(self):
        original_phase = engine._phase
        def fail_after_commit(journal, directory, phase, state, **values):
            if phase == 'ownership' and state == 'finished':
                raise RuntimeError('after commit')
            return original_phase(journal, directory, phase, state, **values)
        with patch.object(engine, '_phase', side_effect=fail_after_commit):
            with self.assertRaises(RuntimeError):
                self.case.run_transaction()
        path = self.s.TOOLS_ROOT/'migrations/routes.json'
        registry = self.s.read_json(path)
        unrelated = {'logical_id': 'other', 'source_installation_id': 'homebrew:formula:other',
                     'transaction_id': 'f'*32, 'status': 'committed'}
        registry['routes'].append(unrelated)
        engine._write(path, registry)
        self.assertEqual(self.case.recover('rollback')['status'], 'rolled_back')
        self.assertEqual(engine.ownership.load_routes(self.s.TOOLS_ROOT)['routes'], [unrelated])

    @unittest.skipUnless(hasattr(os, 'fork'), 'requires POSIX process isolation')
    def test_interrupted_rollback_is_idempotent_for_routing_and_metadata(self):
        import pixi_provider
        for window in ('routing', 'installed.json', 'routes.json'):
            with self.subTest(window=window):
                case = fixture.RecoveryTransactions()
                case.setUp()
                try:
                    active, shim = case.prepare_real_routing()
                    original_phase = engine._phase
                    def after_commit(journal, directory, phase, state, **values):
                        if phase == 'ownership' and state == 'finished':
                            raise RuntimeError('after commit')
                        return original_phase(journal, directory, phase, state, **values)
                    with patch.object(engine.routing, 'activate', wraps=fixture.REAL_ACTIVATE), \
                         patch.object(engine, '_register_pixi_target', wraps=fixture.REAL_REGISTER), \
                         patch.object(pixi_provider, 'installed_record', return_value={'name': 'jq'}), \
                         patch.object(pixi_provider, 'verify_generation'), \
                         patch.object(engine, '_phase', side_effect=after_commit):
                        with self.assertRaises(RuntimeError):
                            case.run_transaction()
                    original_restore = fixture.REAL_RESTORE
                    original_write = engine.ownership.atomic_write_json
                    pid = os.fork()
                    if pid == 0:
                        def restore(*args, **kwargs):
                            value = original_restore(*args, **kwargs)
                            if window == 'routing':
                                os._exit(23)
                            return value
                        def write(path, value):
                            original_write(path, value)
                            if Path(path).name == window:
                                os._exit(23)
                        try:
                            with patch.object(engine.routing, 'restore', side_effect=restore), \
                                 patch.object(engine.ownership, 'atomic_write_json', side_effect=write):
                                case.recover('rollback')
                        except BaseException:
                            os._exit(99)
                        os._exit(98)
                    _, status = os.waitpid(pid, 0)
                    self.assertEqual(os.waitstatus_to_exitcode(status), 23)
                    with patch.object(engine.routing, 'restore', wraps=fixture.REAL_RESTORE):
                        self.assertEqual(case.recover('rollback')['status'], 'rolled_back')
                    self.assertFalse(os.path.lexists(active))
                    self.assertFalse(os.path.lexists(shim))
                    self.assertEqual(engine.ownership.load_routes(case.s.TOOLS_ROOT)['routes'], [])
                    self.assertNotIn('jq', case.s.read_json(case.s.TOOLS_ROOT/'installed.json') or {})
                finally:
                    case.doCleanups()

    def test_verified_http_recovery_survives_inventory_refresh_failure(self):
        _, txn = self.fail_request()
        service, port = self.start_http()
        _, review = self.http(port, '/api/migrations/recovery-plan', {
            'transaction_id': txn['transaction_id'], 'action': 'rollback'})
        with patch.object(service, 'compute_snapshot', side_effect=RuntimeError('refresh offline')):
            status, result = self.http(port, '/api/migrations/approve', {'id': review['plan']['id']})
        self.assertEqual(status, 200)
        self.assertEqual(result['migration']['status'], 'rolled_back')
        self.assertEqual(result['inventory_refresh_error'], 'refresh offline')
        self.assertEqual(result['request']['status'], 'recovered')

    def test_backup_copy_failure_verifies_untouched_source(self):
        with patch.object(engine.shutil, 'copytree', side_effect=OSError('copy failed')):
            with self.assertRaisesRegex(OSError, 'copy failed'):
                self.case.run_transaction()
        self.assertEqual(self.case.stored()['status'], 'rolled_back')
        self.case.install.assert_not_called()
        self.assertEqual((self.s.keg/'bin/jq').read_text(), 'original command')

    @unittest.skipUnless(hasattr(os, 'fork'), 'requires POSIX process isolation')
    def test_process_exit_during_backup_can_verify_source_without_receipt(self):
        original_copy = engine.shutil.copytree
        pid = os.fork()
        if pid == 0:
            def crash(*args, **kwargs):
                original_copy(*args, **kwargs)
                os._exit(23)
            try:
                with patch.object(engine.shutil, 'copytree', side_effect=crash):
                    self.case.run_transaction()
            except BaseException:
                os._exit(99)
            os._exit(98)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 23)
        self.assertNotIn('backup', self.case.stored())
        self.assertEqual(self.case.recover('rollback')['status'], 'rolled_back')
        self.assertEqual(self.s.commands, [])

    def test_real_http_recovery_and_request_status(self):
        _, txn = self.fail_request()
        _, port = self.start_http()
        status, review = self.http(port, '/api/migrations/recovery-plan', {
            'transaction_id': txn['transaction_id'], 'action': 'rollback'})
        self.assertEqual(status, 200)
        status, result = self.http(port, '/api/migrations/approve', {'id': review['plan']['id']})
        self.assertEqual(status, 200)
        self.assertEqual(result['migration']['status'], 'rolled_back')
        self.assertEqual(result['request']['status'], 'recovered')
        status, query = self.http(port, '/api/update-requests/'+'e'*32)
        self.assertEqual(status, 200)
        self.assertEqual(query['request']['result']['rolled_back_count'], 1)

    def test_real_http_stale_recovery_returns_structured_conflict(self):
        _, txn = self.fail_request()
        _, port = self.start_http()
        _, review = self.http(port, '/api/migrations/recovery-plan', {
            'transaction_id': txn['transaction_id'], 'action': 'rollback'})
        self.s.keg.mkdir(parents=True)
        (self.s.keg/'user-file').write_text('new content')
        status, result = self.http(port, '/api/migrations/approve', {'id': review['plan']['id']})
        self.assertEqual(status, 409)
        self.assertEqual(result['error'], 'recovery_state_changed')
        self.assertEqual((self.s.keg/'user-file').read_text(), 'new content')


if __name__ == '__main__':
    unittest.main()
