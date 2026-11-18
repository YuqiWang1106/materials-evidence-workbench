// @ts-check
import {el,button} from './dom.js';
const enc=encodeURIComponent;
const requestId=()=>globalThis.crypto?.randomUUID?.()||'study-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
const pending=new Set(['queued','running','received','publishing']);

export function matchStudyUnit(rows,context){
 if(!context?.studyRef||!context.candidateId)return null;
 const matches=(rows||[]).filter(row=>row.current_available===true&&row.source_id===context.sourceId&&row.source_task_id===context.taskId&&(row.snapshot?.payload?.components||[row.snapshot?.payload]).some(component=>component?.candidate_id===context.candidateId&&component.study_ref===context.studyRef));
 return matches.length===1?matches[0]:null;
}

export function studyWikiState(unit){
 if(!unit)return {label:'未就绪'};
 if(unit.auto_generation_disabled)return {label:'已删除'};
 const job=(unit.wiki_jobs||[]).find(job=>job.snapshot_id===unit.current_snapshot_id);
 if(job?.status==='outcome_unknown')return {label:'结果待确认',job};
 if(job?.status==='failed')return {label:job.error?.code==='BUDGET_DENIED'?'项目额度不足':job.result?.compiled?'发布未完成':job.error?.phase==='input_preparation'?'资料暂无法整理':'整理未完成',retry:true,job};
 if(pending.has(job?.status))return {label:unit.wiki_generation?.ready===false?'未就绪':job.status==='publishing'?'正在发布':'正在整理',poll:unit.wiki_generation?.ready!==false,job};
 if(job?.status==='update_available')return {label:'有更新',job};
 if(unit.page_id)return {label:'',job};
 return {label:unit.wiki_generation?.ready?'尚未整理':'未就绪',retry:Boolean(unit.wiki_generation?.ready),job};
}

/** Read-only refresh; only an explicit retry sends a bound mutation. */
export function mountStudyUnitWiki({root,api,current,go,pollInterval=2000}){
 const el=(tag,cls,value)=>{const node=root.ownerDocument.createElement(tag);if(cls)node.className=cls;if(value!==undefined)node.textContent=String(value);return node;};
 const button=(label,action,cls)=>{const node=el('button',cls,label);node.type='button';node.addEventListener('click',action);return node;};
 const abort=new AbortController();let disposed=false,timer=null,unit=null,busy=false,key='',generation=0;
 root.className='study-unit-wiki';root.setAttribute('aria-live','polite');
 const call=(path,options={})=>api(path,{...options,signal:abort.signal});
 function draw(){if(disposed)return;root.replaceChildren();root.hidden=!current()?.studyRef;if(root.hidden)return;const state=studyWikiState(unit);if(state.label)root.append(el('span','rr-subtle',state.label));if(unit?.page_id&&!unit.auto_generation_disabled){const view=button('查看 Wiki',()=>go('#wiki/'+enc(unit.page_id)),'quiet');view.disabled=Boolean(current()?.interactionBlocked);root.append(view);}if(state.retry){const retry=button('重试',retryJob,'quiet');retry.disabled=busy||Boolean(current()?.interactionBlocked);root.append(retry);}}
 async function refresh(){if(disposed)return;clearTimeout(timer);timer=null;const context=current(),token=++generation;draw();if(!context?.studyRef)return;
  try{const result=await call('/api/sources/'+enc(context.sourceId)+'/study-units');if(disposed||token!==generation)return;unit=matchStudyUnit(result?.units,current());}catch{if(disposed||token!==generation)return;unit=null;}
  draw();if(!disposed&&studyWikiState(unit).poll){timer=setTimeout(refresh,pollInterval);/** @type {number & {unref?: () => void}} */ (timer).unref?.();}
 }
 async function retryJob(){if(disposed||busy||!unit)return;const row=unit,state=studyWikiState(row);if(!state.retry)return;busy=true;draw();try{if(state.job?.result?.compiled)await call('/api/study-wiki-jobs/'+enc(state.job.job_id)+'/retry-publication',{method:'POST',json:{}});else await call('/api/study-units/'+enc(row.unit_id)+'/wiki-jobs',{method:'POST',json:{request_id:requestId(),expected_snapshot_id:row.current_snapshot_id}});}catch{/* The read below preserves the server's durable failure/unknown status. */}finally{busy=false;if(!disposed)await refresh();}}
 return {refresh,sync(){const context=current(),next=JSON.stringify([context?.sourceId,context?.taskId,context?.candidateId,context?.studyRef]);if(next!==key){key=next;unit=null;refresh();}else draw();},dispose(){disposed=true;generation++;clearTimeout(timer);abort.abort();root.replaceChildren();}};
}

export function studyEvidenceView(payload){
 const root=el('div','wiki-study-evidence'),fact=payload?.fact;
 if(fact){root.append(el('h3',null,fact.name||'来源字段'),el('p',null,[fact.value,fact.unit,fact.basis?`基准：${fact.basis}`:''].filter(v=>v!==null&&v!==undefined&&v!=='').join(' · ')));for(const condition of fact.conditions||[])root.append(el('p',null,typeof condition==='string'?condition:JSON.stringify(condition)));if(fact.owner_label||fact.object_label)root.append(el('p',null,fact.owner_label||fact.object_label));}
 const entry=payload?.entry,quote=entry?.evidence?.quote||entry?.text;
 if(typeof quote==='string')root.append(el('blockquote',null,quote));
 for(const citation of payload?.citations||[])if(citation.evidence?.quote)root.append(el('blockquote',null,citation.evidence.quote));
 if(payload?.study_label)root.append(el('h3',null,payload.study_label));
 return root;
}
