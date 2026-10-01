"""Search-only planner metadata transport and controller-owned stopping."""
import json
from types import SimpleNamespace
import unittest
import research.jobs as jobs
from research.controller_contracts import (CrawlResult,Evidence,Judgment,Plan,ReviewDecision,SourceAction)
from research.contracts import Limits,SearchResult
import test_research_controller as fixtures
from test_research_controller import intent


class SearchOnlyWireTests(unittest.TestCase):
    def test_projection_is_allowlisted_before_json_serialization(self):
        self.assertTrue(callable(getattr(jobs,'planner_view',None)),'compact Python IPC projection missing')
        marker='ARTICLE_SENTINEL'
        view=SimpleNamespace(job_id='job',revision=3,question='clock',budget_remaining=2,
            searchable_gaps=('answer',),tasks=(),review_feedback=ReviewDecision(2,True,.9),
            evidence=(Evidence('e1','s1','page_extract',marker*100000,('answer',)),),
            sources=[{'descriptions':[marker]}],requirements=[{'evidence_ids':['secret']}])
        frame=jobs.planner_view(view)
        self.assertLess(len(json.dumps(frame)),1000)
        self.assertNotIn(marker,json.dumps(frame))
        self.assertTrue({'evidence','sources','requirements'}.isdisjoint(frame))
        self.assertEqual(frame['searchable_gaps'],['answer'])

    def test_wire_accepts_searches_only_and_rejects_old_completion_authority(self):
        plan=jobs.parse_plan({'revision':2,'searches':[]})
        self.assertEqual(plan.searches,())
        for extra in [{'finish':False},{'assessments':[]}]:
            with self.assertRaises(ValueError):jobs.parse_plan({'revision':2,'searches':[],**extra})


class SearchOnlyControllerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.ControllerTests.asyncSetUp
    controller=fixtures.ControllerTests.controller

    async def search(self,task):return [SearchResult('title','https://example.com/'+task.query,'snippet')]
    async def judge(self,req):return Judgment(req.view.revision,tuple(SourceAction(s.source_id,'crawl',('answer',)) for s in req.sources))
    async def crawl(self,req):return CrawlResult('complete page content')

    async def test_laya_stop_never_calls_planner_to_read_or_assess_evidence(self):
        calls=[]
        async def planner(view):calls.append(view);return Plan(view.revision,(intent('first'),))
        async def review(view):return ReviewDecision(view.revision,False,.1)
        result=await self.controller(self.search,planner,self.judge,self.crawl,review=review).run()
        self.assertEqual(len(calls),1,'planner called again to judge articles')
        self.assertEqual(result.snapshot.status,'collected')
        self.assertEqual(result.snapshot.reason,'laya_no_more_search')
        self.assertIn('complete page content',result.snapshot.render(max_bytes=10000))

    async def test_exhausted_budget_does_not_trigger_final_planner_assessment(self):
        calls=[]
        async def planner(view):calls.append(view);return Plan(view.revision,(intent('first'),))
        async def review(view):return ReviewDecision(view.revision,True,.9)
        result=await self.controller(self.search,planner,self.judge,self.crawl,review=review,limits=Limits(search_budget=1)).run()
        self.assertEqual(len(calls),1)
        self.assertEqual(result.snapshot.status,'incomplete')
        self.assertEqual(result.snapshot.reason,'budget_exhausted')

    async def test_false_without_complete_crawl_is_not_a_success(self):
        async def planner(view):return Plan(view.revision,(intent('first'),))
        async def review(view):return ReviewDecision(view.revision,False,.1)
        async def broken(req):return CrawlResult('',success=False)
        result=await self.controller(self.search,planner,self.judge,broken,review=review,limits=Limits(search_budget=1)).run()
        self.assertEqual(result.snapshot.status,'incomplete')
        self.assertNotEqual(result.snapshot.reason,'laya_no_more_search')
