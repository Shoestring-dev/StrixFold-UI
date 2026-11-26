"""Direct host routing: no private user data, model requests or real remote commands."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import json
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'mac'))
import chat_direct as D
import chat_tools as T
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import site_fixture


class DirectTests(unittest.TestCase):
    def setUp(self):
        site_fixture.use_targets(self)

    def ctx(self, computer='mac'):
        self.row = {'perm': 'direct', 'perm_opts': json.dumps({'computer': computer})}
        store = type('Store', (), {'chat': lambda _, cid: self.row})()
        return T.TurnCtx('test', self.row, store, 'dsv41-unc', level='direct', opts={'computer': computer})

    def test_permission_is_explicit_and_validated(self):
        self.assertEqual(T.check_perm(None, {'perm': 'direct', 'computer': 'toolbox'}, 'dsv41-unc'),
                         ('direct', {'computer': 'toolbox'}))
        for computer in (None, '', 'box1', 'mac; echo wrong'):
            with self.assertRaises(ValueError):
                T.check_perm(None, {'perm': 'direct', 'computer': computer}, 'dsv41-unc')
        with self.assertRaises(ValueError):
            T.check_perm({'private': True}, {'perm': 'direct', 'computer': 'mac'}, 'dsv41-unc')

    def test_the_mac_is_switched_off_and_a_chat_already_on_it_runs_nothing(self):
        # (its commands could read the Mac's keys and tokens)
        with self.assertRaisesRegex(ValueError, 'switched off'):
            T.check_perm(None, {'perm': 'direct', 'computer': 'mac'}, 'dsv41-unc')
        self.assertIn({'id': 'mac', 'name': 'Mac', 'available': False, 'off': 'off for now'}, D.targets())
        with self.assertRaisesRegex(ValueError, 'switched off'):
            D.argv_for('mac', 'printf test', 1)
        ctx = self.ctx('mac')  # set before the switch, or an agent spawned from such a chat
        with patch.object(D, 'execute') as run:
            r = D.Direct(ctx).call('run_command', {'command': 'cat ~/.ssh/config', 'summary': 'x'}, ctx)
        self.assertEqual((r['status'], r['line']), ('failed', 'Command not run'))
        self.assertIn('switched off', r['text'])
        run.assert_not_called()

    def test_not_offered_without_a_computer_to_run_on(self):
        self.assertEqual(T.offered('direct', 'dsv41-unc'), (True, None))  # this setup's computers
        with patch.dict(D.TARGETS, {'mac': D.TARGETS['mac']}, clear=True):  # a fresh install: the Mac only, switched off
            self.assertEqual(T.offered('direct', 'dsv41-unc'), (False, D.NO_COMPUTER))
            with self.assertRaisesRegex(ValueError, 'direct_targets in config.json'):
                T.check_perm(None, {'perm': 'direct', 'computer': 'mac'}, 'dsv41-unc')

    @patch.object(D, 'MAC_OFF', False)
    def test_mac_retains_private_exclusion(self):
        argv = D.argv_for('mac', 'printf test', 1)
        self.assertEqual(argv[0], '/usr/bin/sandbox-exec')
        self.assertIn('(allow default)', argv[2])
        self.assertIn(D.PRIVATE_PATH, argv[2])
        self.assertEqual(argv[-1], 'printf test')

    def test_windows_command_is_encoded_not_shell_interpolated(self):
        import base64
        command = 'Write-Output "hello; $HOME"'
        argv = D.argv_for('toolbox', command, 3)
        self.assertIn('gpu-pc', argv)
        script = base64.b64decode(argv[-1].split()[-1]).decode('utf-16le')
        self.assertIn(base64.b64encode(command.encode()).decode(), script)
        self.assertIn('ReadLineAsync', script)
        self.assertIn('finally { Stop-Job', script)
        self.assertNotIn(command, argv[-1])

    def test_vps_runs_bash_encoded_with_a_lease(self):
        import base64
        command = """printf '%s\\n' "a'b\\"c $HOME"; exit 3"""
        argv = D.argv_for('vps', command, 7)
        self.assertEqual(argv[:7 + len(D.SSH_SAFE)], ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', *D.SSH_SAFE, 'vps-desktop'])
        self.assertNotIn(command, argv[-1])
        encoded = argv[-1].split('printf %s ')[1].split(' |')[0]
        script = base64.b64decode(encoded).decode()
        self.assertIn(base64.b64encode(command.encode()).decode(), script)
        for piece in ('setsid bash -c "$cmd" &', 'end=$((SECONDS + 7))', 'read -r -t 1 _lease', 'kill -TERM -- "-$pid"', 'wait "$pid"'):
            self.assertIn(piece, script)
        self.assertEqual(T.check_perm(None, {'perm': 'direct', 'computer': 'vps'}, 'dsv41-unc'),
                         ('direct', {'computer': 'vps'}))
        note = D.Direct(self.ctx('vps')).system_note(self.ctx('vps'))
        self.assertIn('VPS using bash', note)
        self.assertIn('DISPLAY=:10', note)
        self.assertIn('127.0.0.1:18080', note)
        self.assertNotIn('DISPLAY', D.Direct(self.ctx('toolbox')).system_note(self.ctx('toolbox')))

    def test_a_computer_without_an_ssh_config_entry_brings_its_own_options(self):
        # a second VPS: no ~/.ssh/config alias, so key and known_hosts come from its target
        argv = D.argv_for('vps2', 'uptime', 5)
        self.assertEqual(argv[:11 + len(D.SSH_SAFE)], ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', *D.SSH_SAFE, '-o', 'IdentityFile=/tmp/id_test',
                                     '-o', 'UserKnownHostsFile=/tmp/known_hosts_test', 'ubuntu@192.0.2.10'])
        self.assertTrue(argv[-1].startswith('exec bash -c '))
        self.assertEqual(D.argv_for('vps', 'uptime', 5)[6 + len(D.SSH_SAFE)], 'vps-desktop')  # an alias's options stay in ~/.ssh/config
        self.assertIn({'id': 'vps2', 'name': 'VPS 2', 'available': True}, D.targets())

    def test_no_approval_prompt_and_selected_host(self):
        ctx = self.ctx('toolbox')
        ctx.ask = lambda _: self.fail('Direct mode must not ask approval')
        with patch.object(D, 'execute', return_value=(0, 'ok', None)) as run:
            result = D.Direct(ctx).call('run_command', {'command': 'Write-Output ok'}, ctx)
        self.assertEqual(result['status'], 'ok')
        self.assertIn('gpu-pc', run.call_args.args[0])

    def test_a_step_reads_as_its_one_line_summary_and_shows_the_command_inside(self):
        # (every step said only "VPS: exit 0")
        ctx = self.ctx('vps')
        self.assertIn('summary', D.Direct(ctx).specs()[0]['function']['parameters']['required'])
        with patch.object(D, 'execute', return_value=(0, 'a\nb', None)):
            r = D.Direct(ctx).call('run_command', {'command': 'ls -la ~/agents', 'summary': 'List the  agents\nfolder'}, ctx)
        self.assertEqual(r['line'], 'VPS: List the agents folder · exit 0')
        self.assertTrue(r['out'].startswith('$ ls -la ~/agents\n'))
        with patch.object(D, 'execute', return_value=(2, '', None)):
            r = D.Direct(ctx).call('run_command', {'command': 'cat ' + 'x' * 200}, ctx)  # no summary: the command, cut
        self.assertTrue(r['line'].startswith('VPS: cat xxx') and r['line'].endswith('… · exit 2'), r['line'])
        self.assertLessEqual(len(r['line']), len('VPS: ') + 90 + len(' · exit 2'))

    def test_host_change_revokes_existing_turn(self):
        ctx = self.ctx()
        tool = D.Direct(ctx)
        self.row['perm_opts'] = json.dumps({'computer': 'toolbox'})
        with patch.object(D, 'execute') as run:
            result = tool.call('run_command', {'command': 'printf no'}, ctx)
        self.assertEqual(result['status'], 'stopped')
        run.assert_not_called()

    def test_downgrade_revokes_existing_turn(self):
        ctx = self.ctx()
        self.row['perm'] = 'none'
        with patch.object(D, 'execute') as run:
            self.assertEqual(D.Direct(ctx).call('run_command', {'command': 'printf no'}, ctx)['status'], 'stopped')
        run.assert_not_called()

    def test_demo_cannot_execute(self):
        ctx = self.ctx(); ctx.fake = True
        with patch.object(D, 'execute') as run:
            self.assertEqual(D.Direct(ctx).call('run_command', {'command': 'printf no'}, ctx)['status'], 'failed')
        run.assert_not_called()

    def test_stop_ends_local_process(self):
        stop = threading.Event()
        timer = threading.Timer(.15, stop.set); timer.start()
        start = time.monotonic()
        try:
            _, _, reason = D.execute(['/bin/sh', '-c', 'sleep 30'], stop.is_set, 40, lambda _: None)
        finally:
            timer.cancel()
        self.assertEqual(reason, 'stopped')
        self.assertLess(time.monotonic()-start, 6)

if __name__ == '__main__': unittest.main()
