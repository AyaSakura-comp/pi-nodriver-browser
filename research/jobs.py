"""Daemon-owned jobs. Planner replies bypass interactive locks on their owning connection.

The private crawl socket is a capability, not a public daemon operation. Provider
success registers exact URLs before the owner may submit a crawl. Neither model
output nor child-supplied text grants navigation authority.
"""
import asyncio
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

from .contracts import Limits, SearchResult, SearchTask, identifier
from .controller import ResearchController
from .controller_contracts import CrawlResult, Plan, RankedSources, SearchIntent
from .crawl_jobs import CrawlConsumer
from .decisions import LayaClient
from .workflow import LayaWorkflow
from .ranking import PageRanker
from . import focus
from .delivery import deliver
from .images import ResearchImages
from .providers import FourgetProvider

SEARCH_CLEANUP_TIMEOUT = 2.0


def planner_view(view):
    """Project metadata BEFORE IPC serialization; articles never enter this frame."""
    return dict(job_id=view.job_id, revision=view.revision, question=view.question,
        searchable_gaps=list(view.searchable_gaps), tasks=[asdict(t) for t in view.tasks],
        budget_remaining=view.budget_remaining,
        review_feedback=None if view.review_feedback is None else dict(
            needs_more_search=view.review_feedback.needs_more_search,
            reason=view.review_feedback.reason))


def parse_plan(value):
    if not isinstance(value, dict) or set(value) != {'revision','searches'}:
        raise ValueError('invalid_plan')
    if type(value['revision']) is not int:
        raise ValueError('invalid_plan')
    if not isinstance(value['searches'], list) or len(value['searches']) > 20:
        raise ValueError('invalid_plan')
    searches = []
    for row in value['searches']:
        if not isinstance(row, dict) or set(row) != {'query','direction','provider','addresses','parent_task_id'} or not isinstance(row['addresses'], list):
            raise ValueError('invalid_plan')
        searches.append(SearchIntent(row['query'],row['direction'],tuple(row['addresses']),row['parent_task_id'],row['provider']))
    return Plan(value['revision'],tuple(searches))


# Searches started while the agent is still writing its research tool call
# (one per finished q1..q4 parameter). A research job of the same session
# consumes them instead of searching again; unused ones expire.
PREFETCH_TTL = 60.0
_PREFETCH = {}


def _prefetch_key(session_id, provider, query):
    return (session_id, provider, ' '.join(query.split()))


def _expire_prefetch():
    now = time.monotonic()
    for key, (task, created) in list(_PREFETCH.items()):
        if now - created > PREFETCH_TTL:
            _PREFETCH.pop(key, None)
            if not task.done(): task.cancel()


class _RegisteredProvider:
    def __init__(self, provider, authority, name, accepting, session_id=None):
        self.provider, self.authority, self.name = provider, authority, name
        self.accepting = accepting
        self.session_id = session_id

    async def search(self, task):
        if not self.accepting[0]: raise ValueError('research_frozen')
        rows = None
        hit = _PREFETCH.pop(_prefetch_key(self.session_id, self.name, task.query), None) if self.session_id else None
        if hit is not None:
            try: rows = await hit[0]
            except Exception: rows = None  # fall back to a normal search
        if rows is None:
            rows = await self.provider.search(task)
        if not self.accepting[0]: raise ValueError('research_frozen')
        if not isinstance(rows,list) or len(rows) > 100 or any(not isinstance(r,SearchResult) or len(r.url)>8192 for r in rows):
            raise ValueError('invalid_provider_results')
        for row in rows:
            self.authority.setdefault(row.url, []).append(dict(taskId=task.task_id,provider=self.name))
        return rows


