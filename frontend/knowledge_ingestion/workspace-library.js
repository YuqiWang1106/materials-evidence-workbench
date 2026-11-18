// @ts-check
import {compareWebsiteTimes} from './website-time-logic.mjs';
import {processingView} from './document-processing.mjs';
import {compactEntityProgress} from './document-observation-view.mjs';
import {sourceActionDialog} from './source-actions.js';
import {pageTitle} from './page-title.js';
import {browseSelect,nameSort} from './browse-ui.js';
import {el,button,input} from './dom.js';
import {mountTableUpload} from './table-upload.js';

const status={queued:'等待整理',reading:'读取原件',extracting:'提取内容',validating:'核对结构',needs_review:'提取完成',publishing:'正在保存',saved:'已保存',write_unknown:'保存待核对',failed:'整理未完成',cancelled:'已取消',superseded:'历史版本',deleting:'正在删除'};
const runningStates=new Set(['queued','reading','extracting','validating','publishing','deleting']);
const walk=node=>[node,...Array.from(node.children||[]).flatMap(walk)];
const isDocument=task=>['paper','patent'].includes(task.source_type);
function documentLabels(task){
 const names=(task.original_names||[]).filter(name=>typeof name==='string'&&name.trim());
 const name=names.join('、')||'原始文件名不可用';
 const title=task.document_title??task.title;
 const subtitle=typeof title==='string'&&title!==task.source_id&&title!==task.task_id&&!names.includes(title)?title:'';
 return {name,subtitle};
}

function currentTables(group){
 const draft=group.draft,regions=draft?.recipe?.region_review?.regions;
 if(draft?.recipe?.entry_profile!=='table_regions_v1'||!Array.isArray(regions))return group.tables;
 const title=draft.title||'资料';
 return regions.map((region,index)=>({name:region.name||`表格 ${index+1}`,original_name:title,sheet:region.sheet,table_range:region.table_range}));
}

