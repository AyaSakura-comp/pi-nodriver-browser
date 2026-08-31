import json
import math
import os
import sys
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from browser_logic import OpenActionGuard, TabActivityRegistry, TabLimitError, VisionCorrectnessGuard, VisionFallbackContext, VisionFallbackGuard, VisionPageState, canonicalize_search_url, format_snapshot, is_confident_option_match, is_semantic_click_attempt, map_screenshot_point_to_viewport, normalize_open_url, parse_command, parse_devtools_active_port, parse_dismiss_options, parse_duration_ms, parse_google_search_payload, parse_long_press, parse_vision_click, parse_vision_mark, parse_vision_mark_drag, rank_option_matches, resolve_browser_executable, resolve_google_redirect_url, resolve_profile_dir, select_diverse_search_results, should_disable_sandbox


class GoogleSearchLogicTests(unittest.TestCase):
    def test_parses_up_to_four_directional_queries_and_removes_duplicates(self):
        payload = json.dumps([
            {'direction': '官方', 'query': 'Pixel 11 official specs'},
            {'direction': '新聞', 'query': 'Pixel 11 latest news'},
            {'direction': '重複', 'query': '  pixel 11 OFFICIAL specs  '},
            {'direction': '評價', 'query': 'Pixel 11 reviews'},
            {'direction': '替代', 'query': 'Pixel 11 alternatives'},
        ])

        self.assertEqual(parse_google_search_payload(payload), [
            {'direction': '官方', 'query': 'Pixel 11 official specs'},
            {'direction': '新聞', 'query': 'Pixel 11 latest news'},
            {'direction': '評價', 'query': 'Pixel 11 reviews'},
            {'direction': '替代', 'query': 'Pixel 11 alternatives'},
        ])

    def test_unwraps_google_redirect_and_removes_tracking_parameters(self):
        wrapped = 'https://www.google.com/url?q=https%3A%2F%2FExample.com%2Farticle%2F%3Futm_source%3Dgoogle%26id%3D7&sa=U'
        self.assertEqual(
            canonicalize_search_url(wrapped),
            'https://example.com/article?id=7',
        )

    def test_resolves_opaque_google_goto_without_following_destination(self):
        opaque = 'https://google.com/goto?url=CAESopaque'
        response = Mock()
        response.status = 302
        response.headers = {'Location': 'https://Example.com/story/?utm_source=google&id=9'}
        opener = Mock()
        opener.open.return_value = response

        self.assertEqual(
            resolve_google_redirect_url(opaque, opener=opener),
            'https://example.com/story?id=9',
        )
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertTrue(request.full_url.startswith('https://www.google.com/goto?'))

    def test_selects_round_robin_results_and_deduplicates_across_directions(self):
        groups = [
            {
                'direction': '官方',
                'query': 'official',
                'results': [
                    {'title': 'Official', 'url': 'https://example.com/product?utm_source=google', 'snippet': 'Primary'},
                    {'title': 'Docs', 'url': 'https://docs.example.com/product', 'snippet': 'Docs'},
                ],
            },
            {
                'direction': '新聞',
                'query': 'news',
                'results': [
                    {'title': 'Duplicate official', 'url': 'https://www.example.com/product/', 'snippet': 'Duplicate'},
                    {'title': 'News', 'url': 'https://news.example.com/story', 'snippet': 'Recent'},
                ],
            },
        ]

        selected = select_diverse_search_results(groups, limit=10)

        self.assertEqual([item['title'] for item in selected], ['Official', 'News', 'Docs'])
        self.assertEqual(selected[0]['directions'], ['官方', '新聞'])
        self.assertEqual(selected[1]['direction'], '新聞')


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

    def test_normalizes_legacy_angle_wrapped_refs_in_ref_positions(self):
        cases = {
            'click <@e16>': ['click', '@e16'],
            'fill <@e6> hkhs7821@gmail.com': ['fill', '@e6', 'hkhs7821@gmail.com'],
            'fill-submit <@e2> "cat treats"': ['fill-submit', '@e2', 'cat treats'],
            'get text <@e4>': ['get', 'text', '@e4'],
        }

        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(parse_command(command), expected)

    def test_does_not_rewrite_angle_wrapped_text_arguments(self):
        self.assertEqual(
            parse_command('click-text "<@e16>"'),
            ['click-text', '<@e16>'],
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


class VisionCommandParsingTests(unittest.TestCase):
    def test_parses_finite_non_negative_marker_coordinates(self):
        self.assertEqual(parse_vision_mark(['vision-mark', '120.5', '300']), (120.5, 300.0))

    def test_rejects_invalid_marker_coordinates(self):
        invalid = (
            ['vision-mark'],
            ['vision-mark', '-1', '20'],
            ['vision-mark', 'nan', '20'],
            ['vision-mark', '20', 'inf'],
            ['vision-mark', '20', '30', 'extra'],
        )
        for parts in invalid:
            with self.subTest(parts=parts), self.assertRaisesRegex(ValueError, 'vision-mark'):
                parse_vision_mark(parts)

    def test_parses_exact_generated_preview_token(self):
        token = '0123456789abcdef01234567'
        self.assertEqual(parse_vision_click(['vision-click', token]), token)

    def test_rejects_malformed_or_extra_preview_tokens(self):
        for parts in (
            ['vision-click'],
            ['vision-click', 'not-a-token'],
            ['vision-click', '0123456789abcdef01234567', 'extra'],
        ):
            with self.subTest(parts=parts), self.assertRaisesRegex(ValueError, 'vision-click'):
                parse_vision_click(parts)


class VisionFallbackGuardTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {'PI_NODRIVER_ALLOW_DIRECT_VISION': '0', 'PI_NODRIVER_VISION_ONLY': '0'})
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        self.guard = VisionFallbackGuard(threshold=3)
        self.page = VisionFallbackContext('tab-a', 'https://example.test/', 'loader-a')

    def test_stays_locked_until_three_semantic_click_failures(self):
        with self.assertRaisesRegex(ValueError, r'0/3'):
            self.guard.require_unlocked('session-a', self.page)

        self.assertEqual(self.guard.record_failure('session-a', self.page), (1, False))
        self.assertEqual(self.guard.record_failure('session-a', self.page), (2, False))
        self.assertEqual(self.guard.record_failure('session-a', self.page), (3, True))
        self.guard.require_unlocked('session-a', self.page)

    def test_different_page_does_not_inherit_unlock(self):
        for _ in range(3):
            self.guard.record_failure('session-a', self.page)
        other = VisionFallbackContext('tab-b', 'https://example.test/next', 'loader-b')

        with self.assertRaisesRegex(ValueError, r'0/3'):
            self.guard.require_unlocked('session-a', other)
        with self.assertRaisesRegex(ValueError, r'0/3'):
            self.guard.require_unlocked('session-a', self.page)

    def test_same_url_reload_does_not_inherit_unlock(self):
        for _ in range(3):
            self.guard.record_failure('session-a', self.page)
        reloaded = VisionFallbackContext('tab-a', self.page.url, 'loader-b')

        with self.assertRaisesRegex(ValueError, r'0/3'):
            self.guard.require_unlocked('session-a', reloaded)

    def test_success_reset_locks_fallback_again(self):
        for _ in range(3):
            self.guard.record_failure('session-a', self.page)
        self.guard.reset('session-a')

        with self.assertRaisesRegex(ValueError, r'0/3'):
            self.guard.require_unlocked('session-a', self.page)

    def test_observe_context_clears_failures_when_context_changes(self):
        for _ in range(3):
            self.guard.record_failure('session-a', self.page)
        other = VisionFallbackContext('tab-b', 'https://example.test/other', 'loader-b')
        self.guard.observe_context('session-a', other)

        with self.assertRaisesRegex(ValueError, r'0/3'):
            self.guard.require_unlocked('session-a', self.page)
        with self.assertRaisesRegex(ValueError, r'0/3'):
            self.guard.require_unlocked('session-a', other)

    def test_observe_context_keeps_failures_when_context_matches(self):
        self.guard.record_failure('session-a', self.page)
        self.guard.observe_context('session-a', self.page)
        self.assertEqual(self.guard.record_failure('session-a', self.page), (2, False))

    def test_threshold_is_fixed_at_three(self):
        for threshold in (0, 1, 2, 4, 11):
            with self.subTest(threshold=threshold), self.assertRaisesRegex(ValueError, 'fixed at 3'):
                VisionFallbackGuard(threshold=threshold)

    def test_only_well_formed_semantic_clicks_count_as_attempts(self):
        accepted = [
            ['click', '@e1'],
            ['click-js', '@e1'],
            ['click-text', 'Checkout'],
            ['click-css', '#checkout'],
        ]
        rejected = [
            ['click', '20', '30'],
            ['click'],
            ['click-js', 'button'],
            ['click-text'],
            ['click-css', ''],
            ['vision-click', '0123456789abcdef01234567'],
        ]

        for parts in accepted:
            with self.subTest(parts=parts):
                self.assertTrue(is_semantic_click_attempt(parts))
        for parts in rejected:
            with self.subTest(parts=parts):
                self.assertFalse(is_semantic_click_attempt(parts))


