import { el, button } from './dom.js';
import { renderWikiSupport } from './research-chat-wiki.js';

const enc=encodeURIComponent;
const cleanId=x=>typeof x==='string'&&x.length>0&&x.length<=256&&!/[\s/\\\x00-\x1f]/.test(x);
const canonical=x=>JSON.stringify(x,(_,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.entries(v).sort(([a],[b])=>a.localeCompare(b))):v);
const SAME=(a,b)=>canonical(a)===canonical(b);
const TYPE={verbatim:'来源逐字内容',parsed:'解析文本，请结合原始来源核对',model_transcription:'模型转写，请结合原始来源核对',unknown:'文字来源类型未确认'};
const scalar=x=>typeof x==='string'||typeof x==='number'||typeof x==='boolean'?String(x):null;
const FIELD={'composition.amount':'组分含量','observation.value':'测量记录','condition.value':'测试条件','property.value':'来源属性'};
const QUAL={loi:'氧指数',limiting_oxygen_index:'氧指数',ul94_rating:'燃烧等级',temperature:'温度',thickness:'厚度'};

export function researchChats(value){return [...new Set((value?.revision?.dependency_manifest?.dependencies||[]).filter(d=>d.kind==='research_turn'&&cleanId(d.chat_id)).map(d=>d.chat_id))];}
export function boundResearchClaim(claim){return Boolean(claim.research_provenance)&&!['editorial','hypothesis','limitation'].includes(claim.research_provenance?.block?.kind);}

const SCOPE_ID=['workspace_id','collection_id','corpus_snapshot_id','use_policy','manifest_sha256'];
const RECORD_ID=['workspace_id','corpus_snapshot_id','task_id','source_id','source_revision_id','record_revision_id','record_snapshot_id','package_sha256'];
const ids=x=>Array.isArray(x)&&x.length>0&&x.length<=100&&x.every(cleanId)&&new Set(x).size===x.length;
const sameScope=(a,b)=>SCOPE_ID.every(k=>(a[k]??null)===(b[k]??null));

function checkedScope(dep,selection){
  const {ref,scope}=dep;
  if(!ref||!scope||!cleanId(scope.workspace_id)||!cleanId(scope.corpus_snapshot_id)||!cleanId(scope.collection_id)||ref.workspace_id!==scope.workspace_id||ref.corpus_snapshot_id!==scope.corpus_snapshot_id)throw Error('unbound source');
  if(!['record_revision','sample_test_context','assertion','evidence','source_passage','wiki_revision'].includes(ref.kind))throw Error('unsupported reference');
  if(!['saved_extractions','source_reviewed','history','explore'].includes(scope.use_policy))throw Error('unbound policy');
  // Dependencies carry compact scopes. Only this revision's frozen selection can restore membership.
  if(!ids(selection?.source_ids)||!selection.scopes||typeof selection.scopes!=='object')throw Error('missing saved selection');
  const matches=Object.entries(selection.scopes).filter(([,bound])=>bound&&sameScope(scope,bound));
  if(matches.length!==1)throw Error('ambiguous or missing saved layer');
  const [layer,bound]=matches[0],expected=ref.kind==='source_passage'?'source_passage':ref.kind==='wiki_revision'?'wiki':'record';
  if(layer!==expected||!ids(bound.source_ids)||!bound.source_ids.every(id=>selection.source_ids.includes(id)))throw Error('source outside selection');
  if(layer==='record'){
    if(scope.use_policy==='explore'||!bound.source_ids.includes(ref.source_id)||!Array.isArray(bound.record_refs)||bound.record_refs.filter(root=>root.kind==='record_revision'&&RECORD_ID.every(k=>typeof ref[k]==='string'&&root[k]===ref[k])).length!==1)throw Error('record outside saved layer');
  }else if(layer==='wiki'){
    if(scope.use_policy!=='source_reviewed'||!Array.isArray(bound.wiki_refs)||bound.wiki_refs.filter(root=>SAME(root,ref)).length!==1)throw Error('Wiki outside saved layer');
  }else{
    // Source layers freeze source IDs and corpus manifest, not individual segments. The exact snapshot/segment is validated by get.
    const source=ref.source_snapshot_ref;
    if(!['explore','source_reviewed'].includes(scope.use_policy)||source?.kind!=='source_snapshot'||source.workspace_id!==scope.workspace_id||!bound.source_ids.includes(source.source_id))throw Error('passage outside saved layer');
  }
  return {ref,scope};
}
function checkedRow(value,binding,request){
  const {ref,scope}=binding;
  if(value?.schema_version!=='materials-knowledge/v1'||value.request_id!==request.request_id||value.operation!=='get'||value.status!=='ok'||value.workspace_id!==scope.workspace_id||value.versions?.corpus_snapshot_id!==scope.corpus_snapshot_id||scope.manifest_sha256&&value.versions?.corpus_manifest_sha256!==scope.manifest_sha256||value.scope?.collection_id!==scope.collection_id||value.scope?.use_policy!==scope.use_policy||value.scope?.sources_failed!==0||value.scope?.sources_read!==value.scope?.source_count||['stale','unknown'].includes(value.scope?.version_freshness)||value.completeness?.has_more!==false||value.completeness?.returned!==1||value.results?.length!==1||value.errors?.length)throw Error('source result mismatch');
  const row=value.results[0];if(row.kind!==ref.kind||!SAME(row.ref,ref)||row.integrity!=='verified'||!['archive_verified','local_verified'].includes(row.storage)||row.availability==='unavailable'||row.status?.source_review==='revoked'||row.source_review==='revoked')throw Error('source identity mismatch');
  return row;
}

