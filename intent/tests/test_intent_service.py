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


if __name__ == "__main__":
    unittest.main()
