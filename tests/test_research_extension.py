from pathlib import Path
import unittest
from tests import test_google_lens as lens_tests

ROOT=Path(__file__).resolve().parents[1]


class ResearchExtensionTests(unittest.TestCase):
    run_node=lens_tests.LensJavaScriptTests.run_node

    def test_real_transport_keeps_planner_frames_pending_and_disconnect_settles(self):
        source=(ROOT/'index.ts').read_text()
        source=source[source.index('type WorkerResponse ='):source.index('function extractSearchResultUrls')]
        self.run_node('''
import assert from 'node:assert/strict';
import {Duplex} from 'node:stream';
import {createInterface} from 'node:readline';
const MARKER='__PI_NODRIVER__';
class FakeSocket extends Duplex {
  sent=[];
  _read() {}
  _write(chunk,encoding,callback) {this.sent.push(JSON.parse(chunk.toString()));callback();}
}
''' + source + '''
const socket=new FakeSocket(); const worker=new NodriverWorker();
worker.attach(socket);
let frames=0, settled=false;
const promise=worker.sendRequest('research','owner',undefined,{jobId:'job'},()=>frames++);
promise.then(()=>settled=true,()=>settled=true);
await new Promise(setImmediate);
socket.push(MARKER+JSON.stringify({id:1,type:'planner_request',jobId:'job'})+'\\n');
await new Promise(setImmediate);
assert.equal(frames,1); assert.equal(settled,false);
socket.push(MARKER+JSON.stringify({id:1,type:'progress',phase:'evidence',prefix:'source text'})+String.fromCharCode(10));
await new Promise(setImmediate);
assert.equal(frames,2); assert.equal(settled,false,'progress is not terminal');
socket.push(MARKER+JSON.stringify({id:1,type:'result',ok:true,text:'complete'})+'\\n');
assert.equal((await promise).text,'complete');
const pending=worker.sendRequest('research','owner',undefined,{jobId:'job2'});
const observed=pending.then(()=> 'resolved',()=> 'rejected');
await new Promise(setImmediate); worker.disconnect();
await new Promise(setImmediate);
assert.equal(worker.pending.size,0,'disconnect must settle and clear pending requests immediately');
assert.equal(await observed,'rejected');
''',suffix='.mts')

    def test_committed_evidence_is_sent_as_cumulative_progress_updates(self):
        source=(ROOT/'index.ts').read_text()
        source=source[source.index('function extractSearchResultUrls'):].replace('export default function','function register',1)
        self.run_node('''
import assert from 'node:assert/strict';
const Type=new Proxy({}, {get:()=> (...args)=>args[0]});
const readBrowserConfig=()=>({browserMode:'direct'});
const DESCRIPTION='',VISION_FALLBACK_GUIDANCE='',SEARCH_FIRST_URL_RULE='';
const DEFAULT_MAX_LINES=2000,DEFAULT_MAX_BYTES=50*1024;
const randomUUID=()=> 'job';const hostClock=()=>({iso:'2026-01-01T00:00:00Z',timezone:'UTC'});
class ResearchPlanner {usage={totalTokens:0};diagnostics={};}
let prefixes=[];
class NodriverWorker {
 async research(params,_sid,_planner,_signal,onEvidence) {
   (globalThis.researchCalls ??= []).push(params);
   assert.equal(params.evidence,'progressive');
   if (!params.streaming) { assert.equal(params.question,'使用者的問題');assert.deepEqual(params.queries,['q1','q2']); }
   for (const p of prefixes) onEvidence?.(p);
   return {action:'research',jobId:'job',fullEvidenceDelivered:true,text:'stable prefix\\nfooter',authorizations:[],sources:[]};
 }
 async cleanupSession() {} disconnect() {}
 async prefetch(q,i,sid) { (globalThis.prefetched ??= []).push([q,i,sid]); }
 async addQuery(sid,p) { (globalThis.added ??= []).push(p); }
}
''' + source + '''
const tools=new Map(),handlers=new Map();
register({on:(n,h)=>handlers.set(n,h),registerTool:t=>tools.set(t.name,t)});
const log=[];
const onUpdate=u=>log.push(u.content.map(c=>c.text).join(''));
const ctx={getContextUsage:()=>({tokens:1000,contextWindow:200000}),sessionManager:{getSessionId:()=> 'owner',
  getBranch:()=>[{type:'message',message:{role:'user',content:[{type:'text',text:'使用者的問題'}]}}]},
  prefill:{begin:()=>{throw new Error('research must not drive the prefill API');}}};
assert.equal(handlers.has('before_provider_request'),false,'the extension no longer talks to the model itself');
prefixes=['stable','stable prefix','stable prefix'];
const result=await tools.get('research').execute('call-1',{q1:'q1',q2:'q2'},undefined,onUpdate,ctx);
assert.deepEqual(log,['','stable','stable prefix'],'status first (no text), then each longer committed prefix once');
assert.ok(result.content[0].text.startsWith(log.at(-1)),'the result extends the last update');
assert.equal(result.details.prefill,undefined);
const upd=handlers.get('message_update');
const part=(id,name)=>({content:[{type:'toolCall',id,name}]});
const ev=(delta,name='research',id='tc-1',type='toolcall_delta')=>({assistantMessageEvent:{type,contentIndex:0,delta,partial:part(id,name)}});
globalThis.researchCalls=[];globalThis.added=[];log.length=0;prefixes=['stable','stable prefix'];
upd(ev('{'),ctx); upd(ev('"q1": "台積電 收盤價'),ctx);
assert.equal(globalThis.researchCalls.length,0,'an unfinished value starts nothing');
upd(ev(' 2026-09-30", '),ctx);
assert.equal(globalThis.researchCalls.length,1,'the job starts as soon as q1 is complete');
assert.deepEqual(globalThis.researchCalls[0].queries,['台積電 收盤價 2026-09-30']);
assert.equal(globalThis.researchCalls[0].streaming,true);
upd(ev('"q2": "TSMC close"}'),ctx); upd(ev('"q2": "TSMC close"}'),ctx);
assert.deepEqual(globalThis.added.map(p=>p.index),[1],'q2 joins the running job once');
upd(ev('','research','tc-1','toolcall_end'),ctx);
assert.equal(globalThis.added.at(-1).done,true,'the end of the call closes the query stream');
upd(ev('{"q1": "other"}','browser','tc-x'),ctx);
assert.equal(globalThis.researchCalls.length,1,'other tools are ignored');
const adopted=await tools.get('research').execute('tc-1',{q1:'台積電 收盤價 2026-09-30',q2:'TSMC close'},undefined,onUpdate,ctx);
assert.equal(globalThis.researchCalls.length,1,'execute adopts the running job instead of starting another');
assert.deepEqual(log,['','stable prefix'],'evidence committed before adoption is replayed as one update');
assert.ok(adopted.content[0].text.startsWith('stable prefix'));
// A started job whose call is never executed is cancelled at message end.
upd(ev('{"q1": "orphan query", '),ctx);
const orphan=globalThis.researchCalls.length;
handlers.get('message_end')({message:{role:'assistant',stopReason:'stop',content:[]}});
upd(ev('{"q1": "orphan query", ','research','tc-3'),ctx);
assert.equal(globalThis.researchCalls.length,orphan+1,'the session is free again after cancellation');
handlers.get('message_end')({message:{role:'assistant',stopReason:'stop',content:[]}});
log.length=0;
await tools.get('research').execute('call-3',{q1:'q1',q2:'q2'},undefined,undefined,{...ctx,prefill:undefined});
assert.deepEqual(log,[],'no onUpdate callback: nothing is sent');
await handlers.get('input')({text:'台積電收盤價'},ctx);
await handlers.get('tool_result')({toolName:'research',isError:false,content:[{type:'text',text:'evidence'}],details:{}},ctx);
const beforeBlocked=globalThis.researchCalls.length;
upd(ev('{"q1": "blocked follow-up query", ','research','tc-9'),ctx);
assert.equal(globalThis.researchCalls.length,beforeBlocked,'a call the guard will block starts no speculative job');
await handlers.get('input')({text:'幫我再查更多'},ctx);
upd(ev('{"q1": "requested follow-up query", ','research','tc-10'),ctx);
assert.equal(globalThis.researchCalls.length,beforeBlocked+1,'an explicit request for more still starts one');
handlers.get('message_end')({message:{role:'assistant',stopReason:'stop',content:[]}});
''',suffix='.mts')

    def test_stale_query_years_move_to_the_current_year_unless_the_user_asked_for_them(self):
        source=(ROOT/'index.ts').read_text()
        start=source.index('const PAST_INTENT'); end=source.index('export default function')
        self.run_node(source[start:end]+r'''
import assert from 'node:assert/strict';
assert.equal(currentYearQuery('台南活動 2025年10月4日','明天台南還有什麼特別的活動',2026),'台南活動 2026年10月4日');
assert.equal(currentYearQuery('Tainan events October 4 2025','明天台南有什麼活動',2026),'Tainan events October 4 2026');
assert.equal(currentYearQuery('台南 2025 煙火','2025年台南煙火有幾場',2026),'台南 2025 煙火','the user named the year');
assert.equal(currentYearQuery('台南 2025 煙火','去年台南煙火有幾場',2026),'台南 2025 煙火','the user asked about the past');
assert.equal(currentYearQuery('颱風 2015 統計','颱風統計',2026),'颱風 2015 統計','older than five years is left alone');
assert.equal(currentYearQuery('台南 2026 10月','明天台南',2026),'台南 2026 10月');
assert.equal(currentYearQuery('訂單 120251004','查訂單',2026),'訂單 120251004','digits inside a longer number are not a year');
''',suffix='.mts')

    def test_tool_registers_only_typed_exact_sources_and_returns_evidence_once(self):
        source=(ROOT/'index.ts').read_text(); source=source[source.index('function extractSearchResultUrls'):].replace('export default function','function register',1)
        self.run_node('''
import assert from 'node:assert/strict';
const readBrowserConfig = () => ({browserMode:"direct"});
const Type=new Proxy({}, {get:()=> (...args)=>args[0]});
const DESCRIPTION='',VISION_FALLBACK_GUIDANCE='',SEARCH_FIRST_URL_RULE='';
const DEFAULT_MAX_LINES=2000,DEFAULT_MAX_BYTES=50*1024;
const randomUUID=()=> 'job'; const hostClock=()=>({iso:'fixture',timezone:'UTC'});
class ResearchPlanner {usage={totalTokens:5}; diagnostics={modelCalls:2,formatRepairs:1,formatFailures:1};}
let packetBytes=0, actualParams;
class NodriverWorker {
  async research(params) {actualParams=params;return {ok:true,action:'research',jobId:'job',fullEvidenceDelivered:true,
    status:'collected',text:packetBytes ? 'X'.repeat(packetBytes) : 'Full evidence https://invented.example/ ignored',
    sources:[{sourceId:'s1',url:'https://www.example.com/path/?x=%2f#first',discoveries:[{provider:'4get',taskId:'q1'}]}],
    authorizations:[
      {url:'https://www.example.com/path/?x=%2f#first',discoveries:[{provider:'4get',taskId:'q1'}]},
      {url:'https://WWW.EXAMPLE.COM/path/?x=%2f#second',discoveries:[{provider:'google',taskId:'q2'}]}
    ]};}
  async cleanupSession() {} disconnect() {}
 async prefetch(q,i,sid) { (globalThis.prefetched ??= []).push([q,i,sid]); }
}
''' + source + '''
const tools=new Map(),handlers=new Map();
register({on:(n,h)=>handlers.set(n,h),registerTool:t=>tools.set(t.name,t)});
const ctx={model:{},modelRegistry:{},getContextUsage:()=>({tokens:1000,contextWindow:200000}),sessionManager:{getSessionId:()=> 'owner'}};
const tool=tools.get('research');
const result=await tool.execute('call',{q1:'fixture'},undefined,undefined,ctx);
assert.equal(result.content[0].text,'Full evidence https://invented.example/ ignored');
assert.equal(result.details.text,undefined);
assert.deepEqual(result.details.plannerDiagnostics,{modelCalls:2,formatRepairs:1,formatFailures:1});
const open=url=>handlers.get('tool_call')({toolName:'browser',input:{command:'open '+url}},ctx);
assert.equal(open('https://www.example.com/path/?x=%2f#first'),undefined);
assert.equal(open('https://WWW.EXAMPLE.COM/path/?x=%2f#second'),undefined);
assert.equal(open('https://www.example.com/path/?x=%2f#second').block,true);
assert.equal(open('https://www.example.com/path/?x=%2f#invented').block,true);
assert.equal(open('https://www.example.com/path/?x=%2f').block,true);
assert.equal(open('https://example.com/path/?x=%2f').block,true);
assert.equal(open('https://invented.example/').block,true);
assert.equal(actualParams.provider,'auto'); assert.equal(actualParams.searchConcurrency,4);
assert.equal(actualParams.crawlWordBudget,20000); assert.equal(actualParams.rankBatchSize,4);
packetBytes=60000;
const medium=await tool.execute('call',{q1:'fixture'},undefined,undefined,ctx);
assert.equal(medium.details.fullEvidenceDelivered,true,'research handoff must not retain the old 50KiB cap');
assert.equal(medium.content[0].text.length,60000);
packetBytes=2*1024*1024+1;
const large=await tool.execute('call',{q1:'fixture'},undefined,undefined,ctx);
assert.equal(large.details.status,'incomplete'); assert.equal(large.details.fullEvidenceDelivered,false);
assert.equal(large.details.reason,'packet_too_large'); assert.ok(large.content[0].text.length<1000);
''',suffix='.mts')
