import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ResearchPlanner, hostClock } from '../research-model.ts';

const model = { id:'fixture',provider:'owner',api:'openai-completions',reasoning:false,
  contextWindow:32000,maxTokens:4096 };
const baseView = {job_id:'job',revision:7,question:'Verify the requested fact',requirements:[{requirement_id:'r1'}],
  gaps:['r1'],searchable_gaps:['r1'],pending_addresses:[],tasks:[],evidence:[],sources:[],budget_remaining:3};
const draft = {searches:[{query:'official requested fact',direction:'official',provider:'4get',addresses:['r1']}]};
function response(value: unknown = draft) {
  return {provider:model.provider,model:model.id,api:model.api,stopReason:'stop',
    content:[{type:'text',text:typeof value==='string'?value:JSON.stringify(value)}],
    usage:{input:2,output:3,cacheRead:0,cacheWrite:0,totalTokens:5,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}};
}
function harness(responses: unknown[]) {
  const calls: any[][]=[];
  const registry={complete:async (...args: any[])=>{
    calls.push(args);
    if (!responses.length) throw new Error('unexpected extra model request');
    return responses.shift();
  }};
  return {calls,registry,planner:new ResearchPlanner({model,modelRegistry:registry} as never,'job',hostClock())};
}

test('post-crawl feedback and all topic/query history reach planner without runtime revision', async()=>{
  const v={...baseView,tasks:[{task_id:'q1',depth:0,addresses:['r1'],query:'first terms',direction:'clock elapsed time',provider:'4get'}],
    review_feedback:{revision:6,needs_more_search:true,probability_true:.8,reason:''}};
  const h=harness([response()]);await h.planner.plan(v,['4get']);
  const context=h.calls[0][1];const payload=JSON.parse(context.messages[0].content[0].text);
  assert.equal(payload.view.review_feedback.needs_more_search,true);
  assert.equal(payload.view.review_feedback.revision,undefined);
  assert.equal(payload.view.search_history[0].direction,'clock elapsed time');
  assert.equal(payload.view.search_history[0].query,'first terms');
  assert.match(context.systemPrompt,/parallel/);
  assert.match(context.systemPrompt,/different/i);
});

test('search planner has no finish authority regardless of review feedback', async()=>{
  for (const needs_more_search of [true,null]) {
    const v={...baseView,review_feedback:{revision:7,needs_more_search,probability_true:null,reason:'fixture'}};
    const h=harness([response({...draft,finish:true})]);
    await assert.rejects(h.planner.plan(v,['4get']),/research_invalid_plan/);
    assert.equal(h.calls.length,1);
  }
});

test('model draft contains no runtime metadata; host binds original revision and root parent', async()=>{
  const h=harness([response()]);
  const out=await h.planner.plan(baseView,['4get']);
  assert.equal(out.revision,7);
  assert.equal(out.searches[0].parent_task_id,null);
  assert.equal(h.calls.length,1);
  const payload=JSON.parse(h.calls[0][1].messages[0].content[0].text);
  assert.equal(payload.view.job_id,undefined);
  assert.equal(payload.view.revision,undefined);
  assert.equal(payload.view.tasks,undefined);
  assert.equal(payload.view.question,baseView.question);
});

test('host selects newest deepest relevant predecessor, not a model-generated ID', async()=>{
  const v={...baseView,tasks:[
    {task_id:'q1',depth:0,addresses:['r1'],query:'root'},
    {task_id:'q2',depth:3,addresses:['r2'],query:'unrelated'},
    {task_id:'q3',depth:1,addresses:['r1'],query:'older'},
    {task_id:'q4',depth:1,addresses:['r1'],query:'latest'},
  ]};
  const h=harness([response()]);const out=await h.planner.plan(v,['4get']);
  assert.equal(out.searches[0].parent_task_id,'q4');
  const payload=JSON.parse(h.calls[0][1].messages[0].content[0].text);
  assert.equal(payload.view.search_history.length,4);
  assert.equal(payload.view.search_history[3].query,'latest');
  assert.equal(payload.view.search_history[3].task_id,undefined);
});

test('a previously unsearched gap uses a deterministic existing round predecessor', async()=>{
  const h=harness([response()]);
  const out=await h.planner.plan({...baseView,tasks:[{task_id:'q1',depth:0,addresses:['other']},
    {task_id:'q2',depth:1,addresses:['other']}]},['4get']);
  assert.equal(out.searches[0].parent_task_id,'q2');
});

test('malformed JSON gets exactly one same-model repair and both usages count', async()=>{
  const h=harness([response('```json\nnot valid JSON\n```'),response()]);
  const out=await h.planner.plan(baseView,['4get']);
  assert.equal(out.revision,7);assert.equal(h.calls.length,2);
  assert.equal(h.calls[0][0],h.calls[1][0]);
  assert.equal(h.calls[0][2].signal,h.calls[1][2].signal,'repair must share the original deadline');
  assert.equal(h.calls[1][2].maxRetries,0);
  const payload=JSON.parse(h.calls[1][1].messages[0].content[0].text);
  assert.equal(payload.view.question,baseView.question);
  assert.equal(payload.format_repair.previous_output,'```json\nnot valid JSON\n```');
  assert.equal(h.planner.usage.totalTokens,10);
  assert.deepEqual(h.planner.diagnostics,{modelCalls:2,formatRepairs:1,formatFailures:1});
});

test('missing required shape may repair but is not silently defaulted', async()=>{
  const h=harness([response({}),response()]);
  await h.planner.plan(baseView,['4get']);assert.equal(h.calls.length,2);
});

test('ordinary extra schema fields may be removed by one repair, not silently accepted', async()=>{
  const h=harness([response({...draft,explanation:'format mistake'}),response()]);
  await h.planner.plan(baseView,['4get']);assert.equal(h.calls.length,2);
});

