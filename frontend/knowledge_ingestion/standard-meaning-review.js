// @ts-check
import {compareWebsiteTimes} from './website-time-logic.mjs';
import {pageTitle} from './page-title.js';
import {groupNotices} from './result-logic.js';
import {el,button,textValue,commitPendingInputs} from './dom.js';
import {initialForm,headerPath,ROW_KINDS,COLUMN_ROLES} from './standard-logic.mjs';
import {sameForm,isPlaceholder,bindingText,curveRole,meaningIssue,headerCells} from './standard-meaning-logic.mjs';
import {completeBatchItems,chooseBatch,batchMatchesDraft,batchMatchesSubmission,batchIdentity,candidateBatchSafe} from './standard-batch-state.mjs';
import {createMeaningGrid} from './standard-meaning-grid.js';
import {renderMeaningEditor} from './standard-meaning-editor.js';
import {renderStandardCurve} from './standard-curve-view.js';
import {columnName} from './reading-logic.mjs';
const copy=v=>structuredClone(v),encode=encodeURIComponent;

/** Source-driven meaning review. Provider submission remains owned by Step165. */
export function mountStandardMeaningReview({root,parentId,api,go,isCurrent=()=>true,storage=globalThis.localStorage,stage='meaning'}){
 let disposed=false,review=null,forms={},selected=0,column=1,dirty=false,conflict=false,busy=false,preparing=false,error='',batchError='',boundBatch=null,pendingSubmission=null;
 let pendingCurve=null,savesPending=0,epoch=0,loadEpoch=0,saveChain=/** @type {Promise<void | boolean>} */ (Promise.resolve()),timer=null,grid=null,sourceGrid=null,preview=null,previewOffset=0,previewRequest=0,previewLoading=false,consent=false,reloadWarning=false;
 let scrolledSelection=-1,modalActive=false,drawing=false,drawAgain=false,verified=false;const edited=new Set(),viewed=new Set();
 const active=()=>!disposed&&isCurrent(),path='/api/table-drafts/'+encode(parentId),storageKey='kb-standard-review:'+parentId;
 const shell=el('div','standard-meaning-workspace'),top=el('div','meaning-top'),feedback=el('div','meaning-feedback'),layout=el('div','meaning-layout'),tabs=el('aside','meaning-tables'),workspace=el('div','meaning-main'),canvas=el('div','meaning-grid'),panel=el('aside','meaning-panel'),reading=el('div','meaning-reading'),modal=el('div','meaning-modal');
 root.className='standard-review standard-meaning-page';layout.append(tabs,workspace);workspace.append(canvas,panel,reading);shell.append(top,feedback,layout,modal);root.replaceChildren(shell);
 const regions=()=>review?.regions||[],current=()=>regions()[selected],currentId=()=>current()?.region.id;
 const confirmed=id=>Boolean(review?.meaning_confirmations?.[id]&&!edited.has(id)&&sameForm(forms[id],review.forms?.[id]));
 const allConfirmed=()=>Boolean(review?.all_meanings_confirmed&&regions().every(i=>confirmed(i.region.id))&&!dirty&&!conflict&&!pendingSubmission);
 const explain=e=>e?.status===409?'版本冲突：另一处修改了含义草稿；当前输入已保留，请载入服务端最新版。':e?.message||'操作未完成，请重试。';
 function persist(){if(!review)return;try{storage?.setItem(storageKey,JSON.stringify({version:review.version,revision:review.revision,draft_sha256:review.draft_sha256,forms,dirty,selected,editedRegions:[...edited],boundBatch,pendingSubmission}));}catch{error='浏览器暂存不可用，请保持页面打开并重试保存。';}}
 function stored(){try{return JSON.parse(storage?.getItem(storageKey)||'null');}catch{return null;}}
 function change(next,id=currentId()){if(!active()||!id||preparing)return;forms={...forms,[id]:copy(next)};edited.add(id);dirty=true;epoch++;preview=null;viewed.clear();consent=false;error='';persist();clearTimeout(timer);pendingCurve=null;if(next.row_kind==='curve_point'&&next.version!=='3'){pendingCurve=id;prepareEditedCurve(id);}else timer=setTimeout(()=>save(),500);draw();}
 async function prepareEditedCurve(id){
  if(!active()||preparing||conflict||!verified)return;preparing=true;error='';const stamp=epoch,form=copy(forms[id]);draw();
  try{await saveChain;if(!active()||stamp!==epoch)return;const revision=review.revision,version=review.version,draft=review.draft_sha256;
   const response=await api(path+'/standard-meaning-candidate',{method:'POST',json:{expected_revision:revision,expected_version:version,region_id:id,form}});
   if(!active()||stamp!==epoch)return;
   if(response.revision!==revision||response.version!==version||response.draft_sha256!==draft||response.region_id!==id||response.form?.version!=='3')throw Error('候选来源版本已变化，请重新载入。');
   forms={...forms,[id]:copy(response.form)};pendingCurve=null;epoch++;persist();timer=setTimeout(()=>save(),0);
  }catch(e){if(active()&&stamp===epoch){error=explain(e);if(e?.status===409)conflict=true;persist();}}
  finally{preparing=false;if(active())draw();}
 }
 async function save(confirmId=null){
  commitPendingInputs();clearTimeout(timer);if(!active()||!review||conflict||preparing||pendingCurve||!verified||batchError)return false;
  if(!dirty&&!confirmId)return true;
  const stamp=epoch,payload=copy(forms);savesPending++;busy=true;draw();
  const task=saveChain.then(async()=>{
   if(!active()||conflict||stamp!==epoch)return false;if(!dirty&&!confirmId)return true;
   const fresh=await api(path+'/standard-review',{method:'PUT',json:{expected_revision:review.revision,expected_version:review.version,forms:payload,review_flow:'per_table',...(confirmId?{confirm_region_id:confirmId}:{})}});
   if(!active())return false;review={...review,...fresh};
   for(const id of [...edited])if(sameForm(forms[id],payload[id]))edited.delete(id);
   dirty=edited.size>0;error='';persist();return !dirty;
  }).catch(e=>{if(active()){error=explain(e);if(e?.status===409)conflict=true;persist();}return false;});
  saveChain=task;const result=await task;savesPending--;busy=savesPending>0;if(active())draw();return result;
 }
 async function confirmTable(){
  if(!canConfirm())return;const id=currentId();if(!await save(id)||!active()||!confirmed(id))return;
  const next=regions().findIndex(item=>!confirmed(item.region.id));if(next<0&&allConfirmed()){go('#standard-preview/'+encode(parentId));return;}
  if(next>=0)selectTable(next);
 }
 function canConfirm(){return stage==='meaning'&&verified&&!modalActive&&!busy&&!preparing&&!pendingCurve&&!conflict&&!batchError&&grid?.available&&current()?.context&&!current()?.error&&!meaningIssue(forms[currentId()],current().context);}
 function selectTable(index){if(modalActive||busy||preparing)return;commitPendingInputs();if(index===selected)return;selected=index;column=headerCells(current()?.context)[0]?.column||1;preview=null;previewOffset=0;consent=false;previewRequest++;persist();draw();if(stage==='reading'&&allConfirmed())programPreview();}
 function mountDialog(dialog){modalActive=true;modal.replaceChildren(dialog);if(typeof dialog.showModal==='function'){dialog.open=false;dialog.removeAttribute('open');dialog.showModal();}draw();}
 function closeDialog(){sourceGrid?.dispose();sourceGrid=null;modalActive=false;modal.replaceChildren();draw();}
 function openEditor(mode){if(modalActive||!current()?.context||busy||preparing)return;const regionId=currentId();mountDialog(renderMeaningEditor({document:globalThis.document,form:forms[currentId()],context:current().context,region:current().region,column,mode,onSave:next=>{modalActive=false;modal.replaceChildren();change(next,regionId);},onCancel:closeDialog}));}
 async function prepareCandidates(request){
  const existingBound=boundBatch&&batchMatchesDraft(boundBatch,review)&&boundBatch.version<=review.version?boundBatch:null;
  if(boundBatch&&!existingBound)boundBatch=null;
  let jobs=[],hasHistory=false;batchError='';
  try{
   const response=await api(path+'/standard-model-batches');if(!active()||request!==loadEpoch)return;
   if(!Array.isArray(response?.batches))throw Error('批次查询返回无效，请重试查询。');
   const histories=response.batches.filter(b=>batchMatchesDraft(b,review));hasHistory=histories.length>0;
   const found=pendingSubmission?response.batches.find(b=>batchMatchesSubmission(b,pendingSubmission)):existingBound?response.batches.find(b=>b.batch_id===existingBound.batch_id):chooseBatch(response.batches,review,'meaning');
   if(pendingSubmission&&!found)throw Error('提交结果待核实，请返回发送预览查询已核对的原批次。');
   if(existingBound&&(!found||!batchMatchesSubmission(found,existingBound)))throw Error('暂时无法读取已接受批次，或批次身份已变化，请重试查询。');
   if(found&&(!completeBatchItems(found)||!candidateBatchSafe(found,review)))throw Error(found.error||'批次当前不可用于含义审核。');
   if(!found&&histories.length)throw Error(histories.find(b=>b.error)?.error||'批次尚未全部完成，请返回发送预览查看进度。');
   if(found){boundBatch=batchIdentity(found);pendingSubmission=null;jobs=found.items;}
   else{const response=await api(path+'/standard-suggestions');jobs=(response.jobs||[]).filter(j=>j.draft_sha256===review.draft_sha256&&j.expected_version===review.version);}
  }catch(e){
   if(!active()||request!==loadEpoch)return;
   if(pendingSubmission||existingBound||hasHistory){batchError=explain(e);return;}
   // Older manual-only services may have no batch endpoint; retain their saved forms.
   let legacyAvailable=false;try{const response=await api(path+'/standard-suggestions');jobs=(response.jobs||[]).filter(j=>j.draft_sha256===review.draft_sha256&&j.expected_version===review.version);legacyAvailable=Array.isArray(response.jobs);}catch{}
   if(!legacyAvailable&&review.model_available&&regions().some(i=>isPlaceholder(forms[i.region.id],i.context)))batchError=explain(e);
  }
  if(!active()||request!==loadEpoch)return;
  const revision=review.revision,version=review.version,draft=review.draft_sha256;
  await Promise.all(regions().map(async item=>{
   const id=item.region.id;if(!item.context)return;
   let form=forms[id];
   if(!edited.has(id)&&isPlaceholder(form,item.context)){
    const latest=jobs.filter(j=>j.region_id===id).sort((a,b)=>compareWebsiteTimes(b.created_at,a.created_at)||(b.attempt||0)-(a.attempt||0))[0];
    const candidate=latest&&['ready_for_review','needs_researcher'].includes(latest.status)?latest.form:null;
    if(candidate)form=copy(candidate);
   }
   if(form?.row_kind==='curve_point'&&form.version!=='3'){
    try{const response=await api(path+'/standard-meaning-candidate',{method:'POST',json:{expected_revision:revision,expected_version:version,region_id:id,form}});
     if(!active()||request!==loadEpoch)return;
     if(response.revision!==revision||response.version!==version||response.draft_sha256!==draft||response.region_id!==id)throw Error('候选来源版本已变化，请重新载入。');
     form=response.form;
    }catch(e){if(active()&&request===loadEpoch)batchError=explain(e);return;}
   }
   if(active()&&request===loadEpoch)forms[id]=copy(form);
  }));
 }
 async function load(discard=false){
  if(modalActive)return;commitPendingInputs();if((dirty||conflict)&&!discard){reloadWarning=true;draw();return;}
  const request=++loadEpoch;verified=false;preparing=true;busy=true;error='';batchError='';reloadWarning=false;clearTimeout(timer);epoch++;previewRequest++;consent=false;preview=null;viewed.clear();draw();
  try{
   await saveChain;const fresh=await api(path+'/standard-review');if(!active()||request!==loadEpoch)return;review=fresh;verified=true;
   const cached=stored(),saved=discard?null:cached,sameDraft=cached&&cached.revision===fresh.revision&&cached.draft_sha256===fresh.draft_sha256;
   // Discard affects editable forms only; an uncertain POST identity is immutable recovery metadata.
   const pending=cached?.pendingSubmission||pendingSubmission;
   boundBatch=sameDraft?cached.boundBatch||boundBatch:boundBatch;pendingSubmission=batchMatchesDraft(pending,fresh)?pending:null;forms={};edited.clear();pendingCurve=null;dirty=false;conflict=false;
   for(const item of regions())if(item.context)forms[item.region.id]=copy(fresh.forms?.[item.region.id]||initialForm(item.context));
   if(sameDraft&&saved?.dirty){forms={...forms,...copy(saved.forms)};dirty=true;for(const id of saved.editedRegions||Object.keys(saved.forms||{}))edited.add(id);conflict=saved.version!==fresh.version;if(conflict)error='版本冲突：当前输入已保留，请载入服务端最新版。';}
   selected=Math.max(0,Math.min(saved?.selected??selected,regions().length-1));column=headerCells(current()?.context)[0]?.column||1;
   if(stage==='meaning')await prepareCandidates(request);
   if(!active()||request!==loadEpoch)return;persist();
  }catch(e){if(active()&&request===loadEpoch){error=explain(e);verified=false;}}
  finally{if(active()&&request===loadEpoch){busy=false;preparing=false;draw();if(stage==='reading'&&allConfirmed())await programPreview();}}
 }
 async function programPreview(offset=0){
  if(!active()||!verified||!allConfirmed()||busy)return;const request=++previewRequest,index=selected,id=currentId(),version=review.version,scope=review.plan?.scopes?.[index]?.id;
  if(!scope){error='此表缺少读取范围，请返回含义审核重新确认。';draw();return;}
  previewLoading=true;preview=null;consent=false;error='';draw();
  try{const result=await api(path+'/standard-preview',{method:'POST',json:{expected_revision:review.revision,expected_version:version,scope_id:scope,offset,limit:30}});
   if(!active()||request!==previewRequest||selected!==index||review.version!==version)return;
   preview=result;previewOffset=offset;viewed.add(id);
  }catch(e){if(active()&&request===previewRequest){error=explain(e);if(e?.status===409)conflict=true;}}
  finally{if(active()&&request===previewRequest){previewLoading=false;draw();}}
 }
 async function extract(){
  if(modalActive||!allConfirmed()||!verified||busy||previewLoading||!preview||!consent||viewed.size!==regions().length)return;
  busy=true;error='';draw();try{const task=await api(path+'/standard-confirm',{method:'POST',json:{expected_revision:review.revision,expected_version:review.version,review_sha256:preview.review_sha256,preview_sha256:preview.preview_sha256,reviewed:true}});if(active())go('#task/'+encode(task.task_id));}
  catch(e){if(active()){error=explain(e);if(e?.status===409)conflict=true;consent=false;}}finally{busy=false;if(active())draw();}
 }
 function showSource(row,source){
  if(modalActive)return;const location=source?.source||source;if(!location?.row||!location?.column)return;
  sourceGrid?.dispose();const dialog=el('dialog','standard-meaning-dialog meaning-source-dialog');dialog.setAttribute('open','');dialog.setAttribute('aria-modal','true');dialog.append(el('h2',null,'原格来源'),button('关闭',closeDialog,'quiet'));
  const host=el('div','meaning-source-modal-grid');dialog.append(host);dialog.addEventListener('cancel',event=>{event.preventDefault();closeDialog();});mountDialog(dialog);
  const item=regions().find(i=>i.region.asset_id===(location.asset_ref||location.asset_id)&&i.region.sheet===location.sheet)||current();let located=false;
  sourceGrid=createMeaningGrid({root:host,parentId,api,isCurrent:active,onReady:()=>{if(sourceGrid?.available&&!located){located=true;sourceGrid.locate(location);}}});sourceGrid.update({item,revision:review.revision,column:location.column});
 }
 function drawTop(){
  top.replaceChildren();const title=el('div','region-title');title.append(pageTitle(el('h1','region-file-name',review?.title||'表格含义'),stage==='reading'?'第 4 步 · 预览读取结果':'第 3 步 · 确认表格含义'));
  const actions=el('div','row');if(stage==='meaning'){const confirm=button('确认本表并继续',confirmTable,'primary');confirm.disabled=!canConfirm();actions.append(confirm);}
  else actions.append(button('返回含义审核',()=>go('#standard/'+encode(parentId)),'secondary'));
  const reload=button('载入服务端最新版',()=>load(),'quiet');reload.disabled=modalActive||busy||preparing;actions.append(reload);top.append(title,actions);feedback.replaceChildren();
  if(error)feedback.append(el('p','message error',error));if(batchError)feedback.append(el('p','message error',batchError),button('重试查询批次',()=>load(dirty||conflict?false:true),'secondary'));
  
  if(pendingSubmission)feedback.append(button('返回发送预览',()=>go('#sol-preview/'+encode(parentId)),'secondary'));
  if(pendingCurve&&!preparing&&!conflict)feedback.append(button('重试准备曲线',()=>prepareEditedCurve(pendingCurve),'secondary'));
  if(dirty&&!busy&&!conflict&&!pendingCurve&&error)feedback.append(button('重试保存',()=>save(),'secondary'));
  if(reloadWarning)feedback.append(el('p','message warn','载入最新版会丢弃本机尚未保存的修改。'),button('放弃本机修改并载入',()=>load(true),'danger'),button('保留当前输入',()=>{reloadWarning=false;draw();},'quiet'));
 }
 function drawTabs(){tabs.replaceChildren();for(const [index,item]of regions().entries()){
  const tab=button('',()=>selectTable(index),'meaning-table'+(selected===index?' active':''));tab.setAttribute('data-region-id',item.region.id);tab.setAttribute('aria-current',String(selected===index));tab.setAttribute('data-confirmed',String(confirmed(item.region.id)));
  tab.disabled=modalActive||busy||preparing;tab.append(el('strong',null,item.region.name||'表格 '+(index+1)),el('span',null,(item.region.sheet||'CSV')+' · '+item.region.table_range),el('small',null,confirmed(item.region.id)?'已确认':'待确认'));tabs.append(tab);
 }if(scrolledSelection!==selected){const tab=tabs.children[selected];if(tab&&tabs.clientWidth&&tabs.scrollWidth>tabs.clientWidth){const left=tab.offsetLeft,right=left+tab.offsetWidth;if(left<tabs.scrollLeft)tabs.scrollLeft=left;else if(right>tabs.scrollLeft+tabs.clientWidth)tabs.scrollLeft=right-tabs.clientWidth;}else if(tab&&tabs.clientHeight&&tabs.scrollHeight>tabs.clientHeight){const top=tab.offsetTop,bottom=top+tab.offsetHeight;if(top<tabs.scrollTop)tabs.scrollTop=top;else if(bottom>tabs.scrollTop+tabs.clientHeight)tabs.scrollTop=bottom-tabs.clientHeight;}scrolledSelection=selected;} }
 function detail(label,value){const node=el('div','meaning-detail');node.append(el('span',null,label),el('strong',null,textValue(value)));return node;}
 function drawPanel(){
  panel.replaceChildren();const item=current(),form=forms[currentId()];if(!item?.context||!form){panel.append(el('p','message error',item?.error||'缺少原表信息'));return;}
  panel.append(el('h2',null,'当前列 '+columnName(column)),el('p','meaning-column-title',headerPath(item.context,column)));
  if(form.version==='3'&&form.row_kind==='curve_point'){
   const c=form.curve,role=curveRole(form,column),series=c.series?.find(s=>s.result_column===column);
   panel.append(detail('行含义','曲线点'),detail('共享横轴',columnName(c.x_column||1)+' · '+bindingText(c.x_label,item.context)),detail('当前职责',({x:'共享横轴',result:'曲线结果',ignored:'本次不整理',unresolved:'尚未确定'})[role]));
   if(role==='x')panel.append(detail('横轴名称',bindingText(c.x_label,item.context)),detail('横轴单位',bindingText(c.x_unit,item.context)));
   if(series)for(const [key,label]of [['material','材料'],['stage','阶段'],['metric','指标'],['unit','单位']])panel.append(detail(label,bindingText(series[key],item.context)));
  }else{
   const role=form.column_roles?.find(r=>r.column===column),measurement=form.measurements?.find(m=>m.result_column===column);
   panel.append(detail('行含义',ROW_KINDS.find(([key])=>key===form.row_kind)?.[1]||form.row_kind),detail('当前职责',COLUMN_ROLES.find(([key])=>key===role?.role)?.[1]||'尚未确定'));
   if(measurement)panel.append(detail('原值类型',measurement.value_type==='number'?'数值':measurement.value_type==='category'?'类别原文':'尚未确定'),detail('指标',measurement.metric?.kind==='header'?headerPath(item.context,column):'来源项目列'),detail('单位',measurement.unit?.kind==='column'?'来源列 '+columnName(measurement.unit.column):bindingText(measurement.unit,item.context)));
   const why=el('details');why.append(el('summary',null,'判断依据与来源'),el('p',null,role?.reason||'尚未判断'));panel.append(why);
  }
  const edit=button('编辑当前列',()=>openEditor('column'),'secondary'),table=button('编辑表格含义',()=>openEditor('table'),'quiet');edit.disabled=table.disabled=modalActive||busy||preparing;panel.append(edit,table);
  const issue=meaningIssue(form,item.context);if(issue)panel.append(el('p','message warn',issue));
  if(!review.model_available)panel.append(el('p','help','模型建议未配置，可手动核对原表。'));
 }
 function drawReading(){
  reading.replaceChildren();if(!allConfirmed()){reading.append(el('p','message warn','请先逐张确认全部表格含义，再预览读取结果。'));return;}
  if(previewLoading){reading.append(el('p','loading','正在读取本表…'));return;}
  if(!preview){if(!busy)reading.append(button('重试读取结果',()=>programPreview(previewOffset),'secondary'));return;}
  const result=preview.reading||{},summary=result.summary||{},completion=result.completion||{},rows=result.rows||[];
  const counts=[];if(summary.series_count>0)counts.push(summary.series_count+' 条曲线');if(summary.point_row_count>0)counts.push(summary.point_row_count+' 行原始点');if(summary.unparsed_x_point_count)counts.push(summary.unparsed_x_point_count+' 个横轴原值未解析');if(summary.unparsed_point_count)counts.push(summary.unparsed_point_count+' 个结果原值未解析');reading.append(el('p','meaning-reading-counts',counts.join(' · ')||'本表 '+(result.total??rows.length)+' 条结果'));
  if(completion.selected_scope_complete===false||summary.outcome==='partial')reading.append(el('p','message warn','本表存在尚未解析的原值，请查看具体原格。'));
  for(const item of rows){const card=el('article','meaning-result');
   if(item.series_preview){card.append(el('h2',null,[item.material_label,item.stage_label].filter(Boolean).join(' · ')||item.field||'曲线结果'),renderStandardCurve(item,{document:globalThis.document,onSource:showSource}));}
   else{card.append(el('h2',null,item.subject_label||item.contexts?.map(c=>c.sample_label||c.test_label).filter(Boolean).join('、')||item.subject||item.record_label||'记录'),detail(item.field_label||item.field||item.field_id||'结果',item.value),detail('单位',item.unit_label||item.unit||'原表未注明'));
    if(item.conditions)card.append(detail('条件',typeof item.conditions==='string'?item.conditions:JSON.stringify(item.conditions)));
    for(const [key,value]of Object.entries(item.source_bindings||{})){
     if(!key.startsWith('qualifier:condition:')&&key!=='qualifier:method'&&key!=='value'&&key!=='unit')continue;
     const label=key==='qualifier:method'?'方法':key==='value'?'原值':key==='unit'?'单位':key.slice('qualifier:condition:'.length).replace(/^header_(\d+)$/,'表头条件 $1');
     for(const source of (Array.isArray(value)?value:[value]).filter(Boolean)){
      if(key.startsWith('qualifier:'))card.append(detail(label,textValue(source.raw)));
      if(source.row&&source.column)card.append(button(label+'原格 '+columnName(source.column)+source.row,()=>showSource(item,source),'quiet'));
     }
    }
   }
   reading.append(card);
  }
  const detailedCurves=new Set(rows.filter(row=>row.series_preview?.point_issue_total>0).map(row=>row.id));
  const extraIssues=(result.issues||[]).filter(issue=>issue.code!=='typed_value_requires_review'||!issue.target_refs?.length||!issue.target_refs.every(ref=>detailedCurves.has(ref)));
  const notices=groupNotices({issues:extraIssues});if(notices.groups.length){const details=el('details','meaning-reading-issues');details.append(el('summary',null,'其他需核对项（'+extraIssues.length+'）'));for(const group of notices.groups){const label=group.items[0]?.code==='typed_value_requires_review'?'原值格式需要核对':group.label;details.append(el('p',null,label+' · '+group.items.length+' 项'));}reading.append(details);}
  const paging=el('div','row');if(previewOffset>0)paging.append(button('上一页',()=>programPreview(Math.max(0,previewOffset-30)),'quiet'));if(result.has_more)paging.append(button('下一页',()=>programPreview(previewOffset+30),'quiet'));reading.append(paging);
  const check=el('input');check.type='checkbox';check.checked=consent;check.disabled=busy;check.setAttribute('aria-label','已核对对象结果条件单位与原格');check.addEventListener('change',()=>{consent=check.checked;draw();});const label=el('label','checkbox');label.append(check,el('span',null,'我已核对各表的对象、结果、条件、单位与原格'));
  const confirm=button('确认并开始整理',extract,'primary');confirm.disabled=modalActive||busy||!consent||viewed.size!==regions().length||!allConfirmed();reading.append(label,confirm);
  if(viewed.size!==regions().length)reading.append(el('p','help','请查看左侧每张表的读取结果后再确认。'));
 }
 function render(){
  drawTop();drawTabs();canvas.hidden=stage!=='meaning';panel.hidden=stage!=='meaning';reading.hidden=stage!=='reading';workspace.className='meaning-main '+(stage==='reading'?'meaning-reading-main':'');
  if(!review){reading.hidden=false;reading.replaceChildren(el('p','loading',error?'无法读取含义草稿':'正在读取含义草稿…'));return;}
  if(stage==='meaning'){
   if(current()?.context){grid??=createMeaningGrid({root:canvas,parentId,api,isCurrent:active,onColumn:value=>{column=value;draw();},onReady:draw});grid.update({item:current(),revision:review.revision,column});}drawPanel();
  }else drawReading();
 }
 function draw(){if(!active())return;if(drawing){drawAgain=true;return;}do{drawAgain=false;drawing=true;try{render();}finally{drawing=false;}}while(drawAgain&&active());}
 const ready=load();return {ready,save,reload:load,dispose(){disposed=true;loadEpoch++;previewRequest++;clearTimeout(timer);grid?.dispose();sourceGrid?.dispose();}};
}
