import os
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from browser_logic import OpenActionGuard, TabActivityRegistry, TabLimitError, challenge_context_reason, is_confident_option_match, parse_command, parse_dismiss_options, parse_long_press, format_snapshot, parse_devtools_active_port, rank_option_matches, resolve_browser_executable, resolve_profile_dir, should_disable_sandbox


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


class LongPressCommandTests(unittest.TestCase):
    def test_parses_ref_and_coordinate_targets_with_bounded_duration(self):
        self.assertEqual(
            parse_long_press(['long-press', '@e7', '--ms=1250']),
            {'kind': 'ref', 'ref': '@e7', 'durationMs': 1250},
        )
        self.assertEqual(
            parse_long_press(['long-press', '120.5', '300', '--ms=900']),
            {'kind': 'coordinates', 'x': 120.5, 'y': 300.0, 'durationMs': 900},
        )

    def test_defaults_to_one_second_and_rejects_unsafe_durations(self):
        self.assertEqual(
            parse_long_press(['long-press', '@e2']),
            {'kind': 'ref', 'ref': '@e2', 'durationMs': 1000},
        )
        for duration in (99, 5001):
            with self.assertRaisesRegex(ValueError, 'between 100 and 5000'):
                parse_long_press(['long-press', '@e2', f'--ms={duration}'])
        for coordinate in ('nan', 'inf'):
            with self.assertRaisesRegex(ValueError, 'finite'):
                parse_long_press(['long-press', coordinate, '20'])
        with self.assertRaisesRegex(ValueError, 'only be provided once'):
            parse_long_press(['long-press', '@e2', '--ms=500', '--ms=600'])

    def test_detects_url_and_text_challenge_contexts(self):
        self.assertIsNotNone(
            challenge_context_reason(
                'https://www.skyscanner.com.tw/sttc/px/captcha-v2/index.html',
            )
        )
        self.assertIsNotNone(
            challenge_context_reason('https://example.test/form', 'Verify you are human — press and hold')
        )
        for text in (
            'Just a moment… Checking your browser',
            'Attention Required — Please verify you are a human',
            'Complete the security check to continue',
        ):
            self.assertIsNotNone(challenge_context_reason('https://example.test/form', text))
        self.assertIsNone(challenge_context_reason('https://example.test/product', 'Hold to preview'))


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


