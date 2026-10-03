import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ResearchPlanner, thinkingOptions, hostClock } from '../research-model.ts';

const model = { id: 'fixture', provider: 'owner-provider', api: 'openai-completions', reasoning: false,
  baseUrl: 'http://fixture.invalid', maxTokens: 4096, contextWindow: 32000 };
const view = { job_id: 'job', revision: 0, question: 'fixture', requirements: [{requirement_id:'r1'}],
  gaps: ['r1'], searchable_gaps: ['r1'], pending_addresses: [], sources: [], tasks: [], evidence: [], budget_remaining: 2 };
const draft = {searches:[{query:'fixture',direction:'official',provider:'4get',addresses:['r1']}]};
const plan = {...draft,revision:0,searches:draft.searches.map(s=>({...s,parent_task_id:null}))};
const message = (value: unknown = draft) => ({provider:model.provider,model:model.id,api:model.api,stopReason:'stop',content:[{type:'text',text:JSON.stringify(value)}],
  usage:{input:2,output:3,cacheRead:0,cacheWrite:0,totalTokens:5,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}});

test('pins current model and uses host auth registry with short tool-free context', async () => {
  const calls: unknown[][] = [];
  const ctx = {model: structuredClone(model), modelRegistry:{complete: async (...args: unknown[]) => {calls.push(args); return message();}}};
  const planner = new ResearchPlanner(ctx as never, 'job', hostClock(() => new Date('2026-01-01T00:00:00Z')));
  ctx.model.id = 'changed';
  assert.deepEqual(await planner.plan(view, ['4get']), plan);
  const [selected, context, options] = calls[0] as any[];
  assert.equal(selected.id, 'fixture'); assert.equal(selected.provider, model.provider);
  assert.equal(selected.baseUrl,model.baseUrl);
  assert.equal(context.messages.length, 1); assert.equal(context.tools, undefined);
  assert.equal(options.maxTokens, 2048); assert.equal(options.maxRetries, 0);
  assert.equal(options.apiKey, undefined); assert.ok(options.signal);
  assert.equal(planner.usage.totalTokens, 5);
});

test('planner prompt requests semantic content and required gap references, not runtime metadata', async () => {
  let prompt = '';
  const planner = new ResearchPlanner({model,modelRegistry:{complete:async (_m: unknown,context: {systemPrompt: string})=>{
    prompt=context.systemPrompt;return message();
  }}} as never,'job',hostClock());
  await planner.plan(view,['4get']);
  assert.match(prompt,/exactly searches/);
  assert.match(prompt,/addresses MUST be a nonempty array/);
  assert.match(prompt,/view\.searchable_gaps/);
  assert.match(prompt,/No Markdown fences or runtime metadata/);
  assert.doesNotMatch(prompt,/Copy view\.revision|parent_task_id/);
});

test('fallback descriptors fail before transport without mutating the invoking model', async () => {
  for (const compat of [
    {allowedFallbackModels:[{provider:'owner-provider',model:'substitute'}]},
    {allowedFallbackModels:[]},
    {openRouterRouting:{allow_fallbacks:true}},
    {openRouterRouting:{order:['first','backup']}},
    {vercelGatewayRouting:{order:['first','backup']}},
  ]) {
    let calls=0;
    const selected={...model,api:'anthropic-messages',compat};
    const original=structuredClone(selected);
    const planner=new ResearchPlanner({model:selected,modelRegistry:{complete:async()=>{calls++;return message();}}} as never,
      'job',hostClock());
    await assert.rejects(planner.plan(view,['4get']), /research_model_fallback_unsupported/);
    assert.equal(calls,0);
    assert.deepEqual(selected,original);
  }
});

test('substituted or missing response identities fail without permissive alias matching', async () => {
  for (const identity of [{model:'substitute'}, {model:'fixture-version'}, {provider:'substitute'},
      {api:'anthropic-messages'}, {model:undefined}, {provider:undefined}]) {
    const planner=new ResearchPlanner({model,modelRegistry:{complete:async()=>({...message(),...identity})}} as never,'job',hostClock());
    await assert.rejects(planner.plan(view,['4get']), /research_model_identity_mismatch/);
  }
});

test('API-specific off controls and unsupported combinations fail closed', () => {
  assert.deepEqual(thinkingOptions({...model, api:'anthropic-messages'} as never), {thinkingEnabled:false});
  assert.deepEqual(thinkingOptions({...model, api:'google-generative-ai'} as never), {thinking:{enabled:false}});
  assert.deepEqual(thinkingOptions({...model, api:'openai-responses'} as never), {});
  for (const bad of [{...model,api:'unknown'}, {...model,reasoning:true},
      {...model,thinkingLevelMap:{off:null}}, {...model,samplingParams:{max_tokens:999999}}]) {
    assert.throws(() => thinkingOptions(bad as never), /research_/);
  }
});

test('rejects persistent malformed runtime fields, unknown references and error stops without leaking bodies', async () => {
  for (const response of [message({...plan,revision:2}),message({...plan,unexpected:'x'}),message({...plan,searches:[{...plan.searches[0],addresses:['invented']}]}),
       {...message(),stopReason:'length'}, {...message(),stopReason:'error',errorMessage:'PRIVATE_API_KEY'},
       {...message(),content:[{type:'toolCall',name:'open',arguments:{}}]}]) {
    const planner = new ResearchPlanner({model,modelRegistry:{complete:async()=>response}} as never,'job',hostClock());
    await assert.rejects(planner.plan(view,['4get']), error => /research_/.test(String(error)) && !String(error).includes('PRIVATE_API_KEY'));
  }
});

test('in-flight cancellation reaches the direct provider and abandons a non-cooperative result', async () => {
  let started: () => void = () => {};
  const entered = new Promise<void>(resolve => {started=resolve;});
  let providerSignal: AbortSignal | undefined;
  const planner = new ResearchPlanner({model,modelRegistry:{complete:async (_model: unknown,_context: unknown,options: {signal: AbortSignal}) => {
    providerSignal=options.signal; started(); return await new Promise(() => {});
  }}} as never,'job',hostClock());
  const controller=new AbortController();
  const pending=planner.plan(view,['4get'],controller.signal);
  await entered; controller.abort();
  await assert.rejects(pending,/research_planner_cancelled/);
  assert.equal(providerSignal?.aborted,true);
});

test('bounded request rejects oversize without model dispatch and cancellation propagates', async () => {
  let calls = 0;
  const planner = new ResearchPlanner({model,modelRegistry:{complete:async()=>{calls++;return message();}}} as never,'job',hostClock());
  await assert.rejects(planner.plan({...view,question:'x'.repeat(100000)},['4get']), /research_planner_input_too_large/);
  const signal = AbortSignal.abort();
  await assert.rejects(planner.plan(view,['4get'],signal), /research_planner_cancelled/);
  assert.equal(calls,0);
});

test('a model the planner cannot drive (e.g. openai-codex) can still run research with agent-written queries', async () => {
  let calls=0;
  const codex={...model,provider:'openai-codex',api:'openai-codex-responses',reasoning:true};
  const planner=new ResearchPlanner({model:codex,modelRegistry:{complete:async()=>{calls++;return message();}}} as never,'job',hostClock());
  assert.deepEqual(planner.diagnostics,{modelCalls:0,formatRepairs:0,formatFailures:0});
  await assert.rejects(planner.plan(view,['4get']), /research_planner_api_unsupported/);
  assert.equal(calls,0);
});
