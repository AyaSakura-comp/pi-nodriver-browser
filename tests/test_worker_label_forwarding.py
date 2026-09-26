import unittest
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER_PY = ROOT / 'worker.py'


class WorkerLabelForwardingTests(unittest.TestCase):
    def setUp(self):
        self.code = WORKER_PY.read_text()

    def test_ref_action_js_contains_label_and_container_forwarding(self):
        self.assertIn("target.tagName === 'LABEL'", self.code)
        self.assertIn("element.tagName === 'LABEL'", self.code)
        self.assertIn("targetControl = associated", self.code)
        self.assertIn("targetControl = byId", self.code)
        self.assertIn("targetControl = adjacent", self.code)
        self.assertIn("targetControl = nested", self.code)

    def test_set_text_contains_label_forwarding(self):
        # Verify that setText itself also resolves label target before checking isControlEditable
        self.assertIn("isControlEditable(target)", self.code)
        self.assertIn("target = associated", self.code)


if __name__ == '__main__':
    unittest.main()
