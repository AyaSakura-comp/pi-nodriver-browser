import tempfile
from pathlib import Path
import unittest
from research.contracts import SearchResult, Limits
from research.controller import ResearchController
from research.controller_contracts import Plan, SearchIntent, Judgment, SourceAction, CrawlResult


class CaptureMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_google_upstream_cap_and_pdf_temp_wiki_remain_explicit(self):
        class Provider:
            async def search(self, task):
                return [SearchResult('Snippet','https://example.com/a','description',truncated=True),
                        SearchResult('PDF','https://example.com/b','PDF')]
        async def planner(view):
            return Plan(view.revision, searches=(SearchIntent('query','official',('r1',)),)) if not view.tasks else Plan(view.revision)
        async def judge(req):
            return Judgment(req.view.revision,tuple(SourceAction(s.source_id,'use_snippet' if s.title=='Snippet' else 'crawl',('r1',)) for s in req.sources))
        async def crawl(req):
            return CrawlResult('PDF wiki notice',truncated=True,success=False,content_mode='temp-wiki',source_chars=50000)
        with tempfile.TemporaryDirectory() as root:
            result = await ResearchController(job_id='job',question='question',requirements=('r1',),
                artifact_root=Path(root),providers={'4get':Provider()},planner=planner,judge=judge,crawl=crawl,
                limits=Limits(search_budget=1)).run()
            packet = result.snapshot.render(max_bytes=50000)
            self.assertIn('truncated: True',packet)
            self.assertIn('content mode: temp-wiki; source chars: 50000',packet)
            self.assertIn('PDF wiki notice',packet)
            self.assertEqual(result.snapshot.status,'incomplete')
