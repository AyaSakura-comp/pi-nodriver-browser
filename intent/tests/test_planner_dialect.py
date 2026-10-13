import io
import json
import unittest
import urllib.error
from unittest.mock import patch

import intent_service as service
import judge
import planner

STATE = {"title": "Pricing", "url": "https://example.test/pricing", "page_excerpt": "Opus", "_snap": "",
         "headings": [], "fingerprint": "f", "elements": 0}


class Reply:
    def __init__(self, content):
        self.body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()

    def __enter__(self):
        return io.BytesIO(self.body)

    def __exit__(self, *exc):
        return False


class ChatBodyTests(unittest.TestCase):
    def sent(self, url):
        bodies = []

        def fake_urlopen(req, timeout):
            bodies.append(json.loads(req.data))
            return Reply('{"action":"done","reason":"shown"}')

        with patch.object(judge.urllib.request, "urlopen", side_effect=fake_urlopen), \
             patch.dict("os.environ", {}, clear=False) as env:
            env.pop("JUDGE_URL", None)
            env.pop("JUDGE_MODEL", None)
            step, _ = planner.next_step("goal", [], STATE, ["Pricing"], url=url, model="m")
        self.assertEqual(step["action"], "done")
        return bodies[0]

    def test_hosted_endpoint_gets_no_llama_cpp_sampling_fields(self):
        # Claude Opus 5.5 answers HTTP 400 "`temperature` is deprecated for this model".
        body = self.sent("https://api.anthropic.com/v1/chat/completions")
        self.assertNotIn("temperature", body)
        self.assertNotIn("chat_template_kwargs", body)

    def test_local_llama_server_keeps_greedy_no_thinking(self):
        body = self.sent("http://127.0.0.1:8001/v1/chat/completions")
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})

    def test_http_error_carries_the_provider_reason(self):
        err = urllib.error.HTTPError("https://api.anthropic.com/v1/chat/completions", 400, "Bad Request", {},
                                     io.BytesIO(b'{"error":{"message":"`temperature` is deprecated for this model."}}'))
        with patch.object(judge.urllib.request, "urlopen", side_effect=err):
            with self.assertRaisesRegex(RuntimeError, r"HTTP 400 from api\.anthropic\.com: .*temperature"):
                judge.chat("https://api.anthropic.com/v1/chat/completions", {}, {}, 5)

    def test_timeout_names_the_busy_planner(self):
        with patch.object(judge.urllib.request, "urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(RuntimeError, r"planner model at 127\.0\.0\.1:8001 did not answer within 30s"):
                judge.chat("http://127.0.0.1:8001/v1/chat/completions", {}, {}, 30)


class TaskAutopilotTests(unittest.TestCase):
    def test_planner_scroll_runs_and_task_returns_final_screenshot(self):
        steps = iter([({"action": "scroll", "value": "down", "reason": "price below"}, 1.0),
                      ({"action": "done", "reason": "price shown"}, 1.0)])
        commands = []

        def fake_cmd(_session, command):
            commands.append(command)
            return {"text": "https://example.test/pricing", "screenshotPath": "/tmp/end.jpg"}

        scrolled = {"status": "SCROLLED", "element": "向下捲動 600px", "page_excerpt": "Opus $4"}
        req = service.Act(session="s", action="task", goal="Opus price")
        with patch.object(service, "_cmd", side_effect=fake_cmd), \
             patch.object(service, "_page_state", return_value=STATE), \
             patch.object(service, "_planner_elements", return_value=[]), \
             patch.object(service, "next_step", side_effect=lambda *a, **k: next(steps)), \
             patch.object(service, "do_scroll", return_value=scrolled) as do_scroll, \
             patch.object(service, "do_read", return_value={"text": "Opus $4"}), \
             patch.object(service, "verify_done", return_value=(True, "Opus $4", 1.0)):
            res = service.do_task(req)

        self.assertEqual(do_scroll.call_args.args[0].value, "down")
        self.assertEqual(res["status"], "TASK_DONE")
        self.assertEqual([s["plan"]["action"] for s in res["steps"]], ["scroll", "done"])
        self.assertEqual(res["screenshot"], "/tmp/end.jpg")
        self.assertEqual(commands[-1], "screenshot")
        # Only the verifier's evidence reaches the agent; the planner's own reading can be wrong.
        self.assertNotIn("reason", res)
        self.assertNotIn("reason", res["steps"][-1]["plan"])
        self.assertEqual(res["evidence"], "Opus $4")

    def test_no_page_and_no_url_fails_without_screenshot(self):
        with patch.object(service, "_cmd", return_value={"text": ""}) as cmd:
            res = service.do_task(service.Act(session="s", action="task", goal="find the price"))
        self.assertEqual(res["status"], "TASK_FAILED")
        self.assertNotIn("screenshot", res)
        self.assertEqual([c.args[1] for c in cmd.call_args_list], ["get url"])


class AbsoluteScrollTests(unittest.TestCase):
    """The worker refuses relative scrolling ('scroll down 600'); the service must send absolute targets."""

    def run_scrolls(self, values, max_y=5000):
        commands = []

        def fake_cmd(_session, command):
            commands.append(command)
            return {"ok": True, "scrollY": 3, "maxY": max_y}    # sampled mid smooth-scroll

        service.SCROLL_Y.clear()
        with patch.object(service, "_cmd", side_effect=fake_cmd), \
             patch.object(service, "_page_state", return_value=STATE), \
             patch.object(service, "_settled_state", return_value=STATE):
            for value in values:
                self.assertEqual(service.do_scroll(service.Act(session="s", action="scroll", value=value))["status"], "SCROLLED")
        return commands

    def test_down_and_up_move_from_the_last_requested_position(self):
        self.assertEqual(self.run_scrolls(["down", "down 900", "up 300"]),
                         ["scroll to 600", "scroll to 1500", "scroll to 1200"])

    def test_position_is_clamped_to_page_height(self):
        self.assertEqual(self.run_scrolls(["down 900", "up 300"], max_y=700), ["scroll to 900", "scroll to 400"])

    def test_words_containing_up_or_top_are_not_directions(self):
        commands = self.run_scrolls(["supported models down", "laptop down"])
        self.assertEqual(commands, ["scroll to 600", "scroll to 1200"])
        self.assertFalse(any("down" in c or "up" in c for c in commands))


class GoalExcerptTests(unittest.TestCase):
    # Shape of claude.com/pricing (API tab): every table cell is its own text line.
    PAGE = "\n".join(["Plans & pricing", "Team & Enterprise", "API"] + ["Opt-out"] * 20 + [
        "Fable 5.1", "Next generation intelligence", "Prompt caching", "Read", "$0.25 / MTok", "Write",
        "$12.50 / MTok", "Input", "$10 / MTok", "Output", "$50 / MTok",
        "Opus 5.5", "Daily driver for agentic coding", "Prompt caching", "Read", "$0.20 / MTok", "Write",
        "$5 / MTok", "Input", "$4 / MTok", "Output", "$20 / MTok",
        "Sonnet 5.5", "High-performance model", "Prompt caching", "Read", "$0.10 / MTok", "Write",
        "$2.50 / MTok", "Input", "$2 / MTok", "Output", "$10 / MTok"] + ["FAQ"] * 30)

    def test_keeps_the_matching_table_row_together(self):
        text = service.goal_excerpt(self.PAGE, "Find the API input and output price per million tokens of Claude Opus 5.5.")
        self.assertIn("Opus 5.5 | Daily driver for agentic coding | Prompt caching | Read | $0.20 / MTok | Write | "
                      "$5 / MTok | Input | $4 / MTok | Output | $20 / MTok", text)

    def test_chinese_goal_matches_by_bigrams(self):
        page = "\n".join(["首頁"] + ["雜項"] * 30 + ["價格", "NT$ 1,990", "運送", "免運"] + ["雜項"] * 30)
        self.assertIn("價格 | NT$ 1,990 | 運送 | 免運", service.goal_excerpt(page, "找出這個商品的價格和運費"))

    def test_read_with_target_returns_whole_rows(self):
        with patch.object(service, "_cmd", return_value={"pageText": self.PAGE}):
            text = service.do_read(service.Act(session="s", action="read", target="Opus 5.5 input output price"))["text"]
        self.assertIn("Opus 5.5\nDaily driver for agentic coding\nPrompt caching\nRead\n$0.20 / MTok\nWrite\n"
                      "$5 / MTok\nInput\n$4 / MTok\nOutput\n$20 / MTok", text)

    def test_no_matching_words_gives_empty_excerpt(self):
        self.assertEqual(service.goal_excerpt("alpha\nbeta", "Opus price"), "")

    def test_omniparser_down_still_reaches_full_view_planner(self):
        def fake_cmd(_session, command):
            if command == "vision-mark omni":
                raise RuntimeError("ConnectionRefusedError: [Errno 111] Connection refused")
            return {"text": "https://example.test/", "pageText": "", "screenshotPath": "/tmp/end.jpg"}

        req = service.Act(session="s", action="task", goal="Opus price", max_steps=3)
        with patch.object(service, "_cmd", side_effect=fake_cmd), \
             patch.object(service, "_page_state", return_value=STATE), \
             patch.object(service, "_planner_elements", return_value=[]), \
             patch.object(service, "next_step", return_value=({"action": "fail", "reason": "not visible"}, 1.0)), \
             patch.object(service, "next_step_full", return_value=({"action": "fail", "reason": "absent"}, 1.0)) as full, \
             patch.object(service, "do_read", return_value={"text": ""}):
            res = service.do_task(req)
        self.assertEqual(res["status"], "TASK_FAILED")
        self.assertEqual([s["view"] for s in res["steps"]], ["compact", "full"])
        self.assertEqual(res["reason"], "absent", "a failure keeps the planner's explanation")
        self.assertEqual(full.call_args.args[5], [], "no boxes without OmniParser")


if __name__ == "__main__":
    unittest.main()
