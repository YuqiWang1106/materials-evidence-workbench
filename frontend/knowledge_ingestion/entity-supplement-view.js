/** Supplemental extraction is explicit and additive; original failure history stays visible. */
export function mountEntitySupplement({root, path, api, onApplied}) {
  const document=root.ownerDocument;
  let disposed = false, busy = false, timer, requestId = null, state, chosenWindows=null, expanded=false;
  const el = (tag, text) => { const n = document.createElement(tag); if (text) n.textContent = text; return n; };
  const labels = {queued:'等待补抽', running:'补抽中', interrupted:'已中断', ready:'候选待合入',
    failed:'补抽未完成', applying:'正在合入', applied:'已合入', unknown:'请求结果未知', pending:'尚未派发', started:'请求中'};
  async function action(fn) {
    if (busy) return; busy = true; render();
    try { await fn(); await refresh(); }
    catch { message.textContent = '操作结果未确认，请刷新状态查看。不要重复创建补抽请求。'; }
    finally { busy = false; if (!disposed) render(); }
  }
  const refinementIds = new Map();
  const message = el('p'); message.setAttribute('role', 'status');
  function button(label, fn) { const b = el('button', label); b.type='button'; b.disabled=busy; b.addEventListener('click',()=>action(fn)); return b; }
  function render() {
    if (disposed || !state) return;
    root.replaceChildren();
    if (!state.failed_windows.length && !state.runs.length) return;
    const details = el('details'); details.open=expanded; details.addEventListener('toggle',()=>{expanded=details.open;});
    details.append(el('summary','失败窗口补抽'));
    details.append(el('p',`仍未完整补齐窗口：${state.remaining_windows.join('、') || '无'}。`));
    details.append(el('p',`原抽取失败窗口：${state.failed_windows.join('、') || '无'}。保留原记录，只追加有原文依据的补抽结果。`));
    if (state.blocked) details.append(el('p','当前无法自动补抽：'+state.blocked));
    const attempted = new Set(state.runs.flatMap(r=>r.intent.window_ids));
    const available = state.remaining_windows.filter(w=>!attempted.has(w));
    chosenWindows ||= new Set(available);
    const selected = [];
    for (const wid of available) {
      const label=el('label'),check=el('input');check.type='checkbox';check.checked=chosenWindows.has(wid);check.disabled=busy;check.addEventListener('change',()=>{if(check.checked)chosenWindows.add(wid);else chosenWindows.delete(wid);});
      label.append(check,el('span',' '+wid+' '));details.append(label);selected.push([wid,check]);
    }
    if (available.length && !state.blocked) details.append(button('补抽所选失败窗口（调用模型）',async()=>{
      const windows=selected.filter(([,c])=>c.checked).map(([w])=>w);
      if (!windows.length) { message.textContent='请先选择失败窗口。'; return; }
      requestId ||= crypto.randomUUID().replaceAll('-','');
      await api(path+'/entity-supplements',{method:'POST',json:{request_id:requestId,
        expected_binding:state.binding,window_ids:windows,operator:'网站用户：定向补抽'}});
      requestId=null;
    }));
    for (const run of state.runs) {
      const box=el('div'); const done=run.units.filter(u=>u.state==='ready').length;
      box.append(el('p',`${labels[run.state]||run.state} · ${done}/${run.units.length} 个分段有候选`));
      for (const unit of run.units) box.append(el('p',`${unit.unit_id} · 第 ${unit.pages.join('、')} 页 · ${run.state==='applied'&&unit.state==='ready'?'已合入':labels[unit.state]||unit.state}`+
        (unit.summary ? ` · ${unit.summary.attributes} 段属性` : '')+(unit.error ? ` · ${unit.error}` : '')));
      if (run.state==='ready'||run.state==='applying') box.append(button('合入补抽候选，保留原结果',async()=>{
        await api(`${path}/entity-supplements/${run.run_id}/apply`,{method:'POST',json:{expected_candidate_sha256:run.candidate_sha256}});
        onApplied?.();
      }));
      if (['interrupted','running','queued'].includes(run.state)) box.append(button('继续未派发分段',async()=>{
        await api(`${path}/entity-supplements/${run.run_id}/resume`,{method:'POST'});
      }));
      if (['ready','failed','interrupted'].includes(run.state)) box.append(button('重新校验已返回结果',async()=>{
        await api(`${path}/entity-supplements/${run.run_id}/recheck`,{method:'POST'});
      }));
      if (['ready','failed','interrupted'].includes(run.state)) box.append(el('p','重校验不重复抽取；可能继续此前未执行的实体匹配调用。结果未知的请求继续隔离。'));
      if (['applied','failed'].includes(run.state) && !state.blocked) {
        const attemptedUnits=new Set(state.runs.filter(r=>r.intent.parent_run_id===run.run_id).flatMap(r=>r.intent.unit_ids));
        for (const unit of run.units.filter(u=>['failed','unknown'].includes(u.state)&&!attemptedUnits.has(u.unit_id))) {
          const label=el('label'),ack=el('input');ack.type='checkbox';ack.disabled=busy;
          label.append(ack,el('span',` ${unit.unit_id}：确认已检查响应恢复；旧请求可能已计费，仍授权更小分段的新请求。`));
          box.append(label,button(`细分补抽 ${unit.unit_id}（调用模型）`,async()=>{
            if (!ack.checked) {message.textContent='请先确认已检查响应恢复，并授权新的模型请求。';return;}
            const key=run.run_id+':'+unit.unit_id;
            if (!refinementIds.has(key)) refinementIds.set(key,crypto.randomUUID().replaceAll('-',''));
            await api(`${path}/entity-supplements/${run.run_id}/refine`,{method:'POST',json:{
              request_id:refinementIds.get(key),expected_binding:state.binding,unit_ids:[unit.unit_id],
              acknowledge_unknown:true,max_chars:1000,operator:'网站用户：已检查恢复并明确授权细分新请求'}});
            refinementIds.delete(key);
          }));
        }
      }
      if(run.state==='applied') box.append(el('p',`补抽完成的原窗口：${run.recovered_windows.join('、')||'暂无完整窗口'}。补抽完成不代表逐条科学核验。`));
      details.append(box);
    }
    details.append(button('刷新补抽状态',async()=>{}),message);root.append(details);
  }
  async function refresh() {
    const next=await api(path+'/entity-supplements'); if(disposed)return; if(!Array.isArray(next.failed_windows)||!Array.isArray(next.runs))throw Error('Invalid supplement status'); state=next;render();
  }
  refresh().catch(()=>{if(!disposed)root.textContent='补抽状态暂不可用，请刷新页面。';});
  timer=setInterval(()=>{if(!busy)refresh().catch(()=>{});},10000);
  return ()=>{disposed=true;clearInterval(timer);};
}