test('old or forged runtime fields cannot override host metadata', async()=>{
  const legacy={...draft,revision:999,searches:[{...draft.searches[0],parent_task_id:'invented'}]};
  const h=harness([response(legacy),response()]);
  const out=await h.planner.plan(baseView,['4get']);
  assert.equal(out.revision,7);assert.equal(out.searches[0].parent_task_id,null);
  assert.equal(h.calls.length,2);
});

test('repair failure stops after two requests, without dispatching another plan', async()=>{
  const h=harness([response('bad JSON'),response('still bad JSON'),response()]);
  await assert.rejects(h.planner.plan(baseView,['4get']),/research_plan_format_error/);
  assert.equal(h.calls.length,2);assert.equal(h.planner.usage.totalTokens,10);
});

test('fabricated evidence, requirements, providers and navigation fields are not repairable', async()=>{
  const bad=[
    {...draft,assessments:[{requirement_id:'r1',evidence_ids:['invented'],contradiction_ids:[]}]},
    {...draft,searches:[{...draft.searches[0],addresses:['invented']}]},
    {...draft,searches:[{...draft.searches[0],provider:'evil'}]},
    {...draft,url:'https://invented.invalid/page'},
  ];
  for (const value of bad) {
    const h=harness([response(value),response()]);
    await assert.rejects(h.planner.plan(baseView,['4get']),/research_invalid_plan/);
    assert.equal(h.calls.length,1);
  }
});

test('format failure plus fabricated evidence still cannot trigger a repair', async()=>{
  const h=harness([response({searches:[],assessments:[{requirement_id:'r1',evidence_ids:['made-up'],contradiction_ids:[]}]}),response()]);
  await assert.rejects(h.planner.plan(baseView,['4get']),/research_invalid_plan/);
  assert.equal(h.calls.length,1);
});

test('navigation fields cannot hide behind a missing top-level field or earlier malformed item', async()=>{
  const bad=[
    {searches:[{...draft.searches[0],url:'https://invented.invalid'}],assessments:[]},
    {...draft,searches:[null,{...draft.searches[0],url:'https://invented.invalid'}]},
    {searches:[],assessments:[{url:'https://invented.invalid'}]},
  ];
  for (const value of bad) {
    const h=harness([response(value),response()]);
    await assert.rejects(h.planner.plan(baseView,['4get']),/research_invalid_plan/);
    assert.equal(h.calls.length,1);
  }
});

test('matching-identity length-terminated repair still accounts for its reported usage', async()=>{
  const h=harness([response('bad'),{...response(),stopReason:'length'},response()]);
  await assert.rejects(h.planner.plan(baseView,['4get']),/research_invalid_model_response/);
  assert.equal(h.calls.length,2);
  assert.equal(h.planner.usage.totalTokens,10);
});

test('unknown references on repaired output remain rejected', async()=>{
  const bad={...draft,assessments:[{requirement_id:'r1',evidence_ids:['invented'],contradiction_ids:[]}]};
  const h=harness([response('bad'),response(bad)]);
  await assert.rejects(h.planner.plan(baseView,['4get']),/research_invalid_plan/);
  assert.equal(h.calls.length,2);
});

test('request snapshot is captured before await; repair cannot rebind to mutated live state', async()=>{
  const v:any=structuredClone(baseView);v.tasks=[{task_id:'q1',depth:0,addresses:['r1']}];
  const providers=['4get'];let calls=0;
  const planner=new ResearchPlanner({model,modelRegistry:{complete:async()=>{
    calls++;
    if(calls===1){v.revision=99;v.tasks[0].task_id='changed';providers[0]='google';return response('bad');}
    return response();
  }}} as never,'job',hostClock());
  const out=await planner.plan(v,providers);
  assert.equal(out.revision,7);assert.equal(out.searches[0].parent_task_id,'q1');
});

test('returned-model substitution or provider errors never trigger format repair', async()=>{
  const h=harness([{...response(),model:'another-model'},response()]);
  await assert.rejects(h.planner.plan(baseView,['4get']),/research_model_identity_mismatch/);
  assert.equal(h.calls.length,1);
  let calls=0;
  const planner=new ResearchPlanner({model,modelRegistry:{complete:async()=>{calls++;throw new Error('SECRET_AUTH');}}} as never,'job',hostClock());
  await assert.rejects(planner.plan(baseView,['4get']),e=>/research_planner_failed/.test(String(e))&&!String(e).includes('SECRET_AUTH'));
  assert.equal(calls,1);
});

test('cancellation during repair cancels both attempts under one lifetime', async()=>{
  const controller=new AbortController();let entered:()=>void=()=>{};
  const ready=new Promise<void>(r=>entered=r);let calls=0;const signals:AbortSignal[]=[];
  const planner=new ResearchPlanner({model,modelRegistry:{complete:async(_m:any,_c:any,opts:any)=>{
    signals.push(opts.signal);calls++;
    if(calls===1)return response('bad');
    entered();return new Promise(()=>{});
  }}} as never,'job',hostClock());
  const promise=planner.plan(baseView,['4get'],controller.signal);
  await ready;controller.abort();
  await assert.rejects(promise,/research_planner_cancelled/);
  assert.equal(calls,2);assert.equal(signals[0],signals[1]);assert.equal(signals[1].aborted,true);
});

test('repair input cap is enforced without clipping prior output or evidence', async()=>{
  const h=harness([response('bad-json'.repeat(2300)),response()]);
  await assert.rejects(h.planner.plan({...baseView,question:'x'.repeat(8000)},['4get']),/research_planner_input_too_large/);
  assert.equal(h.calls.length,1);
});
