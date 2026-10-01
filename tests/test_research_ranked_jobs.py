import asyncio,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import test_research_decisions as fixtures
from research.contracts import SearchResult

class RankedJobTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.LayaTests.asyncSetUp
    asyncTearDown=fixtures.LayaTests.asyncTearDown
    client=fixtures.LayaTests.client

    async def test_page_choice_can_rank_only_actual_candidates(self):
        self.handler=lambda body:{'answers':{'next':{'probabilities':{'s1':.8,'s2':.2}}}}
        c=self.client()
        r=await c.choose('topic',{'s1':'one','s2':'two'},include_none=False)
        self.assertEqual(set(self.requests[0]['questions']['next']['criteria']),{'s1','s2'})
        self.assertEqual(r.ranked[0][0],'s1')

    async def test_choice_none_means_irrelevant_not_insufficient_evidence(self):
        self.handler=lambda body:{'answers':{'next':{'probabilities':{'s1':.8,'none_match':.2}}}}
        c=self.client()
        text='None of these web pages is related to the search topic.'
        await c.choose('topic',{'s1':'page title'},none_description=text)
        self.assertEqual(self.requests[0]['questions']['next']['criteria']['none_match'],text)

    async def test_default_pipeline_mixes_searches_ranks_pages_and_delivers_full_captures(self):
        from research.jobs import ResearchConnection
        started=[];ready=asyncio.Event();crawled=[];conn=None;frames=[]
        async def enter(provider):
            started.append(provider)
            if len(started)==4:ready.set()
            await asyncio.wait_for(ready.wait(),1)
        class Provider:
            async def search(self,task):
                await enter('4get');return [SearchResult(task.query,'https://example.com/'+task.query,'relevant snippet')]
            async def close(self):pass
        async def google(*args,**kw):
            await enter('google');return {'ok':True,'results':[{'title':'google','url':'https://example.com/google','snippet':'relevant snippet'}]}
        async def extract(url,*args,**kw):
            crawled.append(url);await asyncio.sleep(.01);return dict(ok=True,text='word '*10,contentMode='full')
        engine=SimpleNamespace(ensure_browser=AsyncMock(),search_one=google,crawl_one=extract,background_slots=asyncio.Semaphore(2),cleanup_research_owner=AsyncMock())
        def handler(body):
            self.assertEqual(len(started),4)
            self.assertEqual(body['questions']['next']['type'],'choice')
            keys=list(body['questions']['next']['criteria']);p={k:0 for k in keys};p[keys[0]]=1.0
            return {'answers':{'next':{'probabilities':p}}}
        self.handler=handler;client=self.client()
        async def send(frame):
            frames.append(frame);v=frame['view'];self.assertNotIn('evidence',v)
            self.assertEqual(v['provider_slots'],['4get','4get','4get','google'])
            searches=[dict(query='q'+str(i),direction='topic '+str(i),provider=provider,addresses=['answer'],parent_task_id=None) for i,provider in enumerate(v['provider_slots'])]
            conn.reply(dict(type='planner_reply',id=frame['id'],jobId=frame['jobId'],requestId=frame['requestId'],proposal=dict(revision=v['revision'],searches=searches)))
        with tempfile.TemporaryDirectory() as root,patch('research.jobs.LayaClient',return_value=client):
            conn=ResearchConnection(engine,send,Path(root),fourget_factory=Provider)
            result=await asyncio.wait_for(conn.run(1,'owner',dict(jobId='job',question='topic',provider='auto',searchBudget=4,searchConcurrency=4,layaConcurrency=2,crawlWordBudget=10,rankBatchSize=4,ranker='laya',crawlConcurrency=2,clock={'iso':'2026-09-28T00:00:00Z','timezone':'UTC'})),5)
        self.assertEqual(started.count('4get'),3);self.assertEqual(started.count('google'),1)
        self.assertEqual(result['crawlWords'],20);self.assertEqual(len(crawled),2)
        self.assertEqual(result['reason'],'crawl_word_budget_reached')
        self.assertTrue(result['fullEvidenceDelivered']);self.assertTrue(result['rankingHistory'])
        self.assertEqual(result['reviewHistory'],[]);self.assertEqual(len(frames),1)


class NoLayaJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_ranker_none_crawls_serp_order_without_laya_and_accepts_wide_concurrency(self):
        from research.jobs import ResearchConnection
        crawled=[];conn=None
        class Provider:
            async def search(self,task):
                return [SearchResult(task.query+str(i),f'https://example.com/{task.query}/{i}','snippet') for i in range(3)]
            async def close(self):pass
        async def google(*args,**kw):return {'ok':True,'results':[]}
        async def extract(url,*args,**kw):
            crawled.append(url);await asyncio.sleep(.01);return dict(ok=True,text='word '*10,contentMode='full')
        engine=SimpleNamespace(ensure_browser=AsyncMock(),search_one=google,crawl_one=extract,background_slots=asyncio.Semaphore(32),cleanup_research_owner=AsyncMock())
        async def send(frame):
            v=frame['view'];searches=[dict(query='q'+str(i),direction='d',provider=p,addresses=['answer'],parent_task_id=None) for i,p in enumerate(v['provider_slots'][:2])]
            conn.reply(dict(type='planner_reply',id=frame['id'],jobId=frame['jobId'],requestId=frame['requestId'],proposal=dict(revision=v['revision'],searches=searches)))
        with tempfile.TemporaryDirectory() as root,patch('research.jobs.LayaClient') as laya:
            laya.return_value.close=AsyncMock();laya.return_value.choose=AsyncMock()
            conn=ResearchConnection(engine,send,Path(root),fourget_factory=Provider)
            result=await asyncio.wait_for(conn.run(1,'owner',dict(jobId='job',question='topic',provider='auto',searchBudget=2,searchConcurrency=4,layaConcurrency=2,
                crawlWordBudget=100000,rankBatchSize=16,ranker='none',crawlConcurrency=32,clock={'iso':'2026-09-28T00:00:00Z','timezone':'UTC'})),5)
        laya.return_value.choose.assert_not_called()
        self.assertEqual(result['ranker'],'none');self.assertEqual(result['crawlConcurrency'],32)
        # Interleaved search order: each query's first result before any second result.
        self.assertEqual([u.rsplit('/',1)[1] for u in crawled],['0','0','1','1','2','2'])


class SingleRoundAndGoogleRedirectTests(unittest.IsolatedAsyncioTestCase):
    def test_google_goto_resolves_to_its_302_target_and_failures_keep_the_url(self):
        from research import jobs
        import urllib.error
        class Opener:
            def __init__(self,location):self.location=location
            def open(self,request,timeout):
                raise urllib.error.HTTPError(request.full_url,302,'Found',{'Location':self.location},None)
        goto='https://www.google.com/goto?url=CAES123'
        with patch('urllib.request.build_opener',return_value=Opener('https://spaceplace.nasa.gov/seasons/')):
            self.assertEqual(jobs.resolve_google_redirect(goto),'https://spaceplace.nasa.gov/seasons/')
            self.assertEqual(jobs.resolve_google_redirect('https://example.com/a'),'https://example.com/a')
        with patch('urllib.request.build_opener',return_value=Opener('javascript:alert(1)')):
            self.assertEqual(jobs.resolve_google_redirect(goto),goto)
        with patch('urllib.request.build_opener',side_effect=OSError('offline')):
            self.assertEqual(jobs.resolve_google_redirect(goto),goto)

    async def test_default_single_round_crawls_one_wave_and_dedups_resolved_google_urls(self):
        from research.jobs import ResearchConnection
        crawled=[];frames=[];conn=None
        class Provider:
            async def search(self,task):
                return [SearchResult(task.query,'https://spaceplace.nasa.gov/seasons/','snippet'),
                        SearchResult(task.query+'b',f'https://example.com/{task.query}','snippet')]
            async def close(self):pass
        async def google(*args,**kw):
            return {'ok':True,'results':[{'title':'g','url':'https://www.google.com/goto?url=CAES1','snippet':'s'}]}
        async def extract(url,*args,**kw):
            crawled.append(url);return dict(ok=True,text='word '*10,contentMode='full')
        engine=SimpleNamespace(ensure_browser=AsyncMock(),search_one=google,crawl_one=extract,background_slots=asyncio.Semaphore(16),cleanup_research_owner=AsyncMock())
        async def send(frame):
            frames.append(frame);v=frame['view']
            searches=[dict(query='q'+str(len(frames))+str(i),direction='d',provider=p,addresses=['answer'],parent_task_id=None) for i,p in enumerate(v['provider_slots'])]
            conn.reply(dict(type='planner_reply',id=frame['id'],jobId=frame['jobId'],requestId=frame['requestId'],proposal=dict(revision=v['revision'],searches=searches)))
        with tempfile.TemporaryDirectory() as root,patch('research.jobs.LayaClient') as laya, \
             patch('research.jobs.resolve_google_redirect',side_effect=lambda u:'https://spaceplace.nasa.gov/seasons/' if 'goto' in u else u):
            laya.return_value.close=AsyncMock();laya.return_value.choose=AsyncMock()
            conn=ResearchConnection(engine,send,Path(root),fourget_factory=Provider)
            result=await asyncio.wait_for(conn.run(1,'owner',dict(jobId='job',question='topic',provider='auto',searchBudget=8,searchConcurrency=4,layaConcurrency=2,
                clock={'iso':'2026-09-28T00:00:00Z','timezone':'UTC'})),5)
        self.assertEqual(len(frames),1,'exactly one planner round')
        self.assertEqual(result['searchRounds'],1);self.assertEqual(result['crawlConcurrency'],16);self.assertEqual(result['ranker'],'none')
        self.assertEqual(result['budgetUsed'],4)
        self.assertEqual(crawled.count('https://spaceplace.nasa.gov/seasons/'),1,'resolved Google URL and 4get URLs dedup to one crawl')
        self.assertEqual(len(crawled),len(set(crawled)))
        self.assertNotIn('https://www.google.com/goto?url=CAES1',crawled)
        laya.return_value.choose.assert_not_called()


class DateLineTests(unittest.TestCase):
    def test_local_date_line_uses_the_users_timezone(self):
        from research.jobs import local_date_line
        self.assertTrue(local_date_line({'iso':'2026-09-28T23:30:00Z','timezone':'Asia/Taipei'}).startswith('Today: 2026-09-29 (週二)'))
