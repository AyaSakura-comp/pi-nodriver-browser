import assert from 'node:assert/strict';
import {test} from 'node:test';
import {ResearchPlanner,hostClock} from '../research-model.ts';
const model={id:'fixture',provider:'owner',api:'openai-completions',reasoning:false,contextWindow:32000,maxTokens:4096};
const view={job_id:'job',revision:4,question:'Which clock includes sleep?',requirements:[{requirement_id:'answer'}],gaps:['answer'],searchable_gaps:['answer'],pending_addresses:[],tasks:[],evidence:[],sources:[],budget_remaining:3};
const search={query:'Python clock includes sleep',direction:'Compare elapsed-time clocks',provider:'4get',addresses:['answer']};
function fixture(value:unknown={searches:[search]}) {
 const calls:any[]=[];
 const planner=new ResearchPlanner({model,modelRegistry:{complete:async (...args:any[])=>{
  calls.push(args);return {provider:'owner',model:'fixture',api:'openai-completions',stopReason:'stop',content:[{type:'text',text:JSON.stringify(value)}],usage:{input:2,output:3,cacheRead:0,cacheWrite:0,totalTokens:5,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}};
 }}} as never,'job',hostClock());
 return {planner,calls};
}
test('search-only draft has no evidence-assessment or finish authority',async()=>{
 const h=fixture();const out=await h.planner.plan(view,['4get']);
 assert.deepEqual(Object.keys(out).sort(),['revision','searches']);
 assert.equal(out.searches[0].parent_task_id,null);
 const prompt=h.calls[0][1].systemPrompt;
 assert.match(prompt,/exactly searches/);
 assert.doesNotMatch(prompt,/Finish only|independently verify evidence|assessments entries/);
});
test('large articles, source descriptions and evidence refs never enter model input',async()=>{
 const h=fixture();
 await h.planner.plan({...view,requirements:[{requirement_id:'answer',evidence_ids:['SECRET_REFERENCE']}],evidence:[{event_id:'e1',text:'ARTICLE_SENTINEL'.repeat(100000)}],sources:[{source_id:'s1',title:'SOURCE_SENTINEL',descriptions:['SNIPPET_SENTINEL'.repeat(10000)]}]} as never,['4get']);
 assert.equal(h.calls.length,1);
 const context=h.calls[0][1];const text=context.messages[0].content[0].text;const p=JSON.parse(text).view;
 assert.ok(text.length<2000);
 for (const k of ['evidence','sources','requirements'])assert.equal(p[k],undefined);
 assert.doesNotMatch(text,/ARTICLE_SENTINEL|SOURCE_SENTINEL|SNIPPET_SENTINEL|SECRET_REFERENCE/);
});
test('full query/direction history survives with only compact more-search feedback',async()=>{
 const h=fixture();const tasks=[{task_id:'q1',depth:0,query:'previous query',direction:'previous direction',provider:'4get',addresses:['answer']}];
 await h.planner.plan({...view,tasks,review_feedback:{revision:3,needs_more_search:true,reason:'',probability_true:.8}} as never,['4get']);
 const p=JSON.parse(h.calls[0][1].messages[0].content[0].text).view;
 assert.equal(p.search_history[0].query,'previous query');assert.equal(p.search_history[0].direction,'previous direction');
 assert.equal(p.review_feedback.needs_more_search,true);assert.equal(p.review_feedback.revision,undefined);
});
test('duplicate queries are filtered by host, including case whitespace and another provider',async()=>{
 const h=fixture({searches:[search,{...search,query:'Fresh clock alternative'}, {...search,query:' fresh   CLOCK alternative ',provider:'google'}]});
 const tasks=[{task_id:'q1',depth:0,query:' python   CLOCK includes SLEEP ',direction:'already tried',provider:'google',addresses:['answer']}];
 const out=await h.planner.plan({...view,tasks},['4get','google']);
 assert.deepEqual(out.searches.map(s=>s.query),['Fresh clock alternative']);
});
test('former finish and assessments fields are rejected, not silently granted or repaired',async()=>{
 for(const extra of [{finish:true},{assessments:[]},{evidence_ids:['e1']}]){
  const h=fixture({searches:[],...extra});
  await assert.rejects(h.planner.plan(view,['4get']),/research_invalid_plan/);
  assert.equal(h.calls.length,1);
 }
});