# Use the same 3s per-page fetch deadline as interactive crawl by default.
# Override explicitly with RESEARCH_CRAWL_TIMEOUT when longer fetches are acceptable.
# Evidence passage budget (rough tokens) for the answering model.
RESEARCH_SNIPPET_MODE = os.environ.get('RESEARCH_SNIPPET_MODE', 'anchor')
RESEARCH_EVIDENCE_BUDGET = max(500, int(os.environ.get('RESEARCH_EVIDENCE_BUDGET', '6000') or 6000))
RESEARCH_FETCH_TIMEOUT = min(30.0, max(1.0, float(os.environ.get('RESEARCH_CRAWL_TIMEOUT', '3') or 3)))
# Research searches/crawls reuse a browser liveness probe this recent (seconds).
# One CDP probe costs ~0.26 s and holds the lifecycle lock, so probing per page
# serialized the first crawls behind it. A job probes once in the background at
# start; a dead browser still surfaces as a failed page.
BROWSER_PROBE_MAX_AGE = 30.0
# Crawl only each query's top-N search results (0 = no per-query cap). Default 5:
# pages beyond a query's top 5 were mostly ones that missed the crawl timeout.
RESEARCH_CRAWL_TOP_PER_QUERY = max(0, int(os.environ.get('RESEARCH_CRAWL_TOP_PER_QUERY', '5') or 0))
# Images fetched in the background while pages are crawled and listed for the
# answer (0 = off), and how long finished crawling waits for in-flight downloads.
RESEARCH_IMAGES = max(0, int(os.environ.get('RESEARCH_IMAGES', '3') or 0))
RESEARCH_IMAGE_WAIT = max(0.0, float(os.environ.get('RESEARCH_IMAGE_WAIT', '1.0') or 0))


def local_date_line(clock):
    try:
        from zoneinfo import ZoneInfo
        now=datetime.fromisoformat(clock['iso'].replace('Z','+00:00')).astimezone(ZoneInfo(clock['timezone']))
    except Exception:
        now=datetime.fromisoformat(clock['iso'].replace('Z','+00:00'))
    return f"Today: {now:%Y-%m-%d} (週{'一二三四五六日'[now.weekday()]}), timezone {clock.get('timezone','UTC')}. Resolve relative dates (今天/明天/這週…) against this."


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def resolve_google_redirect(url, timeout=5.0):
    """Google SERP links are opaque /goto or /url tokens. Ask Google for its own
    302 target (one request, no redirect followed) so URL dedup can see that a
    Google result and a 4get result are the same page. Any failure keeps the
    observed URL unchanged."""
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != 'https' or parts.netloc != 'www.google.com' or parts.path not in ('/goto', '/url'):
            return url
        request = urllib.request.Request(url, method='GET', headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout):
                return url
        except urllib.error.HTTPError as redirect:
            target = redirect.headers.get('Location', '') if redirect.code in (301, 302, 303, 307, 308) else ''
        target_parts = urllib.parse.urlsplit(target)
        if target_parts.scheme in ('http', 'https') and target_parts.netloc and len(target) <= 2048:
            return target
    except Exception:
        pass
    return url


class _Google:
    def __init__(self, engine, owner): self.engine, self.owner = engine, owner

    async def search(self, task):
        await self.engine.ensure_browser(max_age=BROWSER_PROBE_MAX_AGE)
        # Observed hrefs, except Google's own opaque redirect tokens, which are
        # resolved to Google's 302 target. Google upstream caps snippets at 600 chars.
        result = await self.engine.search_one(dict(query=task.query,direction=task.direction),0,
                                             self.owner,asyncio.Semaphore(1),research=True)
        if not result['ok']:
            raise ValueError('google_search_failed')
        rows = result['results'][:10]
        urls = await asyncio.gather(*(asyncio.to_thread(resolve_google_redirect, r['url']) for r in rows))
        return [SearchResult(r['title'],url,r['snippet'],truncated=r.get('truncated',len(r['snippet'])>=600))
                for r,url in zip(rows,urls)]


