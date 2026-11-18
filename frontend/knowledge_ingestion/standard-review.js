// @ts-check
import {compareWebsiteTimes} from './website-time-logic.mjs';
import {pageTitle} from './page-title.js';
import {mountStandardMeaningReview} from './standard-meaning-review.js';
import {batchPending,completeBatchItems,batchMatchesDraft,chooseBatch,batchReady,batchMatchesSubmission,batchRegresses,candidateBatchSafe,batchIdentity} from './standard-batch-state.mjs';
import {createStandardPayloadGrid} from './standard-payload-grid.js';
import {el,button,input,row,commitPendingInputs} from './dom.js';
import {initialForm} from './standard-logic.mjs';

const encode=id=>encodeURIComponent(id);
const copy=value=>structuredClone(value);
/** Human-owned review; suggestions are editable input and never confirm tasks. */
export function mountStandardReview({root,parentId,api,go,isCurrent=()=>true,storage=globalThis.localStorage,stage='payload'}){
 if(stage!=='payload')return mountStandardMeaningReview({root,parentId,api,go,isCurrent,storage,stage});
 let disposed=false,review=null,forms={},selected=0,preview=null,previewOffset=0,busy=false,dirty=false,conflict=false,
  notice='',error='',timer=null,epoch=0,confirmedCheck=false,saveChain=/** @type {Promise<void | boolean>} */ (Promise.resolve()),reloadWarning=false,discarding=false,phase='';
 const payloads={},sampleSelections={},payloadStale={},payloadChecks={},jobsByRegion={};
 const payloadLoading={},payloadAttempted={},payloadErrors={},payloadRequests={},payloadTasks={};
 let batch=null,batchLoaded=false,batchError='',batchRequest=0,submissionUncertain=false,pendingSubmission=null,pendingLookupEmpty=false,boundBatch=null,hasBatchHistory=false,reviewVerified=false,drawing=false,drawAgain=false;
 let payloadShell=null,payloadTop=null,payloadTabs=null,payloadActions=null,payloadNext=null,payloadGrid=null,payloadEditing=false,jobsLoaded=false;
 function clearPayloads(){
  for(const map of [payloads,sampleSelections,payloadStale,payloadChecks,payloadLoading,payloadAttempted,payloadErrors,payloadTasks])for(const key of Object.keys(map))delete map[key];
  for(const key of Object.keys(payloadRequests)){payloadRequests[key].abort();delete payloadRequests[key];}
  payloadEditing=false;payloadGrid?.reset();
 }
 let jobPollTimer=null,jobPollDelay=1000;
 const previewedRegions=new Set();
 const active=()=>!disposed&&isCurrent(),path='/api/table-drafts/'+encode(parentId),storageKey='kb-standard-review:'+parentId;
 const regions=()=>review?.regions||[],current=()=>regions()[selected],currentForm=()=>forms[current()?.region?.id];
 function persist(){try{storage?.setItem(storageKey,JSON.stringify({version:review?.version,revision:review?.revision,draft_sha256:review?.draft_sha256,forms,dirty,selected,pendingSubmission,boundBatch}));}catch{notice='浏览器暂存不可用，请先保持页面打开。';}}
 function local(){try{return JSON.parse(storage?.getItem(storageKey)||'null');}catch{return null;}}
 function explain(e){return e?.status===409?'另一处修改了含义草稿；当前输入已保留，请载入最新版。':e?.message||'操作未完成。';}
 async function save(){
  commitPendingInputs();clearTimeout(timer);if(!active()||!review||conflict||!dirty)return !dirty;
  const stamp=epoch,payload=copy(forms);
  saveChain=saveChain.then(async()=>{
   if(!active()||conflict||discarding||!dirty||stamp!==epoch)return false;
   // Read the CAS version when this queued write starts, after previous writes finish.
   const previous={revision:review.revision,version:review.version,draft_sha256:review.draft_sha256};
   const fresh=await api(path+'/standard-review',{method:'PUT',json:{expected_revision:review.revision,expected_version:review.version,forms:payload}});
   if(!active()||discarding)return false;
   if(previous.revision!==fresh.revision||previous.version!==fresh.version||previous.draft_sha256!==fresh.draft_sha256){
    // Meaning edits advance the CAS version, but completed candidates remain tied to the same original draft.
    const keepCandidates=stage==='meaning'&&completeBatchItems(batch)&&batchMatchesDraft(batch,{...review,...fresh});
    if(!keepCandidates){batch=null;for(const key of Object.keys(jobsByRegion))delete jobsByRegion[key];}
    batchRequest++;clearPayloads();
   }
   review={...review,...fresh};
   if(stamp===epoch){dirty=false;notice=fresh.ready?'读法已保存，可预览程序读取结果。':'读法草稿已保存；请调整未确定项后再预览。';}
   else{notice='新修改已暂存，正在继续保存…';clearTimeout(timer);timer=setTimeout(()=>save(),0);}
   persist();draw();return !dirty;
  }).catch(e=>{if(active()&&!discarding){error=explain(e);if(e?.status===409)conflict=true;notice='尚未保存到服务';persist();draw();}return false;});
  return saveChain;
 }
 async function reload(discard=false){if(busy)return;
  commitPendingInputs();
  if((dirty||conflict)&&discard!==true){reloadWarning=true;draw();return;}
  busy=true;phase='reload';reviewVerified=false;discarding=true;reloadWarning=false;epoch++;clearTimeout(timer);draw();
  try{await saveChain;const latest=await api(path+'/standard-review');if(!active())return;
   clearPayloads();for(const id of Object.keys(jobsByRegion))delete jobsByRegion[id];batch=null;batchLoaded=false;batchError='';hasBatchHistory=false;batchRequest++;jobsLoaded=false;review=latest;reviewVerified=true;restoreSubmission(pendingSubmission);forms={};for(const item of regions())if(item.context)forms[item.region.id]=copy(latest.forms?.[item.region.id]||initialForm(item.context));
   selected=Math.min(selected,Math.max(0,regions().length-1));dirty=false;conflict=false;preview=null;previewedRegions.clear();confirmedCheck=false;error='';notice='已载入服务端最新读法';persist();await refreshJobs();
  }catch(e){if(active()){error=explain(e);persist();}}finally{busy=false;discarding=false;if(active())draw();}}
 const currentId=()=>current()?.region?.id;
 function bindingApplies(){return boundBatch&&batchMatchesDraft(boundBatch,review)&&(stage==='meaning'?boundBatch.version<=review.version:boundBatch.version===review.version);}
 function restoreSubmission(value){pendingLookupEmpty=false;pendingSubmission=value&&['source_id','revision','version','draft_sha256'].every(key=>value[key]===review[key])?value:null;submissionUncertain=Boolean(pendingSubmission);}
 const reviewStamp=()=>JSON.stringify([review?.source_id,review?.revision,review?.version,review?.draft_sha256]);
 function jobStatusLabel(status){if(stage==='meaning')return ({queued:'等待 Sol 处理',running:'Sol 正在提出建议',needs_researcher:'需要你补充或修改',ready_for_review:'Sol 建议已生成，等待你核对',failed:'未生成可核对建议',outcome_unknown:'请求结果待核实'})[status]||'尚未请求模型';return ({pending:'等待排队',queued:'已排队',running:'处理中',needs_researcher:'需要补充或重试',ready_for_review:'已完成，等待审核',failed:'失败',outcome_unknown:'结果待核实',invalid:'任务身份异常'})[status]||'尚未请求模型';}
 function isPending(job){return job&&['pending','queued','running'].includes(job.status);}
 function scheduleJobPoll(delay=jobPollDelay){if(disposed||jobPollTimer)return;jobPollTimer=setTimeout(async()=>{jobPollTimer=null;await refreshJobs();},delay);}
 function setBatch(value){
  const changed=batch?.batch_id!==value?.batch_id;batch=value;if(value)boundBatch=batchIdentity(value);
  if(changed){for(const id of Object.keys(payloadChecks))delete payloadChecks[id];}
  for(const key of Object.keys(jobsByRegion))delete jobsByRegion[key];
  for(const item of value?.items||[]){
   jobsByRegion[item.region_id]={...item,expected_version:value.version,draft_sha256:value.draft_sha256};
   sampleSelections[item.region_id]=item.sample_range?{sample_range:item.sample_range}:{sample_rows:[...item.sample_rows]};
   if(changed&&payloads[item.region_id]?.payload_sha256!==item.payload_sha256){delete payloadAttempted[item.region_id];delete payloads[item.region_id];}
  }
 }
 async function refreshJobs(){if(!active()||!review)return;const request=++batchRequest,stamp=reviewStamp();
  const currentRequest=()=>active()&&request===batchRequest&&stamp===reviewStamp();
  try{
   const response=await api(path+'/standard-model-batches');if(!currentRequest())return;
   if(!response||!Array.isArray(response.batches))throw Error('批次查询返回无效，请重试查询。');
   const matchingHistory=response.batches.filter(value=>batchMatchesDraft(value,review));hasBatchHistory=matchingHistory.length>0;
   let found;
   if(pendingSubmission)found=response.batches.find(value=>batchMatchesSubmission(value,pendingSubmission))||null;
   else if(batch||bindingApplies())found=response.batches.find(value=>value.batch_id===(batch||boundBatch).batch_id)||null;
   else found=chooseBatch(response.batches,review,stage);
   const binding=batch||(bindingApplies()?boundBatch:null);
   if(found&&binding&&!batchMatchesSubmission(found,binding))throw Error('已接受批次身份已变化，请重新查询。');
   if(found&&batch&&batchRegresses(found,batch))found=found.status==='stale'?{...batch,status:'stale',ready_for_review:false,error:found.error}:batch;
   if(pendingSubmission&&!found){pendingLookupEmpty=true;batchLoaded=true;batchError='提交结果待核实，正在查询原批次。';jobsLoaded=true;draw();scheduleJobPoll();return;}
   // Once accepted, a missing batch is an incomplete lookup, never permission to send again.
   if(binding&&!found)throw Error('暂时无法读取已接受批次，请重试查询。');
   if(!found&&stage==='meaning'&&hasBatchHistory)throw Error(matchingHistory.find(value=>value.error)?.error||'批次尚未全部完成，请返回发送预览查看进度。');
   if(found&&stage==='meaning'&&!candidateBatchSafe(found,review))throw Error(found.error||'已绑定批次当前不可用于含义审核。');
   setBatch(found);batchLoaded=true;batchError='';submissionUncertain=false;pendingSubmission=null;pendingLookupEmpty=false;persist();jobPollDelay=1000;
   if(!batch){
    const legacy=await api(path+'/standard-suggestions');if(!currentRequest())return;
    for(const item of regions()){
     const matches=(legacy.jobs||[]).filter(job=>job.region_id===item.region.id&&job.draft_sha256===review.draft_sha256&&job.expected_version===review.version);
     matches.sort((a,b)=>compareWebsiteTimes(b.created_at,a.created_at)||(b.attempt||0)-(a.attempt||0));
     if(matches[0])jobsByRegion[item.region.id]=matches[0];
    }
   }
   jobsLoaded=true;draw();
   if(stage==='payload'&&batch)await Promise.all(regions().map(item=>{
    const id=item.region.id;if(!payloadAttempted[id])payloadTasks[id]=requestPayload(storedSelection(id),item);return payloadTasks[id];
   }));
   if(currentRequest()&&(batchPending(batch)||Object.values(jobsByRegion).some(isPending)))scheduleJobPoll();
  }catch(e){if(currentRequest()){
   // Manual and historical single-job forms remain usable when batch lookup is unavailable.
   pendingLookupEmpty=false;batchError=explain(e);
   jobsLoaded=true;
   if(stage==='meaning'&&!batch&&!hasBatchHistory&&!boundBatch){try{const legacy=await api(path+'/standard-suggestions');if(!currentRequest())return;for(const job of legacy.jobs||[])if(job.draft_sha256===review.draft_sha256&&job.expected_version===review.version)jobsByRegion[job.region_id]=job;}catch{}}
   draw();if(pendingSubmission||batchPending(batch)||Object.values(jobsByRegion).some(isPending)){jobPollDelay=Math.min(jobPollDelay*2,10000);scheduleJobPoll();}
  }}
 }
 function storedSelection(id){const job=jobsByRegion[id];return sampleSelections[id]||(job?.sample_range?{sample_range:job.sample_range}:job?.sample_rows?{sample_rows:job.sample_rows}:{});}
 function ensurePayload(){
  const id=currentId();if(stage!=='payload'||!jobsLoaded||busy||dirty||conflict||!id||payloadAttempted[id])return;
  payloadAttempted[id]=true;payloadTasks[id]=requestPayload(storedSelection(id));
 }
 async function requestPayload(selection={},item=current()){
  if(dirty||conflict||!item)return;
  const id=item.region.id,stamp=JSON.stringify([review.revision,review.version,review.draft_sha256]);
  payloadRequests[id]?.abort();const controller=new AbortController();payloadRequests[id]=controller;
  const checkedHash=payloadChecks[id];sampleSelections[id]=copy(selection);payloadAttempted[id]=true;payloadLoading[id]=true;payloadStale[id]=true;delete payloadErrors[id];draw();
  try{
   const result=await api(path+'/standard-payload-preview',{method:'POST',json:{expected_revision:review.revision,expected_version:review.version,region_id:id,...selection},signal:controller.signal});
   if(!active()||payloadRequests[id]!==controller||stamp!==JSON.stringify([review.revision,review.version,review.draft_sha256]))return;
   if(result.source_id!==review.source_id||result.region_id!==id||result.revision!==review.revision||result.version!==review.version||result.draft_sha256!==review.draft_sha256)throw Error('预览版本与当前资料不一致，请重新载入。');
   payloads[id]=result;sampleSelections[id]=result.payload?.sample_range?{sample_range:result.payload.sample_range}:{sample_rows:[...result.included_rows]};payloadStale[id]=false;payloadChecks[id]=checkedHash===result.payload_sha256?checkedHash:null;
   if(batch&&batch.items.find(entry=>entry.region_id===id)?.payload_sha256!==result.payload_sha256){payloadStale[id]=true;payloadChecks[id]=null;payloadErrors[id]='预览与已接受批次不一致，请载入服务端最新版。';}
  }catch(e){if(active()&&payloadRequests[id]===controller&&!controller.signal.aborted)payloadErrors[id]=explain(e);}
  finally{if(payloadRequests[id]===controller){delete payloadRequests[id];payloadLoading[id]=false;if(active())draw();}}
 }
 function allChecked(){return regions().length>0&&regions().every(({region})=>{const id=region.id;return payloads[id]&&!payloadStale[id]&&!payloadLoading[id]&&!payloadErrors[id]&&payloadChecks[id]===payloads[id].payload_sha256;});}
 function canSendBatch(){return reviewVerified&&review?.model_available&&batchLoaded&&!batchError&&!batch&&!submissionUncertain&&!busy&&!dirty&&!conflict&&!payloadEditing&&payloadGrid?.available&&allChecked();}
 async function submitBatch(){if(!canSendBatch())return;
  const body={expected_revision:review.revision,expected_version:review.version,confirmed:true,items:regions().map(({region})=>{
   const payload=payloads[region.id];return {region_id:region.id,payload_sha256:payload.payload_sha256,sample_rows:[...payload.included_rows],...(payload.payload?.sample_range?{sample_range:payload.payload.sample_range}:{}),confirmed:true};
  })};
  await dispatchBatch(body);
 }
 function canResubmit(){return reviewVerified&&pendingLookupEmpty&&pendingSubmission&&!busy&&!dirty&&!conflict;}
 async function resubmitPending(){if(!canResubmit())return;await dispatchBatch({expected_revision:pendingSubmission.revision,expected_version:pendingSubmission.version,confirmed:true,items:copy(pendingSubmission.items)});}
 async function dispatchBatch(body){const stamp=reviewStamp(),request=++batchRequest;pendingLookupEmpty=false;
  busy=true;phase='model';submissionUncertain=true;pendingSubmission={source_id:review.source_id,revision:review.revision,version:review.version,draft_sha256:review.draft_sha256,items:copy(body.items)};persist();clearTimeout(jobPollTimer);jobPollTimer=null;error='';draw();
  try{const answer=await api(path+'/standard-model-batches',{method:'POST',json:body});
   if(!active()||request!==batchRequest||stamp!==reviewStamp())return;
   if(!batchMatchesDraft(answer,review)||!batchMatchesSubmission(answer,pendingSubmission))throw Error('批次响应与当前预览版本不一致，请查询批次。');
   setBatch(answer);batchLoaded=true;batchError='';submissionUncertain=false;pendingSubmission=null;persist();notice='全部表格已提交';if(batchPending(batch))scheduleJobPoll();
  }catch(e){if(active()&&stamp===reviewStamp()){
   if([400,409,422].includes(e?.status)){pendingSubmission=null;submissionUncertain=false;persist();}
   error='提交响应未确认，正在查询已接受批次。';await refreshJobs();
   if(batch)error='';else if(!batchError)error=explain(e);
  }}finally{busy=false;if(active())draw();}
 }
 async function submitSuggestion(){if(!reviewVerified||busy||dirty||conflict||batchError||!current())return;
  const id=currentId(),payload=payloads[id],job=jobsByRegion[id],stamp=reviewStamp();
  if(!payload||payloadStale[id]||payloadLoading[id]||payloadEditing||!payloadGrid?.available||payloadChecks[id]!==payload.payload_sha256)return;
  if(!job||!['failed','outcome_unknown','needs_researcher'].includes(job.status)||job.payload_sha256!==payload.payload_sha256||batch?.status==='stale')return;
  busy=true;phase='retry';error='';payloadChecks[id]=null;batchRequest++;draw();
  try{const body={expected_revision:review.revision,expected_version:review.version,region_id:id,sample_rows:payload.included_rows,...(payload.payload?.sample_range?{sample_range:payload.payload.sample_range}:{}),payload_sha256:payload.payload_sha256,confirmed:true};
   const answer=await api(path+'/standard-suggestions/'+encode(job.job_id)+'/retry',{method:'POST',json:body});
   if(!active()||stamp!==reviewStamp())return;jobsByRegion[id]=answer;
   await refreshJobs();if(!batch)scheduleJobPoll();
  }catch(e){if(active()&&stamp===reviewStamp()){error=explain(e);await refreshJobs();}}finally{busy=false;if(active())draw();}
 }
 function drawPayload(){
  if(!payloadShell){
   payloadShell=el('div','standard-workspace standard-payload-workspace');payloadTop=el('div');payloadTabs=el('div','standard-tabs');
   const panel=el('section','panel standard-payload-review'),source=el('div');panel.append(el('h2',null,'预览内容'),source);
   payloadActions=el('div');panel.append(payloadActions);payloadNext=el('div');payloadShell.append(payloadTop,payloadTabs,panel,payloadNext);root.replaceChildren(payloadShell);
   payloadGrid=createStandardPayloadGrid({root:source,parentId,api,isCurrent:active,
    onRange:range=>requestPayload({sample_range:range}),onEditingChange:value=>{payloadEditing=value;payloadChecks[currentId()]=null;draw();},onReady:()=>draw()});
  }
  const header=el('header','region-heading'),context=el('div','region-file-context');context.append(pageTitle(el('h1','region-file-name',review?.title||'确认发送内容'),'第 2 步 · 预览发给 Sol 的内容'));
  header.append(context);payloadTop.replaceChildren(header);
  if(!review)return;
  const summary=el('div','standard-batch-summary');
  if(batch){summary.append(el('strong',null,`已完成 ${batch.items.filter(item=>item.status==='ready_for_review'&&item.form).length} / ${regions().length} 张表`));if(batch.error)summary.append(el('p','message warn',batch.error));}
  else{summary.append(el('span',null,`已核对 ${regions().filter(({region})=>payloads[region.id]&&!payloadStale[region.id]&&!payloadLoading[region.id]&&payloadChecks[region.id]===payloads[region.id].payload_sha256).length} / ${regions().length} 张表`));
   const send=button(busy&&phase==='model'?'正在提交全部表格…':'确认发送全部表格',submitBatch,'primary');send.disabled=!canSendBatch();summary.append(send);}
  header.append(summary);
  if(batchError||submissionUncertain&&!busy){const retry=button('重试查询批次',()=>refreshJobs(),'secondary');retry.disabled=busy;payloadTop.append(el('p','message error',batchError||'提交结果待确认，请先查询批次。'),retry);if(canResubmit())payloadTop.append(button('重新提交已核对内容',resubmitPending,'secondary'));}
  else if(!batchLoaded)payloadTop.append(el('p','loading','正在查询批次…'));
  const reloadButton=button('载入服务端最新版',()=>reload(),'quiet');reloadButton.disabled=busy;
  payloadTop.append(row(button('返回范围',()=>go('#regions/'+encode(parentId)),'quiet'),reloadButton));
  if(error)payloadTop.append(el('p','message error',error));
  if(conflict)payloadTop.append(el('p','message warn','版本冲突：请载入最新版后重新核对。'));
  if(dirty&&!conflict){const retry=button('重试保存',save,'secondary');retry.disabled=busy;payloadTop.append(retry);}
  if(reloadWarning)payloadTop.append(el('p','message warn','本机还有未保存的修改。载入最新版会放弃这些修改。'),row(
   button('保留本机修改',()=>{reloadWarning=false;draw();},'primary'),button('放弃本机修改并载入',()=>reload(true),'secondary')));
  const tabs=[];regions().forEach((item,index)=>{
   const control=button(`${item.region.name||`表 ${index+1}`} · ${item.region.sheet||'CSV'} · ${item.region.table_range}${batch?' · '+jobStatusLabel(jobsByRegion[item.region.id]?.status):''}`,()=>{
    if(selected!==index){selected=index;payloadEditing=false;}persist();draw();
   },selected===index?'primary':'secondary');control.disabled=busy;control.setAttribute('aria-current',selected===index?'true':'false');tabs.push(control);
  });payloadTabs.replaceChildren(...tabs);
  const item=current();payloadActions.replaceChildren();payloadNext.replaceChildren();if(!item)return;
  const id=currentId(),payload=payloads[id],job=jobsByRegion[id];
  payloadGrid.update({item,payload:payloadStale[id]?null:payload,revision:review.revision,disabled:!reviewVerified||busy||dirty||conflict||Boolean(payloadLoading[id])||Boolean(batch)||!batchLoaded||Boolean(batchError)||submissionUncertain});
  if(payloadLoading[id])payloadActions.append(el('p','loading','正在读取预览…'));
  if(payloadErrors[id])payloadActions.append(el('p','message error',payloadErrors[id]),button('重试预览',()=>requestPayload(storedSelection(id)),'secondary'));
  const matchingJob=payload&&job?.payload_sha256===payload.payload_sha256?job:null;
  if(matchingJob){payloadActions.append(el('p','standard-job-status',jobStatusLabel(job.status)));if(job.error)payloadActions.append(el('p','message warn',job.error));}
  if(!review.model_available)payloadActions.append(el('p','message warn','模型建议未配置；可进入下一步手动核对读法。'));
  if(payload&&!payloadStale[id]){
   const retry=matchingJob&&['failed','outcome_unknown','needs_researcher'].includes(job.status)&&batch?.status!=='stale';
   if(!batch||retry){
    const check=input('',()=>{},{type:'checkbox'});check.checked=payloadChecks[id]===payload.payload_sha256;check.disabled=!reviewVerified||busy||dirty||conflict||payloadEditing||!payloadGrid.available||!batchLoaded||Boolean(batchError)||submissionUncertain;
    check.setAttribute('aria-label','我已核对本次发送预览');check.addEventListener('change',()=>{if(check.disabled)return;payloadChecks[id]=check.checked?payload.payload_sha256:null;draw();});
    const label=el('label','standard-payload-consent');label.append(check,el('span',null,retry?'我已重新核对这份内容，并明确同意重试；结果待核实时，原请求可能已经计费。':'我已核对上方将发送的表格内容。'));payloadActions.append(label);
    if(retry){const send=button('明确重试模型请求',submitSuggestion,'primary');send.disabled=check.disabled||!review.model_available||!check.checked;payloadActions.append(send);}
   }
  }
  const next=button(review.model_available?'进入含义审核':'进入含义审核（手动填写）',()=>go('#standard/'+encode(parentId)),'primary');
  next.disabled=!reviewVerified||busy||dirty||conflict||payloadEditing||(review.model_available&&(!batchLoaded||Boolean(batchError)||submissionUncertain||!batchReady(batch,review,payloads,payloadStale,payloadLoading)));payloadNext.append(next);
  ensurePayload();
 }
 function draw(){if(!active())return;if(drawing){drawAgain=true;return;}drawing=true;try{drawNow();}finally{drawing=false;if(drawAgain){drawAgain=false;draw();}}}
 function drawNow(){if(active())drawPayload();}
 async function load(){root.replaceChildren(el('p','loading','正在读取已确认表区…'));
  try{const value=await api(path+'/standard-review');if(!active())return;review=value;reviewVerified=true;
   for(const item of regions())if(item.context)forms[item.region.id]=copy(value.forms?.[item.region.id]||initialForm(item.context));
   const stored=local();boundBatch=batchMatchesDraft(stored?.boundBatch,review)?stored.boundBatch:null;restoreSubmission(stored?.pendingSubmission);if(stored?.dirty&&stored.revision===review.revision&&stored.draft_sha256===review.draft_sha256){
    forms={...forms,...stored.forms};selected=Math.min(stored.selected||0,Math.max(0,regions().length-1));dirty=true;
    if(stored.version!==review.version)conflict=true;else{notice='已恢复浏览器暂存的修改';setTimeout(()=>save(),0);}
   }
   draw();await refreshJobs();if(stage==='payload')await payloadTasks[currentId()];
  }catch(e){if(active()){error=explain(e);root.replaceChildren(el('p','message error',error),button('重试载入',load,'secondary'));}}
 }
 const onUnload=()=>{commitPendingInputs();persist();};globalThis.window?.addEventListener('beforeunload',onUnload);
 const ready=load();return {ready,save,dispose(){commitPendingInputs();persist();disposed=true;clearPayloads();payloadGrid?.dispose();clearTimeout(timer);clearTimeout(jobPollTimer);globalThis.window?.removeEventListener('beforeunload',onUnload);}};
}
