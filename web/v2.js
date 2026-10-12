'use strict';
const $=id=>document.getElementById(id);
let token='', me=null, agents=[], current=null, next=null;
const pending=new Map();
const text=(id,value)=>{$(id).textContent=typeof value==='string'?value:JSON.stringify(value,null,2);};
async function api(path,method='GET',body){
  const signature=method+path+JSON.stringify(body);
  const headers={Authorization:'Bearer '+token};
  if(method!=='GET'){
    if(!pending.has(signature))pending.set(signature,crypto.randomUUID());
    headers['Idempotency-Key']=pending.get(signature);headers['Content-Type']='application/json';
  }
  const response=await fetch('/v2'+path,{method,headers,body:body===undefined?undefined:JSON.stringify(body),cache:'no-store'});
  const data=await response.json();
  if(!response.ok){
    if(response.status!==503)pending.delete(signature);
    if(response.status===401){token='';$('session').hidden=true;$('login').hidden=false;}
    throw new Error(`${data.code} · 请求 ${data.request_id||''}`);
  }
  pending.delete(signature);return data;
}
function guard(fn){return async event=>{event?.preventDefault();try{await fn(event);text('status','已更新');}catch(error){text('status',error.message);}};}
function button(label,fn){const b=document.createElement('button');b.textContent=label;b.onclick=guard(fn);return b;}
function option(select,value,label){const o=document.createElement('option');o.value=value;o.textContent=label;select.append(o);}
async function loadTasks(cursor){
  const response=await api('/tasks'+(cursor?'?cursor='+encodeURIComponent(cursor):''));next=response.next_cursor;
  if(!cursor)$('tasks').replaceChildren();
  for(const task of response.data){const row=document.createElement('div');row.className='task-row';const label=document.createElement('span');label.textContent=`${task.id} · ${task.state} · ${task.pool}`;row.append(label,button('查看',()=>selectTask(task.id)));$('tasks').append(row);}
  $('more').disabled=!next;
}
async function selectTask(id){
  current=(await api('/tasks/'+id)).data;$('detail').hidden=false;text('snapshot',current);
  $('cancel').disabled=['succeeded','failed','cancelled'].includes(current.state);$('delete').disabled=!['succeeded','failed','cancelled'].includes(current.state)||current.deleted;
  if(current.deleted){text('events','内容已删除');text('usage',(await api('/tasks/'+id+'/usage')).data);$('artifacts').replaceChildren();return;}
  text('events',(await api('/tasks/'+id+'/events')).data);text('usage',(await api('/tasks/'+id+'/usage')).data);
  const artifacts=(await api('/tasks/'+id+'/artifacts')).data;$('artifacts').replaceChildren();
  for(const artifact of artifacts){$('artifacts').append(button(`${artifact.name} · ${artifact.size} bytes`,async()=>{
    const data=(await api('/artifacts/'+artifact.id+'/download','POST',{disposition:'attachment'})).data;
    const url=new URL(data.url);if(!['https:','http:'].includes(url.protocol))throw new Error('无效下载地址');
    const a=document.createElement('a');a.href=url.href;a.rel='noopener noreferrer';a.textContent='下载（60 秒有效）';a.target='_blank';$('artifacts').append(a);
  }));}
}
async function loadVersions(){agents=(await api('/agents')).data;$('agent').replaceChildren();for(const a of agents)option($('agent'),a.id+':'+a.version,a.id+' / '+a.version);}
function submission(){const agent=agents.find(a=>a.id+':'+a.version===$('agent').value);if(!agent)throw new Error('请先发布 Agent 版本');return {agent_ref:{id:agent.id,version:agent.version},policy_ref:agent.policy_ref,pool:$('pool').value,input:JSON.parse($('input').value),budget:{token_limit:Number($('tokens').value),cost_limit:Number($('cost').value),currency:'USD',mode:'soft'}};}
async function adminData(){
  if(me.role!=='admin')return;
  $('nodes').replaceChildren();for(const node of (await api('/nodes')).data){const row=document.createElement('div');row.className='node-row';const label=document.createElement('span');label.textContent=`${node.id} · ${node.backend} · ${node.revoked?'已撤销':node.draining?'排空中':'可领取'}`;row.append(label);if(!node.revoked)row.append(button(node.draining?'恢复领取':'排空',async()=>{await api('/nodes/'+node.id,'PATCH',{expected_version:node.state_version,draining:!node.draining});await adminData();}),button('撤销凭证',async()=>{await api('/nodes/'+node.id+'/revoke','POST',{reason:'admin_console'});await adminData();}));$('nodes').append(row);}
  text('audit',(await api('/audit')).data);
}
$('login-form').onsubmit=guard(async()=>{token=$('token').value;$('token').value='';me=(await api('/me')).data;text('identity',`${me.id} · ${me.role}`);$('session').hidden=false;$('login').hidden=true;$('admin').hidden=me.role!=='admin';$('pool').replaceChildren();for(const p of me.pools)option($('pool'),p,p);await loadVersions();await loadTasks();await adminData();});
$('logout').onclick=()=>{token='';location.reload();};
$('refresh').onclick=guard(async()=>{await loadTasks();if(current)await selectTask(current.id);await adminData();});
$('more').onclick=guard(()=>loadTasks(next));
$('submit-form').onsubmit=guard(async()=>{const task=(await api('/tasks','POST',submission())).data;await loadTasks();await selectTask(task.id);});
$('cancel').onclick=guard(async()=>{await api('/tasks/'+current.id+'/cancel','POST',{expected_version:current.state_version,reason:'user_request'});await selectTask(current.id);await loadTasks();});
$('rerun').onclick=guard(async()=>{const task=(await api('/tasks/'+current.id+'/rerun','POST',submission())).data;await loadTasks();await selectTask(task.id);});
$('delete').onclick=guard(async()=>{if(!confirm('删除输入、事件和产物？费用审计记录将保留。'))return;await api('/tasks/'+current.id+'/content','DELETE',{expected_version:current.state_version});await selectTask(current.id);});
$('publish-form').onsubmit=guard(async()=>{await api('/'+$('publish-kind').value+'/'+encodeURIComponent($('publish-name').value)+'/versions','POST',JSON.parse($('publish-body').value));await loadVersions();});
$('ticket-form').onsubmit=guard(async()=>{text('ticket-result',(await api('/nodes/tickets','POST',JSON.parse($('ticket-body').value))).data);});
$('review-form').onsubmit=guard(async()=>{if(!current)throw new Error('先选择任务');await api('/tasks/'+current.id+'/review','POST',{expected_version:current.state_version,outcome:$('review-outcome').value,evidence_refs:[$('review-evidence').value],stop_confirmed:true,cleanup_confirmed:true});await selectTask(current.id);});
$('usage-form').onsubmit=guard(async()=>{if(!current)throw new Error('先选择任务');await api('/tasks/'+current.id+'/usage/'+encodeURIComponent($('usage-id').value)+'/reconcile','POST',{expected_version:Number($('usage-version').value),actual_tokens:Number($('usage-tokens').value),cost:Number($('usage-cost').value),currency:'USD',evidence_ref:$('usage-evidence').value});await selectTask(current.id);});