class ResearchConnection:
    def __init__(self, engine, send, artifact_root: Path, *, fourget_factory=FourgetProvider, judge=None, review=None, rank=None, admission=None):
        self.engine, self.send, self.root = engine, send, artifact_root
        self.fourget_factory, self.injected_judge = fourget_factory, judge
        self.injected_review = review
        self.injected_rank = rank
        self.ranked = rank is not None or (judge is None and review is None)
        self.admission = admission or asyncio.Semaphore(2)
        self.jobs = {}
        self._streams = {}
        self._early = {}
        self._planners = {}
        self._closed = False

    def reply(self, message):
        if not isinstance(message,dict) or set(message) not in (
            {'type','id','jobId','requestId','proposal'}, {'type','id','jobId','requestId','error'}):
            raise ValueError('invalid_planner_reply')
        key = (message['id'],message['jobId'],message['requestId'])
        future = self._planners.get(key)
        if future is None or future.done(): raise ValueError('unowned_or_stale_planner_reply')
        if 'error' in message:
            future.set_exception(ValueError('planner_failed'))
        else:
            try: future.set_result(parse_plan(message['proposal']))
            except Exception: future.set_exception(ValueError('invalid_plan'))

    def add_query(self, session_id, params):
        """Deliver query `index` (or done=True) to a streaming research job."""
        if not isinstance(params,dict) or 'jobId' not in params: raise ValueError('invalid_add_query')
        stream=self._streams.get(params['jobId'])
        if stream is None:
            # The job request may still be on its way: keep a bounded backlog.
            early=self._early.setdefault((session_id,params['jobId']),[])
            if len(early)>=8 or len(self._early)>8: raise ValueError('unknown_streaming_job')
            early.append(dict(params)); return
        if stream['session']!=session_id: raise ValueError('unknown_streaming_job')
        if params.get('done') is True and set(params)=={'jobId','done'}:
            stream['closed']=True
            if stream['controller'] is not None: stream['controller'].close_stream()
            return
        if set(params)!={'jobId','query','index'}: raise ValueError('invalid_add_query')
        query,index=params['query'],params['index']
        if not isinstance(query,str) or not query.strip() or len(query)>200 or type(index) is not int or not 0<=index<4:
            raise ValueError('invalid_add_query')
        provider=stream['cycle'][index%len(stream['cycle'])]
        if stream['controller'] is None:
            stream['pending'].append((query.strip(),provider))
        elif stream['controller'].add_search(query.strip(),provider):
            stream['planned'].append(query.strip())

    async def prefetch(self, session_id, params):
        """Start one search for query `index` of a research call still being
        written. Same engine mapping as the job (3 x 4get, then Google)."""
        if not isinstance(params,dict) or set(params)!={'query','index'}: raise ValueError('invalid_prefetch')
        query, index = params['query'], params['index']
        if not isinstance(query,str) or not query.strip() or len(query)>200 or type(index) is not int or not 0<=index<4:
            raise ValueError('invalid_prefetch')
        provider = ('4get','4get','4get','google')[index]
        key = _prefetch_key(session_id, provider, query)
        _expire_prefetch()
        if key in _PREFETCH: return
        task = SearchTask(f'pf{index}', f'pf{index}', query.strip(), query.strip(), provider)
        async def run():
            if provider == 'google':
                return await _Google(self.engine, 'research-prefetch-'+secrets.token_hex(8)).search(task)
            fourget = self.fourget_factory()
            try: return await fourget.search(task)
            finally:
                try: await fourget.close()
                except Exception: pass
        _PREFETCH[key] = (asyncio.create_task(run(), name=f'research-prefetch:{index}'), time.monotonic())

    async def run(self, request_id, session_id, params):
        if self._closed or self.jobs:
            raise ValueError('research_connection_busy_or_closed')
        fields={'jobId','question','provider','searchBudget','searchConcurrency','layaConcurrency','clock'}
        optional={'crawlWordBudget','rankBatchSize','ranker','crawlConcurrency','searchRounds','evidence','queries','streaming'}
        if not isinstance(params,dict) or not fields<=set(params) or set(params)-(fields|optional): raise ValueError('invalid_research_input')
        word_budget=params.get('crawlWordBudget',20000)
        rank_batch=params.get('rankBatchSize',4)
        ranker_mode=params.get('ranker','none')
        crawl_concurrency=params.get('crawlConcurrency',16)
        search_rounds=params.get('searchRounds',1)
        evidence_mode=params.get('evidence','passages')
        # Queries written by the invoking agent replace the first planner call.
        preset=params.get('queries')
        if preset is not None:
            if (not isinstance(preset,list) or not 1<=len(preset)<=4
                    or any(not isinstance(q,str) or not q.strip() or len(q)>200 for q in preset)):
                raise ValueError('invalid_queries')
            preset=list(dict.fromkeys(q.strip() for q in preset))
        streaming=params.get('streaming',False)
        if type(streaming) is not bool or (streaming and not preset): raise ValueError('invalid_streaming')
        if evidence_mode not in ('passages','filtered','progressive'): raise ValueError('invalid_limits')
        if (ranker_mode not in ('laya','none') or type(crawl_concurrency) is not int or not 1<=crawl_concurrency<=64
                or type(search_rounds) is not int or not 1<=search_rounds<=16):
            raise ValueError('invalid_limits')
        if type(word_budget) is not int or not 1<=word_budget<=100000 or type(rank_batch) is not int or not 1<=rank_batch<=16:
            raise ValueError('invalid_crawl_budget')
        job_id=identifier(params['jobId'])
        if not isinstance(params['question'],str) or not params['question'].strip() or len(params['question'])>4000: raise ValueError('invalid_question')
        if params['provider'] not in ('4get','google','auto'): raise ValueError('invalid_provider')
        for key, high in [('searchBudget',20),('searchConcurrency',4),('layaConcurrency',4)]:
            if type(params[key]) is not int or not 1<=params[key]<=high: raise ValueError('invalid_limits')
        clock=params['clock']
        if not isinstance(clock,dict) or set(clock)!={'iso','timezone'} or not isinstance(clock['timezone'],str) or len(clock['timezone'])>100:
            raise ValueError('invalid_host_clock')
        if not isinstance(clock['iso'],str) or datetime.fromisoformat(clock['iso'].replace('Z','+00:00')).tzinfo is None:
            raise ValueError('invalid_host_clock')
        self.jobs[request_id]=asyncio.current_task()
        if streaming:
            # The agent is still writing q2..q4; add_query() delivers them.
            self._streams[job_id]=dict(controller=None,pending=[],closed=False,session=session_id,
                cycle=('4get','4get','4get','google') if params['provider']=='auto' else (params['provider'],),
                planned=None)
            for early in self._early.pop((session_id,job_id),[]): self.add_query(session_id,early)
        owner='research-'+secrets.token_hex(16)  # never shares interactive session state
        authority={}
        accepting=[True]
        consumer=None
        controller=None
        fourget=self.fourget_factory()
        laya=LayaClient(concurrency=params['layaConcurrency'])
        names=('4get','google') if params['provider']=='auto' else (params['provider'],)
        providers={name:_RegisteredProvider(fourget if name=='4get' else _Google(self.engine,owner),authority,name,accepting,session_id) for name in names}
        planner_count=0
        planned_queries=[]
        from .passages import ProgressivePassages
        from .delivery import deliver_progressive
        progressive = ProgressivePassages(params['question'],budget=RESEARCH_EVIDENCE_BUDGET,snippets=RESEARCH_SNIPPET_MODE) if evidence_mode == 'progressive' else None
        images=None
        if progressive is not None and RESEARCH_IMAGES and hasattr(self.engine,'run_fetch_image'):
            async def fetch_image(url):
                async with self.engine.image_fetch_semaphore:
                    path,mime,width,height,_=await self.engine.run_fetch_image(url,session_id or owner)
                return dict(path=str(path),mime=mime,width=width,height=height)
            def image_ready(text):
                if not accepting[0] or not progressive.append_note(text): return
                frame = dict(type='progress', id=request_id, jobId=job_id, phase='evidence',
                             prefix=local_date_line(clock)+'\n'+progressive.text)
                if progress_queue is not None:
                    if progress_queue.full():
                        progress_queue.get_nowait(); progress_queue.task_done()
                    progress_queue.put_nowait(frame)
            images=ResearchImages(params['question'],fetch_image,max_deliver=RESEARCH_IMAGES,max_fetch=2*RESEARCH_IMAGES,
                                  on_ready=image_ready,is_delivered=lambda sid: sid in progressive.delivered_sources)
        progress_queue = asyncio.Queue(maxsize=1) if progressive is not None else None
        progress_task = None
        def on_page(source, text):
            if progressive is None or not accepting[0]: return
            progressive.queries = tuple(planned_queries)  # planner has run before any crawl
            if not progressive.add(source.source_id, source.title, source.url, text): return
            frame = dict(type='progress', id=request_id, jobId=job_id, phase='evidence',
                         prefix=local_date_line(clock)+'\n'+progressive.text)
            if progress_queue.full():
                progress_queue.get_nowait(); progress_queue.task_done()
            progress_queue.put_nowait(frame)
        def on_results(rows):
            if progressive is None or not accepting[0]: return
            if not progressive.add_results(rows): return
            frame = dict(type='progress', id=request_id, jobId=job_id, phase='evidence',
                         prefix=local_date_line(clock)+'\n'+progressive.text)
            if progress_queue.full():
                progress_queue.get_nowait(); progress_queue.task_done()
            progress_queue.put_nowait(frame)
        async def send_progress():
            while True:
                frame = await progress_queue.get()
                try:
                    await self.send(frame)
                except Exception:
                    pass  # Progress is best-effort; the frozen final packet still returns.
                finally:
                    progress_queue.task_done()
        async def planner(view):
            nonlocal planner_count
            planner_count+=1
            key=(request_id,job_id,f'p{planner_count}')
            future=asyncio.get_running_loop().create_future()
            self._planners[key]=future
            compact=planner_view(view)
            slots=[]
            if self.ranked:
                cycle=('4get','4get','4get','google') if params['provider']=='auto' else (params['provider'],)
                slots=[cycle[(len(view.tasks)+i)%len(cycle)] for i in range(min(4,view.budget_remaining))]
                compact['provider_slots']=slots
            if preset and self.ranked and not view.tasks:
                # Same shape the ranked planner returns (research-model.ts):
                # direction = query, every gap addressed, engines by slot.
                self._planners.pop(key,None); future.cancel()
                queries=preset[:len(slots)]
                planned_queries.extend(queries)
                return Plan(view.revision,tuple(SearchIntent(q,q,tuple(view.searchable_gaps),None,p)
                                                for q,p in zip(queries,slots)))
            frame=dict(type='planner_request',id=request_id,jobId=job_id,requestId=key[2],view=compact,providers=list(names))
            if len(json.dumps(frame).encode())>64*1024: raise ValueError('planner_input_too_large')
            try:
                await self.send(frame)
                plan=await future
                planned_queries.extend(s.query for s in plan.searches)
                if self.ranked and (len(plan.searches)>len(slots) or any(n>Counter(slots)[p] for p,n in Counter(s.provider for s in plan.searches).items())):
                    raise ValueError('invalid_search_mix')
                if not self.ranked and params['provider']=='auto' and not view.tasks and any(s.provider!='4get' for s in plan.searches):
                    raise ValueError('auto_requires_fourget_roots')
                return plan
            finally:
                self._planners.pop(key,None)
                if not future.done(): future.cancel()
        workflow=LayaWorkflow(laya)
        ranker=PageRanker(laya)
        serp_position,serp_count={},{}
        async def serp_order(request):
            # No Laya: crawl deduped URLs in search order, interleaving each
            # query's 1st result, then its 2nd, and so on. Positions are fixed
            # on first sight; later calls only see the not-yet-crawled rest.
            for s in request.sources:
                if s.source_id not in serp_position:
                    topic=s.topic_ids[0] if s.topic_ids else ''
                    serp_position[s.source_id]=serp_count.get(topic,0);serp_count[topic]=serp_position[s.source_id]+1
            order=sorted(request.sources,key=lambda s:serp_position[s.source_id])
            if RESEARCH_CRAWL_TOP_PER_QUERY:
                # Crawl only each query's top-N results; reject the rest so they are never re-ranked.
                keep=[s for s in order if serp_position[s.source_id]<RESEARCH_CRAWL_TOP_PER_QUERY]
                cut=tuple(s.source_id for s in order if serp_position[s.source_id]>=RESEARCH_CRAWL_TOP_PER_QUERY)
                return RankedSources(request.view.revision,tuple(s.source_id for s in keep[:request.limit]),rejected_ids=cut)
            return RankedSources(request.view.revision,tuple(s.source_id for s in order[:request.limit]))
        async def extract(request):
            if not accepting[0] or request.job_id!=job_id or request.url not in authority:
                raise ValueError('research_url_not_authorized')
            await self.engine.ensure_browser(max_age=BROWSER_PROBE_MAX_AGE)
            result=await self.engine.crawl_one(request.url,0,owner,asyncio.Semaphore(1),max_text_units=1_000_000,
                                               fetch_timeout=RESEARCH_FETCH_TIMEOUT)
            mode=result.get('contentMode','full')
            if mode not in ('full','temp-wiki'): mode='full'
            truncated=mode=='temp-wiki' or result.get('truncated',False)
            text=result.get('text','')
            if images is not None and result.get('ok'):
                images.offer(request.source_id,request.title,result.get('imageCandidates') or [])
            return CrawlResult(text,truncated=truncated,
                success=bool(result.get('ok')) and not truncated,content_mode=mode,source_chars=result.get('sourceChars'),
                captured_chars=result.get('capturedChars',len(text)),capture_limit=result.get('captureLimit'),
                capture_units=result.get('captureUnits','codepoints'))
        async def focus_stage(request):
            # Separate from crawling: runs after the crawl slot is released and
            # queues at the daemon-wide 0.6B gate. Bound to this job only.
            if request.job_id!=job_id: raise ValueError('research_focus_job_mismatch')
            focused=await focus.focus_page(params['question'],request.title,request.text,queries=request.queries)
            if focused is None: return None
            pages=Path(self.root)/job_id/'pages';pages.mkdir(mode=0o700,parents=True,exist_ok=True)
            full=pages/f'{identifier(request.source_id)}.txt';full.write_text(request.text,encoding='utf-8')
            stats=focused[1]
            gap=f"; {stats['failed_windows']} of {stats['windows']} windows failed and contribute nothing" if stats['failed_windows'] else ''
            if stats['skipped_windows']: gap+=f"; only the first {stats['windows']} windows were filtered, {stats['skipped_windows']} later windows not read"
            return (f"[Focused excerpt: {stats['lines']} verbatim lines kept by a local filter{gap}; "
                    f"full page ({stats['source_chars']} chars) kept at {full}]\n"
                    +(focused[0] or '(No sentence on this page was judged relevant to the question.)'))
        try:
            if progress_queue is not None:
                progress_task=asyncio.create_task(send_progress(),name=f'research-progress:{job_id}')
            if callable(getattr(self.engine,'ensure_browser',None)):
                probe=asyncio.create_task(self.engine.ensure_browser(),name=f'research-browser-probe:{job_id}')
                probe.add_done_callback(lambda task: task.cancelled() or task.exception())
            sweep=getattr(self.engine,'sweep_crawl_pool',None)
            if sweep is not None:
                try: await sweep()  # leftovers only; tabs another job uses stay open
                except Exception: pass
            async with self.admission, CrawlConsumer(job_id=job_id,extract=extract,concurrency=crawl_concurrency,queue_size=max(32,crawl_concurrency)) as consumer:
                # searchRounds=1: one planner call -> one wave of distinct queries -> crawl it, then deliver.
                controller=ResearchController(job_id=job_id,question=params['question'],requirements=('answer',),
                    max_planner_calls=search_rounds if self.ranked else 16,
                    artifact_root=self.root,providers=providers,planner=planner,
                    judge=None if self.ranked else (self.injected_judge or workflow.judge),
                    review=None if self.ranked else (self.injected_review or workflow.review),
                    rank=(self.injected_rank or (serp_order if ranker_mode=='none' else ranker.rank)) if self.ranked else None,
                    focus=focus_stage if self.ranked and evidence_mode=='filtered' and focus.endpoint() else None,
                    on_page=on_page if progressive is not None else None,
                    on_results=on_results if progressive is not None else None,
                    rank_while_searching=bool(self.ranked and ranker_mode=='none' and self.injected_rank is None
                                              and os.environ.get('RESEARCH_SEARCH_CRAWL_PIPELINE','1')=='1'),
                    streaming=streaming,
                    crawl=consumer.crawl,limits=Limits(search_budget=params['searchBudget'],search_concurrency=params['searchConcurrency'],
                        laya_concurrency=params['layaConcurrency'],crawl_word_budget=word_budget,rank_batch_size=rank_batch,
                        crawl_concurrency=crawl_concurrency,
                        max_packet_bytes=2*1024*1024 if self.ranked else 50*1024))
                stream=self._streams.get(job_id) if streaming else None
                if stream is not None:
                    stream['controller']=controller; stream['planned']=planned_queries
                    for query,provider in stream['pending']:
                        if controller.add_search(query,provider): planned_queries.append(query)
                    if stream['closed']: controller.close_stream()
                result=await controller.run()
                accepting[0]=False
            if progressive is not None and self.ranked:
                # Everything was committed (and prefilled) while crawling; only
                # short status lines follow the prefix. Search snippets for
                # unread sources are already in its "Search results" section.
                packet=local_date_line(clock)+'\n'+progressive.finalize()
                image_section=''
                if images is not None:
                    await images.settle(RESEARCH_IMAGE_WAIT)
                    image_section=images.section(progressive.delivered_sources)
                    packet+=image_section
                # A list of unread sources, the stop reason or the capture path reads
                # as "evidence incomplete" and sent agents off to crawl/fetch more;
                # those stay in the tool details. The text closes the lookup instead.
                packet+=('\nEnd of evidence. This is enough to answer: answer now from the passages and search results above. '
                         'Deliver a highly structured, well-formatted, and exhaustive answer matching the question archetype:\n'
                         '1. Structure & Readability: Do not rely solely on simple bullet lists. Structure the response with clear '
                         'Markdown sections (##), informative summary tables, and detailed narrative highlight sections. '
                         'In tables, use informative headers (Name, Time, Location, Highlights, Cost/Specs, Source) and use <br>• '
                         'for sub-points so cells remain clean and easy to scan.\n'
                         '2. Domain Archetypes (範例引導):\n'
                         '   • 旅遊 / 活動 / 美食 / 行程類：提供完整「人事時地物」與交通指南，務必詳列「費用明細 ($$)（門票/低消/預算/購票通路）」'
                         '以及「網友真實心得與評價 / 避坑踩雷提醒（人潮時段/必點必看/優缺點/推薦理由）」；精選亮點撰寫專屬段落深度介紹。\n'
                         '   • 學術 / 理論 / 技術 / 科普類：深入清楚解釋概念原理與底層機制（核心定義、推導/步驟邏輯、優劣對比、實務意義與業界實踐），'
                         '避免空泛名詞堆砌，以白話易懂且專業的語調深入剖析。\n'
                         '   • 3C / 科技產品 / 評測類：列出規格對比表、官方定價與配置方案 ($$)、社群與媒體實測心得、真實優缺點與適合客群。\n'
                         '3. Exhaustive 5W1H Detail: Extract every supported detail (Who/What/When/Where/How/$$); never omit details or '
                         'write "refer to official site". Prefer a complete list over a summary: if 10 items exist, detail all 10.\n'
                         '4. Inline Sources & Media: End every item and table row with its markdown link ([來源](URL)). '
                         'If downloaded images are listed below, place the most relevant [[image: …]] markers on their own '
                         'lines inside the narrative highlight paragraphs they illustrate.\n'
                         '5. Boundaries & Closure: Do not search, crawl, browse or fetch images on your own. Finish your answer '
                         'by stating clearly what the evidence does not cover, and ask the user whether they want you to '
                         'search further for specific missing details.\n')
                if images is not None and images.delivered:
                    packet+=('This evidence includes downloaded images: place the one or two most relevant '
                             '[[image: …]] markers from the Images list, copied exactly, on their own lines '
                             'inside the narrative highlight paragraphs they illustrate.\n')
                response=deliver_progressive(result.snapshot,packet,max_bytes=2*1024*1024,max_lines=20000)
            elif self.ranked and evidence_mode=='passages':
                response=deliver(result.snapshot,max_bytes=2*1024*1024,max_lines=20000,
                                 passages=dict(question=params['question'],queries=tuple(planned_queries),budget=RESEARCH_EVIDENCE_BUDGET))
            elif self.ranked:
                response=deliver(result.snapshot,max_bytes=2*1024*1024,max_lines=20000,compact=True)
            else:
                response=deliver(result.snapshot)
            if self.ranked and evidence_mode != 'progressive' and response.get('fullEvidenceDelivered'):
                # The answering model must resolve 今天/明天/這週 against the same clock the planner used.
                response['text']=local_date_line(clock)+'\n'+response['text']
            sources=[dict(sourceId=s.source_id,url=s.url,discoveries=authority[s.url]) for s in result.sources]
            return dict(response,jobId=job_id,budgetUsed=result.budget_used,attempts=list(result.attempts),
                        sources=sources,authorizations=[dict(url=url,discoveries=discoveries)
                            for url,discoveries in authority.items()],
                        errors=list(result.errors),reviewHistory=[asdict(r) for r in result.review_history],
                        rankingHistory=[asdict(r) for r in result.ranking_history],crawlWords=result.crawl_words,
                        crawlWordBudget=word_budget,rankBatchSize=rank_batch,ranker=ranker_mode,
                        crawlConcurrency=crawl_concurrency,searchRounds=search_rounds,evidence=evidence_mode,action='research')
        finally:
            accepting[0]=False
            if images is not None: images.cancel()
            if progress_task is not None:
                progress_task.cancel()
                await asyncio.gather(progress_task,return_exceptions=True)
            for key,future in list(self._planners.items()):
                if key[0]==request_id:
                    if not future.done(): future.cancel()
                    self._planners.pop(key,None)
            async def finalize():
                searches=set(controller.pending_search_callbacks) if controller is not None else set()
                pending=set()
                if searches:
                    done,pending=await asyncio.wait(searches,timeout=SEARCH_CLEANUP_TIMEOUT)
                    # The first cancellation may have initiated tab close. Bound
                    # that close too, then join cooperative cancellation before
                    # attempting another owner cleanup under the same global lock.
                    for task in pending: task.cancel()
                    if pending:
                        settled,pending=await asyncio.wait(pending,timeout=SEARCH_CLEANUP_TIMEOUT)
                        done.update(settled)
                    for task in done:
                        if not task.cancelled(): task.exception()
                await fourget.close(); await laya.close()
                detached=set(consumer.detached_tasks) if consumer is not None else set()
                detached.update(pending)
                async def cleanup():
                    if detached:
                        await asyncio.gather(*detached,return_exceptions=True)
                    await self.engine.cleanup_research_owner(owner)
                cleanup_task=asyncio.create_task(cleanup())
                done,_=await asyncio.wait({cleanup_task},timeout=2.0)
                if done:
                    cleanup_task.result()
                else:
                    # A cooperative stalled target close must release the global
                    # tab-management lock. Registry ownership remains intact until
                    # Chrome confirms closure. When extraction itself is still
                    # running, retain the waiter for its eventual owned cleanup.
                    if not any(not task.done() for task in detached):
                        cleanup_task.cancel()
                    self.engine.research_cleanup_tasks.add(cleanup_task)
                    def observe(task):
                        self.engine.research_cleanup_tasks.discard(task)
                        if not task.cancelled(): task.exception()
                    cleanup_task.add_done_callback(observe)
                    if not any(not task.done() for task in detached):
                        await asyncio.wait({cleanup_task},timeout=SEARCH_CLEANUP_TIMEOUT)
                    raise ValueError('research_cleanup_incomplete')
            finalizer=asyncio.create_task(finalize())
            cancelled=False
            try:
                while not finalizer.done():
                    try: await asyncio.shield(finalizer)
                    except asyncio.CancelledError: cancelled=True
                finalizer.result()
                if cancelled: raise asyncio.CancelledError
            finally:
                self.jobs.pop(request_id,None)
                self._streams.pop(job_id,None)

    async def close(self):
        if not self._closed:
            self._closed=True
            tasks=list(self.jobs.values())
            for task in tasks: task.cancel()
            self._close_task=asyncio.gather(*tasks,return_exceptions=True)
        await asyncio.shield(self._close_task)
