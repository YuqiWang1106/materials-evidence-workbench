// @ts-check
import {array, object, buildReviewModel, excluded} from './document-candidate-logic.mjs';

const validHash=value=>typeof value==='string'&&/^[a-f0-9]{64}$/.test(value);
const pathFor=task=>`/api/tasks/${encodeURIComponent(task.task_id)}`;
const conflict=message=>Object.assign(Error(message),{status:409});
const canonical=value=>JSON.stringify(value,(_key,item)=>object(item)===item?Object.fromEntries(Object.entries(item).sort(([a],[b])=>a.localeCompare(b))):item);
const categoryFor=pointer=>/^\/statements\/\d+$/.test(pointer)?'statements':/\/composition\/\d+$/.test(pointer)?'composition':/\/preparation\/\d+$/.test(pointer)?'preparation':/\/(conditions|results|details)\/\d+$/.test(pointer)?'tests':null;
const identity=groups=>canonical(array(groups).map(group=>({candidate_id:group.candidate_id,raw:group.raw})).sort((a,b)=>a.candidate_id.localeCompare(b.candidate_id)));
function inheritedReviewsMatch(parentGroups,childGroups,parentTaskId){
  return parentGroups.every(parent=>{
    const child=childGroups.find(group=>group.candidate_id===parent.candidate_id),prior=array(parent.reviews),next=array(child?.reviews);
    if(prior.length!==next.length||canonical(parent.issue_annotations)!==canonical(child?.issue_annotations))return false;
    const byPointer=new Map(next.map(row=>[row.candidate_ref,row]));
    if(byPointer.size!==next.length)return false;
    return prior.every(row=>{
      const inherited=byPointer.get(row.candidate_ref);
      if(canonical(inherited)===canonical(row))return true;
      // This exact backend transformation invalidates inherited source approvals.
      const revoked={...row,previous_decision:row.decision,decision:'uncertain',source_review_invalidation:{parent_task_id:parentTaskId,reason:'prior_source_review_requires_fresh_confirmation'}};
      return ['confirmed','corrected'].includes(row.decision)&&canonical(inherited)===canonical(revoked);
    });
  });
}
function sameTask(next,task){if(!next||next.task_id!==task.task_id||next.source_id!==task.source_id||next.source_type!==task.source_type)throw Error('任务来源不一致，请重新读取。');return next;}

export function savedDocumentTime(result,task){
  sameTask(result,task);
  if(result.state!=='saved'||typeof result.updated_at!=='number'||!Number.isFinite(result.updated_at)||result.updated_at<=0||!Number.isFinite(new Date(result.updated_at*1000).getTime()))throw Error('尚未取得已保存状态和有效更新时间，请稍后重试。');
  return new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23',timeZoneName:'longOffset'}).format(new Date(result.updated_at*1000));
}

export async function readDocumentCandidates(api,task,isActive=()=>true){
  let offset=0,candidateHash,reviewHash;const groups=[];
  while(true){
    if(!isActive())throw Error('已离开当前资料。');
    const page=await api(`${pathFor(task)}/document-candidates?offset=${offset}&limit=100`);
    if(!isActive())throw Error('已离开当前资料。');
    if(page.status!=='available'||!Array.isArray(page.groups)||!validHash(page.candidate_sha256)||!validHash(page.review_sha256))throw Error('候选内容暂不可用，请重新读取。');
    if(candidateHash&&(candidateHash!==page.candidate_sha256||reviewHash!==page.review_sha256))throw conflict('候选或审核版本已变化，请重新读取。');
    candidateHash=page.candidate_sha256;reviewHash=page.review_sha256;groups.push(...page.groups);
    if(!page.has_more)break;
    if(!page.groups.length)throw Error('候选分页不完整，请重新读取。');
    offset+=page.groups.length;
  }
  return {groups,candidateHash,reviewHash};
}