function rowView(row){
  const content=el('div','wiki-research-source-content');
  content.append(el('p','wiki-provenance-note',row.storage==='archive_verified'?'已归档 · 归档核验通过。':'探索快照（本地校验），不代表正式归档。'));
  content.append(el('h3',null,row.kind==='wiki_revision'?'关联 Wiki 导读（非原始实测）':row.evidence_data?.source_title||row.label||'论文／专利依据'));
  if(row.kind==='wiki_revision'){content.append(el('p',null,row.body||'导读正文为空。'));return content;}
  const evidence=row.kind==='source_passage'?row:row.evidence_data;
  if(evidence){const kind=row.kind==='source_passage'?row.content_kind:row.evidence_data.effective_content_kind;content.append(el('p','wiki-provenance-note',TYPE[kind]||TYPE.unknown));if(typeof evidence.quote==='string')content.append(el('blockquote',null,evidence.quote));}
  if(row.kind==='source_passage')content.append(el('p','wiki-provenance-note','原段作为背景与缺口，不代表已核验测量或独立实验验证。'));
  for(const v of row.values||[]){const raw=scalar(v.raw_value);if(raw===null)continue;const names=Object.values(v.qualifiers||{}).map(x=>QUAL[x]).filter(Boolean);content.append(el('p',null,[names.join(' · ')||FIELD[v.field_id]||'来源记录',raw,v.unit,v.basis?`基准：${v.basis}`:''].filter(Boolean).join(' · ')));}
  if(!evidence?.quote&&!row.values?.length)content.append(el('p','muted','已读取这一版本；该依据没有单独的文本值，可返回研究对话查看上下文。'));
  return content;
}

