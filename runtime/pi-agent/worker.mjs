import {randomUUID} from 'node:crypto';
import {mkdir,open,readFile,rename,unlink} from 'node:fs/promises';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {runPi} from './session.mjs';
import {bounded,checksum,httpsUrl,imageCall,jsonFetch} from './transport.mjs';

export class Control {
  constructor(url,token){this.url=httpsUrl(url);this.token=token;}
  async post(path,value,key=randomUUID()){
    const result=await jsonFetch(this.url+'/v3'+path,{method:'POST',headers:{Authorization:'Bearer '+this.token,'Content-Type':'application/json','Idempotency-Key':key},body:JSON.stringify(value),signal:AbortSignal.timeout(10000)});
    return result.data;
  }
}
export class Journal {
  constructor(directory){this.directory=directory;}
  async init(){await mkdir(this.directory,{recursive:true,mode:0o700});}
  path(slot){return join(this.directory,`slot-${slot}.json`);}
  async load(slot){try{return JSON.parse(await readFile(this.path(slot),'utf8'));}catch(e){if(e.code==='ENOENT')return null;throw e;}}
  async save(slot,value){const temp=this.path(slot)+'.tmp';const file=await open(temp,'w',0o600);try{await file.writeFile(JSON.stringify(value));await file.sync();}finally{await file.close();}await rename(temp,this.path(slot));await this.sync();}
  async sync(){const directory=await open(this.directory,'r');try{await directory.sync();}finally{await directory.close();}}
  async remove(slot){await unlink(this.path(slot));await this.sync();}
}
export class Worker {
  constructor(control,journal,{slots=1,pi=runPi,provider=imageCall}={}){this.control=control;this.journal=journal;this.slots=slots;this.pi=pi;this.provider=provider;}
  fence(job){return {task_id:job.task.id,epoch:job.attempt.epoch};}
  async abandon(job){return this.control.post(`/runner/attempts/${job.attempt.id}/abandon`,this.fence(job));}
  async download(job,id,signal){
    const meta=await this.control.post(`/runner/attempts/${job.attempt.id}/artifacts/${id}/download-url`,this.fence(job));
    const u=new URL(meta.url);if(u.protocol!=='https:')throw new Error('STORAGE_TLS_REQUIRED');
    const bytes=await bounded(await fetch(u,{redirect:'error',signal}),20971520);
    if(checksum(bytes)!==meta.sha256||bytes.length!==meta.size_bytes)throw new Error('ARTIFACT_MISMATCH');
    return {bytes,media_type:meta.media_type,id};
  }
  async upload(job,op,image,signal,index=0){
    const value=await this.control.post(`/runner/attempts/${job.attempt.id}/uploads`,{...this.fence(job),operation_id:op.id,size_bytes:image.bytes.length,sha256:image.sha256,media_type:image.media_type},op.id+':output:'+index);
    if(new URL(value.upload_url).protocol!=='https:')throw new Error('STORAGE_TLS_REQUIRED');
    const response=await fetch(value.upload_url,{method:'PUT',headers:{'Content-Type':image.media_type},body:image.bytes,redirect:'error',signal});
    if(!response.ok)throw new Error('UPLOAD_FAILED');
    const version=response.headers.get('x-amz-version-id');if(!version||version==='null')throw new Error('STORAGE_VERSION_REQUIRED');
    await this.control.post(`/runner/attempts/${job.attempt.id}/artifacts/${value.artifact_id}/commit`,{...this.fence(job),version_id:version},value.artifact_id);
    return value.artifact_id;
  }
  async operation(job,kind,toolCallId,prompt='',references=[]){
    const op=await this.control.post(`/runner/attempts/${job.attempt.id}/image-operations`,{...this.fence(job),operation_id:randomUUID(),tool_call_id:toolCallId,kind,prompt,count:kind==='agent'?1:job.task.count,reference_artifact_ids:references});
    const credential=await this.control.post(`/runner/attempts/${job.attempt.id}/credentials:issue`,{...this.fence(job),operation_id:op.id});
    await this.control.post(`/runner/image-operations/${op.id}/dispatch`,this.fence(job),op.id);
    return {op,credential};
  }
  async report(job,op,state,cost=null){return this.control.post(`/runner/image-operations/${op.id}/report`,{...this.fence(job),state,actual_cost_micro:cost},op.id+':'+state);}
  async execute(job){
    const controller=new AbortController();const signal=controller.signal;let pulseBusy=false;let output=[],selected=[],summary='';
    let deadlineTimer=setTimeout(()=>controller.abort(),Math.max(0,Date.parse(job.deadline_at)-Date.now()));
    const renew=async()=>{const r=await this.control.post(`/runner/attempts/${job.attempt.id}/renew`,this.fence(job));if(!r.authorized||r.stop_requested||Date.parse(r.lease_expires_at)<=Date.now())throw new Error('STOPPED');};
    const pulse=setInterval(async()=>{if(pulseBusy)return;pulseBusy=true;try{await renew();}catch{controller.abort();}finally{pulseBusy=false;}},5000);
    let agent;
    try{
      await renew();
      const refIds=job.task.kind==='selection'?job.dependency_results.flatMap(d=>d.artifact_ids):job.context.reference_artifact_ids;
      const refs=[];let total=0;
      for(const id of refIds){const ref=await this.download(job,id,signal);total+=ref.bytes.length;if(total>25165824||refs.length>=16)throw new Error('CONTEXT_IMAGE_LIMIT');refs.push(ref);}
      agent=await this.operation(job,'agent','pi-session');
      const answer=await this.pi({job,binding:agent.credential.binding,secret:agent.credential.secret,signal,
        images:refs.map(r=>({type:'image',data:r.bytes.toString('base64'),mimeType:r.media_type})),
        generate:async(tool,prompt)=>{
          if(signal.aborted)throw new Error('STOPPED');
          const request=await this.operation(job,job.task.kind,tool,prompt,job.task.kind==='edit'?job.context.reference_artifact_ids:[]);
          try{
            const images=await this.provider(request.credential.binding,request.credential.secret,request.op,refs,signal);
            await this.report(job,request.op,'succeeded',request.credential.binding.reserve_micro*images.length);
            for(const [index,image] of images.entries())output.push(await this.upload(job,request.op,image,signal,index));
            return {artifact_ids:output};
          }catch{try{await this.report(job,request.op,'unknown');}catch{}throw new Error('IMAGE_OUTCOME_UNKNOWN');}
          finally{request.credential.secret='';}
        },
        select:async value=>{
          if(!value.artifact_ids.length||value.artifact_ids.some(id=>!refIds.includes(id)))throw new Error('SELECTION_FORBIDDEN');
          selected=[...new Set(value.artifact_ids)];summary=value.summary;return {selected_artifact_ids:selected};
        }});
      await this.report(job,agent.op,'succeeded',answer.cost_micro);
      const complete=answer.used_tool && (job.task.kind==='selection'?selected.length>0:output.length===job.task.count);
      await this.control.post(`/runner/attempts/${job.attempt.id}/checkpoints`,{...this.fence(job),expected_revision:0,completed_tool_calls:[agent.op.id],artifact_ids:output,phase:'finalizing'});
      await this.control.post(`/runner/attempts/${job.attempt.id}/complete`,{...this.fence(job),outcome:complete?'succeeded':'failed',artifact_ids:output,selected_artifact_ids:selected,summary:summary||'Image workflow completed.'},job.attempt.id+':complete');
    }catch{
      if(agent){try{await this.report(job,agent.op,'unknown');}catch{}}
      await this.abandon(job);
    }finally{clearInterval(pulse);clearTimeout(deadlineTimer);controller.abort();if(agent)agent.credential.secret='';}
  }
  async tick(slot){
    let saved=await this.journal.load(slot);
    if(saved?.job){await this.abandon(saved.job);await this.journal.remove(slot);return;}
    if(!saved){saved={claim_id:randomUUID()};await this.journal.save(slot,saved);}
    const job=await this.control.post('/runner/claims',{claim_id:saved.claim_id,protocol:'image-agent/1.0',available_slots:this.slots},saved.claim_id);
    if(!job.task){await this.journal.remove(slot);return;}
    await this.journal.save(slot,{job}); // Durable before credentials or provider effects.
    await this.execute(job);await this.journal.remove(slot);
  }
}
async function main(){
  const slots=Number(process.env.AGENT_IMAGE_SLOTS||1);if(!Number.isInteger(slots)||slots<1||slots>32)throw new Error('INVALID_SLOTS');
  const journal=new Journal(process.env.AGENT_IMAGE_STATE||'.local/pi-image');await journal.init();
  const lock=await open(join(journal.directory,'worker.lock'),'wx',0o600);await lock.writeFile(String(process.pid));await lock.sync();
  const worker=new Worker(new Control(process.env.AGENT_CONTROL_URL,process.env.AGENT_IMAGE_WORKER_TOKEN),journal,{slots});
  const stop=async()=>{await lock.close();await unlink(join(journal.directory,'worker.lock'));process.exit(0);};
  process.once('SIGTERM',()=>void stop());process.once('SIGINT',()=>void stop());
  await Promise.all(Array.from({length:slots},async(_,slot)=>{while(true){try{await worker.tick(slot);}catch{console.error('image_worker_iteration_failed');}await new Promise(r=>setTimeout(r,1000));}}));
}
if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href)main().catch(()=>{console.error('image_worker_start_failed');process.exitCode=1;});
