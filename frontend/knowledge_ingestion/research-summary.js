// @ts-check
import { el, button, field, bindInput } from './dom.js';

const ACTIVE = new Set(['queued', 'running', 'saving', 'publishing']);
const STATES = new Set([...ACTIVE, 'completed', 'proposal_ready', 'save_failed', 'publish_failed', 'failed', 'interrupted', 'cancelled']);
const LABELS = { queued:'等待整理', running:'正在整理', saving:'正在保存草稿', completed:'草稿已保存', proposal_ready:'更新提案已准备，目标页尚未更改', save_failed:'结果已保留，尚未保存草稿', failed:'整理未完成，未自动重试', interrupted:'整理中断，未自动重发', cancelled:'整理已取消' };
const UNIFIED_LABELS = {saving:'正在保存', publishing:'正在发布', completed:'已发布', save_failed:'结果已保留，尚未保存', publish_failed:'已保存，发布未完成'};
const WIKI_NOT_READY='Wiki 整理模型尚未就绪，请补齐本实例的 Wiki 模型配置，保存后正常重启网站，再刷新状态。';
const ERRORS = { SUMMARY_CONFLICT:'目标或请求内容已变化。提案仍保留，请重新读取后选择新目标版本。', SUMMARY_WINDOW_TOO_LARGE:'窗口内容过多，请减少整理轮数后重新整理。', INVALID_SUMMARY_REQUEST:'窗口或依据不可用，请选择完整轮次并核对来源。', RESEARCH_MODEL_NOT_READY:WIKI_NOT_READY, WIKI_SELF_REFERENCE:'更新目标不能引用自身导读，请选择只含原始论文依据的窗口。' };
const enc = encodeURIComponent;
const same = (a,b) => JSON.stringify(a) === JSON.stringify(b);
const normalize = r => ({ request_id:r?.request_id, end_turn_id:r?.end_turn_id, max_turns:r?.max_turns ?? 20, target_page_id:r?.target_page_id ?? null, base_revision:r?.base_revision ?? null, base_content_hash:r?.base_content_hash ?? null });
const validId = x => typeof x === 'string' && x.length > 0 && x.length <= 256 && !/[\s/\\\x00-\x1f]/.test(x);
const validRequest = r => validId(r?.request_id) && validId(r?.end_turn_id) && Number.isInteger(r.max_turns) && r.max_turns >= 1 && r.max_turns <= 20 && (r.target_page_id === null ? r.base_revision === null && r.base_content_hash === null : validId(r.target_page_id) && Number.isInteger(r.base_revision) && r.base_revision > 0 && /^[a-f0-9]{64}$/.test(r.base_content_hash));
const localStorage = () => { try { return globalThis.sessionStorage; } catch { return null; } };

/**
 * Independent of chat generation: opening and recovery only read summary jobs.
 * @param {{
 *   root: HTMLElement,
 *   api: typeof import('./app.js').api,
 *   chatId: string,
 *   go?: (hash: string) => void,
 *   isCurrent?: () => boolean,
 *   storage?: Storage,
 *   pollInterval?: number,
 * }} options
 */