class VisionCoordinateMappingTests(unittest.TestCase):
    def test_maps_screenshot_pixels_through_scaled_visual_viewport(self):
        page = VisionPageState(
            'tab-a', 'https://example.test/', 390, 844,
            visual_width=195, visual_height=422, visual_scale=2,
        )

        self.assertEqual(
            map_screenshot_point_to_viewport(page, 390, 844, 300, 330),
            (150.0, 165.0),
        )

    def test_maps_nonstandard_webui_screenshot_size(self):
        page = VisionPageState(
            'tab-a', 'chrome://extensions/', 390, 844,
            visual_width=390, visual_height=844, visual_scale=1,
        )
        x, y = map_screenshot_point_to_viewport(page, 980, 2121, 490, 1060.5)

        self.assertAlmostEqual(x, 195)
        self.assertAlmostEqual(y, 422)

    def test_rejects_invalid_screenshot_or_visual_dimensions(self):
        page = VisionPageState(
            'tab-a', 'https://example.test/', 390, 844,
            visual_width=0, visual_height=844,
        )
        with self.assertRaisesRegex(ValueError, 'dimensions'):
            map_screenshot_point_to_viewport(page, 390, 844, 20, 30)


class VisionCorrectnessGuardTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {'PI_NODRIVER_ALLOW_DIRECT_VISION': '0', 'PI_NODRIVER_XVFB_FORWARD_CLICK': '0'})
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        self.now = 100.0
        self.guard = VisionCorrectnessGuard(ttl_seconds=30, clock=lambda: self.now)
        self.page = VisionPageState(
            target_id='tab-a',
            url='https://example.test/',
            width=390,
            height=844,
            loader_id='loader-a',
            scroll_x=0,
            scroll_y=0,
            visual_offset_x=0,
            visual_offset_y=0,
            visual_width=390,
            visual_height=844,
            visual_scale=1,
        )

    def test_requires_fresh_current_viewport_screenshot_before_marker(self):
        with self.assertRaisesRegex(ValueError, 'VISION_SCREENSHOT_REQUIRED'):
            self.guard.issue_marker(
                'session-a', self.page, 120, 300, '0123456789abcdef01234567', 'hash-a'
            )

    def test_rejects_marker_outside_screenshot_viewport(self):
        self.guard.record_screenshot('session-a', self.page)

        with self.assertRaisesRegex(ValueError, 'outside'):
            self.guard.issue_marker(
                'session-a', self.page, 390, 300, '0123456789abcdef01234567', 'hash-a'
            )

    def test_rejects_marker_after_page_or_viewport_changes(self):
        self.guard.record_screenshot('session-a', self.page)
        changed = VisionPageState('tab-a', 'https://example.test/next', 390, 844)

        with self.assertRaisesRegex(ValueError, 'fresh screenshot'):
            self.guard.issue_marker(
                'session-a', changed, 120, 300, '0123456789abcdef01234567', 'hash-a'
            )

    def test_replacement_marker_invalidates_previous_token(self):
        first = '0123456789abcdef01234567'
        second = '89abcdef0123456701234567'
        self.guard.record_screenshot('session-a', self.page)
        self.guard.issue_marker('session-a', self.page, 20, 30, first, 'hash-a')
        self.guard.issue_marker('session-a', self.page, 120, 300, second, 'hash-a')

        with self.assertRaisesRegex(ValueError, 'current marked preview'):
            self.guard.consume_marker('session-a', self.page, first, 'hash-a')
        marker = self.guard.consume_marker('session-a', self.page, second, 'hash-a')
        self.assertEqual((marker.x, marker.y), (120, 300))

    def test_confirmation_is_one_time_and_bound_to_page(self):
        token = '0123456789abcdef01234567'
        self.guard.record_screenshot('session-a', self.page)
        self.guard.issue_marker('session-a', self.page, 120, 300, token, 'hash-a')
        wrong_page = VisionPageState(
            **{**self.page.__dict__, 'target_id': 'tab-b'}
        )

        with self.assertRaisesRegex(ValueError, 'page changed'):
            self.guard.consume_marker('session-a', wrong_page, token, 'hash-a')
        with self.assertRaisesRegex(ValueError, 'current marked preview'):
            self.guard.consume_marker('session-a', self.page, token, 'hash-a')

    def test_expired_screenshot_and_marker_fail_closed(self):
        token = '0123456789abcdef01234567'
        self.guard.record_screenshot('session-a', self.page)
        self.now += 31
        with self.assertRaisesRegex(ValueError, 'fresh screenshot'):
            self.guard.issue_marker('session-a', self.page, 120, 300, token, 'hash-a')

        self.guard.record_screenshot('session-a', self.page)
        self.guard.issue_marker('session-a', self.page, 120, 300, token, 'hash-a')
        self.now += 31
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.guard.consume_marker('session-a', self.page, token, 'hash-a')

    def test_invalidate_clears_screenshot_and_marker(self):
        token = '0123456789abcdef01234567'
        self.guard.record_screenshot('session-a', self.page)
        self.guard.issue_marker('session-a', self.page, 120, 300, token, 'hash-a')
        self.guard.invalidate('session-a')

        with self.assertRaisesRegex(ValueError, 'VISION_SCREENSHOT_REQUIRED'):
            self.guard.issue_marker('session-a', self.page, 120, 300, token, 'hash-a')
        with self.assertRaisesRegex(ValueError, 'current marked preview'):
            self.guard.consume_marker('session-a', self.page, token, 'hash-a')

    def test_loader_identity_change_invalidates_same_url_preview(self):
        token = '0123456789abcdef01234567'
        self.guard.record_screenshot('session-a', self.page)
        self.guard.issue_marker('session-a', self.page, 120, 300, token, 'hash-a')
        reloaded = VisionPageState(**{**self.page.__dict__, 'loader_id': 'loader-b'})

        with self.assertRaisesRegex(ValueError, 'page changed'):
            self.guard.consume_marker('session-a', reloaded, token, 'hash-a')

    def test_rendered_content_change_invalidates_preview(self):
        token = '0123456789abcdef01234567'
        self.guard.record_screenshot('session-a', self.page)
        self.guard.issue_marker('session-a', self.page, 120, 300, token, 'hash-a')

        with self.assertRaisesRegex(ValueError, 'rendered content changed'):
            self.guard.consume_marker('session-a', self.page, token, 'hash-b')
        with self.assertRaisesRegex(ValueError, 'current marked preview'):
            self.guard.consume_marker('session-a', self.page, token, 'hash-a')

    def test_issues_and_consumes_drag_marker(self):
        token = '0123456789abcdef01234567'
        self.guard.record_screenshot('session-a', self.page)
        marker = self.guard.issue_drag_marker(
            'session-a', self.page, 50, 100, 200, 300, token, 'hash-a'
        )
        self.assertTrue(marker.is_drag)
        self.assertEqual(marker.x, 50)
        self.assertEqual(marker.y, 100)
        self.assertEqual(marker.end_x, 200)
        self.assertEqual(marker.end_y, 300)
        consumed = self.guard.consume_marker('session-a', self.page, token, 'hash-a')
        self.assertEqual(consumed, marker)

    def test_parse_vision_mark_drag(self):
        x1, y1, x2, y2 = parse_vision_mark_drag(['vision-mark-drag', '10.5', '20.5', '300.0', '400.0'])
        self.assertEqual((x1, y1, x2, y2), (10.5, 20.5, 300.0, 400.0))
        with self.assertRaisesRegex(ValueError, 'usage'):
            parse_vision_mark_drag(['vision-mark-drag', '10', '20'])
        with self.assertRaisesRegex(ValueError, 'numeric'):
            parse_vision_mark_drag(['vision-mark-drag', '10', '20', 'abc', '400'])

    def test_parse_long_press(self):
        ref, duration = parse_long_press(['long-press', '@e1'])
        self.assertEqual(ref, '@e1')
        self.assertEqual(duration, 1000)
        
        # Test unit formats
        ref, duration = parse_long_press(['long-press', '@e5', '1500'])
        self.assertEqual((ref, duration), ('@e5', 1500))
        ref, duration = parse_long_press(['long-press', '@e5', '1500ms'])
        self.assertEqual((ref, duration), ('@e5', 1500))
        ref, duration = parse_long_press(['long-press', '@e5', '2s'])
        self.assertEqual((ref, duration), ('@e5', 2000))
        ref, duration = parse_long_press(['long-press', '@e5', '1.5s'])
        self.assertEqual((ref, duration), ('@e5', 1500))
        ref, duration = parse_long_press(['long-press', '@e5', '2']) # <= 30 seconds auto-detection
        self.assertEqual((ref, duration), ('@e5', 2000))
        ref, duration = parse_long_press(['long-press', '@e5', '3.5'])
        self.assertEqual((ref, duration), ('@e5', 3500))

        # Test environment variables
        with patch.dict(os.environ, {'PI_NODRIVER_DEFAULT_LONG_PRESS_MS': '2.5s'}):
            self.assertEqual(parse_duration_ms(), 2500)
            ref, duration = parse_long_press(['long-press', '@e1'])
            self.assertEqual(duration, 2500)

        with patch.dict(os.environ, {'PI_NODRIVER_FORCE_LONG_PRESS_MS': '3s'}):
            self.assertEqual(parse_duration_ms('1s'), 3000)
            ref, duration = parse_long_press(['long-press', '@e1', '500ms'])
            self.assertEqual(duration, 3000)

        with self.assertRaisesRegex(ValueError, 'usage'):
            parse_long_press(['long-press'])
        with self.assertRaisesRegex(ValueError, 'usage'):
            parse_long_press(['long-press', 'invalid-ref'])
        with self.assertRaisesRegex(ValueError, 'invalid duration'):
            parse_long_press(['long-press', '@e1', '-500'])
        with self.assertRaisesRegex(ValueError, 'invalid duration'):
            parse_long_press(['long-press', '@e1', 'abc'])

    def test_rejects_non_finite_preview_ttl(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'ttl'):
                VisionCorrectnessGuard(ttl_seconds=value)


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


