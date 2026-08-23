import os
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from browser_logic import OpenActionGuard, TabActivityRegistry, TabLimitError, parse_command, parse_dismiss_options, format_snapshot, parse_devtools_active_port, resolve_browser_executable, resolve_profile_dir, should_disable_sandbox


class DevToolsPortTests(unittest.TestCase):
    def test_parses_chrome_active_port_file(self):
        self.assertEqual(parse_devtools_active_port('43127\n/devtools/browser/id\n'), 43127)

    def test_rejects_invalid_active_port_file(self):
        with self.assertRaises(ValueError):
            parse_devtools_active_port('not-a-port\n')


class DismissOptionsTests(unittest.TestCase):
    def test_defaults_to_rejecting_optional_cookies(self):
        self.assertEqual(parse_dismiss_options(['dismiss', 'overlays']), 'reject-optional')

    def test_accepts_explicit_cookie_policy(self):
        self.assertEqual(parse_dismiss_options(['dismiss', 'overlays', '--cookies=accept']), 'accept')

    def test_rejects_unknown_cookie_policy(self):
        with self.assertRaisesRegex(ValueError, 'cookie policy'):
            parse_dismiss_options(['dismiss', 'overlays', '--cookies=surprise'])


class ParseCommandTests(unittest.TestCase):
    def test_preserves_quoted_fill_text(self):
        self.assertEqual(
            parse_command('fill @e2 "鼎泰豐 101 店"'),
            ['fill', '@e2', '鼎泰豐 101 店'],
        )

    def test_parses_upload_command_with_multiple_paths(self):
        self.assertEqual(
            parse_command('upload @e1 "/tmp/doc 1.pdf" /tmp/doc2.png'),
            ['upload', '@e1', '/tmp/doc 1.pdf', '/tmp/doc2.png'],
        )

    def test_parses_scroll_commands(self):
        self.assertEqual(parse_command('scroll down'), ['scroll', 'down'])
        self.assertEqual(parse_command('scroll bottom'), ['scroll', 'bottom'])
        self.assertEqual(parse_command('scroll top'), ['scroll', 'top'])
        self.assertEqual(parse_command('scroll down 1000'), ['scroll', 'down', '1000'])

    def test_rejects_empty_command(self):
        with self.assertRaisesRegex(ValueError, 'empty browser command'):
            parse_command('   ')

    def test_rejects_shell_style_command_chaining(self):
        with self.assertRaisesRegex(ValueError, 'one browser command'):
            parse_command('wait 2000 && screenshot')


class BrowserExecutableTests(unittest.TestCase):
    def test_prefers_explicit_profile_directory(self):
        with patch.dict(os.environ, {'PI_NODRIVER_PROFILE': '/tmp/custom-profile'}):
            self.assertEqual(str(resolve_profile_dir()), '/tmp/custom-profile')

    def test_no_sandbox_is_opt_in(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(should_disable_sandbox())
        with patch.dict(os.environ, {'PI_NODRIVER_NO_SANDBOX': '1'}):
            self.assertTrue(should_disable_sandbox())

    def test_prefers_explicit_browser_path(self):
        with patch.dict(os.environ, {'PI_NODRIVER_CHROME': '/custom/chrome'}):
            self.assertEqual(resolve_browser_executable(), '/custom/chrome')

    @patch('browser_logic.shutil.which')
    def test_discovers_google_chrome_on_path(self, which):
        which.side_effect = lambda command: '/usr/bin/google-chrome' if command == 'google-chrome' else None
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_browser_executable(), '/usr/bin/google-chrome')


class OpenActionGuardTests(unittest.TestCase):
    def test_blocks_third_consecutive_open_even_when_urls_differ(self):
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open')
        guard.check('session-a', 'open')

        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.check('session-a', 'open')

    def test_remains_blocked_until_a_non_open_action(self):
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open')
        guard.check('session-a', 'open')

        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'blocked until'):
                guard.check('session-a', 'open')

        guard.check('session-a', 'snapshot')
        guard.check('session-a', 'open')
        guard.check('session-a', 'open')

    def test_tracks_sessions_independently(self):
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open')
        guard.check('session-a', 'open')
        guard.check('session-b', 'open')
        guard.check('session-b', 'open')

        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.check('session-a', 'open')
        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.check('session-b', 'open')


class FakeTarget:
    def __init__(self, target_id):
        self.target_id = target_id


class FakePage:
    def __init__(self, target_id):
        self.target = FakeTarget(target_id)


class TabActivityRegistryTests(unittest.TestCase):
    def test_opening_thirty_evicts_least_recently_used_and_keeps_recently_touched_old_tabs(self):
        now = 0

        def clock():
            nonlocal now
            now += 1
            return now

        registry = TabActivityRegistry(max_tabs=20, clock=clock)
        pages = [FakePage(f'tab-{index}') for index in range(30)]

        for index, page in enumerate(pages):
            for victim in registry.evictions_for_new_tabs(1):
                registry.remove(victim.page)
            registry.register(page, f'session-{index}')
            if index == 19:
                registry.touch(pages[0])
                registry.touch(pages[1])

        remaining = {record.target_id for record in registry.records()}
        self.assertEqual(len(remaining), 20)
        self.assertIn('tab-0', remaining)
        self.assertIn('tab-1', remaining)
        self.assertTrue({f'tab-{index}' for index in range(2, 12)}.isdisjoint(remaining))
        self.assertTrue({f'tab-{index}' for index in range(12, 30)}.issubset(remaining))

    def test_active_sessions_are_not_eviction_candidates(self):
        registry = TabActivityRegistry(max_tabs=2)
        registry.register(FakePage('tab-a'), 'session-a')
        registry.register(FakePage('tab-b'), 'session-b')

        victims = registry.evictions_for_new_tabs(1, protected_sessions={'session-a'})

        self.assertEqual([victim.target_id for victim in victims], ['tab-b'])

    def test_raises_when_all_tabs_are_protected(self):
        registry = TabActivityRegistry(max_tabs=2)
        registry.register(FakePage('tab-a'), 'session-a')
        registry.register(FakePage('tab-b'), 'session-b')

        with self.assertRaisesRegex(TabLimitError, 'TAB_LIMIT'):
            registry.evictions_for_new_tabs(
                1,
                protected_sessions={'session-a', 'session-b'},
            )


class SnapshotFormattingTests(unittest.TestCase):
    def test_marks_download_targets_with_the_suggested_filename(self):
        output = format_snapshot([{
            'ref': 'e2',
            'tag': 'a',
            'text': 'Export report',
            'href': 'https://example.test/report',
            'download': 'quarterly.pdf',
        }])

        self.assertIn('download="quarterly.pdf"', output)

    def test_compacts_long_text_and_urls_to_bound_snapshot_tokens(self):
        output = format_snapshot([{
            'ref': 'e9',
            'tag': 'a',
            'text': 'T' * 400,
            'href': 'https://example.test/' + 'p' * 400,
        }])

        self.assertIn('"' + 'T' * 160 + '…"', output)
        self.assertIn('href="https://example.test/' + 'p' * 139 + '…"', output)
        self.assertLessEqual(len(output), 340)

    def test_formats_interactive_element_for_agent(self):
        output = format_snapshot([
            {
                'ref': 'e1',
                'tag': 'a',
                'text': '現場到號查詢',
                'href': 'https://example.test/queue',
                'placeholder': '',
                'ariaLabel': '',
                'value': '',
            }
        ])
        self.assertEqual(
            output,
            '@e1 <a> "現場到號查詢" href="https://example.test/queue"',
        )


if __name__ == '__main__':
    unittest.main()
