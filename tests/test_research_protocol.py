"""Transport wiring contracts; functional jobs/process behavior lives in named suites."""
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]


class ResearchProtocolTests(unittest.TestCase):
    def test_streams_only_terminal_frames_settle_and_no_replay(self):
        source=(ROOT/'index.ts').read_text()
        self.assertIn('response.type === "planner_request"',source)
        self.assertIn('request.onFrame(response)',source)
        self.assertIn('async research(',source)
        method=source.split('async research(',1)[1].split('async cleanupSession',1)[0]
        self.assertIn('this.sendRequest(',method)
        self.assertNotIn('this.request(',method)
        self.assertIn('name: "research"',source)
        tool=source.split('name: "research"',1)[1].split('pi.registerTool',1)[0]
        self.assertNotIn('queue.then',tool)
        self.assertNotIn('truncateHead',tool)
        self.assertIn('ResearchPlanner',tool)
        self.assertIn('fullEvidenceDelivered',tool)

    def test_daemon_dispatch_is_before_interactive_lock_and_replies_bypass_it(self):
        source=(ROOT/'worker.py').read_text().split('async def server_main',1)[1]
        self.assertIn('ResearchConnection(',source)
        self.assertIn("request.get('type') == 'planner_reply'",source)
        dispatch=source.split('async def process_request',1)[1]
        self.assertLess(dispatch.index("if action == 'research':"),dispatch.index('async with session_lock:'))
        self.assertIn('await research.close()',source)
        # Existing ordinary interactive serialization must not be removed.
        self.assertIn('async with session_lock:',source)
        self.assertIn('async with browser_structure_lock:',source)