export function documentSavePlan(groups,drafts={},exclusions={}){
  if(!Array.isArray(groups)||!groups.length)throw Error('没有可保存的候选内容。');
  const model=buildReviewModel(groups),batches=[],keys=new Set(),ids=new Set();let included=0;
  for(const group of groups){
    if(object(group)!==group||group.raw_status!=='available'||object(group.raw)!==group.raw||typeof group.candidate_id!=='string'||!group.candidate_id||ids.has(group.candidate_id))throw Error('候选组不完整或无法读取，请重新读取。');
    ids.add(group.candidate_id);
    const facts=new Map(model.facts.filter(fact=>fact.group===group).map(fact=>[fact.pointer,fact]));
    if(!Array.isArray(group.review_scopes))throw Error('保存范围未提供，请更新服务后重新读取。');
    const seen=new Set(),scopes=new Set();
    for(const scope of group.review_scopes){
      const scopeKey=JSON.stringify([scope?.kind,scope?.source_ref]);
      if(object(scope)!==scope||!['sample','shared_method','cross_sample_statements','unclassified'].includes(scope.kind)||typeof scope.source_ref!=='string'||!scope.source_ref.startsWith('/')||!Array.isArray(scope.candidate_refs)||scopes.has(scopeKey))throw Error('保存范围异常，请重新读取。');
      scopes.add(scopeKey);const categories=new Map(),changedCategories=new Set();
      for(const pointer of scope.candidate_refs){
        const fact=facts.get(pointer),category=categoryFor(pointer);
        if(!fact||!category||seen.has(pointer))throw Error('保存范围存在重复或未知内容，请重新读取。');
        seen.add(pointer);keys.add(fact.key);
        const hasDraft=Object.hasOwn(drafts,fact.key),omit=exclusions[fact.key]===true;
        if(hasDraft||omit)changedCategories.add(category);
        if(excluded(fact))continue;
        if(fact.malformed&&!omit)throw Error('结构异常内容须先标记为不记录，再保存。');
        if(!omit)included++;
        const correction=hasDraft?drafts[fact.key]:object(fact.review?.correction);
        if(hasDraft&&object(correction)!==correction)throw Error('本地修改格式异常，请重新核对。');
        const decision=omit?'excluded':Object.keys(correction).length||fact.review?.ownership_target_ref?'corrected':'confirmed';
        const issueHash=group.issue_annotations?.field_sha256?.[pointer];
        if(fact.review?.decision!==decision||fact.review?.source_review_invalidation||(issueHash&&fact.review?.issue_scope_sha256!==issueHash))changedCategories.add(category);
        const item={candidate_ref:pointer,decision,note:'',...(hasDraft&&!fact.malformed?{correction}:{})};
        if(!categories.has(category))categories.set(category,[]);
        categories.get(category).push({key:fact.key,item});
      }
      // A changed category must still include every unlocked member.
      for(const [category,members] of categories)if(changedCategories.has(category))batches.push({groupId:group.candidate_id,reviewScope:{kind:scope.kind,source_ref:scope.source_ref,category},members});
    }
    if(seen.size!==facts.size)throw Error('保存范围未覆盖全部内容，请重新读取。');
  }
  if([...Object.keys(drafts),...Object.keys(exclusions)].some(key=>!keys.has(key)))throw conflict('本地草稿与候选内容不一致，请重新读取后核对。');
  if(!included)throw Error('全篇没有可记录的内容，请至少保留一项。');
  return batches;
}

/** @typedef {{task: Record<string, any>, groups: any[], candidateHash: string, reviewHash: string}} SavedSnapshot */

/**
 * Runs one frozen, full-document save. All requests use server versions; no extraction is scheduled.
 * @param {{
 *   api: typeof import('./app.js').api,
 *   task: Record<string, any>,
 *   groups: any[],
 *   candidateHash: string,
 *   reviewHash: string,
 *   drafts?: Record<string, any>,
 *   exclusions?: Record<string, boolean>,
 *   isActive?: () => boolean,
 *   onSnapshot?: (snapshot: SavedSnapshot) => void,
 *   onRevision?: (snapshot: SavedSnapshot) => void,
 *   onReview?: (update: {groupId: string, keys: string[], response: Record<string, any>}) => void,
 *   onTask?: (task: Record<string, any>) => void,
 * }} options
 */
