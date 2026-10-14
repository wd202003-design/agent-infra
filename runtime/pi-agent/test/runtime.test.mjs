import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,readFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {runPi} from '../session.mjs';
import {imageCall,checksum} from '../transport.mjs';
import {Journal,Worker} from '../worker.mjs';
const binding={base_url:'https://provider.invalid/v1',model:'model-a',input_micro_per_million:1000000,output_micro_per_million:2000000};
const job={task:{id:'task',kind:'generate',instruction:'blue landscape',count:1},attempt:{id:'attempt',epoch:1},context:{brief:'blue',version:1,reference_artifact_ids:[]},dependency_results:[],deadline_at:new Date(Date.now()+60000).toISOString()};
const reply=(message)=>Response.json({choices:[{message}],usage:{prompt_tokens:10,completion_tokens:2}});
const call={role:'assistant',content:null,tool_calls:[{id:'tool-1',type:'function',function:{name:'generate_image',arguments:JSON.stringify({prompt:'blue landscape'})}}]};

test('real Pi session runs only image tool and accounts both model calls',async()=>{
 const original=global.fetch;let calls=0,tools=0;
 global.fetch=async(url,init)=>{assert.equal(url,binding.base_url+'/chat/completions');assert.equal(init.redirect,'error');assert.equal(init.headers.Authorization,'Bearer test-secret-a');const b=JSON.parse(init.body);assert.deepEqual(b.tools.map(t=>t.function.name),['generate_image']);assert.ok(!init.body.includes('test-secret-a'));calls++;return reply(calls===1?call:{role:'assistant',content:'Done'});};
 try{const out=await runPi({job,binding,secret:'test-secret-a',signal:new AbortController().signal,generate:async()=>{tools++;return {artifact_ids:['image-a']};}});assert.deepEqual(out,{cost_micro:28,used_tool:true});assert.equal(tools,1);assert.equal(calls,2);}finally{global.fetch=original;}
});

test('concurrent Pi sessions never cross model, key, or context',async()=>{
 const original=global.fetch;const counts={};let arrivals=0,release;const barrier=new Promise(r=>release=r);
 global.fetch=async(url,init)=>{const b=JSON.parse(init.body),label=b.model;counts[label]=(counts[label]||0)+1;assert.equal(init.headers.Authorization,'Bearer key-'+label);assert.equal(url,'https://'+label+'.invalid/v1/chat/completions');assert.ok(init.body.includes('brief-'+label));assert.ok(!init.body.includes('brief-'+(label==='a'?'b':'a')));if(counts[label]===1){if(++arrivals===2)release();await barrier;return reply(call);}return reply({role:'assistant',content:'Done'});};
 try{const results=await Promise.all(['a','b'].map(label=>runPi({job:{...job,context:{brief:'brief-'+label}},binding:{...binding,model:label,base_url:'https://'+label+'.invalid/v1'},secret:'key-'+label,signal:new AbortController().signal,generate:async()=>({artifact_ids:[label]})})));assert.ok(results.every(r=>r.used_tool));assert.deepEqual(counts,{a:2,b:2});}finally{global.fetch=original;}
});

test('provider failure is never transparently retried',async()=>{
 const original=global.fetch;let calls=0;global.fetch=async()=>{calls++;return new Response('unavailable',{status:502});};
 try{await assert.rejects(runPi({job,binding,secret:'test-key',signal:new AbortController().signal,generate:async()=>assert.fail('must not generate')}),/MODEL_OUTCOME_UNKNOWN/);assert.equal(calls,1);}finally{global.fetch=original;}
});

test('image response rejects URLs without fetching them',async()=>{
 const original=global.fetch;let calls=0;global.fetch=async()=>{calls++;return Response.json({data:[{url:'http://169.254.169.254/'}]});};
 try{await assert.rejects(imageCall(binding,'test-key',{id:'op',kind:'generate',count:1},[],new AbortController().signal),/INVALID_IMAGE_RESPONSE/);assert.equal(calls,1);}finally{global.fetch=original;}
});

test('durable restart abandons an existing job without replaying provider effects',async()=>{
 const dir=await mkdtemp(join(tmpdir(),'pi-journal-'));const journal=new Journal(dir);await journal.init();const calls=[];
 try{await journal.save(0,{job});const worker=new Worker({post:async(path)=>{calls.push(path);return {state:'needs_review'};}},journal,{pi:async()=>assert.fail('Pi replay'),provider:async()=>assert.fail('provider replay')});await worker.tick(0);assert.deepEqual(calls,['/runner/attempts/attempt/abandon']);assert.equal(await journal.load(0),null);}finally{await rm(dir,{recursive:true,force:true});}
});

test('worker commits images before completion and never journals provider credentials',async()=>{
 const dir=await mkdtemp(join(tmpdir(),'pi-worker-'));const journal=new Journal(dir);await journal.init();let operations=0,upload=0;const calls=[];
 const control={post:async(path,value)=>{calls.push(path);if(path==='/runner/claims')return job;if(path.endsWith('/renew'))return {authorized:true,lease_expires_at:job.deadline_at};if(path.endsWith('/image-operations'))return {...value,id:'op'+(++operations)};if(path.endsWith('/credentials:issue'))return {binding:{...binding,reserve_micro:100},secret:'provider-secret-never-on-disk'};if(path.endsWith('/uploads'))return {artifact_id:'art'+(++upload),upload_url:'https://storage.invalid/put'};if(path.endsWith('/complete')){assert.equal(value.outcome,'succeeded');assert.deepEqual(value.artifact_ids,['art1']);}return {};}};
 const original=global.fetch;global.fetch=async()=>new Response('',{headers:{'x-amz-version-id':'v1'}});
 try{const worker=new Worker(control,journal,{pi:async({generate})=>{assert.ok(!(await readFile(journal.path(0),'utf8')).includes('provider-secret'));await generate('tool','blue');return {used_tool:true,cost_micro:20};},provider:async()=>[{bytes:Buffer.from('png'),sha256:checksum(Buffer.from('png')),media_type:'image/png'}]});await worker.tick(0);assert.equal(operations,2);assert.ok(calls.findIndex(p=>p.endsWith('/commit'))<calls.findIndex(p=>p.endsWith('/complete')));assert.equal(await journal.load(0),null);}finally{global.fetch=original;await rm(dir,{recursive:true,force:true});}
});