class OpenUrlNormalizationTests(unittest.TestCase):
    def test_rewrites_only_exact_alias_hosts(self):
        self.assertEqual(
            normalize_open_url('https://shop.pchome.tw/item?id=1'),
            'https://shop.pchome.com.tw/item?id=1',
        )
        self.assertEqual(
            normalize_open_url('https://www.momoshop.tw/product/1'),
            'https://www.momoshop.com.tw/product/1',
        )

    def test_does_not_rewrite_unrelated_hosts_paths_or_queries(self):
        urls = (
            'https://pchome.tw.evil.example/path',
            'https://example.test/pchome.tw/item',
            'https://example.test/?next=https://momoshop.tw/item',
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(normalize_open_url(url), url)

    def test_rewrites_only_the_exact_legacy_momo_login_route(self):
        self.assertEqual(
            normalize_open_url('https://www.momoshop.com.tw/mymomo/login.momo'),
            'https://account.momoshop.com.tw/mobile',
        )
        unrelated = 'https://example.test/momoshop.com.tw/mymomo/login.momo'
        self.assertEqual(normalize_open_url(unrelated), unrelated)


class OpenActionGuardTests(unittest.TestCase):
    def test_blocks_third_consecutive_open_to_the_same_origin(self):
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open', 'https://shop.example/a')
        guard.check('session-a', 'open', 'https://shop.example/b')

        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.check('session-a', 'open', 'https://shop.example/c')

    def test_blocked_same_origin_remains_blocked_until_recovery_action(self):
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open', 'https://shop.example/a')
        guard.check('session-a', 'open', 'https://shop.example/b')

        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.check('session-a', 'open', 'https://shop.example/c')
        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.check('session-a', 'open', 'https://shop.example/d')

    def test_success_and_failure_counts_combine_for_same_origin_limit(self):
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open', 'https://shop.example/success')
        guard.record_failure('session-a', 'https://shop.example/failure')

        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.pending_open('session-a', 'https://shop.example/third')

    def test_equivalent_idn_spellings_share_an_origin_streak(self):
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open', 'https://bücher.example/a')
        guard.check('session-a', 'open', 'https://xn--bcher-kva.example/b')

        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.check('session-a', 'open', 'https://BÜCHER.example:443/c')

    def test_equivalent_ipv4_spellings_share_an_origin_streak(self):
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open', 'http://127.1/a')
        guard.check('session-a', 'open', 'http://2130706433/b')

        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.check('session-a', 'open', 'http://127.0.0.1:80/c')

    def test_idna_deviation_character_shares_chromium_origin(self):
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open', 'https://faß.de/a')
        guard.check('session-a', 'open', 'https://xn--fa-hia.de/b')

        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.check('session-a', 'open', 'https://faß.de:443/c')

    def test_percent_encoded_ipv4_shares_chromium_origin(self):
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open', 'http://%31%32%37.0.0.1/a')
        guard.check('session-a', 'open', 'http://127.1/b')

        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.check('session-a', 'open', 'http://2130706433/c')

    def test_special_url_backslashes_share_chromium_origin(self):
        guard = OpenActionGuard(limit=3)
        guard.check('session-a', 'open', 'http://example.test/a')
        guard.check('session-a', 'open', r'http://example.test\b')
        guard.check('session-a', 'open', r'http:/\\example.test/c')

        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.check('session-a', 'open', 'http://example.test/d')

    def test_domain_trailing_dot_remains_a_distinct_chromium_origin(self):
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open', 'http://example.test/a')
        guard.check('session-a', 'open', 'http://example.test./b')
        guard.check('session-a', 'open', 'http://example.test/c')

    def test_fullwidth_legacy_ipv4_is_remapped_before_numeric_parsing(self):
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open', 'http://０x７f.１/a')
        guard.check('session-a', 'open', 'http://127.0.0.1/b')

        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.check('session-a', 'open', 'http://2130706433/c')

    def test_ascii_tab_preprocessing_matches_chromium_special_url_origin(self):
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open', 'http://example.test/a')
        guard.check('session-a', 'open', 'http:\t//example.test/b')

        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.check('session-a', 'open', 'http://example.test/c')

    def test_default_ports_match_chromium_for_all_special_origin_schemes(self):
        for scheme, port in (('ftp', 21), ('http', 80), ('https', 443), ('ws', 80), ('wss', 443)):
            with self.subTest(scheme=scheme):
                guard = OpenActionGuard(limit=2)
                guard.check('session-a', 'open', f'{scheme}://example.test/a')
                guard.check('session-a', 'open', f'{scheme}://example.test:{port}/b')
                with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
                    guard.check('session-a', 'open', f'{scheme}://example.test/c')

    def test_chromium_ignored_host_format_characters_do_not_split_origins(self):
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open', 'http://exa\u2061mple.test/a')
        guard.check('session-a', 'open', 'http://exa\u206ample.test/b')

        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.check('session-a', 'open', 'http://example.test/c')

    def test_unicode_symbol_host_shares_chromium_punycode_origin(self):
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open', 'http://☃.net/a')
        guard.check('session-a', 'open', 'http://xn--n3h.net/b')

        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.check('session-a', 'open', 'http://%E2%98%83.net/c')

    def test_ipv4_mapped_ipv6_shares_chromium_hex_origin(self):
        self.assertEqual(
            OpenActionGuard.origin('http://[::ffff:127.0.0.1]/a'),
            'http://[::ffff:7f00:1]',
        )
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open', 'http://[::ffff:127.0.0.1]/a')
        guard.check('session-a', 'open', 'http://[::ffff:7f00:1]/b')

        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.check('session-a', 'open', 'http://[0:0:0:0:0:ffff:7f00:1]/c')

    def test_ascii_host_disallowed_by_idna_but_allowed_by_chromium_is_stable(self):
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open', 'http://exa_mple.test/a')
        guard.check('session-a', 'open', 'http://exa_mple.test/b')

        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.check('session-a', 'open', 'http://exa_mple.test/c')

    def test_numeric_hosts_with_multiple_trailing_dots_remain_distinct_domains(self):
        self.assertEqual(OpenActionGuard.origin('http://1../a'), 'http://1..')
        self.assertEqual(OpenActionGuard.origin('http://1.../a'), 'http://1...')
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open', 'http://1../a')
        guard.check('session-a', 'open', 'http://1.../b')
        guard.check('session-a', 'open', 'http://1../c')

    def test_blob_urls_inherit_their_http_origin(self):
        self.assertEqual(
            OpenActionGuard.origin('blob:https://example.test/first-id'),
            'https://example.test',
        )
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open', 'blob:https://example.test/first-id')
        guard.check('session-a', 'open', 'https://example.test/page')
        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.check('session-a', 'open', 'blob:https://example.test/second-id')

    def test_opaque_url_schemes_share_the_null_origin(self):
        self.assertEqual(OpenActionGuard.origin('blob:null/first-id'), 'null')
        self.assertEqual(OpenActionGuard.origin('file:///tmp/example'), 'null')
        self.assertEqual(OpenActionGuard.origin('data:text/plain,example'), 'null')

    def test_known_open_url_aliases_share_an_origin_streak(self):
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open', 'https://pchome.tw/a')
        guard.check('session-a', 'open', 'https://pchome.com.tw/b')

        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.check('session-a', 'open', 'https://pchome.tw/c')

    def test_allows_consecutive_opens_to_distinct_origins(self):
        guard = OpenActionGuard(limit=2)

        for index in range(30):
            guard.check('session-a', 'open', f'https://shop-{index}.example/')

    def test_a_distinct_origin_recovers_after_a_same_origin_block(self):
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open', 'https://blocked.example/a')
        guard.check('session-a', 'open', 'https://blocked.example/b')
        with self.assertRaisesRegex(ValueError, 'same origin'):
            guard.check('session-a', 'open', 'https://blocked.example/c')

        guard.check('session-a', 'open', 'https://next.example/')

    def test_non_open_action_resets_same_origin_count(self):
        guard = OpenActionGuard(limit=2)
        guard.check('session-a', 'open', 'https://shop.example/a')
        guard.check('session-a', 'open', 'https://shop.example/b')
        guard.check('session-a', 'snapshot')
        guard.check('session-a', 'open', 'https://shop.example/c')
        guard.check('session-a', 'open', 'https://shop.example/d')

    def test_tracks_sessions_independently(self):
        guard = OpenActionGuard(limit=2)
        for session_id in ('session-a', 'session-b'):
            guard.check(session_id, 'open', 'https://shop.example/a')
            guard.check(session_id, 'open', 'https://shop.example/b')

        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.check('session-a', 'open', 'https://shop.example/c')
        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            guard.check('session-b', 'open', 'https://shop.example/c')


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

    def test_exposes_checkbox_proxy_state(self):
        output = format_snapshot([{
            'ref': 'e4',
            'tag': 'label',
            'text': 'Receive marketing email',
            'controlType': 'checkbox',
            'checked': True,
            'required': False,
        }])

        self.assertIn('control="checkbox"', output)
        self.assertIn('checked="true"', output)
        self.assertIn('required="false"', output)

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
