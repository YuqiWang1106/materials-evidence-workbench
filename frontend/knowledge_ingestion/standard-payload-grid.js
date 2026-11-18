import {el,button} from './dom.js';
import {bounds,contains,cornerRange,intersection} from './region-review-logic.mjs';
import {createRegionSourceGrid} from './region-source-grid.js';

/** Persistent original-sheet viewport. Preview highlights always come from server cells. */
export function createStandardPayloadGrid({root,parentId,api,isCurrent,onRange,onEditingChange,onReady}){
 const toolbar=el('div','standard-payload-toolbar'),canvas=el('div','standard-payload-source'),feedback=el('div');
 root.append(toolbar,canvas,feedback);
 let state=null,grid=null,key=null,generation=0,disposed=false,editing=false,anchor=null,message='',sourceError='',available=false;
 let draftPromise=null,metadataRevision=null;const inventories=new Map();
 const active=()=>!disposed&&isCurrent();
 function paint(){
  if(!state)return;const legend=el('div','standard-payload-legend');legend.append(el('span','payload-header-key','表头（固定）'),el('span','payload-sample-key','发送样例'));
  const edit=button(editing?'取消修改':'修改范围',()=>{
   editing=!editing;anchor=null;message='';grid?.setSelection(null);onEditingChange(editing);paint();
  },'secondary');edit.disabled=state.disabled||(!editing&&!available);toolbar.replaceChildren(legend,edit);
  feedback.replaceChildren();
  if(editing)feedback.append(el('p','standard-pick-message',anchor?'请选择右下角，可滚动工作表。':'请选择左上角。'));
  if(message){const text=el('p','message error',message);text.setAttribute('role','alert');feedback.append(text);}
  if(sourceError)feedback.append(el('p','message error',sourceError),button('重试读取工作表',()=>{key=null;sourceError='';inventories.clear();draftPromise=null;ensureGrid();},'secondary'));
  const cells=[...(state.payload?.payload?.headers||[]),...(state.payload?.payload?.examples||[])].flatMap(line=>line.cells);
  grid?.setHighlights(cells);
 }
 function pick(cell){
  if(!active()||!editing||state.disabled)return;
  const region=state.item.region;
  if(!contains(region.table_range,cell)){message='超出当前表格的范围，请在 '+region.table_range+' 内选择。';paint();return;}
  message='';
  if(!anchor){anchor=cell;grid.setSelection(null,anchor);paint();return;}
  const chosen=cornerRange(anchor,cell),sample=intersection(chosen,region.data_ranges[0]);anchor=null;grid.setSelection(null);
  if(!sample){message='请至少选择一行样例数据；完整表头会固定保留。';paint();return;}
  const b=bounds(sample);if(b[2]-b[0]+1>24){message='最多选择 24 行样例数据，请缩小发送范围。';paint();return;}
  editing=false;onEditingChange(false);onRange(sample);paint();
 }
 async function ensureGrid(){
  if(!state||!active())return;const region=state.item.region;
  const nextKey=JSON.stringify([state.revision,region.asset_id,region.sheet??null,state.item.context?.source_sha256]);
  if(key===nextKey)return;
  key=nextKey;const request=++generation;available=false;sourceError='';grid?.dispose();grid=null;
  canvas.replaceChildren(el('p','loading','正在读取工作表…'));paint();onReady();
  try{
   if(metadataRevision!==state.revision){metadataRevision=state.revision;draftPromise=null;inventories.clear();}
   draftPromise??=api('/api/table-drafts/'+encodeURIComponent(parentId));
   const draft=await draftPromise;
   if(!active()||request!==generation)return;
   if(draft.revision!==state.revision)throw Error('表格范围已变化，请载入服务端最新版。');
   const options=draft.reader_options?.[region.asset_id]||{};
   const inventoryKey=JSON.stringify([region.asset_id,options]);
   if(!inventories.has(inventoryKey))inventories.set(inventoryKey,api('/api/assets/'+encodeURIComponent(region.asset_id)+'/tables',{method:'POST',json:options}));
   const inventory=await inventories.get(inventoryKey);
   if(!active()||request!==generation)return;
   const table=inventory.tables?.find(t=>(t.sheet??null)===(region.sheet??null));
   if(!table)throw Error('找不到当前工作表，请载入服务端最新版。');
   grid=createRegionSourceGrid({table:{...table,asset_id:region.asset_id},readerOptions:options,api,isCurrent:()=>active()&&request===generation,initialRange:state.item.region.table_range,onCell:pick,
    onStatus:status=>{if(active()&&request===generation&&available!==status.ready){available=status.ready;paint();onReady();}}});
   canvas.replaceChildren(grid.element);paint();
  }catch(e){if(active()&&request===generation){sourceError=e.message||'工作表读取失败。';canvas.replaceChildren();paint();onReady();}}
 }
 return {
  get available(){return available;},
  update(next){
   const changed=state?.item.region.id!==next.item.region.id;
   state=next;
   if(changed){editing=false;anchor=null;message='';grid?.setSelection(null);if(grid&&key===JSON.stringify([state.revision,state.item.region.asset_id,state.item.region.sheet??null,state.item.context?.source_sha256]))grid.locate(state.item.region.table_range);}
   paint();ensureGrid();
  },
  reset(){generation++;key=null;available=false;draftPromise=null;inventories.clear();metadataRevision=null;editing=false;anchor=null;grid?.dispose();grid=null;canvas.replaceChildren();},
  dispose(){disposed=true;generation++;grid?.dispose();},
 };
}
