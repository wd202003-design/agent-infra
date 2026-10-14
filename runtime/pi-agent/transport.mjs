import { createHash } from 'node:crypto';

export async function bounded(response, limit) {
  if (!response.ok) throw new Error('REMOTE_REQUEST_FAILED');
  if (Number(response.headers.get('content-length') || 0) > limit) throw new Error('RESPONSE_LIMIT');
  const parts=[];let size=0;
  for await (const chunk of response.body) {size+=chunk.length;if(size>limit)throw new Error('RESPONSE_LIMIT');parts.push(chunk);}
  return Buffer.concat(parts);
}
export async function jsonFetch(url, options={}, limit=262144) {
  const response=await fetch(url,{...options,redirect:'error'});
  return JSON.parse((await bounded(response,limit)).toString('utf8'));
}
export function httpsUrl(value){const u=new URL(value);if(u.protocol!=='https:'||u.username||u.password||u.search||u.hash)throw new Error('HTTPS_REQUIRED');return value.replace(/\/$/,'');}
export const checksum=data=>createHash('sha256').update(data).digest('hex');
export async function imageCall(binding,secret,operation,references,signal) {
  const endpoint=httpsUrl(binding.base_url)+'/images/'+(operation.kind==='edit'?'edits':'generations');
  const headers={Authorization:'Bearer '+secret,'Idempotency-Key':operation.id};
  let body;
  if(operation.kind==='edit') {
    body=new FormData();body.set('model',binding.model);body.set('prompt',operation.prompt);
    body.set('n',String(operation.count));body.set('size',operation.size);body.set('quality',operation.quality);body.set('response_format','b64_json');
    for(const [i,ref] of references.entries())body.append('image[]',new Blob([ref.bytes],{type:ref.media_type}),`reference-${i}.${ref.media_type.split('/')[1]}`);
  } else {headers['Content-Type']='application/json';body=JSON.stringify({model:binding.model,prompt:operation.prompt,n:operation.count,size:operation.size,quality:operation.quality,response_format:'b64_json'});}
  // No retries and no arbitrary provider result URL downloads (SSRF).
  const data=await jsonFetch(endpoint,{method:'POST',headers,body,signal},115343360);
  if(!Array.isArray(data.data)||data.data.length!==operation.count)throw new Error('INVALID_IMAGE_RESPONSE');
  return data.data.map(item=>{
    if(typeof item.b64_json!=='string'||item.b64_json.length>27962032||/[^A-Za-z0-9+/=]/.test(item.b64_json))throw new Error('INVALID_IMAGE_RESPONSE');
    const bytes=Buffer.from(item.b64_json,'base64');if(bytes.toString('base64')!==item.b64_json||!bytes.length||bytes.length>20971520)throw new Error('IMAGE_LIMIT');
    let media_type=bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10]))?'image/png':bytes[0]===255&&bytes[1]===216?'image/jpeg':bytes.subarray(0,4).toString()==='RIFF'&&bytes.subarray(8,12).toString()==='WEBP'?'image/webp':null;
    if(!media_type)throw new Error('IMAGE_TYPE');return {bytes,media_type,sha256:checksum(bytes)};
  });
}
