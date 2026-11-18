import {compareWebsiteTimes} from './website-time-logic.mjs';
/** Durable batch identity and review gates; diagnostics.ready is a later execution gate. */
export const batchPending=batch=>Boolean(batch?.items?.some(item=>['pending','queued','running'].includes(item.status)));
export function completeBatchItems(batch){return Boolean(batch?.items?.length&&batch.items.every(item=>item.job_id&&item.status==='ready_for_review'&&item.form&&typeof item.form==='object'&&!Array.isArray(item.form)&&Object.keys(item.form).length));}
export function batchMatchesDraft(batch,review){
 const ids=review?.regions?.map(item=>item.region.id)||[];
 return Boolean(batch&&review&&batch.source_id===review.source_id&&batch.revision===review.revision&&batch.draft_sha256===review.draft_sha256&&batch.items?.length===ids.length&&new Set(batch.items.map(item=>item.region_id)).size===ids.length&&batch.items.every(item=>ids.includes(item.region_id)));
}
export function chooseBatch(batches,review,stage){
 const candidates=(batches||[]).filter(batch=>batchMatchesDraft(batch,review)&&(stage==='meaning'?batch.version<=review.version&&completeBatchItems(batch)&&candidateBatchSafe(batch,review):batch.version===review.version));
 return candidates.sort((a,b)=>b.version-a.version||compareWebsiteTimes(b.created_at,a.created_at)||String(b.batch_id).localeCompare(String(a.batch_id)))[0]||null;
}
export function batchReady(batch,review,payloads,stale,loading){
 return batchMatchesDraft(batch,review)&&batch.version===review.version&&batch.status==='ready_for_review'&&batch.ready_for_review===true&&completeBatchItems(batch)&&batch.items.every(item=>!stale[item.region_id]&&!loading[item.region_id]&&payloads[item.region_id]?.payload_sha256===item.payload_sha256);
}

export function batchMatchesSubmission(batch,snapshot){
 return Boolean(batch&&snapshot&&['source_id','revision','version','draft_sha256'].every(key=>batch[key]===snapshot[key])&&batch.items?.length===snapshot.items?.length&&snapshot.items.every(saved=>{
  const item=batch.items.find(value=>value.region_id===saved.region_id);
  return item&&item.payload_sha256===saved.payload_sha256&&JSON.stringify(item.sample_rows)===JSON.stringify(saved.sample_rows)&&(item.sample_range||null)===(saved.sample_range||null)&&(!saved.job_id||item.job_id===saved.job_id);
 }));
}
export function batchRegresses(next,previous){return previous.items.some(old=>{const item=next.items.find(value=>value.region_id===old.region_id);return (item.attempt??0)<(old.attempt??0)||((item.attempt??0)===(old.attempt??0)&&old.status==='ready_for_review'&&item.status!=='ready_for_review');});}

export function candidateBatchSafe(batch,review){
 return batch.status==='ready_for_review'&&!batch.error||batch.status==='stale'&&batch.version<review.version&&batch.error==='含义版本已变化；剩余表格不会继续发送';
}
export function batchIdentity(batch){return {batch_id:batch.batch_id,source_id:batch.source_id,revision:batch.revision,version:batch.version,draft_sha256:batch.draft_sha256,items:batch.items.map(({region_id,job_id,payload_sha256,sample_rows,sample_range})=>({region_id,job_id,payload_sha256,sample_rows:[...sample_rows],...(sample_range?{sample_range}:{})}))};}
