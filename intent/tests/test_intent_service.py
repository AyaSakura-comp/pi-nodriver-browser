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

    def test_full_view_does_not_reclick_a_no_effect_target(self):
        # HayAegis 2026-10-20: the full view showed @e4 again after its click changed nothing,
        # and the planner clicked it a second time instead of being blocked.
        state = {"url": "https://hayaegis.test/login", "title": "HayAegis", "headings": [],
                 "page_excerpt": "", "fingerprint": "same", "elements": 1,
                 "_snap": '@e4 <button> "進入投資組合儀表板"'}
        click = {"action": "click", "ref": "@e4", "target": "進入投資組合儀表板"}
        acted = []

        def fake_act(sub, action, _p=None):
            acted.append(sub.target)
            return {"status": "CLICKED_UNCONFIRMED", "title": "HayAegis"}

        def fake_cmd(_session, command):
            if command == "vision-mark omni":
                raise ConnectionRefusedError(111, "Connection refused")
            return {"text": state["url"]}

        req = service.Act(session="s", action="task", goal="登入後進入投資組合儀表板", max_steps=15)
        with patch.object(service, "_page_state", return_value=state), \
             patch.object(service, "_goal_text", return_value=""), \
             patch.object(service, "_cmd", side_effect=fake_cmd), \
             patch.object(service, "next_step", return_value=({"action": "fail"}, 1.0)), \
             patch.object(service, "next_step_full", return_value=(click, 1.0)), \
             patch.object(service, "do_act", side_effect=fake_act):
            result = service._run_task(req)

        self.assertEqual(acted, ["進入投資組合儀表板"])
        self.assertIn("BLOCKED_REPEAT", [s.get("status") for s in result["steps"]])
        self.assertEqual(result["status"], "TASK_STOPPED")
        self.assertLess(len(result["steps"]), 6)

    LOGIN = {"url": "https://hayaegis.test/", "title": "HayAegis", "headings": [], "page_excerpt": "帳號 密碼",
             "fingerprint": "f", "elements": 3,
             "_snap": '@e2 <input> "輸入你的帳號" type="text"\n@e3 <input> "密碼" type="password"\n@e4 <button> "進入投資組合儀表板"'}

    def _fill(self, ref, value, goal):
        commands = []
        req = service.Act(session="s", action="fill", ref=ref, value=value, goal=goal)
        with patch.object(service, "_page_state", return_value=self.LOGIN), \
             patch.object(service, "_cmd", side_effect=lambda _s, c: commands.append(c) or {"ok": True}):
            return service.do_act(req, "fill"), commands

    def test_login_form_fill_with_invented_value_needs_input(self):
        for ref, value in (("@e2", "admin"), ("@e3", "password123")):
            result, commands = self._fill(ref, value, "在已登入的 HayAegis 進入交易帳本")
            self.assertEqual(result["status"], "NEEDS_INPUT")
            self.assertEqual(commands, [])

    def test_login_form_fill_with_value_from_goal_runs(self):
        result, commands = self._fill("@e2", "aya", "用帳號 aya 登入 HayAegis")
        self.assertTrue(result["status"].startswith("FILLED"))
        self.assertEqual(commands, ['fill @e2 "aya"'])

    def test_task_stops_with_needs_input_instead_of_guessing(self):
        fill = ({"action": "fill", "target": "input type=text: 輸入你的帳號", "value": "admin"}, 1.0)
        with patch.object(service, "_page_state", return_value=self.LOGIN), \
             patch.object(service, "_goal_text", return_value=""), \
             patch.object(service, "_cmd", return_value={"text": "https://hayaegis.test/"}), \
             patch.object(service, "next_step", return_value=fill):
            result = service._run_task(service.Act(session="s", action="task", goal="在已登入的 HayAegis 進入交易帳本"))
        self.assertEqual(result["status"], "TASK_NEEDS_INPUT")

    def test_full_view_prefers_ref_and_box_click_keeps_omni_preview(self):
        page = {**self.LOGIN, "_snap": '@e4 <button> "進入投資組合儀表板"\n@e5 <div>'}
        moved = {**page, "url": "https://hayaegis.test/dashboard", "fingerprint": "g"}
        commands, acted = [], []

        def fake_cmd(_s, c):
            commands.append(c)
            return {"elements": [{"id": 0, "center": [10, 20]}], "screenshotPath": "/tmp/x.jpg"} if c == "vision-mark omni" else {"text": page["url"]}

        steps = iter([({"action": "click", "ref": "@e4", "box": 0, "target": "登入"}, 1.0),
                      ({"action": "click", "ref": "", "box": 0, "target": "icon"}, 1.0),
                      ({"action": "done"}, 1.0)])
        with patch.object(service, "_page_state", return_value=page), \
             patch.object(service, "_settled_state", return_value=moved), \
             patch.object(service, "_goal_text", return_value=""), \
             patch.object(service, "_cmd", side_effect=fake_cmd), \
             patch.object(service, "next_step", return_value=({"action": "fail"}, 1.0)), \
             patch.object(service, "next_step_full", side_effect=lambda *a, **k: next(steps)), \
             patch.object(service, "do_act", side_effect=lambda sub, a, p=None: acted.append(sub.ref) or {"status": "FILLED"}), \
             patch.object(service, "do_read", return_value={"text": ""}), \
             patch.object(service, "verify_done", return_value=(True, "ok", 1.0)):
            service._run_task(service.Act(session="s", action="task", goal="g", url=None))
        self.assertEqual(acted, ["@e4"])
        i = commands.index("vision-click 10 20")
        self.assertEqual(commands[i - 1], "vision-mark omni")

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
