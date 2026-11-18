import {pageTitle} from './page-title.js';
import {el,button,input,field,row,textValue,commitPendingInputs} from './dom.js';
import {clone,columnName,DraftSession} from './reading-logic.mjs';
import {REGION_PROFILE,bounds,cornerRange,confirmReviewed} from './region-review-logic.mjs';
import {DraftEditorOwnership} from './draft-ownership.mjs';
import {createRegionSourceGrid} from './region-source-grid.js';
import {mountTableUpload} from './table-upload.js';

const owners=new DraftEditorOwnership(),tableKey=t=>JSON.stringify([t.asset_id,t.sheet??null]);
function downloadFile(value,name){const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'})),a=el('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
const regionName=(region,index=0)=>region.name||`表格 ${index+1}`;
function iconButton(label,path,action){
 const control=button('',action,'quiet');control.className+=' region-icon-button';control.setAttribute('aria-label',label);control.title=label;
 const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');svg.setAttribute('focusable','false');
 const shape=document.createElementNS('http://www.w3.org/2000/svg','path');shape.setAttribute('d',path);shape.setAttribute('fill','none');shape.setAttribute('stroke','currentColor');shape.setAttribute('stroke-width','1.8');shape.setAttribute('stroke-linecap','round');shape.setAttribute('stroke-linejoin','round');svg.append(shape);control.append(svg);return control;
}
function revisedRegion(original,{id,table,name,range}){
 const b=bounds(range),headerCount=original?Math.max(1,(original.header_ranges||[]).reduce((count,item)=>{const h=bounds(item);return count+h[2]-h[0]+1;},0)):1;
 const headerEnd=Math.min(b[0]+headerCount-1,b[2]);
 return {...original,id,asset_id:table.asset_id,sheet:table.sheet??null,name,table_range:range,
  header_ranges:[`${columnName(b[1])}${b[0]}:${columnName(b[3])}${headerEnd}`],
  data_ranges:headerEnd<b[2]?[`${columnName(b[1])}${headerEnd+1}:${columnName(b[3])}${b[2]}`]:[],context_ranges:[]};
}

/** Range decisions only; the backend owns sheet confirmation and source identity. */
export function mountRegionReview({root,sourceId,api,go,isCurrent=()=>true,storage=globalThis.localStorage,download=downloadFile,rawTableAnalysis=false,tableChat=false}){
 let disposed=false,session=null,tables=[],inventories=new Map(),review=null,reviewRequest=0,timer=null,scrollTimer=null,busy=false,error='',storageError='',uploadForm=null;
 let ui={table:0,region:null,sheetKey:null,scrolls:{}},modal=null,validationModal=null,mainGrid=null,mainGridKey=null,selectedCell=null,pageHost=null,modalHost=null,mainDetail=null,modifyButton=null,addButton=null,sheetHeading=null,workspace=null,topHost=null,layout=null,candidatesHost=null,sourceHost=null,actionsHost=null;
 const readerPreviews=new Map(),own=owners.acquire(sourceId||'upload'),active=()=>!disposed&&isCurrent()&&own();
 const path='/api/table-drafts/'+encodeURIComponent(sourceId||''),storageKey='kb-region-draft:'+sourceId;
 const regions=()=>session?.draft.recipe.region_review.regions||[],currentTable=()=>tables[ui.table];
 const sheetRegions=()=>{const t=currentTable();return t?regions().filter(r=>tableKey(r)===tableKey(t)):[];};
 const currentRegion=()=>sheetRegions().find(r=>r.id===ui.region),readerPending=()=>[...inventories.values()].some(inv=>inv.requires_reader_selection);
 const prepared=()=>{const t=currentTable();return t&&(session.draft.recipe.region_review.initialized_sheets||[]).some(k=>tableKey(k)===tableKey(t));};
 const editable=()=>active()&&!busy&&!session.conflict&&prepared()&&!readerPending();
 function persist(){clearTimeout(scrollTimer);scrollTimer=null;if(disposed||!session||!own())return;try{storage?.setItem(storageKey,JSON.stringify({draft:session.draft,dirty:session.dirty,ui}));storageError='';}catch{storageError='浏览器暂存不可用，请保持页面打开并保存。';}}
 function persistScroll(){clearTimeout(scrollTimer);scrollTimer=setTimeout(()=>{if(active())persist();},180);}
 function fail(e){if(!active())return;if(e.status===409)session.conflict=true;error=e.status===409?'另一处更新了草稿。当前输入已保留，请载入最新版本后重新核对。':e.message||'操作未完成，请重试。';persist();draw();}
 function selectFirst(){if(!currentRegion())ui.region=sheetRegions()[0]?.id||null;}
 function clearGrid(){mainGrid?.dispose();mainGrid=null;mainGridKey=null;selectedCell=null;}
 function nextId(){let n=1;while(regions().some(r=>r.id==='manual-'+n))n++;return 'manual-'+n;}
 function mutate(fn){
  if(!editable())return;const next=clone(session.draft.recipe);fn(next.region_review.regions);session.edit({recipe:next});reviewRequest++;error='';
  if(review)review={...review,range_confirmed:false,all_sheets_confirmed:false,ready:false,sheets:(review.sheets||[]).map(s=>tableKey(s)===tableKey(currentTable())?{...s,confirmed:false}:s)};
  selectFirst();persist();clearTimeout(timer);timer=setTimeout(()=>flush(),400);draw();
 }
 async function inspect(){
  if(!active()||!session||session.dirty||session.conflict)return false;
  const request=++reviewRequest,epoch=session.change,revision=session.draft.revision,data=await api(path+'/region-review');
  if(!active()||request!==reviewRequest||epoch!==session.change||session.dirty||data.revision!==revision||data.draft_sha256!==session.draft.draft_sha256)return false;
  review=data;draw();return true;
 }
 async function flush(){
  commitPendingInputs();clearTimeout(timer);if(!active()||!session)return false;
  try{await session.save();persist();if(!active())return false;await inspect();return active()&&!session.dirty&&!session.conflict;}
  catch(e){fail(e);return false;}
 }
 async function loadInventory(){
  const epoch=session.change,values=await Promise.all(session.draft.asset_ids.map(async id=>[id,await api('/api/assets/'+encodeURIComponent(id)+'/tables',{method:'POST',json:session.draft.reader_options[id]||{}})]));
  if(!active()||epoch!==session.change)return false;
  inventories=new Map(values);tables=values.flatMap(([id,inv])=>(inv.tables||[]).map(t=>({...t,asset_id:id})));
  ui.table=Math.max(0,Math.min(Number(ui.table)||0,tables.length-1));
  for(const [id,inv]of values)if(inv.requires_reader_selection)for(const candidate of inv.reader_candidates||[])loadReaderPreview(id,candidate);
  return true;
 }
 async function loadReaderPreview(id,candidate){
  const key=JSON.stringify([id,candidate.reader_options]),table=candidate.tables?.[0];if(readerPreviews.has(key)||!table?.nrows||!table?.ncols)return;
  readerPreviews.set(key,{loading:true});
  try{const preview=await api('/api/assets/'+encodeURIComponent(id)+'/table-preview',{method:'POST',json:{sheet:table.sheet??null,row_start:1,row_end:Math.min(5,table.nrows),column_start:1,column_end:Math.min(6,table.ncols),reader_options:candidate.reader_options}});if(active()){readerPreviews.set(key,{preview});draw();}}
  catch(e){if(active()){readerPreviews.set(key,{error:e.message});draw();}}
 }
 async function prepareIndex(index){
  if(!active()||readerPending()||session.conflict||!tables[index])return false;
  ui.table=index;ui.sheetKey=tableKey(currentTable());ui.region=null;draw();
  const t=currentTable(),epoch=session.change,prior=session;
  const draft=await api(path+'/prepare-region-sheet',{method:'POST',json:{expected_revision:session.draft.revision,asset_id:t.asset_id,sheet:t.sheet??null}});
  if(!active())return false;
  // The preparation response is a complete draft. Never replace newer inputs.
  if(session!==prior||session.change!==epoch||session.dirty)throw Error('准备工作表期间输入发生变化，已保留当前修改，请重试保存。');
  session=new DraftSession(draft,api);selectFirst();persist();await inspect();draw();return true;
 }
 function revealSheet(){sheetHeading?.focus?.({preventScroll:true});sheetHeading?.scrollIntoView?.({block:'start'});}
 async function switchSheet(index){
  if(busy||modal||validationModal||session.conflict||index===ui.table)return;busy=true;error='';let changed=false;draw();
  try{if(await flush())changed=await prepareIndex(index);}catch(e){fail(e);}finally{busy=false;if(active()){draw();if(changed)revealSheet();}}
 }
 async function retryPrepare(){
  if(busy||modal||validationModal||session.conflict||readerPending())return;busy=true;error='';draw();
  try{if(await flush())await prepareIndex(ui.table);}catch(e){fail(e);}finally{busy=false;if(active())draw();}
 }
 async function chooseReader(id,options){
  if(busy||session.conflict)return;busy=true;reviewRequest++;review=null;clearGrid();
  const next=clone(session.draft.recipe);next.region_review.regions=next.region_review.regions.filter(r=>r.asset_id!==id);
  if(next.region_review.initialized_sheets)next.region_review.initialized_sheets=next.region_review.initialized_sheets.filter(s=>s.asset_id!==id);
  session.edit({recipe:next,reader_options:{...session.draft.reader_options,[id]:clone(options)}});ui.scrolls={};persist();draw();
  try{await session.save();persist();if(!active())return;if(await loadInventory()){await inspect();if(!readerPending()&&tables.length)await prepareIndex(0);}}
  catch(e){fail(e);}finally{busy=false;if(active())draw();}
 }
 async function saveCurrentTable(){
  if(busy||modal||validationModal||session.conflict||readerPending()||!prepared()||!currentRegion())return;
  busy=true;error='';draw();
  try{
   if(!await flush())return;
   const region=currentRegion(),epoch=session.change;
   const result=await api(path+'/raw-tables',{method:'POST',json:{expected_revision:session.draft.revision,region_id:region.id,reviewed:true,operator:'本地使用者'}});
   if(!active()||epoch!==session.change)return;
   if(!result.local_saved||!result.table_version_id)throw Error('表格尚未保存，请重试。');
   persist();go(tableChat?'#table-chat/new/'+encodeURIComponent(result.table_version_id)+'?source='+encodeURIComponent(sourceId):'#table-analysis/'+encodeURIComponent(sourceId)+'/'+encodeURIComponent(result.table_version_id));
  }catch(e){if(active()&&e.rangeConflict)showValidationDialog(e.rangeConflict);else fail(e);}
  finally{busy=false;if(active())draw();}
 }
 async function confirmSheet(){
  if(busy||modal||validationModal||session.conflict||readerPending()||!prepared())return;busy=true;error='';let changed=false;draw();
  try{
   if(!await flush())return;const t=currentTable(),epoch=session.change;
   const result=await api(path+'/confirm-region-sheet',{method:'POST',json:{expected_revision:session.draft.revision,asset_id:t.asset_id,sheet:t.sheet??null,operator:'本地使用者'}});
   if(!active()||epoch!==session.change)return;review={...review,...result};
   if(!result.confirmed){showValidationDialog(result);return;}
   persist();
   if(result.next_sheet){const index=tables.findIndex(s=>tableKey(s)===tableKey(result.next_sheet));if(index<0)throw Error('未找到下一张原始工作表，请重新载入。');changed=await prepareIndex(index);}
   else if(result.all_sheets_confirmed){
    if(!regions().length){error='整个文件没有保留任何表格。请返回需要的工作表新增表格，再继续。';return;}
    if(!await inspect()||!review.ready)throw Error('范围仍有待处理的问题，请核对后重试。');
    const confirmed=await confirmReviewed({session,review,api,isCurrent:active});
    if(confirmed?.range_confirmed&&active())go('#sol-preview/'+encodeURIComponent(sourceId));
   }else throw Error('工作表确认尚未完成，请重试。');
  }catch(e){fail(e);}finally{busy=false;if(active()){draw();if(changed)revealSheet();}}
 }
 function detail(node,cell){
  node.replaceChildren();if(!cell)return;node.append(el('strong',null,cell.address+' · 原值'),el('p',null,textValue(cell.raw_value)));
  if(cell.merged_origin)node.append(el('p','help','合并原点：'+cell.merged_origin+'；空格不会继承原点值。'));
  if(cell.formula)node.append(el('p','help','原公式：'+cell.formula+'；未计算。'));
  if(cell.comment)node.append(el('p','help','原批注：'+cell.comment.text));
 }
 function selectRegion(region){if(busy||modal||validationModal)return;ui.region=region.id;selectedCell=null;persist();draw();mainGrid?.locate(region.table_range);}
 function openEditor(original=null){
  if(!editable()||modal||validationModal)return;
  const editing={original,name:original?regionName(original,sheetRegions().indexOf(original)):`表格 ${sheetRegions().length+1}`,range:original?.table_range||null,anchor:null};modal=editing;
  const overlay=el('div','region-modal-backdrop'),box=el('section','region-modal');box.setAttribute('role','dialog');box.setAttribute('aria-modal','true');box.setAttribute('aria-labelledby','region-editor-title');
  const heading=el('h2',null,original?'修改表格':'新增表格');heading.id='region-editor-title';
  const name=input(editing.name,value=>{editing.name=value;});name.setAttribute('aria-label','表格名称');name.addEventListener('input',()=>{editing.name=name.value;});
  const rangeLabel=el('p','region-pick-message'),message=el('p','message error'),cellDetail=el('div','region-cell-detail');message.setAttribute('role','alert');
  const refreshRange=()=>{rangeLabel.textContent=editing.anchor?`已选 ${columnName(editing.anchor.column)}${editing.anchor.row}，滚动原表后点选对角另一端。`:editing.range?`已选范围：${editing.range}。重新选择时依次点击两个角。`:'依次点选表格的两个对角，可连续横向和纵向滚动。';save.disabled=!editing.range||Boolean(editing.anchor);};
  const cancel=button('取消',closeEditor,'quiet'),save=button('保存表格',()=>{
   if(modal!==editing||!editable())return;const nameValue=editing.name.trim();
   if(!nameValue||Array.from(nameValue).length>120){message.textContent='请输入 1–120 字的表格名称。';name.focus();return;}
   if(!editing.range||editing.anchor){message.textContent='请选完表格的两个对角。';return;}
   try{const region=revisedRegion(original,{id:original?.id||nextId(),table:currentTable(),name:nameValue,range:editing.range});
    closeEditor(false);ui.region=region.id;mutate(list=>{const index=list.findIndex(r=>r.id===region.id);if(index>=0)list[index]=region;else list.push(region);});modifyButton?.focus?.({preventScroll:true});
   }catch(e){message.textContent=e.message;}
  },'primary');
  editing.grid=createRegionSourceGrid({table:currentTable(),readerOptions:session.draft.reader_options[currentTable().asset_id]||{},api,isCurrent:()=>active()&&modal===editing,selection:editing.range,initialRange:original?.table_range,onCell:cell=>{
   if(modal!==editing)return;detail(cellDetail,cell);
   if(!editing.anchor)editing.anchor={row:cell.row,column:cell.column};
   else{editing.range=cornerRange(editing.anchor,cell);editing.anchor=null;}
   editing.grid.setSelection(editing.range,editing.anchor);message.textContent='';refreshRange();
  }});
  refreshRange();
  box.append(heading,field('表格名称',name,rawTableAnalysis?undefined:'只修改名称与范围，原表内容保持只读。'),rangeLabel,editing.grid.element,cellDetail,message,row(cancel,save));overlay.append(box);
  box.addEventListener('keydown',event=>{
   if(event.key==='Escape'){event.preventDefault();closeEditor();}
   if(event.key==='Tab'){
    const controls=box.querySelectorAll?[...box.querySelectorAll('input,button:not([disabled]),[tabindex="0"]')]:[name,cancel,save].filter(n=>!n.disabled),first=controls[0],last=controls.at(-1);
    if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}
   }
  });
  draw();modalHost.replaceChildren(overlay);name.focus();
 }
 function closeEditor(redraw=true){
  if(!modal)return;const wasEditing=Boolean(modal.original);modal.grid.dispose();modal=null;modalHost.replaceChildren();pageHost.inert=false;
  if(redraw){draw();(wasEditing?modifyButton:addButton)?.focus?.({preventScroll:true});}
 }
 function showValidationDialog(result){
  validationModal=result;draw();
  const overlay=el('div','region-modal-backdrop'),box=el('section','region-modal region-validation');
  box.setAttribute('role','dialog');box.setAttribute('aria-modal','true');box.setAttribute('aria-labelledby','region-validation-title');
  const overlaps=result.overlaps||[];
  const heading=el('h2',null,overlaps.length?'检测到表格范围重叠':'本工作表无法确认');heading.id='region-validation-title';
  box.append(heading);
  if(overlaps.length){
   const list=el('ul','region-validation-list');
   for(const overlap of overlaps){
    const item=el('li','region-validation-item');
    item.append(el('p',null,`${overlap.left.name}（${overlap.left.range}）与 ${overlap.right.name}（${overlap.right.range}）重叠，交集：${overlap.intersection_range}`));
    list.append(item);
   }
   box.append(el('h3',null,`范围重叠（${overlaps.length}）`),list);
  }
  const details=(result.issues||[]).filter(issue=>{
   if(!['table_overlap','region_overlap'].includes(issue.code)||!issue.region_id||!issue.other_region_id)return true;
   return !overlaps.some(pair=>
    (pair.left.id===issue.region_id&&pair.right.id===issue.other_region_id)||
    (pair.right.id===issue.region_id&&pair.left.id===issue.other_region_id));
  });
  const geometryMessages={
   'Standard table requires one header and one data rectangle':'当前表格尚未明确一块表头和一块数据区，无法生成发给 Sol 的内容。',
   'Standard table requires contiguous aligned header and data rectangles':'表头和数据区需要上下连续、列范围一致，并完整覆盖当前表格范围。',
   'Standard table exceeds header, row, or column capacity':'当前表格超过处理上限：表头最多 8 行，数据最多 10000 行、50 列。',
   'Off-table context is outside the standard-table scope':'当前表格包含表外上下文，现有标准表读法暂不支持。',
   'Region requires bounded rectangular source ranges':'表格需要明确起止行列的矩形范围。',
  };
  if(details.length){
   const list=el('ul','region-validation-list');
   for(const issue of details){
    const region=regions().find(item=>item.id===issue.region_id),name=issue.name||(region?regionName(region):''),range=region?.table_range;
    const label=[name,range?`（${range}）`:''].join('');
    const message=issue.code==='standard_geometry_required'&&Object.hasOwn(geometryMessages,issue.message)?geometryMessages[issue.message]:issue.message||'后端未提供具体错误原因，请重试确认。';
    const locations=[issue.range&&issue.range!==range?`涉及范围：${issue.range}`:'',issue.merged_range?`原合并范围：${issue.merged_range}`:''].filter(Boolean);
    const item=el('li','region-validation-item');item.append(el('p',null,`${label?label+'：':''}${message}${locations.length?' · '+locations.join(' · '):''}`));list.append(item);
   }
   box.append(list);
  }else if(!overlaps.length)box.append(el('p','region-validation-list','后端未提供具体错误原因，请重试确认。'));
  const close=button('关闭',()=>closeValidation(),'primary');box.append(row(close));
  overlay.append(box);modalHost.replaceChildren(overlay);close.focus();
  box.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();closeValidation();}});
 }
 function closeValidation(regionId=null){
  if(!validationModal)return;validationModal=null;modalHost.replaceChildren();pageHost.inert=false;draw();
  const region=regionId&&regions().find(item=>item.id===regionId);
  if(region)selectRegion(region);else sheetHeading?.focus?.({preventScroll:true});
 }
 function updateSourcePanel(){
  const t=currentTable(),region=currentRegion();
  actionsHost.replaceChildren();modifyButton=null;
  if(region){const modify=iconButton('修改表格','M16 4l4 4M4 20l4-.8L19 8a2.1 2.1 0 0 0-3-3L5 16l-1 4Zm9-13 3 3',()=>openEditor(region)),remove=iconButton('删除表格','M4 7h16M10 11v6m4-6v6M5 7l1 14h12l1-14M9 7V4h6v3',()=>{if(editable())mutate(list=>list.splice(list.findIndex(r=>r.id===region.id),1));});
   modify.disabled=remove.disabled=!editable();modifyButton=modify;actionsHost.append(modify,remove);
  }
  const key=t&&JSON.stringify([tableKey(t),t.nrows,t.ncols,session.draft.reader_options[t.asset_id]||{}]);
  if(t&&key!==mainGridKey){
   clearGrid();mainGridKey=key;
   mainGrid=createRegionSourceGrid({table:t,readerOptions:session.draft.reader_options[t.asset_id]||{},api,isCurrent:()=>active()&&key===mainGridKey,selection:region?.table_range,initialScroll:ui.scrolls[tableKey(t)]||{top:0,left:0},onScroll:position=>{if(active()&&key===mainGridKey){ui.scrolls[tableKey(t)]=position;persistScroll();}},onCell:cell=>{selectedCell=cell;detail(mainDetail,cell);}});
   // Replacing a source is deliberate. Routine redraws must never move its live scroller.
   sourceHost.replaceChildren(actionsHost,mainGrid.element,mainDetail);
  }else if(!t&&mainGrid){clearGrid();sourceHost.replaceChildren(actionsHost,mainDetail);}
  mainGrid?.setSelection(region?.table_range||null);detail(mainDetail,selectedCell);
 }
 function candidatePanel(){
  const panel=el('aside','panel region-candidates'),retained=sheetRegions();panel.setAttribute('aria-label','当前工作表表格和新增操作');
  for(const [index,r]of retained.entries()){
   const card=el('div','region-candidate'+(r.id===ui.region?' active':'')),choose=button(`${regionName(r,index)} · ${r.table_range}`,()=>selectRegion(r),'quiet');choose.disabled=busy;choose.setAttribute('aria-current',r.id===ui.region?'true':'false');card.append(choose);panel.append(card);
  }
  const add=button('新增表格',()=>openEditor(),'secondary');add.disabled=!editable();addButton=add;panel.append(add);return panel;
 }
 function readerChoices(shell){
  for(const [id,inv]of inventories)if(inv.requires_reader_selection){const choices=el('section','panel');choices.append(el('h2',null,'选择原表的读取方式'),el('p','help','按编码、分隔方式与行列数选择。确认前不会整理范围。'));
   for(const c of inv.reader_candidates||[]){const o=c.reader_options||{},d=o.dialect?.delimiter||o.delimiter,q=o.dialect?.quotechar??o.quotechar,quote=q==='"'?'双引号':q==="'"?'单引号':q===null?'不使用引号':q||'默认引号',label=`${o.encoding||'原编码'} · ${d==='\t'?'Tab':d||'默认分隔'} · ${quote} · ${(c.tables||[]).map(t=>`${t.nrows} 行 × ${t.ncols} 列`).join('、')}`;
    const card=el('div','region-reader-choice'),b=button(label,()=>chooseReader(id,o),'secondary');b.disabled=busy||session.conflict;card.append(b);const sample=readerPreviews.get(JSON.stringify([id,c.reader_options]));
    if(sample?.preview){const wrap=el('div','region-reader-sample'),grid=el('table');for(const sourceRow of sample.preview.rows||[]){const line=el('tr');for(const cell of sourceRow.cells||[])line.append(el('td',null,`${cell.address} · ${textValue(cell.raw_value)}`));grid.append(line);}wrap.append(grid);card.append(wrap,el('p','help','原文预览：最多前 5 行、6 列。点击上方读法继续。'));}
    else card.append(el('p','help',sample?.error||'正在读取这种读法的原文…'));choices.append(card);
   }shell.append(choices);
  }
 }
 async function reload(){
  if(busy)return;busy=true;draw();
  try{const latest=await api(path);if(!active())return;download({draft:session.draft,confirmed:false},'范围修改备份.json');session=new DraftSession(latest,api);review=null;error='';ui.region=null;clearGrid();persist();if(await loadInventory()){await inspect();if(!readerPending()&&tables.length)await prepareIndex(ui.table);}}
  catch(e){fail(e);}finally{busy=false;if(active())draw();}
 }
 function draw(){
  if(!active()||!session)return;
  if(!pageHost){
   pageHost=el('div','region-page-host');modalHost=el('div','region-modal-host');workspace=el('div','region-workspace');topHost=el('div','region-page-top');
   layout=el('div','region-layout');candidatesHost=el('div','region-candidates-host');sourceHost=el('section','panel region-source');sourceHost.setAttribute('aria-label','原工作表内容');
   actionsHost=el('div','region-source-actions');mainDetail=el('div','region-cell-detail');sourceHost.append(actionsHost,mainDetail);
   layout.append(candidatesHost,sourceHost);workspace.append(topHost,layout);pageHost.append(workspace);root.replaceChildren(pageHost,modalHost);
  }
  const shell=el('div'),header=el('header','region-heading'),table=currentTable();
  sheetHeading=el('p','region-sheet-name',`工作表：${table?(table.sheet||'CSV 表格'):'待读取'}`);sheetHeading.tabIndex=-1;
  const context=el('div','region-file-context');context.append(pageTitle(el('h1','region-file-name',session.draft.title),'第 1 步 · 确认表格'),sheetHeading);
  const confirm=button(rawTableAnalysis?(busy?'正在处理…':'确认并保存本表'):'确认本工作表并继续',rawTableAnalysis?saveCurrentTable:confirmSheet,'primary');confirm.className+=' region-header-confirm';confirm.disabled=busy||Boolean(modal||validationModal)||!prepared()||session.conflict||readerPending()||(rawTableAnalysis&&!currentRegion());
  header.append(context,confirm);shell.append(header);
  if(rawTableAnalysis){const saved=button(tableChat?'对话分析':'已保存表格',()=>go(tableChat?'#table-chat/new?source='+encodeURIComponent(sourceId):'#table-analysis/'+encodeURIComponent(sourceId)),'quiet');saved.disabled=busy||Boolean(modal||validationModal);context.append(saved);}
  if(error){const message=el('div','message error');message.setAttribute('role','alert');message.append(el('p',null,error));
   if(session.dirty&&!session.conflict){const retry=button('重试保存',async()=>{if(await flush()){error='';draw();}},'secondary');retry.disabled=busy;message.append(retry);}
   if(!busy&&currentTable()&&!prepared()&&!readerPending()&&!session.conflict)message.append(button('重试载入工作表',retryPrepare,'secondary'));shell.append(message);
  }
  if(storageError)shell.append(el('p','message warn',storageError));
  if(session.conflict){const conflict=el('div','message warn');conflict.append(el('p',null,'草稿有冲突，当前输入不会覆盖另一处的修改。'),button('下载当前修改备份',()=>download({draft:session.draft,confirmed:false},'范围修改备份.json'),'quiet'),button('载入最新草稿',reload,'secondary'));shell.append(conflict);}
  readerChoices(shell);
  if(tables.length){
   if(tables.length>1){const progress=el('nav','region-sheet-progress');progress.setAttribute('aria-label','切换工作表');
    for(const [index,t]of tables.entries()){
     const receipt=review?.sheets?.find(s=>tableKey(s)===tableKey(t)),label=`${index+1}. ${t.sheet||'CSV 表格'}${receipt?.confirmed?' · 已确认':''}`;
     const control=button(label,()=>switchSheet(index),index===ui.table?'primary':'quiet');control.setAttribute('aria-current',index===ui.table?'step':'false');
     control.disabled=busy||Boolean(modal||validationModal)||session.conflict||readerPending()||(!rawTableAnalysis&&!receipt?.initialized&&index>ui.table);progress.append(control);
    }shell.append(progress);
   }
   candidatesHost.replaceChildren(candidatePanel());
  }
  updateSourcePanel();layout.hidden=!tables.length;
  pageHost.inert=Boolean(modal||validationModal);topHost.replaceChildren(...shell.children);
  // The grid, its ancestors and dialogs stay attached while surrounding controls refresh.
 }
 function upload(){
  const panel=el('section','panel region-upload');panel.append(pageTitle(el('h1',null,'先把表格范围看清楚'),'范围核对','上传一个 CSV 或 Excel 文件，逐张工作表核对表格。'));
  root.replaceChildren(panel);uploadForm=mountTableUpload({root:panel,api,isCurrent:active,submitLabel:'上传并核对范围',onUploaded:id=>go('#regions/'+encodeURIComponent(id))});
 }
 async function load(){
  if(!sourceId){upload();return;}root.replaceChildren(el('p','loading','正在读取范围草稿…'));busy=true;
  try{const remote=await api(path);if(!active())return;if(remote.recipe.entry_profile!==REGION_PROFILE){go('#draft/'+encodeURIComponent(sourceId)+'/2');return;}
   session=new DraftSession(remote,api);let local;try{local=JSON.parse(storage?.getItem(storageKey)||'null');}catch{}
   if(local?.draft?.source_id===sourceId&&local.dirty){session.edit({recipe:local.draft.recipe,title:local.draft.title,reader_options:local.draft.reader_options});if(local.draft.revision!==remote.revision)session.conflict=true;}
   ui={...ui,...local?.ui};ui.scrolls=ui.scrolls&&typeof ui.scrolls==='object'?ui.scrolls:{};
   if(!await loadInventory())return;selectFirst();persist();draw();
   if(session.conflict)return;if(session.dirty&&!await flush())return;else await inspect();
   if(!readerPending()&&tables.length){const remembered=ui.sheetKey?tables.findIndex(t=>tableKey(t)===ui.sheetKey):-1,next=review?.next_sheet?tables.findIndex(t=>tableKey(t)===tableKey(review.next_sheet)):-1;await prepareIndex(remembered>=0?remembered:next>=0?next:ui.table);}
  }catch(e){if(session)fail(e);else if(active())root.replaceChildren(el('p','message error',e.message),button('重新载入',load,'secondary'));}
  finally{busy=false;if(active()&&session)draw();}
 }
 const onUnload=()=>{commitPendingInputs();persist();};globalThis.window?.addEventListener('beforeunload',onUnload);
 const ready=load();return {ready,flush,dispose(){commitPendingInputs();persist();disposed=true;uploadForm?.dispose();reviewRequest++;clearTimeout(timer);mainGrid?.dispose();modal?.grid.dispose();globalThis.window?.removeEventListener('beforeunload',onUnload);}};
}