class OptionMatchingTests(unittest.TestCase):
    def test_ranks_visible_text_before_an_unrelated_exact_value(self):
        matches = rank_option_matches([
            {'index': 1, 'text': 'Wrong CPU with duplicate lookup value', 'value': '9800X3D'},
            {'index': 2, 'text': 'AMD Ryzen 7 9800X3D', 'value': 'cpu-9800'},
        ], '9800X3D')

        self.assertEqual(matches[0]['index'], 2)
        self.assertTrue(is_confident_option_match(matches, '9800X3D'))

    def test_token_search_handles_spacing_punctuation_and_word_order(self):
        matches = rank_option_matches([
            {'index': 1, 'text': 'DDR5-5600 32GB CL46', 'value': 'slow'},
            {'index': 2, 'text': 'FURY Beast 32GB(16GB*2), DDR5-6000 / CL30', 'value': 'fast'},
        ], '32gb 6000 cl30')

        self.assertEqual([match['index'] for match in matches], [2])

    def test_similarly_ranked_product_variants_require_refinement(self):
        matches = rank_option_matches([
            {'index': 1, 'text': 'AMD R7 9800X3D 代理盒裝', 'value': 'boxed'},
            {'index': 2, 'text': '搭板專案 AMD R7 9800X3D 代理盒裝', 'value': 'bundle'},
        ], '9800X3D')

        self.assertFalse(is_confident_option_match(matches, '9800X3D'))

    def test_alphanumeric_model_tokens_match_with_optional_spacing(self):
        matches = rank_option_matches([
            {'index': 1, 'text': 'ASUS DUAL-RTX5070-O12G', 'value': 'gpu'},
        ], 'RTX 5070')

        self.assertEqual(matches[0]['index'], 1)
        self.assertTrue(is_confident_option_match(matches, 'RTX 5070'))

    def test_manufacturer_prefix_does_not_disable_model_pair_safety(self):
        matches = rank_option_matches([
            {
                'index': 1,
                'text': 'ASUS RTX chassis compatible with a 5070W power supply',
                'value': 'wrong',
            },
            {
                'index': 2,
                'text': 'ASUS DUAL RTX 5070 O12G graphics card',
                'value': 'right',
            },
        ], 'ASUS RTX 5070')

        self.assertEqual([match['index'] for match in matches], [2])

    def test_missing_numeric_query_token_is_not_a_full_fuzzy_match(self):
        matches = rank_option_matches([
            {'index': 1, 'text': 'NVIDIA RTX 4080 graphics card', 'value': 'gpu'},
        ], 'NVIDIA RTX 5090')

        self.assertEqual(matches, [])

    def test_model_prefix_and_number_must_appear_together(self):
        matches = rank_option_matches([
            {
                'index': 1,
                'text': 'Ryzen 7 7800X3D desktop with RX9060XT',
                'value': 'bundle',
            },
            {
                'index': 2,
                'text': 'Radeon RX 7800 XT graphics card',
                'value': 'gpu',
            },
        ], 'RX 7800')

        self.assertEqual([match['index'] for match in matches], [2])

    def test_missing_alpha_anchor_does_not_match_a_price_number(self):
        matches = rank_option_matches([
            {'index': 1, 'text': 'DDR5 memory, $7800', 'value': 'ram'},
        ], 'RX 7800')

        self.assertEqual(matches, [])

    def test_control_label_can_disambiguate_options_across_dropdowns(self):
        matches = rank_option_matches([
            {
                'index': 1,
                'text': 'RTX workstation product',
                'searchText': 'Desktop systems RTX workstation product',
                'value': 'desktop',
            },
            {
                'index': 2,
                'text': 'RTX gaming card',
                'searchText': 'GraphicsVGA RTX gaming card',
                'value': 'gpu',
            },
        ], 'Graphics card RTX')

        self.assertEqual(matches[0]['index'], 2)

    def test_numeric_tokens_do_not_match_inside_larger_numbers(self):
        matches = rank_option_matches([
            {'index': 1, 'text': 'DDR5-16000 MT/s', 'value': 'fast'},
        ], '6000')

        self.assertEqual(matches, [])
        self.assertFalse(is_confident_option_match(matches, '6000'))

    def test_exact_full_visible_text_is_confident_despite_similar_variants(self):
        options = [
            {'index': 1, 'text': 'AMD R7 9800X3D 代理盒裝', 'value': 'boxed'},
            {'index': 2, 'text': '搭板專案 AMD R7 9800X3D 代理盒裝', 'value': 'bundle'},
        ]
        matches = rank_option_matches(options, 'AMD R7 9800X3D 代理盒裝')

        self.assertEqual(matches[0]['index'], 1)
        self.assertTrue(is_confident_option_match(matches, 'AMD R7 9800X3D 代理盒裝'))


class SnapshotFormattingTests(unittest.TestCase):
    def test_labels_elements_with_their_iframe_context(self):
        output = format_snapshot([{
            'ref': 'e7',
            'tag': 'select',
            'text': 'AMD Ryzen 7 9800X3D',
            'frame': 'PC configurator',
        }])

        self.assertIn('frame="PC configurator"', output)

    def test_marks_selected_option_for_select_controls(self):
        output = format_snapshot([{
            'ref': 'e8',
            'tag': 'select',
            'text': 'Choose AMD Ryzen 7 9800X3D',
            'selected': 'AMD Ryzen 7 9800X3D',
        }])

        self.assertIn('selected="AMD Ryzen 7 9800X3D"', output)

    def test_describes_large_dropdown_without_dumping_its_option_corpus(self):
        output = format_snapshot([{
            'ref': 'e9',
            'tag': 'select',
            'text': 'Intel first option AMD Ryzen 7 9800X3D much later',
            'controlLabel': 'Processor / CPU',
            'optionCount': 48,
            'optionType': 'text',
            'selected': 'Choose a processor',
        }])

        self.assertIn('label="Processor / CPU"', output)
        self.assertIn('options="48 text"', output)
        self.assertIn('selected="Choose a processor"', output)
        self.assertNotIn('Intel first option', output)
        self.assertIn('find-option', output)

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