// Locate the answer that actually used this entry, including its dependency closure.
// A chat's final selection is only a composer restoration hint for unified summaries.
function unifiedOrigin(context,deps,id){
  if(!cleanId(context?.chat_id)||!cleanId(id)||!Array.isArray(context.turn_contexts)||context.turn_contexts.length>20)throw Error('missing turn binding');
  const registry=context.registry,entry=registry?.[id];if(!entry||entry.id!==id)throw Error('missing registry entry');
  for(const turn of context.turn_contexts){
    if(!cleanId(turn.turn_id)||!Array.isArray(turn.used_ids)||!deps.some(d=>d.kind==='research_turn'&&d.chat_id===context.chat_id&&d.turn_id===turn.turn_id))throw Error('unbound turn');
    const seen=new Set(),visiting=new Set();
    function visit(key,depth=0){
      if(!cleanId(key)||depth>8||visiting.has(key)||seen.size>1000||!registry[key]||registry[key].id!==key)throw Error('invalid evidence closure');
      if(seen.has(key))return;visiting.add(key);
      const children=registry[key].dependency_ids||[];if(!Array.isArray(children))throw Error('invalid dependencies');
      for(const child of children)visit(child,depth+1);visiting.delete(key);seen.add(key);
    }
    for(const used of turn.used_ids)visit(used);
    if(seen.has(id))return {chatId:context.chat_id,turnId:turn.turn_id,entry};
  }
  throw Error('entry not used by saved window');
}
function checkedUnifiedSupport(value,entry){
  // The server freshly validates source authority. Bind that response to this
  // revision's frozen entry; only location and expanded program are added on open.
  if(!value||Object.entries(entry).some(([key,saved])=>!['location','program'].includes(key)&&!SAME(value[key],saved)))throw Error('support changed');
  if(entry.program?.artifact_ref){const ref=value.program?.artifact_ref||value.program?.ref;if(ref&&!SAME(ref,entry.program.artifact_ref))throw Error('artifact owner changed');}
  return ['wiki_content','wiki_evidence','unified_support','wiki_support'].includes(value.kind)?renderWikiSupport(value,{preview:true}):rowView(value.location||value);
}

/** Fresh authoritative reads use each support's original layer scope. No model calls. */
export async function researchEvidence({value,target,call,go,active}){
  const manifest=value.revision.dependency_manifest||{},deps=manifest.dependencies||[];
  const unified=manifest.research_summary?.input_policy_version==='unified-research-summary-v1';
  const references=deps.filter(d=>d.kind==='research_reference'),chats=researchChats(value);
  const list=el('div','wiki-evidence');target.append(list);let ready=true;
  if(manifest.research_summary&&!chats.length){list.append(el('p','wiki-error','研究对话绑定缺失，请重新整理。'));ready=false;}
  for(const [i,id] of chats.entries())list.append(button(chats.length===1?'返回研究对话':'返回研究对话 '+(i+1),()=>go('#research-chat/'+enc(id)),'quiet'));
  const seen=new Set();
  for(const dep of references){
    const key=unified?dep.research_entry_id:canonical([dep.ref,dep.scope]);if(seen.has(key))continue;seen.add(key);
    if(!active())return false;
    const item=el('article','wiki-evidence-source');item.append(el('p',null,'正在读取依据…'));list.append(item);
    try{
      if(unified){
        const origin=unifiedOrigin(manifest.research_summary,deps,dep.research_entry_id);
        const result=await call('/api/research-chats/'+enc(origin.chatId)+'/turns/'+enc(origin.turnId)+'/supports/'+enc(dep.research_entry_id));if(!active())return false;
        item.replaceChildren(checkedUnifiedSupport(result,origin.entry));continue;
      }
      const binding=checkedScope(dep,manifest.research_summary?.selection);
      const request={schema_version:'materials-knowledge/v1',request_id:globalThis.crypto.randomUUID().replaceAll('-',''),target:structuredClone(dep.ref),use_policy:dep.scope.use_policy,view:'location'};
      const result=await call('/api/knowledge/v1/get',{method:'POST',json:request});if(!active())return false;
      item.replaceChildren(rowView(checkedRow(result,binding,request)));
    }catch(e){ready=false;if(active()&&e?.name!=='AbortError')item.replaceChildren(el('p','wiki-error','此依据未能按原范围与版本读取。请返回研究对话核对来源。'));}
  }
  if(!references.length&&chats.length)list.append(el('p','muted','此窗口只保留对话目标或解释；不据此认定实测结果。'));
  return ready;
}
