const $ = id => document.getElementById(id);
let token = '', selected = null, timer = null, pending = null;
async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json', ...options.headers}});
  if (!response.ok) { const e = await response.json(); throw new Error(`${response.status}: ${e.detail}`); }
  return response.json();
}
function error(e) { $('message').textContent = e.message; }
async function detail() {
  if (!selected) return;
  const [task, events] = await Promise.all([api(`/api/tasks/${selected}`), api(`/api/tasks/${selected}/events`)]);
  $('detail').textContent = JSON.stringify({...task, events}, null, 2);
  $('cancel').disabled = !['queued', 'running'].includes(task.state);
  $('download').disabled = !task.result;
}
async function refresh() {
  const tasks = await api('/api/tasks');
  $('tasks').replaceChildren();
  for (const task of tasks) {
    const b = document.createElement('button'); b.className = 'task';
    b.textContent = `${task.id.slice(0, 8)} · ${task.payload.kind} · ${task.state}`;
    b.onclick = () => { selected = task.id; detail().catch(error); };
    $('tasks').append(b);
  }
  await detail();
}
$('connect').onclick = async () => {
  clearInterval(timer); token = $('token').value;
  try {
    const info = await api('/api/info');
    $('mode').textContent = `后端：${info.backend} ｜ ${info.simulated ? '模拟模式：不执行代码' : '真实沙箱适配：需独立集成验收'} ｜ 单任务开发版 / 未接入 LLM`;
    $('submit').disabled = false; $('message').textContent = '已连接';
    await refresh(); timer = setInterval(() => refresh().catch(error), 1500);
  } catch (e) { $('submit').disabled = true; error(e); }
};
$('submit').onclick = async () => {
  $('submit').disabled = true;
  const body = JSON.stringify({kind: $('kind').value, source: $('source').value, timeout_seconds: Number($('timeout').value)});
  // Preserve the same key when a response is lost; changed input gets a new key.
  if (!pending || pending.body !== body) pending = {body, key: crypto.randomUUID()};
  try {
    const task = await api('/api/tasks', {method: 'POST', headers: {'Idempotency-Key': pending.key}, body});
    selected = task.id; pending = null; $('message').textContent = '已提交'; await refresh();
  } catch (e) { error(e); } finally { $('submit').disabled = false; }
};
$('cancel').onclick = async () => { try { await api(`/api/tasks/${selected}/cancel`, {method: 'POST'}); await refresh(); } catch(e) { error(e); } };
$('download').onclick = async () => {
  try {
    const result = await api(`/api/tasks/${selected}/result`);
    const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], {type: 'application/json'}));
    const a = document.createElement('a'); a.href = url; a.download = `${selected}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch(e) { error(e); }
};
$('kind').onchange = () => {
  const examples = {python: 'print(sum(range(10)))', shell: 'printf "hello sandbox\\n"', browser: 'from playwright.sync_api import sync_playwright\nwith sync_playwright() as p:\n    browser = p.chromium.launch(headless=True)\n    page = browser.new_page()\n    page.set_content("<title>Agent Infra</title><h1>Sandbox ready</h1>")\n    print(page.title())\n    browser.close()'};
  $('source').value = examples[$('kind').value];
};
