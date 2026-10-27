let state, refreshing = false;
const $ = id => document.getElementById(id);
function node(tag, text) { const e = document.createElement(tag); e.textContent = text; return e; }
async function request(path, body) {
  if (!state) throw Error('服务尚未连接，请稍后重试');
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json','X-Wereply-Token':state.token}, body:JSON.stringify(body)});
  const data = await r.json(); if (!r.ok) throw Error(data.error); return data;
}
async function post(path, body) { await request(path, body); await refresh(); }
function act(fn) {
  return async event => {
    const button = event?.currentTarget;
    if (button) button.disabled = true;
    $('feedback').textContent = '';
    try { await fn(); } catch (e) { $('feedback').textContent = e.message; }
    finally { if (button) button.disabled = false; }
  };
}
function edit(rule) {
  const f = $('rule').elements;
  f.chat.value = rule.chat; f.display.value = rule.name || rule.chat;
  f.mode.value = rule.mode; f.body.value = rule.body; f.enabled.checked = !!rule.enabled;
  f.body.focus();
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const r = await fetch('/api/state'); if (!r.ok) throw Error('无法读取服务状态'); state = await r.json();
    $('toggle').disabled = !state.initialized;
    $('toggle').textContent = state.active ? '暂停自动回复' : '开启自动回复';
    $('notice').textContent = state.error || (!state.initialized ? `正在建立历史基线，已扫描 ${state.baseline_messages || 0} 条消息。完成后可以开启自动回复；扫描期间不会回复旧消息。` : '');
    const h = state.health;
    $('connection').textContent = `读取：${h.read_available?'可用':'不可用'}　发送：${h.send_available?'可用':'不可用'}　自动回复：${state.active?'已开启':'已暂停'}`;
    $('account').textContent = (state.account ? `绑定账号：${state.account.slice(0,12)}` : '尚未绑定账号') + (state.checked_at ? `　检查于 ${new Date(state.checked_at*1000).toLocaleTimeString()}` : '');
    const stages = {checking:'检查微信环境',baseline:'扫描历史消息',reading:'读取新消息',waiting:'等待新消息',generating:'生成 LLM 回复',preparing:'准备回复',sending:'发送回复',error:'处理遇到错误'};
    const age = state.stage_at ? Math.max(0, Math.round(Date.now()/1000-state.stage_at)) : 0;
    const counts = state.job_counts || {};
    const reason = !state.active ? '自动回复已暂停' : !h.send_available ? (h.error || h.account_error || '桌面暂不可发送') : !state.rules.some(r=>r.enabled) ? '没有启用的回复规则' : null;
    $('activity').textContent = `${stages[state.stage] || '等待检查'}${state.stage && state.stage !== 'waiting' ? `（${age} 秒）` : ''} · 等待 ${counts.pending || 0} · 未知 ${counts.unknown || 0}` + (reason ? `。${reason}` : '');
    const batch = state.last_batch;
    const reasonNames = {duplicate:'重复',baseline:'历史基线',paused:'暂停期间',unsupported:'本人、群聊或非文本',unmatched:'没有匹配规则',queued:'已创建回复'};
    $('batch').textContent = batch ? `最近非空批次 ${new Date(batch.at*1000).toLocaleTimeString()}：${batch.count} 条；` + Object.entries(batch.reasons).filter(([,n])=>n).map(([k,n])=>`${reasonNames[k]} ${n}`).join('，') + `。最近一页读取耗时 ${state.page_seconds ?? '—'} 秒。` : '尚未读取到消息批次。';
    // Avoid replacing focused controls during polling.
    if (!$('rules').contains(document.activeElement)) {
      $('rules').replaceChildren();
      for (const rule of state.rules) {
        const e = node('article','');
        e.append(node('strong',rule.name || rule.chat),node('p',rule.chat),node('p',`${rule.enabled?'已启用':'已关闭'} · ${rule.mode==='fixed'?'固定文本':'LLM 生成'}`),node('p',rule.body));
        const b = node('button','编辑规则'); b.className='secondary'; b.onclick=()=>edit(rule);
        const remove = node('button','删除规则'); remove.className='secondary';
        remove.onclick=act(async()=>{await post('/api/rules/delete',{chat:rule.chat});$('feedback').textContent='规则已删除，等待中的回复已取消。';});
        e.append(b,remove);$('rules').append(e);
      }
      if (!state.rules.length) $('rules').append(node('p','还没有规则。搜索联系人并设置回复内容即可开始。'));
    }
    if (!$('jobs').contains(document.activeElement)) {
      const labels={pending:'等待处理',sending:'发送中',recorded:'本地已确认',unknown:'结果未知',failed:'失败',cancelled:'已取消'};
      $('jobs').replaceChildren();
      for (const job of state.jobs) {
        const e=node('article','');e.append(node('strong',state.rules.find(r=>r.chat===job.chat)?.name || job.chat),node('p',labels[job.status]||job.status),node('time',new Date(job.created*1000).toLocaleString()),node('p',job.text||'尚未生成回复'));
        if (job.status==='unknown') {const b=node('button','核对发送结果');b.onclick=act(()=>post('/api/reconcile',{id:job.id}));e.append(b);}
        if (job.detail) {const d=node('details','');d.append(node('summary','查看处理详情'),node('p',job.detail));e.append(d);}
        $('jobs').append(e);
      }
      if (!state.jobs.length) $('jobs').append(node('p','暂无回复任务。上方运行状态会显示消息未生成回复的原因。'));
    }
  } finally { refreshing=false; }
}
$('toggle').onclick=act(()=>post('/api/active',{active:!state.active}));
$('clear').onclick=()=>$('rule').reset();
$('rule').elements.chat.oninput=()=>{$('rule').elements.display.value='';};
$('search').onsubmit=async event=>{
  event.preventDefault(); const button=event.target.querySelector('button'); button.disabled=true;
  $('searchStatus').textContent='正在查找联系人…'; $('contacts').replaceChildren();
  try {
    const data=await request('/api/contacts',{query:event.target.elements.query.value.trim()});
    $('searchStatus').textContent=data.contacts.length ? `找到 ${data.contacts.length} 位联系人（最多显示 30 位），请选择：` : '没有找到，请使用备注或昵称搜索；微信号不等于内部 ID。';
    for (const c of data.contacts) {
      const button=node('button',`${c.remark || c.nick_name || c.username}　${c.username}`);button.className='contact-choice secondary';
      button.onclick=()=>{const f=$('rule').elements;f.chat.value=c.username;f.display.value=c.remark || c.nick_name || c.username;f.body.focus();$('searchStatus').textContent=`已选择 ${f.display.value}`;};
      $('contacts').append(button);
    }
  } catch(e) { $('searchStatus').textContent=e.message; } finally {button.disabled=false;}
};
$('rule').onsubmit=async event=>{
  event.preventDefault(); const button=event.target.querySelector('button[type=submit]');button.disabled=true;$('feedback').textContent='正在核对联系人并保存…';
  try {
    const f=event.target.elements;await post('/api/rules',{chat:f.chat.value.trim(),mode:f.mode.value,body:f.body.value,enabled:f.enabled.checked});
    $('feedback').textContent='规则已保存。开启自动回复后，对方新发来的文字消息才会触发回复。';
  } catch(e) { $('feedback').textContent=e.message; } finally {button.disabled=false;}
};
refresh().catch(e=>$('notice').textContent=e.message);
// Schedule the next refresh after completion, so slow requests never overlap.
async function poll() {if (!document.hidden) await refresh().catch(e=>$('notice').textContent=e.message);setTimeout(poll,4000);}
setTimeout(poll,4000);
