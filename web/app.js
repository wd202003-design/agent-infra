"use strict";
const $ = id => document.getElementById(id);
let token = "", user = null, selected = null, pending = null, refreshing = false;
const states = {queued:"排队中",running:"执行中",waiting:"等待中",cancel_requested:"取消中，尚未确认停止",needs_review:"待核对，停止或结果仍存在未知",succeeded:"成功",failed:"失败",cancelled:"已取消"};
function notice(text) { $("notice").textContent = text; }
async function api(path, options = {}) {
  const response = await fetch("/v1" + path, {...options, headers:{"Authorization":"Bearer " + token,"Content-Type":"application/json",...(options.headers || {})}});
  if (!response.ok) {
    const err = await response.json().catch(() => ({code:"network_error"}));
    throw new Error(response.status + " · " + (err.code || "request_failed"));
  }
  return response;
}
async function json(path, options) { return (await api(path, options)).json(); }
function option(parent, value, text) { const el = document.createElement("option"); el.value = value; el.textContent = text; parent.append(el); }
function disconnected() { token="";user=null;selected=null;pending=null;$("token").value="";$("identity").textContent="未连接";$("tasks").replaceChildren();$("nodes").replaceChildren();$("agent").replaceChildren();$("pool").replaceChildren();$("detail").hidden=true;$("admin").hidden=true; }
$("disconnect").onclick=()=>{disconnected();notice("已清除当前会话。");};
$("connect").onsubmit=async event=>{
  event.preventDefault();const entered=$("token").value;disconnected();token=entered;
  try {
    user=await json("/me");$("identity").textContent=user.id+" · "+user.role;
    for(const agent of await json("/agents")) option($("agent"),agent.id,agent.agent_id+" @ "+agent.version+" · "+agent.mode);
    for(const pool of user.pools) option($("pool"),pool,pool);
    $("admin").hidden=user.role!=="admin";notice(user.model_configured?"已连接；模型网关已配置，实际可用性需任务验证。":"已连接；模型网关未配置，当前可提交固定程序或 Demo。");await refresh();
  } catch(error) { disconnected();notice(error.message); }
};
$("submit").onsubmit=async event=>{
  event.preventDefault();if(!user)return notice("请先连接。");
  const [agent_id,version]=$("agent").value.split(":");
  const payload={agent_id,version,pool:$("pool").value,prompt:$("prompt").value,timeout_seconds:Number($("timeout").value),token_budget:Number($("budget").value)};
  const input=JSON.stringify(payload);
  if(pending&&pending.input!==input)return notice("上次提交结果尚未确认。请恢复上次输入并重试，或在任务列表确认后刷新页面。");
  pending=pending||{key:crypto.randomUUID(),input};$("submit-button").disabled=true;
  try { const task=await json("/tasks",{method:"POST",headers:{"Idempotency-Key":pending.key},body:input});pending=null;selected=task.id;notice("任务已持久化："+task.id);await refresh(); }
  catch(error){notice("提交结果待确认；相同输入重试会复用幂等键。"+error.message);}
  finally{$("submit-button").disabled=false;}
};
async function showTask() {
  if(!selected)return;const task=await json("/tasks/"+selected);$("detail").hidden=false;$("task-title").textContent=task.id;
  $("status").textContent=(states[task.state]||task.state)+(task.simulated?" · DEMO 模拟，未执行源码或调用模型":"")+" · epoch "+task.epoch;
  $("usage").textContent="模型已计量 "+task.model_actual+" tokens；预留/待核对 "+task.model_reserved+"；额度 "+task.token_budget+"。货币费用尚未实现。";
  $("result").textContent=task.result||"暂无结果";$("download").disabled=task.result===null;
  $("cancel").disabled=["succeeded","failed","cancelled"].includes(task.state);
  const events=await json("/tasks/"+selected+"/events?after=0");$("events").textContent=events.items.map(e=>e.sequence+" · "+e.type+" · "+(states[e.state]||e.state)+" "+JSON.stringify(e.details)).join("\n");
  $("review").hidden=user.role!=="admin"||!["needs_review","cancel_requested"].includes(task.state);
}
async function refresh() {
  if(!token||refreshing)return;refreshing=true;
  try {
    const tasks=await json("/tasks");$("tasks").replaceChildren();
    if(!tasks.length)$("tasks").textContent="暂无任务";
    for(const task of tasks){const row=document.createElement("li"),button=document.createElement("button");button.textContent=task.agent_id+" · "+(states[task.state]||task.state)+(task.simulated?" · DEMO":"");button.onclick=()=>{selected=task.id;showTask().catch(e=>notice(e.message));};row.append(button,document.createTextNode(task.id));$("tasks").append(row);}
    if(user.role==="admin"){
      $("nodes").replaceChildren();for(const node of await json("/nodes")){const row=document.createElement("li");row.textContent=node.name+" · "+node.pool+" · "+node.backend+" · "+(node.revoked?"已吊销":node.draining?"排空中":"接收任务");
        for(const [label,patch] of [[node.draining?"恢复领取":"排空",{draining:!node.draining,revoked:node.revoked}],["吊销",{draining:true,revoked:true}]]){const button=document.createElement("button");button.textContent=label;button.disabled=node.revoked;button.onclick=async()=>{try{await json("/nodes/"+node.id,{method:"PATCH",body:JSON.stringify(patch)});await refresh();}catch(e){notice(e.message);}};row.append(button);}$("nodes").append(row);}
    } await showTask();
  }catch(error){notice(error.message);}finally{refreshing=false;}
}
$("refresh").onclick=refresh;
$("cancel").onclick=async()=>{try{await json("/tasks/"+selected+"/cancel",{method:"POST"});notice("取消请求已保存；停止与回收确认前不会显示取消完成。外部动作不会自动回滚。");await showTask();}catch(e){notice(e.message);}};
$("download").onclick=async()=>{try{const response=await api("/tasks/"+selected+"/result");const url=URL.createObjectURL(await response.blob());const a=document.createElement("a");a.href=url;a.download="result.json";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){notice(e.message);}};
$("review").onsubmit=async event=>{event.preventDefault();try{await json("/tasks/"+selected+"/review",{method:"POST",body:JSON.stringify({outcome:$("outcome").value,stopped:true,cleanup_confirmed:true,evidence:$("evidence").value})});$("confirmed").checked=false;await refresh();}catch(e){notice(e.message);}};
setInterval(refresh,3000);
