import asyncio
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

HELPER = Path(__file__).resolve().parents[1] / 'ops' / 'browser-target-recovery.py'

class TargetRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def load(self):
        self.assertTrue(HELPER.exists(), 'bounded target recovery helper is missing')
        spec = importlib.util.spec_from_file_location('target_recovery', HELPER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    async def test_explicit_zero_port_is_rejected_without_profile_or_network(self):
        module = self.load()
        with patch('sys.argv', ['recovery', '--port', '0', 'A' * 32]), \
                patch.object(module.Path, 'read_text', return_value='45661\n') as profile, \
                patch.object(module.urllib.request, 'urlopen') as fetch:
            with self.assertRaises(SystemExit) as raised:
                await module.main()
            self.assertEqual(raised.exception.code, 2)
            profile.assert_not_called()
            fetch.assert_not_called()

    async def test_healthy_target_is_never_terminated(self):
        module = self.load()
        calls = []
        async def rpc(url, method, params=None):
            calls.append(method)
            return {'result': {'value': 1}}
        result = await module.recover_target({'id': 'a', 'webSocketDebuggerUrl': 'ws://local/a'}, 'ws://local/browser', rpc)
        self.assertEqual(result['status'], 'healthy')
        self.assertEqual(calls, ['Runtime.evaluate'])

    async def test_unresponsive_target_terminates_then_verifies_without_navigation(self):
        module = self.load()
        calls = []
        async def rpc(url, method, params=None):
            calls.append(method)
            if len(calls) == 1:
                raise TimeoutError()
            return {'result': {'value': 1}}
        result = await module.recover_target({'id': 'a', 'webSocketDebuggerUrl': 'ws://local/a'}, 'ws://local/browser', rpc)
        self.assertEqual(result['status'], 'recovered')
        self.assertEqual(calls, ['Runtime.evaluate', 'Runtime.terminateExecution', 'Runtime.evaluate'])

    async def test_still_hung_target_is_closed_only_by_exact_id(self):
        module = self.load()
        calls = []
        async def rpc(url, method, params=None):
            calls.append((url, method, params))
            if method != 'Target.closeTarget':
                raise TimeoutError()
            return {'success': True}
        result = await module.recover_target({'id': 'a', 'webSocketDebuggerUrl': 'ws://local/a'}, 'ws://local/browser', rpc)
        self.assertEqual(result['status'], 'closed_hung_target')
        self.assertEqual(calls[-1], ('ws://local/browser', 'Target.closeTarget', {'targetId': 'a'}))
        self.assertFalse(any('navigate' in method.lower() for _, method, _ in calls))
