"""Transaction fault injection. All paths and provider operations are fixtures."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import migration_engine as engine
import unified_updates as unified

REAL_ACTIVATE = engine.routing.activate
REAL_RESTORE = engine.routing.restore


class FixtureService:
    def __init__(self, root, *, create_source=True):
        self.DATA_DIR = root / 'data'
        self.TOOLS_ROOT = root / 'tools'
        self.keg = root / 'Cellar' / 'jq' / '1.0'
        if create_source:
            (self.keg / 'bin').mkdir(parents=True)
            (self.keg / 'bin/jq').write_text('original command')
            (self.keg / 'bin/jq').chmod(0o755)
            (self.keg / 'bin/alias').symlink_to('jq')
            (self.keg / 'INSTALL_RECEIPT.json').write_text('{}')
        self.commands = []
        self.failure = None

    def read_json(self, path):
        return json.loads(Path(path).read_text()) if Path(path).is_file() else None

    def brew_bin(self):
        return 'fixture-brew-never-executed'

    def run_cmd(self, argv, **kwargs):
        self.commands.append((argv, kwargs))
        if argv[1] == 'uninstall':
            if self.failure == 'partial':
                (self.keg / 'bin/jq').unlink()
                return SimpleNamespace(code=1, stderr='partial removal', stdout='')
            if self.failure == 'noop':
                return SimpleNamespace(code=0, stderr='', stdout='')
            shutil.rmtree(self.keg)
        return SimpleNamespace(code=0, stderr='', stdout='')

    def raise_if_cancel_requested(self):
        pass

    def now_iso(self):
        return 'fixture-time'

    def compute_snapshot(self, *args, **kwargs):
        return {'packages': [self.row]}

    def safe_refresh_snapshot(self, snapshot):
        return {'packages': []}, None

    def set_check_progress(self, **kwargs):
        pass


class RecoveryTransactions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.s = FixtureService(self.root)
        self.directory = engine._transaction_path(self.s, 'a'*32)
        self.directory.mkdir(parents=True)
        self.plan = {
            'id': 'b'*32, 'schema_version': engine.SCHEMA, 'action': 'migrate',
            'expires_at': time.time()+600, 'fingerprint': 'fingerprint',
            'profile': 'jq', 'source': {'name': 'jq', 'keg': str(self.s.keg),
                'installation_id': 'homebrew:formula:jq',
                'commands': {'jq': str(self.s.keg / 'bin/jq')}},
            'target': {'provider': 'pixi', 'installation_id': 'pixi:jq',
                'directory': str(self.root / 'target'), 'prefix': str(self.root / 'prefix'),
                'candidate': {}, 'commands': {'jq': str(self.root / 'target/jq')}}}
        self.journal = {'schema_version': engine.SCHEMA, 'transaction_id': 'a'*32,
                        'plan': self.plan, 'phases': {}, 'status': 'running'}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        def mocked(obj, attr, **kwargs):
            return self.stack.enter_context(patch.object(obj, attr, **kwargs))
        import pixi_provider
        self.install = mocked(pixi_provider, 'install_locked', return_value=self.root/'prefix')
        self.ready = mocked(engine, '_target_ready', return_value={'ok': True})
        self.safe = mocked(engine, '_source_still_safe', return_value={})
        self.activate = mocked(engine.routing, 'activate', return_value={
            'active': '/fixture/active', 'target': '/fixture/target'})
        self.restore = mocked(engine.routing, 'restore')
        self.shell = mocked(engine.routing, 'verify_shell')
        self.postcheck = mocked(engine, '_target_postcheck', return_value={'ok': True})
        mocked(engine.routing, 'owned_shim_hashes', return_value={})
        self.register = mocked(engine, '_register_pixi_target', return_value=[{
            'installation_id': 'pixi:jq'}])
        # Use the real ownership writer: journals and registry survive restart.

    def run_transaction(self):
        return engine._run(self.s, self.journal, self.directory)

    def stored(self):
        return self.s.read_json(self.directory/'journal.json')

    def recover(self, action, service=None):
        s = service or self.s
        preview = engine.recovery_plan(s, 'a'*32, action)
        return engine.approve(s, preview['id'])

    def backup(self):
        engine._backup(self.s, self.journal, self.directory)
        self.journal['phases'] = {'install': {'status': 'finished'}}
        engine._write(self.directory/'journal.json', self.journal)

    def test_success_commits_only_after_postcheck(self):
        result = self.run_transaction()
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(self.s.keg.exists())
        self.assertTrue(engine._verify_backup(result))
        self.assertEqual(engine.ownership.load_routes(self.s.TOOLS_ROOT)['routes'][0]
                         ['transaction_id'], 'a'*32)
        with self.assertRaisesRegex(engine.MigrationConflict, 'already_resolved'):
            engine.recovery_plan(self.s, 'a'*32, 'rollback')

    def test_install_failure_leaves_source_and_does_not_activate(self):
        self.install.side_effect = RuntimeError('install failed')
        with self.assertRaisesRegex(RuntimeError, 'install failed'):
            self.run_transaction()
        self.assertEqual(self.stored()['status'], 'rolled_back')
        self.assertTrue((self.s.keg/'bin/jq').exists())
        self.activate.assert_not_called()
        self.assertEqual(self.s.commands, [])

    def test_smoke_failure_never_uninstalls_source(self):
        self.ready.side_effect = RuntimeError('smoke failed')
        with self.assertRaisesRegex(RuntimeError, 'smoke failed'):
            self.run_transaction()
        self.assertEqual(self.stored()['status'], 'rolled_back')
        self.assertEqual(self.s.commands, [])
        self.activate.assert_not_called()

    def test_activation_failure_restores_owned_pending_state(self):
        def interrupted(*args, **kwargs):
            kwargs['progress']({'state': 'pointer_switch_pending',
                                'active': '/fixture/active', 'target': '/fixture/target'})
            raise KeyboardInterrupt()
        self.activate.side_effect = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.run_transaction()
        self.assertEqual(self.stored()['status'], 'rolled_back')
        self.restore.assert_called_once()
        self.assertEqual(self.s.commands, [])

    def test_partial_uninstall_rolls_back_missing_entries_after_restart(self):
        self.s.failure = 'partial'
        with self.assertRaisesRegex(engine.MigrationConflict, 'partial removal'):
            self.run_transaction()
        self.assertEqual(self.stored()['status'], 'needs_attention')
        self.restore.assert_not_called()
        # Reconstruct the service without recreating source files.
        restarted = FixtureService(self.root, create_source=False)
        result = self.recover('rollback', restarted)
        self.assertEqual(result['status'], 'rolled_back')
        self.assertEqual((self.s.keg/'bin/jq').read_text(), 'original command')
        self.assertEqual(engine._manifest(self.s.keg), engine._verify_backup(result)[1])
        self.assertEqual([cmd[0][1] for cmd in self.s.commands], ['uninstall'])
        self.assertEqual([cmd[0][1] for cmd in restarted.commands], ['link'])
        self.assertFalse(restarted.commands[-1][1]['cancellable'])
        self.assertNotIn('--force', restarted.commands[-1][0])

    def test_partial_source_edit_blocks_rollback_without_overwrite(self):
        self.s.failure = 'partial'
        with self.assertRaises(engine.MigrationConflict):
            self.run_transaction()
        (self.s.keg/'INSTALL_RECEIPT.json').write_text('independent edit')
        with self.assertRaisesRegex(engine.MigrationConflict, 'source_contents_changed'):
            self.recover('rollback')
        self.assertEqual(self.stored()['status'], 'needs_attention')
        self.assertEqual((self.s.keg/'INSTALL_RECEIPT.json').read_text(), 'independent edit')
        self.assertFalse((self.s.keg/'bin/jq').exists())
        self.restore.assert_not_called()

    def test_recovery_approval_refuses_source_changed_after_preview(self):
        self.backup()
        self.journal.update(status='needs_attention')
        engine._write(self.directory/'journal.json', self.journal)
        preview = engine.recovery_plan(self.s, 'a'*32, 'rollback')
        (self.s.keg/'bin/jq').write_text('new edit')
        with self.assertRaisesRegex(engine.MigrationConflict, 'recovery_state_changed'):
            engine.approve(self.s, preview['id'])
        self.assertEqual(self.s.commands, [])

    def test_missing_source_after_cleanup_can_resume_without_second_uninstall(self):
        self.postcheck.side_effect = RuntimeError('postcheck failed')
        with self.assertRaises(RuntimeError):
            self.run_transaction()
        self.postcheck.side_effect = None
        result = self.recover('resume')
        self.assertEqual(result['status'], 'completed')
        self.install.assert_called_once()
        self.activate.assert_called_once()
        self.assertEqual([cmd[0][1] for cmd in self.s.commands], ['uninstall'])

    def test_partial_uninstall_cannot_resume_by_uninstalling_again(self):
        self.s.failure = 'partial'
        with self.assertRaises(engine.MigrationConflict):
            self.run_transaction()
        with self.assertRaisesRegex(engine.MigrationConflict, 'source_changed_before_cleanup'):
            self.recover('resume')
        self.assertEqual([cmd[0][1] for cmd in self.s.commands], ['uninstall'])
        self.restore.assert_not_called()

    def test_resume_smoke_failure_after_cleanup_keeps_target_route(self):
        self.postcheck.side_effect = RuntimeError('postcheck failed')
        with self.assertRaises(RuntimeError):
            self.run_transaction()
        self.ready.side_effect = RuntimeError('target changed')
        with self.assertRaisesRegex(RuntimeError, 'target changed'):
            self.recover('resume')
        self.assertEqual(self.stored()['status'], 'needs_attention')
        self.restore.assert_not_called()

    def test_incomplete_install_is_not_assumed_complete_on_resume(self):
        self.backup()
        self.journal['phases']['install']['status'] = 'started'
        engine._write(self.directory/'journal.json', self.journal)
        with self.assertRaisesRegex(engine.MigrationConflict, 'target_install_not_completed'):
            self.recover('resume')
        self.install.assert_not_called()
        self.assertEqual(self.s.commands, [])

    def test_success_exit_from_uninstall_with_keg_present_is_not_success(self):
        self.s.failure = 'noop'
        with self.assertRaisesRegex(engine.MigrationConflict, 'source_uninstall_incomplete'):
            self.run_transaction()
        self.register.assert_not_called()
        self.assertEqual(self.stored()['status'], 'needs_attention')

    def test_changed_backup_blocks_recovery(self):
        self.backup()
        backup, _ = engine._verify_backup(self.journal)
        (backup/'bin/jq').write_text('damaged backup')
        with self.assertRaisesRegex(engine.MigrationConflict, 'backup_contents_changed'):
            self.recover('rollback')
        self.assertEqual(self.s.commands, [])

    def test_symlink_replacing_source_is_never_followed(self):
        self.backup()
        other = self.root/'independent'
        other.mkdir()
        shutil.rmtree(self.s.keg)
        self.s.keg.symlink_to(other)
        with self.assertRaisesRegex(engine.MigrationConflict, 'source_path_changed'):
            self.recover('rollback')
        self.assertEqual(list(other.iterdir()), [])

    def test_full_removal_can_rollback_and_relink_after_restart(self):
        self.postcheck.side_effect = RuntimeError('postcheck failed')
        with self.assertRaises(RuntimeError):
            self.run_transaction()
        restarted = FixtureService(self.root, create_source=False)
        result = self.recover('rollback', restarted)
        self.assertEqual(result['status'], 'rolled_back')
        self.assertEqual(engine._manifest(self.s.keg), engine._verify_backup(result)[1])
        self.assertEqual(restarted.commands[0][0][1], 'link')

    def test_independent_new_file_is_preserved_and_blocks_rollback(self):
        self.s.failure = 'partial'
        with self.assertRaises(engine.MigrationConflict):
            self.run_transaction()
        (self.s.keg/'user-file').write_text('keep me')
        with self.assertRaisesRegex(engine.MigrationConflict, 'source_contents_changed'):
            self.recover('rollback')
        self.assertEqual((self.s.keg/'user-file').read_text(), 'keep me')
        self.restore.assert_not_called()

    def test_partial_keg_with_changed_directory_symlink_blocks_restore(self):
        self.s.failure = 'partial'
        with self.assertRaises(engine.MigrationConflict):
            self.run_transaction()
        shutil.rmtree(self.s.keg/'bin')
        other = self.root/'other-bin'
        other.mkdir()
        (self.s.keg/'bin').symlink_to(other)
        with self.assertRaisesRegex(engine.MigrationConflict, 'source_contents_changed'):
            self.recover('rollback')
        self.assertEqual(list(other.iterdir()), [])

    def test_relink_failure_stays_unresolved_and_can_be_retried(self):
        self.s.failure = 'partial'
        with self.assertRaises(engine.MigrationConflict):
            self.run_transaction()
        with patch.object(self.s, 'run_cmd', return_value=SimpleNamespace(
                code=1, stderr='link failed', stdout='')):
            with self.assertRaisesRegex(engine.MigrationConflict, 'link failed'):
                self.recover('rollback')
        self.assertEqual(self.stored()['status'], 'needs_attention')
        self.restore.assert_not_called()
        self.assertEqual(self.recover('rollback')['status'], 'rolled_back')
        self.assertEqual(self.s.commands[-1][0][1], 'link')

    def test_missing_source_before_cleanup_is_not_silently_accepted(self):
        self.backup()
        shutil.rmtree(self.s.keg)
        with self.assertRaisesRegex(engine.MigrationConflict, 'source_disappeared_before_cleanup'):
            self.recover('resume')
        self.register.assert_not_called()
        self.assertEqual(self.stored()['status'], 'needs_attention')

    def test_changed_source_before_uninstall_is_not_removed(self):
        def alter_source(*args, **kwargs):
            (self.s.keg/'bin/jq').write_text('independent edit')
            return {'active': '/fixture/active', 'target': '/fixture/target'}
        self.activate.side_effect = alter_source
        with self.assertRaisesRegex(engine.MigrationConflict, 'source_changed_before_cleanup'):
            self.run_transaction()
        self.assertEqual((self.s.keg/'bin/jq').read_text(), 'independent edit')
        self.assertEqual(self.s.commands, [])
        self.assertEqual(self.stored()['status'], 'needs_attention')

    def test_rollback_does_not_silently_rewrite_committed_ownership(self):
        self.backup()
        engine._write(self.s.TOOLS_ROOT/'migrations/routes.json', {
            'schema_version': 1, 'routes': [{'logical_id': 'jq', 'status': 'committed',
                'transaction_id': 'a'*32}]})
        with self.assertRaisesRegex(engine.MigrationConflict, 'ownership_requires_reconciliation'):
            self.recover('rollback')
        self.assertEqual(engine.ownership.load_routes(self.s.TOOLS_ROOT)['routes'][0]
                         ['transaction_id'], 'a'*32)
        self.restore.assert_not_called()

    def test_macports_uses_the_same_partial_source_recovery(self):
        self.plan['profile'] = 'terminal-notifier'
        self.plan['source']['name'] = 'terminal-notifier'
        self.plan['target'].update(provider='macports', ports=['terminal-notifier'])
        self.s.failure = 'partial'
        with patch.object(engine.mp, 'privileged', return_value=SimpleNamespace(code=0)):
            with self.assertRaises(engine.MigrationConflict):
                self.run_transaction()
        result = self.recover('rollback')
        self.assertEqual(result['status'], 'rolled_back')
        self.assertEqual(engine._manifest(self.s.keg), engine._verify_backup(result)[1])
        self.install.assert_not_called()

    def test_resume_after_ownership_write_does_not_increment_generation(self):
        original_phase = engine._phase
        def interrupted(journal, directory, phase, state, **values):
            if phase == 'ownership' and state == 'finished':
                raise KeyboardInterrupt()
            return original_phase(journal, directory, phase, state, **values)
        with patch.object(engine, '_phase', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.run_transaction()
        before = (self.s.TOOLS_ROOT/'migrations/routes.json').read_bytes()
        restarted = FixtureService(self.root, create_source=False)
        self.assertEqual(self.recover('resume', restarted)['status'], 'completed')
        self.assertEqual((self.s.TOOLS_ROOT/'migrations/routes.json').read_bytes(), before)
        self.assertEqual(restarted.commands, [])

    def test_changed_ownership_is_not_overwritten_on_resume(self):
        original_phase = engine._phase
        def interrupted(journal, directory, phase, state, **values):
            if phase == 'ownership' and state == 'finished':
                raise KeyboardInterrupt()
            return original_phase(journal, directory, phase, state, **values)
        with patch.object(engine, '_phase', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.run_transaction()
        path = self.s.TOOLS_ROOT/'migrations/routes.json'
        data = self.s.read_json(path)
        data['routes'][0]['active'] = '/independent/route'
        engine._write(path, data)
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'ownership changed independently'):
            self.recover('resume')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.stored()['status'], 'needs_attention')

    @unittest.skipUnless(hasattr(os, 'fork'), 'requires POSIX process isolation')
    def test_process_exit_after_uninstall_resumes_from_durable_journal(self):
        original_phase = engine._phase
        pid = os.fork()
        if pid == 0:
            def exit_after_removal(journal, directory, phase, state, **values):
                if phase == 'cleanup' and state == 'finished':
                    os._exit(23)  # no handlers, finally blocks or in-memory state survive
                return original_phase(journal, directory, phase, state, **values)
            try:
                with patch.object(engine, '_phase', side_effect=exit_after_removal):
                    self.run_transaction()
            except BaseException:
                os._exit(99)
            os._exit(98)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 23)
        self.assertFalse(self.s.keg.exists())
        self.assertEqual(self.stored()['phases']['cleanup']['status'], 'started')
        restarted = FixtureService(self.root, create_source=False)
        self.assertEqual(self.recover('resume', restarted)['status'], 'completed')
        self.install.assert_not_called()  # the parent/restarted execution did not reinstall
        self.assertEqual(restarted.commands, [])

    def test_edited_pixi_metadata_blocks_rollback_before_source_or_route_changes(self):
        self.s.failure = 'partial'
        with self.assertRaises(engine.MigrationConflict):
            self.run_transaction()
        journal = self.stored()
        record = {'transaction_id': 'a'*32, 'prefix': self.plan['target']['prefix'],
                  'version': '2.0', 'executables': ['jq']}
        journal['pixi_managed_record'] = record
        engine._write(self.directory/'journal.json', journal)
        engine._write(self.s.TOOLS_ROOT/'installed.json', {
            'jq': dict(record, executables=['user-added-command'])})
        with self.assertRaisesRegex(engine.MigrationConflict, 'pixi_managed_record_changed'):
            self.recover('rollback')
        self.assertFalse((self.s.keg/'bin/jq').exists())
        self.restore.assert_not_called()
        self.assertEqual(self.s.read_json(self.s.TOOLS_ROOT/'installed.json')['jq']
                         ['executables'], ['user-added-command'])

    def test_new_ownership_by_another_transaction_is_not_replaced_on_resume(self):
        self.postcheck.side_effect = RuntimeError('postcheck failed')
        with self.assertRaises(RuntimeError):
            self.run_transaction()
        self.postcheck.side_effect = None
        path = self.s.TOOLS_ROOT/'migrations/routes.json'
        engine._write(path, {'schema_version': 1, 'routes': [{
            'logical_id': 'jq', 'source_installation_id': 'homebrew:formula:jq',
            'transaction_id': 'f'*32, 'status': 'committed'}]})
        before = path.read_bytes()
        with self.assertRaisesRegex(engine.MigrationConflict, 'ownership_changed_during_recovery'):
            self.recover('resume')
        self.assertEqual(path.read_bytes(), before)

    def prepare_real_routing(self):
        target = Path(self.plan['target']['commands']['jq'])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('fixture target')
        target.chmod(0o755)
        return self.s.TOOLS_ROOT/'migrations/active/jq', self.s.TOOLS_ROOT/'bin/jq'

    def test_real_routing_is_restored_after_partial_source_removal(self):
        active, shim = self.prepare_real_routing()
        self.s.failure = 'partial'
        with patch.object(engine.routing, 'activate', wraps=REAL_ACTIVATE), \
             patch.object(engine.routing, 'restore', wraps=REAL_RESTORE):
            with self.assertRaises(engine.MigrationConflict):
                self.run_transaction()
            self.assertTrue(active.is_symlink())
            self.assertTrue(shim.is_file())
            self.assertEqual(self.recover('rollback')['status'], 'rolled_back')
        self.assertFalse(os.path.lexists(active))
        self.assertFalse(os.path.lexists(shim))
        self.assertEqual((self.s.keg/'bin/jq').read_text(), 'original command')

    def test_real_routing_preserves_an_independent_shim_edit(self):
        active, shim = self.prepare_real_routing()
        self.s.failure = 'partial'
        with patch.object(engine.routing, 'activate', wraps=REAL_ACTIVATE), \
             patch.object(engine.routing, 'restore', wraps=REAL_RESTORE):
            with self.assertRaises(engine.MigrationConflict):
                self.run_transaction()
            shim.write_text('independent shim')
            with self.assertRaisesRegex(engine.routing.RoutingConflict, 'changed shim'):
                self.recover('rollback')
        self.assertEqual(shim.read_text(), 'independent shim')
        self.assertTrue(active.is_symlink())
        self.assertEqual(self.stored()['status'], 'needs_attention')

    @unittest.skipUnless(hasattr(os, 'fork'), 'requires POSIX process isolation')
    def test_process_exit_after_real_pointer_switch_can_rollback(self):
        active, shim = self.prepare_real_routing()
        original_phase = engine._phase
        pid = os.fork()
        if pid == 0:
            def exit_after_activation(journal, directory, phase, state, **values):
                if phase == 'activate' and state == 'finished':
                    os._exit(23)
                return original_phase(journal, directory, phase, state, **values)
            try:
                with patch.object(engine.routing, 'activate', wraps=REAL_ACTIVATE), \
                     patch.object(engine, '_phase', side_effect=exit_after_activation):
                    self.run_transaction()
            except BaseException:
                os._exit(99)
            os._exit(98)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 23)
        self.assertTrue(active.is_symlink())
        self.assertEqual(self.stored()['activation_pending']['state'], 'pointer_switch_pending')
        with patch.object(engine.routing, 'restore', wraps=REAL_RESTORE):
            self.assertEqual(self.recover('rollback')['status'], 'rolled_back')
        self.assertFalse(os.path.lexists(active))
        self.assertFalse(os.path.lexists(shim))
        self.assertEqual(self.s.commands, [])

    def test_unified_request_replay_does_not_repeat_failed_migration(self):
        row = {'name': 'jq', 'kind': 'formula', 'provider': 'homebrew',
               'installation_id': 'homebrew:formula:jq', 'installed_version': '1.0'}
        self.s.row = row
        decision = {'decision_id': 'c'*32, 'material_fingerprint': 'material',
                    'status': 'ready', 'action': 'migrate', 'logical_id': 'jq',
                    'source_installation_id': row['installation_id'],
                    'installed_package_version': '1.0', 'installed_version': '1.0',
                    'current_provider': 'homebrew', 'target_provider': 'pixi',
                    'selected_candidate': {'migration_plan_id': self.plan['id'],
                                           'migration_fingerprint': 'fingerprint'}}
        engine._write(engine._plan_path(self.s, self.plan['id']), self.plan)
        engine._write(self.s.DATA_DIR/'update-plans'/('d'*32+'.json'), {
            'id': 'd'*32, 'expires_at': time.time()+600, 'decisions': [decision]})
        body = {'request_id': 'e'*32, 'plan_id': 'd'*32,
                'decisions': [{'decision_id': 'c'*32, 'material_fingerprint': 'material'}]}
        self.s.failure = 'partial'
        with patch.object(unified, '_revalidate_migration'), \
             patch.object(engine, '_material', return_value=({}, 'fingerprint')):
            with self.assertRaises(engine.MigrationConflict):
                unified.execute(self.s, body)
            with self.assertRaisesRegex(unified.PlanError, 'needs_recovery'):
                unified.execute(self.s, body)
        self.install.assert_called_once()
        self.assertEqual([cmd[0][1] for cmd in self.s.commands], ['uninstall'])
        request = self.s.read_json(unified._request_path(self.s, 'e'*32))
        self.assertEqual(request['status'], 'needs_recovery')


if __name__ == '__main__':
    unittest.main()
