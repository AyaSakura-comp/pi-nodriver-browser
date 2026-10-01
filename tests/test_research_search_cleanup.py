"""Cancelled/expired searches remain owned through actual daemon-tab cleanup."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from research.controller import ResearchController
from research.jobs import ResearchConnection
from tests import test_research_browser_ops as browser_fixtures


class SearchCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_and_expired_google_close_is_joined_without_blocking_other_owners(self):
        for stop in ('cancel','expire'):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as root:
                engine,tab=browser_fixtures.BrowserResearchOpsTests().engine()
                engine.background_slots=asyncio.Semaphore(1)
                engine.cleanup_pdf_session=AsyncMock()
                unrelated=SimpleNamespace()
                engine.tab_registry.register(unrelated,'unrelated','page')
                navigating,closing,close_cancelled,release=(asyncio.Event() for _ in range(4))
                search_tasks=[]
                async def create(owner,kind):
                    engine.tab_registry.register(tab,owner,kind)
                    return tab
                async def navigate(url): navigating.set(); await asyncio.Event().wait()
                async def evict(record):
                    self.assertIs(record.page,tab)
                    if not closing.is_set():
                        search_tasks.append(asyncio.current_task())
                        closing.set()
                        try: await release.wait()
                        except asyncio.CancelledError:
                            close_cancelled.set(); raise
                    engine.tab_registry.remove(record.page)
                engine.create_managed_tab=AsyncMock(side_effect=create)
                tab.get.side_effect=navigate
                engine.evict_tab=AsyncMock(side_effect=evict)
                conn=None
                controllers=[]
                def controller(**kwargs):
                    value=ResearchController(**kwargs,callback_timeout=0.03 if stop=='expire' else 10)
                    controllers.append(value)
                    return value
                async def send(frame):
                    view=frame['view']
                    searches=[] if view['tasks'] else [dict(query='q',direction='official',provider='google',addresses=['answer'],parent_task_id=None)]
                    conn.reply(dict(type='planner_reply',id=1,jobId='job',requestId=frame['requestId'],
                        proposal=dict(revision=view['revision'],searches=searches)))
                with patch('research.jobs.ResearchController',side_effect=controller), \
                     patch('research.jobs.SEARCH_CLEANUP_TIMEOUT',0.02,create=True), \
                     patch('worker.BACKGROUND_TAB_CLOSE_TIMEOUT',10,create=True):
                    conn=ResearchConnection(engine,send,Path(root))
                    job=asyncio.create_task(conn.run(1,'owner',dict(jobId='job',question='q',provider='google',searchBudget=1,
                        searchConcurrency=1,layaConcurrency=1,clock={'iso':'2026-01-01T00:00:00Z','timezone':'UTC'})))
                    try:
                        await asyncio.wait_for(navigating.wait(),1)
                        if stop=='cancel': job.cancel()
                        await asyncio.wait_for(closing.wait(),1)
                        done,_=await asyncio.wait({job},timeout=1)
                        self.assertTrue(done,'job lost ownership of the cancelled search close')
                        result=job.result()
                        self.assertEqual(result['status'],'cancelled' if stop=='cancel' else 'incomplete')
                        self.assertTrue(close_cancelled.is_set())
                        self.assertTrue(all(t.done() for t in search_tasks))
                        self.assertFalse(controllers[0].pending_search_callbacks)
                        self.assertFalse(engine.research_cleanup_tasks)
                        self.assertFalse(conn.jobs)
                        await asyncio.wait_for(engine.tab_management_lock.acquire(),1)
                        engine.tab_management_lock.release()
                        await asyncio.wait_for(engine.background_slots.acquire(),1)
                        engine.background_slots.release()
                        self.assertEqual([r.session_id for r in engine.tab_registry.records()],['unrelated'])
                        snapshot=json.loads((Path(root)/'job'/'snapshot.json').read_text())
                        self.assertNotEqual(snapshot['status'],'sufficient')
                    finally:
                        release.set()
                        await asyncio.wait_for(asyncio.gather(job,return_exceptions=True),4)