export async function saveDocumentLatest({api,task:initialTask,groups:initialGroups,candidateHash:initialHash,reviewHash:initialReview,drafts={},exclusions={},isActive=()=>true,onSnapshot=()=>{},onReview=()=>{},onTask=()=>{},onRevision=()=>{}}){
  const alive=()=>{if(!isActive())throw Error('已离开当前资料。');};
  const frozenDrafts=structuredClone(drafts),frozenExclusions=structuredClone(exclusions);
  // Check every group before a revision or review mutation can be started.
  documentSavePlan(initialGroups,frozenDrafts,frozenExclusions);
  alive();let task=sameTask(await api(pathFor(initialTask)),initialTask);alive();
  let snapshot=await readDocumentCandidates(api,task,isActive);
  if(snapshot.candidateHash!==initialHash||identity(snapshot.groups)!==identity(initialGroups))throw conflict('候选版本已变化，本地草稿已保留，请重新读取。');
  if(snapshot.reviewHash!==initialReview)throw conflict('审核版本已变化，本地草稿已保留，请重新读取后核对。');
  const hasChanges=Object.keys(frozenDrafts).length>0||Object.values(frozenExclusions).some(Boolean);
  const recover=async()=>{
    alive();task=sameTask(await api(pathFor(task)),task);alive();
    if(['publishing','write_unknown'].includes(task.state)){task=sameTask(await api(`${pathFor(task)}/reconcile`,{method:'POST'}),task);alive();}
    onTask(task);savedDocumentTime(task,task);return task;
  };
  if(['publishing','write_unknown'].includes(task.state)){
    await recover();
    if(!hasChanges)return task;
  }
  if(task.state==='saved'){
    if(!hasChanges){savedDocumentTime(task,task);onTask(task);return task;}
    // An unchanged parent hash is also the idempotency identity for a lost revision response.
    const options={method:'POST',json:{expected_candidate_sha256:snapshot.candidateHash,expected_review_sha256:snapshot.reviewHash}};
    let revision;
    try{alive();revision=await api(`${pathFor(task)}/document-review-revision`,options);}catch(error){alive();if(error.status&&error.status<500)throw error;revision=await api(`${pathFor(task)}/document-review-revision`,options);}
    alive();
    if(!revision?.task_id||revision.source_id!==task.source_id||revision.source_type!==task.source_type||revision.state!=='needs_review')throw Error('审核修订状态异常，请重新读取。');
    const revised=await readDocumentCandidates(api,revision,isActive);
    if(identity(revised.groups)!==identity(snapshot.groups))throw conflict('修订候选内容不一致，草稿未迁移。');
    if(!inheritedReviewsMatch(snapshot.groups,revised.groups,task.task_id))throw conflict('修订审核已变化，本地草稿已保留；请从我的资料打开最新版本核对。');
    documentSavePlan(revised.groups,frozenDrafts,frozenExclusions);alive();task=revision;snapshot=revised;onRevision({task,...snapshot});
  }
  if(task.state==='failed')throw Error('当前任务处于失败状态，现有服务不支持直接保存；本地草稿已保留。');
  if(task.state!=='needs_review')throw conflict('当前版本不允许保存，请打开最新审核版本。');
  const batches=documentSavePlan(snapshot.groups,frozenDrafts,frozenExclusions);
  alive();onSnapshot({task,...snapshot});let reviewHash=snapshot.reviewHash;
  for(const batch of batches){
    alive();
    const response=await api(`${pathFor(task)}/document-candidate-reviews/batch`,{method:'POST',json:{expected_candidate_sha256:snapshot.candidateHash,expected_review_sha256:reviewHash,candidate_id:batch.groupId,operator:'本地使用者',items:batch.members.map(member=>member.item),review_scope:batch.reviewScope}});
    alive();
    if(response.candidate_id!==batch.groupId||response.candidate_sha256!==snapshot.candidateHash||!validHash(response.review_sha256)||!Array.isArray(response.reviews)||response.reviews.length!==batch.members.length||new Set(response.reviews.map(row=>row.candidate_ref)).size!==batch.members.length||!batch.members.every(member=>response.reviews.some(row=>row.candidate_ref===member.item.candidate_ref&&row.decision===member.item.decision)))throw Error('审核响应不完整，请重新读取后再保存。');
    reviewHash=response.review_sha256;
    onReview({groupId:batch.groupId,keys:batch.members.map(member=>member.key),response});
  }
  alive();let result;
  try{result=await api(`${pathFor(task)}/publish`,{method:'POST',json:{expected_package_sha256:snapshot.candidateHash,expected_review_sha256:reviewHash}});}catch(error){
    alive();
    if(error.status>=400&&error.status<500){
      // Existing publication 409 responses include both CAS and semantic failures.
      // Preserve their detail; only the former requires a fresh review snapshot.
      error.documentSavePhase='publish';
      error.documentSaveConflict=['review version changed; reload before publishing','review version is required; reload before publishing','Sol 候选版本已变化，请刷新后重新确认','reviewed result version changed'].includes(error.message);
      throw error;
    }
    return recover();
  }
  alive();sameTask(result,task);
  if(['publishing','write_unknown'].includes(result.state)){task=result;return recover();}
  savedDocumentTime(result,task);onTask(result);return result;
}
