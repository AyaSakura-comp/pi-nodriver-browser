import json
import subprocess
import unittest


class ResearchCaptureTests(unittest.TestCase):
    def test_utf16_boundary_never_splits_surrogate_pair(self):
        from research.capture import research_capture_js
        for text,limit,expected in [('A😀B',2,'A'),('A😀B',3,'A😀'),('A😀',3,'A😀'),('é\n文',3,'é\n文')]:
            with self.subTest(text=text,limit=limit):
                script='globalThis.document={body:{innerText:'+json.dumps(text)+'}};console.log(JSON.stringify('+research_capture_js(limit)+'));'
                result=json.loads(subprocess.check_output(['node','-e',script],text=True,timeout=3))
                self.assertEqual(result['text'],expected)
                self.assertEqual(result['sourceChars'],len(text.encode('utf-16-le'))//2)
                self.assertEqual(result['capturedChars'],len(expected.encode('utf-16-le'))//2)
                self.assertEqual(result['truncated'],text!=expected)
                self.assertEqual(result['captureUnits'],'utf16')
                self.assertEqual(result['captureLimit'],limit)
