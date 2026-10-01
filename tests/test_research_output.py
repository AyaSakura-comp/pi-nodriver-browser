import tempfile
from pathlib import Path
import unittest
from research.evidence import EvidenceWriter


class ResearchOutputTests(unittest.IsolatedAsyncioTestCase):
    async def snapshot(self, root, text):
        async with EvidenceWriter(Path(root), 'job') as writer:
            await writer.append(dict(eventId='e1',sourceId='s1',topicIds=['t1'],kind='page_extract',
                url='https://example.com/',title='Fixture',text=text,status='completed',truncated=False))
            return await writer.freeze(status='incomplete',reason='fixture',gaps=['r1'])

    async def test_full_packet_unicode_and_lines_preserved_once(self):
        from research.delivery import deliver
        with tempfile.TemporaryDirectory() as root:
            text = '完整的證據\nSecond line'
            snapshot = await self.snapshot(root,text)
            result = deliver(snapshot)
            self.assertTrue(result['fullEvidenceDelivered'])
            self.assertEqual(result['text'].count(text),1)
            self.assertIn('https://example.com/',result['text'])

    async def test_byte_and_line_overflow_preserve_immutable_artifact(self):
        from research.delivery import deliver
        for text in ['資料'*20000, 'x\n'*2001]:
            with self.subTest(size=len(text)), tempfile.TemporaryDirectory() as root:
                snapshot = await self.snapshot(root,text)
                before = snapshot.path.read_bytes()
                result = deliver(snapshot)
                self.assertEqual(result['status'],'incomplete')
                self.assertEqual(result['reason'],'packet_too_large')
                self.assertFalse(result['fullEvidenceDelivered'])
                self.assertNotIn(text,result['text'])
                self.assertEqual(snapshot.path.read_bytes(),before)
                self.assertEqual(result['sha256'],snapshot.sha256)
