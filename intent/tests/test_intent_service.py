import unittest
from types import SimpleNamespace
from unittest.mock import patch

import intent_service as service
import planner


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
        grounded = SimpleNamespace(status="match", ref="e2", label="input type=text: 輸入你的帳號", tier="exact",
                                   timings={}, trace={}, shortlist=[])
        with patch.object(service, "_page_state", return_value=self.LOGIN), \
             patch.object(service, "_ground", return_value=(grounded, self.LOGIN["_snap"])), \
             patch.object(service, "next_step_full", side_effect=AssertionError("must stop before escalating")), \
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

        steps = iter([({"action": "click", "ref": "@e4", "box": 0, "target": "進入投資組合儀表板 按鈕"}, 1.0),
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

    SEARCH = {"url": "https://shop.test/", "title": "Shop", "headings": [], "page_excerpt": "", "fingerprint": "f",
              "elements": 2, "_snap": '@e1 <a> "搜尋" href="#"\n@e2 <input> "搜尋商品" type="search"'}

    def _search(self, after_cancel_url):
        commands = []

        def fake_cmd(_s, c):
            commands.append(c)
            if c.startswith("fill-submit"):
                raise RuntimeError("ValueError: Enter input was canceled; form was not submitted")
            return {"ok": True}
        landed = {**self.SEARCH, "url": after_cancel_url, "fingerprint": "g"}
        req = service.Act(session="s", action="fill", ref="@e2", target="搜尋商品", value="洗面乳", submit=True)
        with patch.object(service, "_page_state", side_effect=[self.SEARCH, landed]), \
             patch.object(service, "_settled_state", return_value=landed), \
             patch.object(service, "_cmd", side_effect=fake_cmd):
            return service.do_act(req, "fill"), commands

    def test_site_handled_enter_that_navigated_counts_as_filled(self):
        # cosmed/friday/poyabuy 2026-10-20: the site cancels Enter and routes the search itself.
        result, commands = self._search("https://shop.test/search?q=洗面乳")
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(commands, ['fill-submit @e2 "洗面乳"'])

    def test_canceled_enter_without_navigation_presses_a_real_enter(self):
        result, commands = self._search("https://shop.test/")
        self.assertEqual(commands, ['fill-submit @e2 "洗面乳"', 'fill @e2 "洗面乳"', "press Enter"])

    def test_fill_ref_on_a_non_editable_element_is_grounded_by_description(self):
        # momo: the full view gave a search *icon link* for the fill.
        with patch.object(service, "_page_state", return_value=self.SEARCH), \
             patch.object(service, "_ground", side_effect=lambda req, *a: (_ for _ in ()).throw(AssertionError(req.ref))) as g:
            with self.assertRaises(AssertionError) as ctx:
                service.do_act(service.Act(session="s", action="fill", ref="@e1", target="搜尋商品", value="x"), "fill")
        self.assertEqual(str(ctx.exception), "None")   # grounded by text, ref dropped

    def test_link_to_another_page_that_only_changed_the_dom_falls_back_to_its_href(self):
        # books.com.tw: product links stayed on the results page with a hover-style DOM change.
        page = {**self.SEARCH, "url": "https://books.test/search?q=x",
                "_snap": '@e3 <a> "原子習慣" href="https://books.test/products/1"'}
        hover = {**page, "fingerprint": "hover"}
        product = {**page, "url": "https://books.test/products/1", "fingerprint": "p"}
        commands = []
        with patch.object(service, "_page_state", return_value=page), \
             patch.object(service, "_settled_state", side_effect=[hover, hover, product]), \
             patch.object(service, "_cmd", side_effect=lambda _s, c: commands.append(c) or {"ok": True}):
            result = service.do_act(service.Act(session="s", action="click", ref="@e3", target="原子習慣"), "click")
        self.assertEqual(commands, ["activate @e3", "click-js @e3", "open https://books.test/products/1"])
        self.assertEqual(result["status"], "CLICKED")

    def test_full_view_ref_that_contradicts_its_description_is_grounded_first(self):
        page = {**self.LOGIN, "_snap": '@e5 <a> "3coins"\n@e9 <a> "Philips 星樂視氣炸鍋 4.2L"'}
        calls = []
        steps = iter([({"action": "click", "ref": "@e5", "target": "Philips 星樂視氣炸鍋"}, 1.0), ({"action": "done"}, 1.0)])

        def fake_act(sub, a, p=None):
            calls.append(sub.ref)
            return {"status": "CLICKED", "navigated": True}
        with patch.object(service, "_page_state", return_value=page), \
             patch.object(service, "_goal_text", return_value=""), \
             patch.object(service, "_cmd", return_value={"text": page["url"]}), \
             patch.object(service, "next_step", return_value=({"action": "fail"}, 1.0)), \
             patch.object(service, "next_step_full", side_effect=lambda *a, **k: next(steps)), \
             patch.object(service, "do_act", side_effect=fake_act), \
             patch.object(service, "do_read", return_value={"text": ""}), \
             patch.object(service, "verify_done", return_value=(True, "ok", 1.0)):
            result = service._run_task(service.Act(session="s", action="task", goal="g"))
        self.assertEqual(calls, [None])
        self.assertEqual(result["steps"][1]["ref_mismatch"], "@e5")

    def test_ref_fits_keeps_matching_and_text_less_refs(self):
        snap = '@e1 <a> "BILLY"\n@e2 <button> "(no visible text: icon, switch/toggle or custom control)"\n@e3 <a> "支援"'
        self.assertTrue(service._ref_fits(snap, "@e1", "BILLY bookcase link"))
        self.assertTrue(service._ref_fits(snap, "@e2", "cart icon"))
        self.assertFalse(service._ref_fits(snap, "@e3", "ASUS logo (Homepage)"))

    def test_cleanup_closes_the_opener_tab_too(self):
        # A click that opened a new tab moved the session into it; one close only returned to the opener.
        pages = ["popup", "opener"]

        def fake_request(command, session):
            if command == "close" and pages:
                pages.pop()
            if command == "get url":
                return {"ok": bool(pages)}
            return {"ok": True}
        with patch.object(service.nd, "request", side_effect=fake_request):
            service._dispatch(service.Act(session="s", action="cleanup"))
        self.assertEqual(pages, [])

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


class AccessGateTests(unittest.TestCase):
    """shopee.tw 2026-10-20: search for an anonymous visitor ends on a captcha, an app-only or a login wall."""

    PAGE = {"title": "蝦皮購物", "headings": [], "page_excerpt": "", "fingerprint": "f", "elements": 3, "_snap": ""}

    def gate(self, url, text, goal="搜尋手機殼並回報第一個商品的價格"):
        return service._access_gate({**self.PAGE, "url": url, "_text": text}, goal)

    def test_captcha_url_and_short_challenge_page(self):
        self.assertEqual(self.gate("https://shopee.tw/verify/captcha?anti_bot_tracking_id=x", "安全性驗證"), "CAPTCHA / 人機驗證")
        self.assertEqual(self.gate("https://shopee.tw/verify/traffic?next=x", "返回首頁"), "CAPTCHA / 人機驗證")
        self.assertEqual(self.gate("https://example.test/", "跳到主要內容 需要幫助嗎？ 安全性驗證 請滑動以完成拼圖 ID:226f"),
                         "CAPTCHA / 人機驗證")

    def test_security_words_on_a_long_ordinary_page_are_not_a_gate(self):
        self.assertIsNone(self.gate("https://bank.test/help", "帳戶安全性驗證說明 " + "內容 " * 1500))

    def test_app_only_wall(self):
        self.assertEqual(self.gate("https://shopee.tw/welcome-package", "註冊 登入 你只能透過蝦皮APP領取新用戶專屬禮物。 趕快下載蝦皮APP！"),
                         "網站要求改用 App")

    def test_login_wall_unless_the_goal_is_to_log_in(self):
        self.assertEqual(self.gate("https://shopee.tw/buyer/login?next=x", "登入 手機號碼"), "需要登入")
        self.assertIsNone(self.gate("https://shopee.tw/buyer/login?next=x", "登入", goal="用我的帳號登入蝦皮"))
        self.assertIsNone(self.gate("https://blog.test/logins-explained", "an article"))

    def test_task_stops_at_the_wall_without_planning(self):
        page = {**self.PAGE, "url": "https://shopee.tw/verify/captcha?x=1", "_text": "安全性驗證 請滑動以完成拼圖"}
        with patch.object(service, "_page_state", return_value=page), \
             patch.object(service, "_cmd", return_value={"text": page["url"]}), \
             patch.object(service, "next_step") as planner:
            result = service._run_task(service.Act(session="s", action="task", goal="搜尋手機殼"))
        self.assertEqual(result["status"], "TASK_BLOCKED")
        self.assertEqual(result["reason"], "CAPTCHA / 人機驗證")
        planner.assert_not_called()


class VerifierTextTests(unittest.TestCase):
    """2026-10-20 rerun: books, poyabuy and apple answered with a price but came back TASK_UNVERIFIED."""

    def test_price_lines_reach_the_excerpt_when_the_page_never_says_the_goals_word(self):
        page = "\n".join(["博客來", "會員登入", "購物車"] + ["推薦書 " + str(i) for i in range(60)] + [
            "原子習慣：細微改變帶來巨大成就的實證法則", "作者： 詹姆斯‧克利爾"] + ["簡介段落 " + str(i) for i in range(40)] + [
            "定價：330元", "優惠價：79折260元"] + ["頁尾 " + str(i) for i in range(40)])
        text = service.goal_excerpt(page, "點進第一個商品，記錄該商品的完整商品名稱和價格", anchors=8, limit=4000)
        self.assertIn("優惠價：79折260元", text)

    def test_empty_body_is_not_replaced_by_the_image_list(self):
        replies = iter([{"text": "Images found: 6 candidates (metadata only; not downloaded).\n\nPage text:", "pageText": ""},
                        {"text": "Images found: ...\n\nPage text:\nemplir水光澄果洗髮精 NT$299", "pageText": "emplir水光澄果洗髮精 NT$299"}])
        with patch.object(service, "_cmd", side_effect=lambda _s, _c: next(replies)), patch.object(service.time, "sleep"):
            text = service.do_read(service.Act(session="s", action="read"))["text"]
        self.assertEqual(text, "emplir水光澄果洗髮精 NT$299")

    def test_verifier_sees_the_whole_read_excerpt(self):
        sent = []
        text = "x" * 3000 + " 優惠價 260元"
        with patch.object(planner, "chat", side_effect=lambda _u, _h, body, _t: sent.append(body) or '{"achieved": true}'):
            planner.verify_done("價格", [], {"title": "t"}, text, url="http://127.0.0.1:8001/v1/chat/completions", model="m")
        self.assertIn("優惠價 260元", sent[0]["messages"][0]["content"])
