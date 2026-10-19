import unittest
from unittest.mock import patch

import intent_service as service


class AnchorClickFallbackTests(unittest.TestCase):
    def setUp(self):
        service.PENDING.clear()

    def test_unconfirmed_anchor_activation_gets_one_click_js_fallback(self):
        before = {
            "url": "https://github.com/example/repo/pulls",
            "title": "Pull requests",
            "headings": [],
            "page_excerpt": "PR #35",
            "fingerprint": "same",
            "elements": 1,
            "_snap": '@e1 <a> "PR #35" href="https://github.com/example/repo/pull/35"',
        }
        after = {**before, "url": "https://github.com/example/repo/pull/35", "fingerprint": "changed"}
        commands = []

        def fake_cmd(_session, command):
            commands.append(command)
            return {"ok": True, "screenshotPath": "/tmp/shot.jpg"}

        req = service.Act(session="s", action="click", ref="@e1", target="PR #35")
        with patch.object(service, "_page_state", return_value=before), \
             patch.object(service, "_settled_state", side_effect=[before, after]), \
             patch.object(service, "_cmd", side_effect=fake_cmd):
            result = service.do_act(req, "click")

        self.assertEqual(commands, ["activate @e1", "click-js @e1"])
        self.assertEqual(result["status"], "CLICKED")
        self.assertEqual(result["decided_by"], "full-view+click-js-fallback")

    def test_non_anchor_noop_does_not_get_click_js_fallback(self):
        before = {
            "url": "https://example.test/form",
            "title": "Form",
            "headings": [],
            "page_excerpt": "Save",
            "fingerprint": "same",
            "elements": 1,
            "_snap": '@e1 <button> "Save"',
        }
        commands = []

        def fake_cmd(_session, command):
            commands.append(command)
            return {"ok": True}

        req = service.Act(session="s", action="click", ref="@e1", target="Save")
        with patch.object(service, "_page_state", return_value=before), \
             patch.object(service, "_settled_state", return_value=before), \
             patch.object(service, "_cmd", side_effect=fake_cmd):
            result = service.do_act(req, "click")

        self.assertEqual(commands, ["activate @e1"])
        self.assertEqual(result["status"], "CLICKED_UNCONFIRMED")

    def test_unconfirmed_anchor_click_and_js_gets_href_direct_nav_fallback(self):
        before = {
            "url": "https://www.cht.com.tw/",
            "title": "中華電信首頁",
            "headings": [],
            "page_excerpt": "Apple專區",
            "fingerprint": "same",
            "elements": 1,
            "_snap": '@e1 <a> "Apple專區" href="https://www.cht.com.tw/home/apple/iphone/index"',
        }
        after_nav = {**before, "url": "https://www.cht.com.tw/home/apple/iphone/index", "title": "Apple專區", "fingerprint": "changed"}
        commands = []

        def fake_cmd(_session, command):
            commands.append(command)
            return {"ok": True, "screenshotPath": "/tmp/shot.jpg", "targetHref": "https://www.cht.com.tw/home/apple/iphone/index"}

        req = service.Act(session="s", action="click", ref="@e1", target="Apple專區")
        with patch.object(service, "_page_state", return_value=before), \
             patch.object(service, "_settled_state", side_effect=[before, before, after_nav]), \
             patch.object(service, "_cmd", side_effect=fake_cmd):
            result = service.do_act(req, "click")

        self.assertEqual(commands, ["activate @e1", "click-js @e1", "open https://www.cht.com.tw/home/apple/iphone/index"])
        self.assertEqual(result["status"], "CLICKED")
        self.assertEqual(result["decided_by"], "full-view+href-direct-nav")

    def test_planner_elements_exclude_negative_feedback(self):
        snap = '@e1 <a> "Apple專區" href="/apple"\n@e2 <button> "搜尋"'
        # Without exclude: both are present
        all_cands = service._planner_elements("s", "找 Apple", snap)
        self.assertTrue(any("Apple專區" in c for c in all_cands))
        self.assertTrue(any("搜尋" in c for c in all_cands))

        # With exclude: Apple專區 is filtered out
        filtered = service._planner_elements("s", "找 Apple", snap, exclude={"Apple專區"})
        self.assertFalse(any("Apple專區" in c for c in filtered))
        self.assertTrue(any("搜尋" in c for c in filtered))

    def test_frame_aware_candidate_preservation(self):
        snap = (
            '@e12 <a> "登入"\n'
            '@e91 <input> "0928240312" label="帳號" frame="account.momoshop.com.tw" type="text"\n'
            '@e96 <a> "登入" frame="account.momoshop.com.tw"'
        )
        cands = service._planner_elements("s", "點擊登入按鈕", snap)
        login_cands = [c for c in cands if "登入" in c]
        self.assertEqual(len(login_cands), 2)
        self.assertTrue(any("frame: account.momoshop.com.tw" in c for c in login_cands))

    def test_active_tab_and_current_url_annotation(self):
        snap = (
            '@e1 <a> "續約購機" scope="nav" active-tab="true" current-url="true"\n'
            '@e2 <a> "iPhone 16 Pro" scope="main"'
        )
        cands = service._planner_elements("s", "續約購機", snap)
        self.assertTrue(any("目前已在此分頁 active/current tab" in c and "導航選單 nav" in c for c in cands if "續約購機" in c))
        self.assertTrue(any("iPhone 16 Pro" in c for c in cands))

    def test_active_tab_deprioritized_when_ranking(self):
        # Generate 35 candidates so limit (30) forces scoring
        lines = ['@e1 <a> "續約購機" active-tab="true" current-url="true"']
        for i in range(2, 36):
            lines.append(f'@e{i} <button> "選購商品 {i}"')
        snap = "\n".join(lines)
        ranked = service._planner_elements("s", "續約購機", snap, limit=30)
        # Verify active-tab item was deprioritized compared to other high scoring items
        self.assertEqual(len(ranked), 30)

    def test_planner_elements_excludes_container_divs_from_fill_candidates(self):
        snap = (
            '@e1 <div> "商品編號:DEBV7O-A900IOY01 商品描述很長... 加入購物車"\n'
            '@e2 <button> "加入購物車"\n'
            '@e3 <input> "數量" type="text"'
        )
        cands = service._planner_elements("s", "加入購物車", snap)
        self.assertFalse(any("商品編號" in c for c in cands))
        self.assertTrue(any("加入購物車" in c for c in cands))
        self.assertTrue(any("input" in c for c in cands))

    def test_nav_scope_not_penalized_when_matching_goal(self):
        snap = (
            '@e1 <button> "加入購物車" scope="nav"\n'
            '@e2 <a> "網站地圖" scope="nav"'
        )
        cands = service._planner_elements("s", "加入購物車", snap)
        self.assertTrue(any("加入購物車" in c for c in cands))

    def test_goal_excerpt_anchors_header_and_merges_spans(self):
        text = "\n".join([
            "PChome 24h購物",
            "購物車 1",
            "我的訂單",
            "商品編號:DEBV7O",
            "Royal Canin 皇家貓飼料",
            "商品已加入購物車！",
        ] + [f"無關商品介紹說明第 {i} 行" for i in range(30)])
        excerpt = service.goal_excerpt(text, "加入購物車")
        self.assertIn("購物車 1", excerpt)
        self.assertIn("商品已加入購物車！", excerpt)


if __name__ == "__main__":
    unittest.main()