export function mountWorkspaceLibrary({root,api,go,isCurrent=()=>true}){
 root.className='workspace-library';let catalog=[],drafts=[],tasks=[],loading=true,error='',disposed=false,uploadModal=null,typeFilter='all',sort='asc';const active=()=>!disposed&&isCurrent();
 let pollTimer=null,loadController=null,loadPromise=null,actionModal=null,refreshError='';const pendingOperations=new Map(),opened=new Map();
 const pageDocument=root.ownerDocument;
 const heading=el('header','library-heading'),search=input('',()=>{},{type:'search',placeholder:'搜索文件、表格或任务'}),list=el('div','workspace-library-list');
 search.setAttribute('aria-label','搜索资料');search.addEventListener('input',render);heading.append(pageTitle(el('h1',null,'资料库'),'资料整理','浏览已有文件、工作表与整理任务。'),search);
 const summary=el('p','browse-summary'),tools=el('div','browse-toolbar');summary.setAttribute('role','status');
 tools.append(browseSelect('资料类型',[['all','全部类型'],['table','表格'],['paper','论文'],['patent','专利']],value=>{typeFilter=value;render();}),browseSelect('名称排序',[['asc','名称 A → Z'],['desc','名称 Z → A']],value=>{sort=value;render();}));
 const uploadActions=el('div','library-entry-actions'),modalHost=el('div'),actionHost=el('div'),notice=el('p','library-operation-notice'),uploadButton=button('上传表格',openUpload,'secondary');uploadActions.append(uploadButton,button('上传论文 / 专利',()=>go('#documents'),'secondary'));notice.setAttribute('role','status');root.replaceChildren(heading,uploadActions,tools,notice,summary,list,modalHost,actionHost);
 function closeUpload(force=false,restoreFocus=true){
  if(!uploadModal||(!force&&uploadModal.controller.isBusy()))return;
  const current=uploadModal;uploadModal=null;current.controller.dispose();current.dialog.close?.();modalHost.replaceChildren();if(restoreFocus&&active())uploadButton.focus();
 }
 function openUpload(){
  if(!active())return;if(uploadModal){uploadModal.controller.file.focus();return;}
  const dialog=el('dialog','library-upload-dialog'),title=el('h2',null,'上传表格'),host=el('div'),current={dialog,controller:null};
  title.id='library-upload-title';dialog.setAttribute('aria-label','上传表格');dialog.setAttribute('aria-labelledby',title.id);dialog.setAttribute('aria-modal','true');dialog.append(title,host);uploadModal=current;
  current.controller=mountTableUpload({root:host,api,isCurrent:()=>active()&&uploadModal===current,onCancel:()=>closeUpload(),onUploaded:sourceId=>{if(!active()||uploadModal!==current)return;closeUpload(true,false);go('#regions/'+encodeURIComponent(sourceId));}});
  dialog.addEventListener('cancel',event=>{event.preventDefault();closeUpload();});modalHost.append(dialog);
  if(dialog.showModal)dialog.showModal();else{dialog.open=true;dialog.setAttribute('open','');}current.controller.file.focus();
 }
 function actionButtons(sourceId,title,task){
  const box=el('div','library-source-actions');
  const deleting=task?.state==='deleting'||pendingOperations.has(sourceId);
  for(const [kind,label] of [['reparse','重新解析'],['delete','删除资料']]){
   const item=button(label,()=>openAction(kind,sourceId,title,item),kind==='delete'?'quiet':'secondary');
   item.dataset.focusKey=sourceId+':'+kind;
   item.disabled=deleting||(kind==='reparse'&&runningStates.has(task?.state));
   if(item.disabled)item.title=deleting?'正在清理资料，请等待操作完成':'当前任务正在处理，请结束后重新解析';
   box.append(item);
  }
  return box;
 }
 function openAction(kind,sourceId,title,trigger){
  if(!active()||actionModal)return;
  actionModal=sourceActionDialog({host:actionHost,api,sourceId,title,kind,isCurrent:active,
   onClose:()=>{actionModal=null;if(active())(walk(list).find(node=>node.dataset.focusKey===trigger.dataset.focusKey&&!node.disabled)||search).focus();},
   onComplete:operation=>{
    if(!active())return;
    if(operation.kind==='delete'&&operation.state!=='completed'){
     pendingOperations.set(sourceId,{operation,title,source_type:tasks.find(t=>t.source_id===sourceId)?.source_type||'experimental'});
     tasks=tasks.map(t=>t.source_id===sourceId?{...t,state:'deleting',source_operation:operation}:t);
    }
    notice.textContent=operation.kind==='delete'?'删除请求已登记，正在停止处理并清理内容。':'完整重解析已开始，旧结果已撤下。';
    render();load(true);
    if(operation.kind==='reparse'&&operation.result?.draft_revision)go('#regions/'+encodeURIComponent(sourceId));
   }});
 }
 function preserveState(){
  for(const card of Array.from(list.children||[])){
   const id=card.dataset.sourceId;if(!id)continue;
   let index=0;
   for(const node of walk(card)){
    if(node.tagName?.toLowerCase()==='details')opened.set(id+':'+(node.dataset.stateKey||index++),Boolean(node.open));
   }
  }
  const focused=pageDocument.activeElement;
  return walk(list).includes(focused)?focused.dataset.focusKey:null;
 }
 function restoreState(focusKey){
  for(const card of Array.from(list.children||[])){
   const id=card.dataset.sourceId;if(!id)continue;
   let index=0;
   for(const node of walk(card)){
    if(node.tagName?.toLowerCase()==='details'){
     const key=id+':'+(node.dataset.stateKey||index++);if(opened.has(key))node.open=opened.get(key);
    }
    if(['button','summary'].includes(node.tagName?.toLowerCase())&&!node.dataset.focusKey)node.dataset.focusKey=id+':'+node.tagName.toLowerCase()+':'+node.textContent;
   }
  }
  if(focusKey)walk(list).find(node=>node.dataset.focusKey===focusKey&&!node.disabled)?.focus({preventScroll:true});
 }
 function render(){
  if(!active())return;const focused=preserveState(),scrollPositions=[];for(let node=root;node;node=node.parentElement)scrollPositions.push([node,node.scrollTop]);list.replaceChildren();const q=search.value.trim().toLocaleLowerCase();
  if(loading){summary.textContent='';list.append(el('p','loading','正在读取资料…'));return;}
  if(error){summary.textContent='';list.append(el('div','message error',error),button('重试',load,'secondary'));return;}
  const latest=new Map();for(const table of catalog){const old=latest.get(table.table_id);if(!old||compareWebsiteTimes(table.created_at,old.created_at)>0)latest.set(table.table_id,table);}
  const bySource=new Map();
  const sourceGroup=sourceId=>{const key=sourceId||'unknown';if(!bySource.has(key))bySource.set(key,{tables:[],draft:null,tasks:[]});return bySource.get(key);};
  for(const table of latest.values())sourceGroup(table.source_id).tables.push(table);
  for(const draft of drafts)sourceGroup(draft.source_id).draft=draft;
  for(const task of tasks)if(!isDocument(task))sourceGroup(task.source_id).tasks.push(task);
  const visible=[];
  for(const [sourceId,group] of bySource){
   const tables=currentTables(group).slice().sort((a,b)=>(a.original_name||'').localeCompare(b.original_name||'')||(a.sheet||'').localeCompare(b.sheet||''));
   const name=group.draft?.title||tables[0]?.original_name||group.tasks[0]?.title||'资料';
   const haystack=[name,sourceId,...tables.flatMap(table=>[table.name,table.original_name,table.sheet,table.table_range]),...group.tasks.map(task=>task.title)].join(' ').toLocaleLowerCase();
   if(['all','table'].includes(typeFilter)&&(!q||haystack.includes(q)))visible.push({sourceId,...group,name,tables});
  }
  const nameCounts=new Map();for(const group of visible)nameCounts.set(group.name,(nameCounts.get(group.name)||0)+1);const seenNames=new Map();
  for(const group of nameSort(visible,g=>g.name,sort==='desc')){
   const card=el('section','library-source-card'),head=el('header','library-source-heading'),copy=el('div'),draft=group.draft;
   card.dataset.sourceId=group.sourceId;
   const currentTask=group.tasks[0];
   const duplicate=nameCounts.get(group.name)>1,ordinal=(seenNames.get(group.name)||0)+1;seenNames.set(group.name,ordinal);
   copy.append(el('h2',null,group.name),el('p','muted',(duplicate?'来源 '+ordinal+' · ':'')+group.tables.length+' 张表格'));head.append(copy);
   if(draft){
    const actions=el('div','library-source-actions'),parent=draft.recipe?.standard_review?.parent_source_id;
    const edit=button(parent?'继续核对含义':draft.recipe?.entry_profile==='table_regions_v1'?'修改':'继续整理',()=>go(parent?'#standard/'+encodeURIComponent(parent):draft.recipe?.entry_profile==='table_regions_v1'?'#regions/'+encodeURIComponent(group.sourceId):'#regions'),'secondary');edit.disabled=currentTask?.state==='deleting'||pendingOperations.has(group.sourceId);actions.append(edit);head.append(actions);
   }
   head.append(actionButtons(group.sourceId,group.name,currentTask));card.append(head);
   if(currentTask?.source_operation)card.append(el('p','library-operation-status',currentTask.source_operation.error||'正在停止处理并清理资料…'));
   else if(currentTask?.processing)card.append(processingView(currentTask.processing));
   if(currentTask?.state==='deleting'){list.append(card);continue;}
   let renderedRows=0;const overflow=el('details','library-overflow');overflow.append(el('summary',null,'展开其余表格（'+Math.max(0,group.tables.length-4)+'）'));const fileGroups=new Map();for(const table of group.tables){const key=table.asset_id||table.original_name||'原件';if(!fileGroups.has(key))fileGroups.set(key,[]);fileGroups.get(key).push(table);}
   for(const fileTables of fileGroups.values()){
    const file=el('section','library-file-group');if(fileGroups.size>1||fileTables[0].original_name!==group.name)file.append(el('h3',null,fileTables[0].original_name||'原件'));
    for(const table of fileTables){
     const row=el('div','library-table-row'),details=el('span','library-table-copy');
     const dimensions=Number.isFinite(table.row_count)&&Number.isFinite(table.column_count)?table.row_count+' 行 · '+table.column_count+' 列':null;
     details.append(el('strong',null,table.name||'表格'),el('small',null,[table.sheet,table.table_range,dimensions].filter(Boolean).join(' · ')));row.append(details);if(renderedRows++<4)file.append(row);else overflow.append(row);
    }
    card.append(file);
   }
   if(group.tables.length>4)card.append(overflow);
   if(!group.tables.length)card.append(el('p','library-no-tables',draft?.recipe?.entry_profile==='table_regions_v1'?'当前没有识别表格，可点击修改核对范围。':'尚未保存表格。'));
   list.append(card);
  }
  const documents=new Map();for(const task of tasks.filter(isDocument)){const key=task.source_id||task.task_id;if(!documents.has(key))documents.set(key,task);}
  let documentCount=0;
  for(const task of nameSort([...documents.values()],t=>documentLabels(t).name,sort==='desc')){
   if(typeFilter!=='all'&&typeFilter!==task.source_type)continue;
   const {name,subtitle}=documentLabels(task),originals=task.processing?.documents||[];
   if(q&&![name,subtitle,...originals.map(d=>d.name)].join(' ').toLocaleLowerCase().includes(q))continue;documentCount++;
   const card=el('article','library-document-card'),copy=el('div','library-document-copy');copy.append(el('span','browse-kind',task.source_type==='patent'?'专利':'论文'),el('strong',null,name));
   if(subtitle)copy.append(el('small','muted',subtitle));
   card.dataset.sourceId=task.source_id;if(task.processing)copy.append(task.document_extraction_mode==='entity-graph-v1'
     ? compactEntityProgress(task.processing,task.state) : processingView(task.processing));
   if(task.source_operation)copy.append(el('p','library-operation-status',task.source_operation.error||'正在停止处理并清理资料…'));
   if(originals.length>1){const files=el('details','library-overflow');files.append(el('summary',null,'原件清单（'+originals.length+'）'));for(const original of originals)files.append(el('p',null,original.name));copy.append(files);}
   const actions=actionButtons(task.source_id,name,task),open=button('打开资料',()=>go('#task/'+encodeURIComponent(task.task_id)),'quiet');open.disabled=task.state==='deleting';actions.append(open);
   card.append(copy,el('span','browse-status',status[task.state]||task.state||'状态未知'),actions);list.append(card);
  }
  const total=new Set([...bySource.keys(),...documents.keys()]).size;
  summary.textContent='已加载 '+total+' 份资料 · 表格来源 '+bySource.size+' · 论文 '+[...documents.values()].filter(t=>t.source_type==='paper').length+' · 专利 '+[...documents.values()].filter(t=>t.source_type==='patent').length+' · 当前显示 '+(visible.length+documentCount)+' 项';
  restoreState(focused);for(const [node,top] of scrollPositions)node.scrollTop=top;
  if(refreshError)notice.textContent=refreshError;
  if(!visible.length&&!documentCount)list.append(el('p','empty',q||typeFilter!=='all'?'没有匹配的资料。':'还没有资料。可以上传表格、论文或专利开始整理。'));
 }
 function schedule(){
  clearTimeout(pollTimer);pollTimer=null;
  if(active()&&pageDocument.visibilityState!=='hidden'&&(refreshError||tasks.some(t=>runningStates.has(t.state))||pendingOperations.size)){
   pollTimer=setTimeout(()=>load(true),3000);/** @type {number & {unref?: () => void}} */ (pollTimer).unref?.();
  }
 }
 function load(background=false){
  if(!active())return Promise.resolve();
  if(loadPromise)return loadPromise;
  const controller=new AbortController();loadController=controller;
  if(!background){loading=true;error='';render();}
  loadPromise=(async()=>{
   try{
    const [raw,draftList,taskList]=await Promise.all([api('/api/raw-tables',{signal:controller.signal}),api('/api/table-drafts',{signal:controller.signal}),api('/api/tasks',{signal:controller.signal})]);
    if(!active()||controller.signal.aborted)return;
    catalog=raw.tables||[];drafts=draftList||[];tasks=taskList||[];
    for(const [sourceId,pending] of pendingOperations){
     const operation=await api('/api/source-operations/'+encodeURIComponent(pending.operation.operation_id),{signal:controller.signal});
     if(!active()||controller.signal.aborted)return;
     if(operation.state==='completed'){pendingOperations.delete(sourceId);notice.textContent='资料及其专属子内容已删除。';}
     else if(!tasks.some(t=>t.source_id===sourceId))tasks.push({task_id:operation.operation_id,source_id:sourceId,source_type:pending.source_type,title:pending.title,state:'deleting',source_operation:operation,processing:{stage:'deleting',determinate:false}});
    }
    const deleting=new Set(tasks.filter(t=>t.state==='deleting').map(t=>t.source_id));
    catalog=catalog.filter(t=>!deleting.has(t.source_id));drafts=drafts.filter(t=>!deleting.has(t.source_id));
    loading=false;refreshError='';error='';render();
   }catch(e){
    if(!active()||controller.signal.aborted)return;
    const detail=[e.message,e.code&&'代码 '+e.code,Array.isArray(e.gaps)&&e.gaps.length?'待补 '+e.gaps.join('、'):''].filter(Boolean).join(' · ');
    if(background){refreshError='状态更新失败，已保留当前显示：'+detail;notice.textContent=refreshError;}
    else{error=detail;loading=false;render();}
   }finally{loadPromise=null;if(loadController===controller)loadController=null;schedule();}
  })();
  return loadPromise;
 }
 function visibilityChanged(){
  if(pageDocument.visibilityState==='hidden'){clearTimeout(pollTimer);pollTimer=null;loadController?.abort();}
  else if(active()){if(loadPromise)loadPromise.finally(()=>{if(active()&&pageDocument.visibilityState!=='hidden')load(true);});else load(true);}
 }
 pageDocument.addEventListener?.('visibilitychange',visibilityChanged);
 const ready=load();return {ready,refresh:()=>load(true),dispose(){
  disposed=true;clearTimeout(pollTimer);loadController?.abort();pageDocument.removeEventListener?.('visibilitychange',visibilityChanged);
  actionModal?.dispose();closeUpload(true,false);
 }};
}