export function mountResearchSummary({ root, api, chatId, go=()=>{}, isCurrent=()=>true, storage=localStorage(), pollInterval=1200 }) {
  const key='sog.research-summary.v1:'+chatId, base='/api/research-chats/'+enc(chatId)+'/wiki-summaries', abort=new AbortController();
  let disposed=false, busy=false, loaded=false, modelReady=false, turns=[], cutoff='', maxTurns=20, target='', targetBase=null, targetLoading=false, targetEpoch=0, epoch=0, timer=null, jobs=[], nextOffset=null, pending=null, error='';
  try { const saved=JSON.parse(storage?.getItem(key)||'null'); if(saved?.chat_id===chatId && saved.version===1 && ['create','cancel','save-retry','apply'].includes(saved.pending?.operation) && validRequest(normalize(saved.pending.request)) && (saved.pending.operation==='create'||validId(saved.pending.job_id))) pending=saved.pending; } catch { /* Corrupt local cache is not server authority. */ }
  if(pending){const r=normalize(pending.request);cutoff=r.end_turn_id;maxTurns=r.max_turns;target=r.target_page_id||'';targetBase=target?{base_revision:r.base_revision,base_content_hash:r.base_content_hash}:null;}
  const alive=()=>!disposed&&isCurrent();
  const call=(path,options={})=>api(path,{...options,signal:abort.signal});
  const area=el('details','research-summary'), heading=el('summary',null,'沉淀设置与记录'), controls=el('div','research-summary-controls'), status=el('div','research-summary-status'), history=el('div','research-summary-history');
  status.setAttribute('role','status');area.append(heading,controls,status,history);root.replaceChildren(area);
  let pages=[];
  function persist() { try { if(!storage) throw Error(); storage.setItem(key,JSON.stringify({version:1,chat_id:chatId,pending})); return true; } catch { error='无法保存请求状态，请恢复浏览器存储后再整理。'; return false; } }
  function validJob(j, expected=null) {
    const r=normalize(j?.request);
    if(!j||j.chat_id!==chatId||!validId(j.job_id)||!STATES.has(j.status)||!validRequest(r))return false;
    if(expected && (!same(r,normalize(expected.request)) || (expected.job_id && expected.job_id!==j.job_id)))return false;
    if(j.status==='completed' && (!validId(j.page_id)||(r.target_page_id && j.page_id!==r.target_page_id)))return false;
    if(j.status==='proposal_ready' && (!r.target_page_id||j.proposal?.base_revision!==r.base_revision||j.proposal?.base_content_hash!==r.base_content_hash||typeof j.proposal?.diff!=='string'))return false;
    return true;
  }
  function accept(j,expected=null) {
    if(!validJob(j,expected))throw Error('整理回执不匹配，原请求保留，尚未确认结果。');
    const old=jobs.findIndex(x=>x.job_id===j.job_id);if(old<0)jobs.unshift(j);else jobs[old]=j;
    if(pending && validJob(j,pending) && (pending.operation==='create' || pending.operation==='cancel'&&!ACTIVE.has(j.status) || ['apply','save-retry'].includes(pending.operation)&&['completed','proposal_ready','save_failed','publish_failed','failed','interrupted','cancelled'].includes(j.status))) {pending=null;persist();}
  }
  function windowProblem() {
    if(!turns.length)return '先完成一轮研究对话再整理。';
    if(!Number.isInteger(maxTurns)||maxTurns<1||maxTurns>20)return '整理轮数应为 1 到 20。';
    const end=turns.findIndex(t=>t.turn_id===cutoff);if(end<0)return '请选择截止轮次。';
    if(turns.slice(Math.max(0,end-maxTurns+1),end+1).some(t=>t.status!=='completed'||!t.answer))return '所选窗口包含未完成轮次，请明确调整截止轮次或缩小窗口。';
    return '';
  }
  function select(label, options, value, change) {
    const node=el('select');node.setAttribute('aria-label',label);if(value&&!options.some(([id])=>id===value))options=[...options,[value,'原请求已固定的选择']];for(const [id,text] of options){const o=el('option',null,text);o.value=id;node.append(o);}node.value=value;node.disabled=busy||Boolean(pending);node.addEventListener('change',()=>change(node.value));return field(label,node);
  }
  function draw() {
    if(!alive())return;controls.replaceChildren();status.replaceChildren();history.replaceChildren();
    controls.append(select('截止轮次',turns.map((t,i)=>[t.turn_id,`${i+1}. ${String(t.message||'研究轮次').slice(0,55)}${t.status==='completed'?'':'（未完成）'}`]),cutoff,v=>{cutoff=v;draw();}));
    const count=el('input');count.type='number';count.min=1;count.max=20;count.value=String(maxTurns);count.setAttribute('aria-label','整理轮数');count.disabled=busy||Boolean(pending);bindInput(count,()=>{maxTurns=Number(count.value);draw();});controls.append(field('整理轮数',count));
    controls.append(select('保存方式',[['','新建研究主题'],...pages.filter(p=>p.availability!=='withdrawn').map(p=>[p.page_id,String(p.title||'研究主题').slice(0,120)])],target,v=>chooseTarget(v)));
    const create=button('沉淀到 Wiki',createJob,'primary');create.disabled=!loaded||!modelReady||busy||Boolean(pending)||jobs.some(j=>ACTIVE.has(j.status))||Boolean(windowProblem())||targetLoading||Boolean(target&&!targetBase);controls.append(create);
    if(windowProblem())status.append(el('p','muted',windowProblem()));
    if(!modelReady)status.append(el('p','muted',WIKI_NOT_READY));
    if(targetLoading)status.append(el('p','muted','正在读取目标版本…'));
    if(targetBase)status.append(el('p','muted','将生成更新提案；应用之前不会修改已有主题。'));
    if(error)status.append(el('p','research-notice',error));
    if(pending)status.append(el('p','research-notice','请求结果尚未确认。只查询原请求，不会自动重新生成。'));
    const query=button('查询整理状态',()=>refresh().catch(showError),'quiet');query.disabled=busy;status.append(query);
    for(const j of jobs){
      const card=el('article','research-summary-job');card.append(el('strong',null,(j.publication_mode==='unified_chat_summary_auto'&&UNIFIED_LABELS[j.status])||LABELS[j.status]));if(j.message)card.append(el('p','muted',String(j.message).slice(0,1000)));
      const cutoffTurn=turns.find(t=>t.turn_id===j.request.end_turn_id);
      card.append(el('p','muted','整理窗口：截至'+(cutoffTurn?String(cutoffTurn.message||'研究轮次').slice(0,80):'已保存轮次')+' · 最多 '+j.request.max_turns+' 轮'));
      if(j.request.target_page_id){const targetPage=pages.find(p=>p.page_id===j.request.target_page_id);card.append(el('p','muted','目标主题：'+String(targetPage?.title||'原选择的主题').slice(0,120)),button('查看目标主题',()=>go('#wiki/'+enc(j.request.target_page_id)),'quiet'));}
      const disabled=busy||Boolean(pending);
      if(j.status==='completed')card.append(button('查看 Wiki',()=>go('#wiki/'+enc(j.page_id)),'quiet'));
      if(ACTIVE.has(j.status)){const b=button('取消整理',()=>command(j,'cancel'),'quiet');b.disabled=disabled;card.append(b);}
      if(['save_failed','publish_failed'].includes(j.status)&&j.can_save_retry===true){const b=button(j.status==='publish_failed'?'继续发布（不重新生成）':'继续保存（不重新生成）',()=>command(j,'save-retry'),'secondary');b.disabled=disabled;card.append(b);}
      if(j.status==='proposal_ready'){
        const diff=el('details','research-summary-diff');diff.append(el('summary',null,'查看更新差异'),el('pre',null,j.proposal.diff));card.append(diff);
        const b=button('应用到该主题',()=>command(j,'apply'),'secondary');b.disabled=disabled;card.append(b);
      }
      history.append(card);
    }
    if(nextOffset!==null){const more=button(pending?'继续查找原请求':'更多整理记录',()=>readMore().catch(showError),'quiet');more.disabled=busy;history.append(more);}
  }
  function showError(e) {if(alive()&&e?.name!=='AbortError'){error=ERRORS[e?.code]||'读取或请求未完成，请查询整理状态；未自动重发。';draw();}}
  function schedule(){clearTimeout(timer);if(alive()&&jobs.some(j=>ACTIVE.has(j.status)))timer=setTimeout(()=>poll().catch(showError),pollInterval);}
  async function chooseTarget(value){target=value;targetBase=null;const version=++targetEpoch;error='';targetLoading=Boolean(target);draw();if(!target)return;
    try{const page=await call('/api/wiki/pages/'+enc(value));if(!alive()||version!==targetEpoch)return;if(page.page_id!==value||page.current_revision!==page.revision?.revision||page.current_revision_id!==page.revision?.revision_id||!Number.isInteger(page.current_revision)||page.current_revision<1||!/^[a-f0-9]{64}$/.test(page.revision.content_hash)||page.availability==='withdrawn')throw Error('target mismatch');targetBase={base_revision:page.current_revision,base_content_hash:page.revision.content_hash};}catch(e){if(version===targetEpoch)showError(e);}finally{if(alive()&&version===targetEpoch){targetLoading=false;draw();}}
  }
  async function pageRead(offset,version){
    const result=await call(base+'?offset='+offset+'&limit=25');if(!alive()||version!==epoch)return false;
    if(!Array.isArray(result?.jobs)||result.jobs.length>25||result.jobs.some(j=>!validJob(j)))throw Error('invalid summary list');
    for(const j of result.jobs)accept(j);nextOffset=result.jobs.length===25?offset+25:null;return true;
  }
  async function refresh(){const version=++epoch;error='';let offset=0;if(!await pageRead(offset,version))return;
    // Bounded recovery scans; the continuation remains reachable for older requests.
    for(let n=1;pending&&nextOffset!==null&&n<20;n++){offset=nextOffset;if(!await pageRead(offset,version))return;}
    if(!alive()||version!==epoch)return;loaded=true;draw();schedule();
  }
  async function readMore(){if(nextOffset===null||busy)return;const version=++epoch;await pageRead(nextOffset,version);if(alive()&&version===epoch){draw();schedule();}}
  async function poll(){const version=++epoch;try{for(const j of [...jobs].filter(j=>ACTIVE.has(j.status))){const value=await call(base+'/'+enc(j.job_id));if(!alive()||version!==epoch)return;accept(value,{job_id:j.job_id,request:j.request});}if(alive()){draw();}}finally{schedule();}}
  async function createJob(){if(!alive()||busy||pending||!loaded||!modelReady||windowProblem()||jobs.some(j=>ACTIVE.has(j.status))||targetLoading||target&&!targetBase)return;
    const request=normalize({request_id:globalThis.crypto.randomUUID().replaceAll('-',''),end_turn_id:cutoff,max_turns:maxTurns,target_page_id:target||null,...(targetBase||{})});pending={operation:'create',request};if(!persist()){pending=null;draw();return;}await sendPending();
  }
  async function command(j,operation){if(!alive()||busy||pending)return;pending={operation,job_id:j.job_id,request:normalize(j.request)};if(!persist()){pending=null;draw();return;}await sendPending();}
  async function sendPending(){if(!pending||busy||!alive())return;const intent=structuredClone(pending);busy=true;++epoch;error='';draw();
    try{const value=await call(base+(intent.operation==='create'?'':'/'+enc(intent.job_id)+'/'+intent.operation),{method:'POST',json:intent.operation==='create'?intent.request:{}});if(!alive())return;accept(value,intent);if(intent.operation==='apply'&&value.status==='completed')go('#wiki/'+enc(value.page_id));}
    catch(e){if(alive()){error=ERRORS[e?.code]||'请求结果尚未确认，请查询原请求，未自动重发。';if(Number.isInteger(e.status)&&e.status>=400&&e.status<500&&![408,429].includes(e.status)){pending=null;persist();}}}
    finally{if(alive()){busy=false;draw();schedule();}}
  }
  const ready=(async()=>{const pagesRead=call('/api/wiki/pages').then(value=>{if(!alive())return;if(!Array.isArray(value?.pages)||value.pages.some(p=>!validId(p.page_id)))throw Error('invalid pages');pages=value.pages;draw();}).catch(showError);await refresh();await pagesRead;})().catch(showError);
  draw();
  return {ready,updateChat(chat,ready){if(!alive()||chat?.chat_id!==chatId)return;const previousLast=turns.at(-1)?.turn_id,wasLatest=!pending&&(!cutoff||cutoff===previousLast);turns=Array.isArray(chat.turns)?chat.turns:[];modelReady=Boolean(ready);if(wasLatest)cutoff=turns.at(-1)?.turn_id||'';draw();},dispose(){disposed=true;++epoch;++targetEpoch;clearTimeout(timer);abort.abort();}};
}
