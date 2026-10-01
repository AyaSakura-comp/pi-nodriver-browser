import asyncio
import importlib.util
import unittest
from types import SimpleNamespace
from dataclasses import replace
import test_research_controller as fixtures
from test_research_controller import intent
from research.contracts import SearchResult,Limits
from research.controller_contracts import Plan,CrawlResult,SourceView
from research.decisions import Ranking


class RankedTests(unittest.IsolatedAsyncioTestCase):
    def modules(self):
        self.assertIsNotNone(importlib.util.find_spec('research.ranking'),'page tournament missing')
        from research.ranking import PageRanker
        from research.controller_contracts import RankRequest,RankedSources
        return PageRanker,RankRequest,RankedSources

    async def test_tournament_selects_top_without_comparing_cross_group_probabilities(self):
        PageRanker,Req,_=self.modules();calls=[]
        class Client:
            async def choose(self,state,candidates,**kw):
                calls.append(tuple(candidates));ids=sorted(candidates,key=lambda x:int(x[1:]),reverse=True)
                # Deliberately non-comparable local distributions; only local winner matters.
                return Ranking(tuple([(ids[0],.51)]+[(s,.48/max(1,len(ids)-1)) for s in ids[1:]]+[('none_match',.01)]),0)
        ranker=PageRanker(Client())
        sources=tuple(SourceView(f's{i}','https://example.com/'+str(i),'Title '+str(i),('t1',),'seen',('description',)) for i in range(1,11))
        req=Req(SimpleNamespace(revision=2,question='topic'),sources,4)
        result=await ranker.rank(req)
        self.assertEqual(result.source_ids,('s10','s9','s8','s7'))
        self.assertTrue(all(1<=len(c)<=3 for c in calls))
        first=len(calls)
        result2=await ranker.rank(Req(SimpleNamespace(revision=3,question='topic'),sources[:6],2))
        self.assertEqual(result2.source_ids,('s6','s5'))
        self.assertLess(len(calls)-first,first,'remaining tournament should reuse unchanged branches')

    async def test_unknown_choice_and_group_failure_are_explicit_not_navigation(self):
        PageRanker,Req,_=self.modules()
        class Client:
            async def choose(self,state,candidates,**kw):return Ranking((('none_match',.9),),0)
        sources=tuple(SourceView(f's{i}',f'https://example.com/{i}','Title',('t1',),'seen',('desc',)) for i in (1,2))
        r=await PageRanker(Client()).rank(Req(SimpleNamespace(revision=0,question='q'),sources,2))
        self.assertEqual(r.source_ids,());self.assertEqual(r.failed_ids,('s1','s2'));self.assertEqual(r.rejected_ids,())
        class Broken:
            async def choose(self,*args,**kw):raise RuntimeError('private text must not leak')
        r=await PageRanker(Broken()).rank(Req(SimpleNamespace(revision=0,question='q'),sources,2))
        self.assertEqual(r.source_ids,());self.assertTrue(r.errors);self.assertNotIn('private',str(r.errors))

    async def test_later_invalid_choice_does_not_reject_already_selected_winner(self):
        PageRanker,Req,_=self.modules();calls=0
        class Client:
            async def choose(self,state,candidates,**kw):
                nonlocal calls
                calls+=1
                return Ranking(((next(iter(candidates)),.9),('none_match',.1)) if calls==1 else (('none_match',.9),),0)
        sources=tuple(SourceView(f's{i}',f'https://example.com/{i}',str(i),('t1',),'seen',('desc',)) for i in range(3))
        r=await PageRanker(Client()).rank(Req(SimpleNamespace(revision=0,question='q'),sources,3))
        self.assertEqual(r.source_ids,('s0',))
        self.assertFalse(set(r.source_ids)&set(r.rejected_ids))

    async def test_rough_word_budget_requires_no_tokenizer(self):
        self.assertIsNotNone(importlib.util.find_spec('research.rough_budget'),'rough budget counter missing')
        from research.rough_budget import rough_words
        self.assertEqual(rough_words('hello world'),2)
        self.assertEqual(rough_words('中文測試'),4)
        self.assertEqual(rough_words(''),0)
        self.assertGreater(rough_words('x'*800),1)
        self.assertGreater(rough_words('+'*800),1)


class RankedControllerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.ControllerTests.asyncSetUp
    controller=fixtures.ControllerTests.controller
    def contracts(self):
        import research.controller_contracts as c
        self.assertTrue(hasattr(c,'RankRequest'),'ranked controller contract missing')
        return c.RankedSources

    async def test_search_barrier_dedup_ranked_order_and_no_six_crawl_cap(self):
        Result=self.contracts();searched=[];ranked=[];crawled=[]
        async def search(t):
            searched.append(t.query)
            return [SearchResult(str(i),f'https://example.com/{i}','snippet') for i in range(8)]
        async def rank(req):
            self.assertEqual(len(searched),4)
            ids=[s.source_id for s in req.sources];self.assertEqual(len(ids),len(set(ids)))
            self.assertTrue(all(s.status=='seen' for s in req.sources))
            ranked.append(ids);return Result(req.view.revision,tuple(reversed(ids))[:req.limit])
        async def crawl(req):crawled.append(req.source_id);return CrawlResult('small page')
        async def planner(v):
            if not v.tasks:return Plan(v.revision,tuple(intent(f'q{i}') for i in range(4)))
            self.fail('budget exhausted must not call planner again')
        c=self.controller(search,planner,None,crawl,rank=rank,limits=Limits(search_budget=4,search_concurrency=4,rank_batch_size=3,crawl_word_budget=20000))
        r=await c.run()
        self.assertEqual(crawled[:3],['s8','s7','s6']);self.assertEqual(len(crawled),8)
        self.assertEqual(len(crawled),len(set(crawled)))
        self.assertEqual(r.snapshot.status,'collected')

    async def test_ranking_runs_in_background_while_crawls_are_in_flight(self):
        Result=self.contracts();log=[];inflight=0;overlap=0
        async def search(t):return [SearchResult(str(i),f'https://example.com/{i}','desc') for i in range(6)]
        async def rank(req):
            nonlocal overlap
            overlap+=inflight>0
            log.append(('rank',req.limit))
            await asyncio.sleep(.01)
            return Result(req.view.revision,tuple(s.source_id for s in req.sources)[:req.limit])
        async def crawl(req):
            nonlocal inflight
            inflight+=1;log.append(('crawl',req.source_id))
            await asyncio.sleep(.05)
            inflight-=1
            return CrawlResult('page')
        async def planner(v):return Plan(v.revision,(intent('query'),))
        c=self.controller(search,planner,None,crawl,rank=rank,limits=Limits(search_budget=1,crawl_word_budget=20000,rank_batch_size=2,crawl_concurrency=2))
        r=await c.run()
        crawls=[x for x in log if x[0]=='crawl']
        self.assertEqual(len(crawls),6)
        self.assertTrue(all(limit==1 for kind,limit in log if kind=='rank'),'one winner per background rank')
        self.assertEqual(log[1][0],'crawl','first crawl starts right after the first winner, not a batch')
        self.assertGreater(overlap,0,'ranking must overlap in-flight crawls')
        self.assertEqual(r.snapshot.status,'collected')

    async def test_committed_page_progress_excludes_failed_and_partial_captures(self):
        events = []
        async def search(task):
            return [SearchResult(str(i), f'https://example.com/{i}', '台積電 收盤價') for i in range(3)]
        async def rank(req):
            Result = self.contracts()
            return Result(req.view.revision, tuple(s.source_id for s in req.sources)[:req.limit])
        async def crawl(req):
            if req.source_id == 's2':
                return CrawlResult('台積電 收盤價 1190', truncated=True)
            if req.source_id == 's3':
                raise RuntimeError('network')
            return CrawlResult('台積電 收盤價 1200')
        async def planner(v): return Plan(v.revision, (intent('台積電 收盤價'),))
        controller = self.controller(search, planner, None, crawl, rank=rank,
                                     on_page=lambda source, text: events.append((source.source_id, text)),
                                     limits=Limits(search_budget=1, crawl_word_budget=100))
        result = await controller.run()
        # Failed/partial pages never commit text; they report '' so progressive
        # evidence can release the budget reserved for them.
        self.assertEqual([e for e in events if e[1]], [('s1', '台積電 收盤價 1200')])
        self.assertEqual({sid for sid, text in events if not text}, {'s2', 's3'})
        self.assertTrue(any(e.kind == 'page_extract' for e in result.evidence))

    async def test_word_threshold_stops_new_crawls_but_keeps_inflight_full_text(self):
        Result=self.contracts();crawled=[]
        async def search(t):return [SearchResult(str(i),f'https://example.com/{i}','desc') for i in range(8)]
        async def rank(req):return Result(req.view.revision,tuple(s.source_id for s in req.sources)[:req.limit])
        async def crawl(req):
            crawled.append(req.source_id)
            await asyncio.sleep(.01)
            return CrawlResult('one two three four five six')
        async def planner(v):return Plan(v.revision,(intent('query'),))
        c=self.controller(search,planner,None,crawl,rank=rank,limits=Limits(search_budget=1,crawl_word_budget=5,rank_batch_size=4,crawl_concurrency=2))
        r=await c.run();self.assertEqual(len(crawled),2)
        self.assertEqual(r.snapshot.reason,'crawl_word_budget_reached')
        self.assertEqual(r.crawl_words,12)
        self.assertEqual(len([e for e in r.evidence if e.kind=='page_extract']),2)
        self.assertTrue(all(e.text.endswith('five six') for e in r.evidence if e.kind=='page_extract'))
        self.assertFalse(any(s.status=='queued' for s in r.sources))

    async def test_tournament_gets_a_separate_aggregate_deadline(self):
        Result=self.contracts()
        from research.ranking import PageRanker
        calls=0;sem=asyncio.Semaphore(2)
        class Client:
            async def choose(self,state,candidates,**kw):
                nonlocal calls
                async with sem:
                    calls+=1;await asyncio.sleep(.02)
                    return Ranking(((next(iter(candidates)),.9),('none_match',.1)),0)
        async def search(t):return [SearchResult(str(i),f'https://example.com/{t.query}/{i}','desc') for i in range(10)]
        async def planner(v):return Plan(v.revision,tuple(intent(f'q{i}') for i in range(4)))
        async def crawl(req):return CrawlResult('some complete content')
        r=await asyncio.wait_for(self.controller(search,planner,None,crawl,rank=PageRanker(Client()).rank,callback_timeout=.03,
            limits=Limits(search_budget=4,search_concurrency=4,crawl_word_budget=1)).run(),2)
        self.assertEqual(r.snapshot.status,'collected')
        self.assertTrue(any(e.kind=='page_extract' for e in r.evidence))
        self.assertGreater(calls,10)
        self.assertNotIn('rank:callback_timeout',r.errors)

    async def test_rank_transport_failure_is_not_labeled_irrelevant_or_retried(self):
        Result=self.contracts();calls=0
        async def search(t):return [SearchResult('a','https://example.com/a','desc')]
        async def rank(req):raise RuntimeError('unavailable')
        async def planner(v):
            nonlocal calls
            calls+=1;return Plan(v.revision,(intent('q'),))
        r=await self.controller(search,planner,None,rank=rank).run()
        self.assertEqual(r.snapshot.reason,'ranking_failed')
        self.assertEqual(r.snapshot.status,'failed')
        self.assertEqual(calls,1)
        self.assertTrue(all(s.status=='seen' for s in r.sources))

    async def test_previously_crawled_and_rejected_urls_are_not_reranked(self):
        Result=self.contracts();pools=[]
        async def search(t):return [SearchResult('x','https://example.com/same','desc')]
        async def rank(req):pools.append(req.sources);return Result(req.view.revision,(),tuple(s.source_id for s in req.sources))
        async def planner(v):
            if not v.tasks:return Plan(v.revision,(intent('first'),))
            if len(v.tasks)==1:return Plan(v.revision,(intent('different',parent=v.tasks[0].task_id),))
            return Plan(v.revision)
        r=await self.controller(search,planner,None,rank=rank,limits=Limits(search_budget=2)).run()
        self.assertEqual(len(pools),1);self.assertEqual(r.snapshot.status,'incomplete')
