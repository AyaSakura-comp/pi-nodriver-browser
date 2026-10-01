"""Binary Laya gates and the bounded post-crawl feedback loop, entirely offline."""
import asyncio
import importlib.util
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace

import test_research_controller as fixtures
from test_research_controller import intent
import test_research_decisions as decision_fixtures
from research.contracts import Limits, SearchResult
from research.controller_contracts import Assessment, CrawlResult, Judgment, Plan, SourceAction


class BinaryProtocolTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = decision_fixtures.LayaTests.asyncSetUp
    asyncTearDown = decision_fixtures.LayaTests.asyncTearDown
    client = decision_fixtures.LayaTests.client

    async def test_noul_wire_and_boolean_no_action_choice(self):
        c = self.client()
        self.assertTrue(callable(getattr(c, 'decide', None)), 'binary Laya protocol is missing')
        self.handler = lambda body: {'answers': {'should_crawl': {'type':'noul', 'noul':0.8}}}
        d = await c.decide('topic + source', 'should_crawl', 'Is this source related to this topic?')
        self.assertIs(d.value, True)
        self.assertEqual(d.probability_true, .8)
        q = self.requests[0]['questions']['should_crawl']
        self.assertEqual(q['type'], 'noul')
        self.assertNotIn('criteria', q)
        self.handler = lambda body: {'answers': {'should_crawl': {'type':'noul', 'noul':0.2}}}
        self.assertIs((await c.decide('s', 'should_crawl', 'q')).value, False)

    async def test_binary_response_validation_and_oversize(self):
        from research.decisions import DecisionError
        c = self.client()
        self.assertTrue(callable(getattr(c, 'decide', None)), 'binary Laya protocol is missing')
        for p in [True, -1, 2, float('nan'), 10**400, '0.9', None]:
            self.handler = lambda body, p=p: {'answers': {'needs_more_search': {'type':'noul','noul':p}}}
            with self.subTest(p=p), self.assertRaises(DecisionError):
                await c.decide('state', 'needs_more_search', 'More?')
        before=len(self.requests)
        with self.assertRaises(ValueError):
            await self.client(max_state_chars=10).decide('x'*11, 'needs_more_search', 'More?')
        self.assertEqual(len(self.requests),before)

    async def test_topic_guide_and_url_are_supplied_and_only_crawl_or_ignore_returned(self):
        self.assertIsNotNone(importlib.util.find_spec('research.workflow'), 'binary workflow binding is missing')
        from research.workflow import LayaWorkflow
        from research.controller_contracts import JudgeRequest, ResearchView, SourceView
        c=self.client();self.handler=lambda body:{'answers':{'should_crawl':{'type':'noul','noul':.9}}}
        task=intent('clock sleep',parent=None)
        view=ResearchView('job','Compare three clocks',0,(),('answer',),(),('answer',),(),(),(task,),3)
        source=SourceView('source','https://example.com/exact','Clocks',('t1',),'discovered',('sleep included',))
        judgment=await LayaWorkflow(c).judge(JudgeRequest(view,task,(source,)))
        state=json.loads(self.requests[0]['state'])
        self.assertEqual(state['topic'],task.direction)
        self.assertEqual(state['query'],task.query)
        self.assertEqual(state['url'],source.url)
        self.assertEqual(judgment.actions[0].action,'crawl')
        self.assertFalse(judgment.assessments)

    async def test_connection_uses_binary_round_loop_and_exports_feedback(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import AsyncMock, patch
        from research.jobs import ResearchConnection
        c=self.client();review_count=0
        def answer(body):
            nonlocal review_count
            name=next(iter(body['questions']))
            self.assertIn(name,('should_crawl','needs_more_search'))
            if name=='needs_more_search':review_count+=1
            return {'answers':{name:{'type':'noul','noul':.9 if name=='should_crawl' or review_count==1 else .1}}}
        self.handler=answer
        class Provider:
            async def search(self,task):return [SearchResult('title','https://example.com/'+task.query,'snippet')]
            async def close(self):pass
        async def extract(url,*args,**kwargs):return dict(ok=True,text='full '+url,contentMode='full')
        engine=SimpleNamespace(ensure_browser=AsyncMock(),crawl_one=extract,background_slots=asyncio.Semaphore(2),cleanup_research_owner=AsyncMock())
        conn=None;seen=[]
        async def send(frame):
            view=frame['view'];seen.append(view)
            if not view['tasks']:searches=[dict(query='first',direction='clock elapsed time',provider='4get',addresses=['answer'],parent_task_id=None)]
            elif len(view['tasks'])==1:searches=[dict(query='different',direction='clock sleep exception',provider='4get',addresses=['answer'],parent_task_id=view['tasks'][0]['task_id'])]
            else:searches=[]
            self.assertNotIn('evidence',view)
            self.assertNotIn('sources',view)
            proposal=dict(revision=view['revision'],searches=searches)
            conn.reply(dict(type='planner_reply',id=frame['id'],jobId=frame['jobId'],requestId=frame['requestId'],proposal=proposal))
        with tempfile.TemporaryDirectory() as root, patch('research.jobs.LayaClient',return_value=c):
            from research.workflow import LayaWorkflow
            legacy=LayaWorkflow(c)
            conn=ResearchConnection(engine,send,Path(root),fourget_factory=Provider,judge=legacy.judge,review=legacy.review)
            result=await asyncio.wait_for(conn.run(1,'owner',dict(jobId='job',question='Compare clocks',provider='4get',searchBudget=3,searchConcurrency=2,layaConcurrency=2,clock={'iso':'2026-09-28T00:00:00Z','timezone':'UTC'})),5)
        self.assertEqual(review_count,2,'missing post-crawl binary review in production connection')
        self.assertEqual(result['budgetUsed'],2)
        self.assertEqual([d['needs_more_search'] for d in result['reviewHistory']],[True,False])
        self.assertEqual(len(seen),2,'no final evidence-assessment planner call')
        self.assertEqual([t['query'] for t in seen[-1]['tasks']],['first'])
        self.assertIn('full https://example.com/different',result['text'])
        self.assertTrue(result['fullEvidenceDelivered'])

    async def test_post_crawl_review_receives_full_evidence_and_search_history(self):
        self.assertIsNotNone(importlib.util.find_spec('research.workflow'), 'binary workflow binding is missing')
        from research.workflow import LayaWorkflow
        from research.controller_contracts import ResearchView, SourceView, Evidence
        c=self.client();self.handler=lambda body:{'answers':{'needs_more_search':{'type':'noul','noul':.1}}}
        task=intent('clock sleep');source=SourceView('s1','https://example.com/exact','Clocks',('t1',),'completed',('snippet',))
        ev=Evidence('e1','s1','page_extract','FULL\n正文 tail',('answer',))
        view=ResearchView('job','Compare clocks',3,(),('answer',),(),('answer',),(ev,),(source,),(task,),2)
        decision=await LayaWorkflow(c).review(view)
        self.assertFalse(decision.needs_more_search)
        self.assertEqual(decision.revision,3)
        state=json.loads(self.requests[0]['state'])
        self.assertEqual(state['pages'][0]['text'],ev.text)
        self.assertEqual(state['search_history'][0]['direction'],task.direction)
        self.assertEqual(state['search_history'][0]['query'],task.query)
        self.assertEqual(self.requests[0]['questions']['needs_more_search']['type'],'noul')
        # A partial extraction is not enough to skip further searching.
        partial=replace(view,evidence=(replace(ev,truncated=True,addresses=()),))
        d=await LayaWorkflow(c).review(partial)
        self.assertTrue(d.needs_more_search)
        self.assertEqual(len(self.requests),1)


class BinaryLoopTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ControllerTests.asyncSetUp
    controller = fixtures.ControllerTests.controller

    def decision(self,view,more):
        import research.controller_contracts as contracts
        self.assertTrue(hasattr(contracts,'ReviewDecision'),'post-crawl review contract missing')
        return contracts.ReviewDecision(view.revision,more, .9 if more else .1)

    async def search(self,task):
        return [SearchResult(task.query,'https://example.com/'+task.query,'raw observed snippet')]

    async def judge(self,req):
        return Judgment(req.view.revision,tuple(SourceAction(s.source_id,'crawl',('answer',)) for s in req.sources))

    def finish(self,view):
        ids=tuple(e.event_id for e in view.evidence if e.kind=='page_extract' and e.addresses)
        return Plan(view.revision,assessments=(Assessment('answer',ids),),finish=True)

    async def test_more_true_reopens_gap_despite_citations_and_remembers_prior_directions(self):
        self.decision(SimpleNamespace(revision=0),True)
        seen=[];reviews=[]
        async def crawl(req):return CrawlResult('complete '+req.task.query)
        async def review(view):
            reviews.append(view)
            return self.decision(view,len(view.tasks)==1)
        async def planner(view):
            seen.append(view)
            if not view.tasks:return Plan(view.revision,(intent('initial'),))
            if len(view.tasks)==1:
                self.assertIs(view.review_feedback.needs_more_search,True)
                self.assertIn('answer',view.searchable_gaps)
                return Plan(view.revision,(intent('different',parent=view.tasks[0].task_id),),self.finish(view).assessments,True)
            self.fail('Laya no-more must not trigger another planner assessment')
        result=await self.controller(self.search,planner,self.judge,crawl,review=review).run()
        self.assertEqual(result.snapshot.status,'collected')
        self.assertEqual(result.budget_used,2)
        self.assertEqual(len(reviews),2)
        self.assertEqual(len(seen),2)
        self.assertEqual([t.query for t in reviews[-1].tasks],['initial','different'])
        self.assertEqual([d.needs_more_search for d in result.review_history],[True,False])
        snippets=[e for e in result.evidence if e.kind=='search_snippet']
        self.assertEqual(len(snippets),2)
        self.assertTrue(all(not e.addresses for e in snippets),'raw unjudged snippets must not grant coverage')

    async def test_review_waits_for_every_parallel_crawl_before_final_planner(self):
        self.decision(SimpleNamespace(revision=0),True)
        both=asyncio.Event();release=asyncio.Event();started=[];reviews=[]
        async def crawl(req):
            started.append(req.task.query)
            if len(started)==2:both.set()
            await release.wait()
            return CrawlResult('full '+req.task.query)
        async def review(view):
            self.assertTrue(release.is_set());self.assertFalse(view.pending_addresses)
            self.assertEqual(len([e for e in view.evidence if e.kind=='page_extract']),2)
            reviews.append(view);return self.decision(view,False)
        async def planner(view):
            if not view.tasks:return Plan(view.revision,(intent('one'),intent('two')))
            self.assertEqual(len(reviews),1)
            return self.finish(view)
        running=asyncio.create_task(self.controller(self.search,planner,self.judge,crawl,review=review).run())
        await asyncio.wait_for(both.wait(),1)
        self.assertEqual(reviews,[]);release.set()
        result=await asyncio.wait_for(running,2)
        self.assertEqual(result.snapshot.status,'collected')
        self.assertEqual(len(reviews),1)

    async def test_review_error_and_true_cannot_be_overridden_by_planner_finish(self):
        self.decision(SimpleNamespace(revision=0),True)
        async def crawl(req):return CrawlResult('full')
        async def planner(view):
            if not view.tasks:return Plan(view.revision,(intent('one'),))
            return self.finish(view)
        for more in ['error',True]:
            async def review(view):
                if more=='error':raise ValueError('input too large')
                return self.decision(view,True)
            with self.subTest(more=more):
                controller=self.controller(self.search,planner,self.judge,crawl,review=review,limits=Limits(search_budget=1))
                controller.root /= str(more)
                result=await controller.run()
                self.assertEqual(result.snapshot.status,'incomplete')
                self.assertTrue(result.snapshot.gaps)
                self.assertIs(result.review_history[-1].needs_more_search,None if more=='error' else True)

    async def test_false_review_collects_but_does_not_certify_answer_correctness(self):
        self.decision(SimpleNamespace(revision=0),False)
        async def crawl(req):return CrawlResult('full')
        async def review(view):return self.decision(view,False)
        async def planner(view):
            if not view.tasks:return Plan(view.revision,(intent('one'),))
            return Plan(view.revision,finish=True)  # no cited support
        result=await self.controller(self.search,planner,self.judge,crawl,review=review).run()
        self.assertEqual(result.snapshot.status,'collected')
        self.assertTrue(all(not r.supported for r in result.requirements))
        self.assertIn('NOT been verified',result.snapshot.render(max_bytes=10000))

    async def test_one_failed_source_does_not_discard_positive_siblings(self):
        from research.workflow import LayaWorkflow
        from research.decisions import BinaryDecision, DecisionError
        seen=[];crawled=[]
        class Client:
            async def decide(self,state,*args):
                title=json.loads(state)['title'];seen.append(title)
                if title=='1':raise DecisionError('fixture failure')
                return BinaryDecision(True,.9,0)
        async def search(task):return [SearchResult(str(i),f'https://example.com/{i}','snippet') for i in range(3)]
        async def crawl(req):crawled.append(req.title);return CrawlResult('full '+req.title)
        async def review(view):return self.decision(view,False)
        async def planner(view):
            if not view.tasks:return Plan(view.revision,(intent('one'),))
            return self.finish(view)
        result=await self.controller(search,planner,LayaWorkflow(Client()).judge,crawl,review=review).run()
        self.assertEqual(sorted(seen),['0','1','2'])
        self.assertEqual(sorted(crawled),['0','2'])
        self.assertEqual(result.snapshot.status,'collected')
        self.assertTrue(any('judge:DecisionError' in e for e in result.errors))

    async def test_source_callbacks_have_individual_deadlines_not_one_batch_deadline(self):
        seen=[]
        async def search(task):return [SearchResult(str(i),f'https://example.com/{i}','snippet') for i in range(8)]
        async def judge(req):
            actions=[]
            for s in req.sources:
                await asyncio.sleep(.025)
                seen.append(s.source_id);actions.append(SourceAction(s.source_id,'crawl',('answer',)))
            return Judgment(req.view.revision,tuple(actions))
        async def crawl(req):return CrawlResult('full '+req.title)
        async def review(view):return self.decision(view,False)
        async def planner(view):
            if not view.tasks:return Plan(view.revision,(intent('one'),))
            return self.finish(view)
        result=await self.controller(search,planner,judge,crawl,review=review,callback_timeout=.12,
            limits=Limits(laya_concurrency=1,max_crawls=10)).run()
        self.assertEqual(len(seen),8)
        self.assertEqual(len([e for e in result.evidence if e.kind=='page_extract']),8)
        self.assertEqual(result.snapshot.status,'collected')
        self.assertFalse(any('callback_timeout' in e for e in result.errors))

    async def test_cancellation_stops_review_without_new_queries(self):
        self.decision(SimpleNamespace(revision=0),False)
        entered=asyncio.Event();cancelled=asyncio.Event()
        async def review(view):
            entered.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        async def crawl(req):return CrawlResult('full')
        async def planner(view):return Plan(view.revision,(intent('one'),))
        run=asyncio.create_task(self.controller(self.search,planner,self.judge,crawl,review=review).run())
        await asyncio.wait_for(entered.wait(),1);run.cancel()
        result=await asyncio.wait_for(run,1)
        await asyncio.wait_for(cancelled.wait(),1)
        self.assertEqual(result.snapshot.status,'cancelled');self.assertEqual(result.budget_used,1)
