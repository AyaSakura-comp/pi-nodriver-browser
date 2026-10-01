"""Real temporary Unix socket dispatch, fake engine/providers, no service startup."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import worker
from research.contracts import SearchResult
from research.controller_contracts import Judgment, SourceAction
from research.jobs import ResearchConnection


class ResearchDaemonTests(unittest.IsolatedAsyncioTestCase):
    async def test_research_bypasses_interactive_lock_and_disconnect_cancels_owned_job(self):
        started, held, release, cleanup = (asyncio.Event() for _ in range(4))
        class Provider:
            async def search(self,task): return [SearchResult('Fixture','https://example.com/','Complete answer')]
            async def close(self): pass
        async def judge(req): return Judgment(req.view.revision,tuple(SourceAction(s.source_id,'use_snippet',('answer',)) for s in req.sources))
        engine=SimpleNamespace(close=AsyncMock(),cleanup_research_owner=AsyncMock(side_effect=lambda owner: cleanup.set()),research_cleanup_tasks=set())
        async def command(engine,request):
            if request['command']=='hold': held.set(); await release.wait()
            return dict(id=request['id'],ok=True,text='fixture',action=request['command'])
        actual_start=asyncio.start_unix_server
        async def start(*args,**kwargs):
            server=await actual_start(*args,**kwargs); started.set(); return server
        def connection(*args,**kwargs): return ResearchConnection(*args,**kwargs,fourget_factory=Provider,judge=judge)
        loop=asyncio.get_running_loop()
        with tempfile.TemporaryDirectory() as root, patch.object(worker,'BrowserWorker',return_value=engine), \
             patch.object(worker,'execute_request',side_effect=command), patch('research.jobs.ResearchConnection',side_effect=connection), \
             patch.object(asyncio,'start_unix_server',side_effect=start), patch.object(loop,'add_signal_handler'):
            socket_path=str(Path(root)/'daemon.sock')
            server_task=asyncio.create_task(worker.server_main(socket_path))
            await asyncio.wait_for(started.wait(),1)
            reader,writer=await asyncio.open_unix_connection(socket_path)
            async def send(value): writer.write(json.dumps(value).encode()+b'\n'); await writer.drain()
            async def read():
                line=await asyncio.wait_for(reader.readline(),3)
                self.assertTrue(line.startswith(worker.MARKER.encode()))
                return json.loads(line[len(worker.MARKER):])
            try:
                await send(dict(id=1,sessionId='owner',command='hold'))
                await asyncio.wait_for(held.wait(),1)
                params=dict(jobId='job',question='question',provider='4get',searchBudget=1,searchConcurrency=1,layaConcurrency=1,
                            clock={'iso':'2026-01-01T00:00:00Z','timezone':'UTC'})
                await send(dict(id=2,sessionId='owner',command='research',research=params))
                frames=[]
                while True:
                    frame=await read(); frames.append(frame)
                    if frame.get('type')=='planner_request':
                        view=frame['view']
                        searches=[] if view['tasks'] else [dict(query='q',direction='official',provider='4get',addresses=['answer'],parent_task_id=None)]
                        self.assertNotIn('evidence',view)
                        await send(dict(type='planner_reply',id=2,jobId='job',requestId=frame['requestId'],proposal=dict(revision=view['revision'],searches=searches)))
                    else:
                        self.assertTrue(frame['ok']); self.assertEqual(frame['id'],2)
                        self.assertTrue(frame['fullEvidenceDelivered']); self.assertIn('Complete answer',frame['text']); break
                self.assertFalse(release.is_set())
                self.assertTrue(cleanup.is_set())
                # Start another job, disconnect while its first planner is pending.
                cleanup.clear(); params['jobId']='disconnected'
                await send(dict(id=3,sessionId='owner',command='research',research=params))
                self.assertEqual((await read())['type'],'planner_request')
                writer.close(); await writer.wait_closed()
                await asyncio.wait_for(cleanup.wait(),3)
                snapshots=list((Path(root)/'research-artifacts'/'disconnected').glob('snapshot.json'))
                self.assertEqual(json.loads(snapshots[0].read_text())['status'],'cancelled')
            finally:
                release.set(); writer.close()
                # Stop this fixture's server only, through its temporary socket.
                stop_reader,stop_writer=await asyncio.open_unix_connection(socket_path)
                stop_writer.write(b'{"id":99,"command":"shutdown"}\n'); await stop_writer.drain()
                await asyncio.wait_for(stop_reader.readline(),2)
                stop_writer.close(); await stop_writer.wait_closed()
                await asyncio.wait_for(server_task,3)
