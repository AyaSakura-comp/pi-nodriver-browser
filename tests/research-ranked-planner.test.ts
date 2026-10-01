import assert from 'node:assert/strict';
import {test} from 'node:test';
import {ResearchPlanner,hostClock,dateTable} from '../research-model.ts';
const model={id:'fixture',provider:'owner',api:'openai-completions',reasoning:false,contextWindow:32000,maxTokens:4096};
const view={job_id:'job',revision:1,question:'Explain seasons',searchable_gaps:['answer'],tasks:[],budget_remaining:8,provider_slots:['4get','4get','4get','google']};
const queries=Array.from({length:4},(_,i)=>'different query '+i);
function setup(values:any[]){
 const calls:any[]=[];
 const planner=new ResearchPlanner({model,modelRegistry:{complete:async(...args:any[])=>{calls.push(args);return {provider:'owner',model:'fixture',api:'openai-completions',stopReason:'stop',content:[{type:'text',text:JSON.stringify(values.shift())}],usage:{input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}};}}} as never,'job',hostClock());return {planner,calls};
}
test('host owns a 3-fourget 1-google mixed wave; model outputs only keyword queries',async()=>{
 const h=setup([{queries}]);const out=await h.planner.plan(view,['4get','google']);
 assert.deepEqual(out.searches.map(s=>s.provider),['4get','4get','4get','google']);
 const payload=JSON.parse(h.calls[0][1].messages[0].content[0].text);
 assert.equal(payload.max_queries,4);assert.deepEqual(payload.already_searched,[]);
 assert.equal(payload.view,undefined);assert.equal(payload.evidence,undefined);
 assert.deepEqual(out.searches.map(s=>s.addresses),[['answer'],['answer'],['answer'],['answer']]);
 assert.doesNotMatch(h.calls[0][1].systemPrompt,/Otherwise entries|query,direction,provider,addresses/);
});
test('a short ranked wave is accepted without repair and keeps the google slot',async()=>{
 const h=setup([{queries:queries.slice(0,3)}]);const out=await h.planner.plan(view,['4get','google']);
 assert.equal(h.calls.length,1);assert.deepEqual(out.searches.map(s=>s.provider),['4get','4get','google']);
});
test('markdown fences are stripped without a model repair; invalid JSON gets one repair',async()=>{
 const calls:any[]=[];const texts=['```json\n'+JSON.stringify({queries})+'\n```','not json',JSON.stringify({queries})];
 const planner=new ResearchPlanner({model,modelRegistry:{complete:async(...args:any[])=>{calls.push(args);return {provider:'owner',model:'fixture',api:'openai-completions',stopReason:'stop',content:[{type:'text',text:texts.shift()}],usage:{input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}};}}} as never,'job',hostClock());
 assert.equal((await planner.plan(view,['4get','google'])).searches.length,4);assert.equal(calls.length,1);
 assert.equal((await planner.plan(view,['4get','google'])).searches.length,4);assert.equal(calls.length,3);
});
test('duplicate suppression does not shift the google assignment',async()=>{
 const h=setup([{queries}]);const out=await h.planner.plan({...view,tasks:[{task_id:'q0',depth:0,query:queries[0],addresses:['answer']}]},['4get','google']);
 assert.deepEqual(out.searches.map(s=>s.provider),['4get','4get','google']);
 assert.deepEqual(JSON.parse(h.calls[0][1].messages[0].content[0].text).already_searched,[queries[0]]);
});
test('empty wave is permitted for genuinely missing product/location; no filler queries',async()=>{
 const h=setup([{queries:[]}]);const out=await h.planner.plan(view,['4get','google']);assert.deepEqual(out.searches,[]);
});

test('host computes relative dates in the user timezone; the model only copies them',()=>{
 const t=dateTable({iso:'2026-09-29T06:00:00.000Z',timezone:'Asia/Taipei'});
 assert.equal(t.today,'2026-09-29 (週二)');assert.equal(t.tomorrow,'2026-09-30');assert.equal(t.day_after_tomorrow,'2026-10-01');
 assert.equal(t.this_weekend,'2026-10-03 ~ 2026-10-04');assert.equal(t['this_week_週五'],'2026-10-02');assert.equal(t['next_week_週一'],'2026-10-05');
 // 23:30 UTC on the 28th is already the 29th in Taipei
 assert.equal(dateTable({iso:'2026-09-28T23:30:00.000Z',timezone:'Asia/Taipei'}).today,'2026-09-29 (週二)');
 // Sunday belongs to the week that started on Monday
 assert.equal(dateTable({iso:'2026-10-04T04:00:00.000Z',timezone:'Asia/Taipei'}).this_weekend,'2026-10-03 ~ 2026-10-04');
});
test('ranked planner payload carries the date table',async()=>{
 const h=setup([{queries}]);await h.planner.plan(view,['4get','google']);
 const payload=JSON.parse(h.calls[0][1].messages[0].content[0].text);
 assert.ok(payload.dates.tomorrow && payload.dates.this_weekend);
});
