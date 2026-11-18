// @ts-check
import {el,button} from './dom.js';
import {createRegionSourceGrid} from './region-source-grid.js';
import {bounds,contains} from './region-review-logic.mjs';
import {columnName} from './reading-logic.mjs';
/**
 * The source viewport and its ancestors stay mounted while meaning controls change.
 * @param {{
 *   root: HTMLElement,
 *   parentId: string,
 *   api: typeof import('./app.js').api,
 *   isCurrent?: () => boolean,
 *   onColumn?: (column: number) => void,
 *   onReady?: () => void,
 * }} options
 */
export function createMeaningGrid({root,parentId,api,isCurrent=()=>true,onColumn=()=>{},onReady=()=>{}}){
 const canvas=el('div','meaning-source-canvas'),feedback=el('div');root.append(canvas,feedback);
 let state=null,grid=null,key=null,generation=0,disposed=false,available=false,draftPromise=null,revision=null;
 const inventories=new Map(),positions=new Map();const active=()=>!disposed&&isCurrent();
 function highlight(){if(!state||!grid)return;const b=bounds(state.item.region.table_range),col=state.column;grid.setSelection(col>=b[1]&&col<=b[3]?`${columnName(col)}${b[0]}:${columnName(col)}${b[2]}`:null);}
 function pick(column){const b=bounds(state.item.region.table_range);if(column>=b[1]&&column<=b[3])onColumn(column);}
 async function ensure(){
  if(!state||!active())return;const region=state.item.region,nextKey=JSON.stringify([state.revision,region.asset_id,region.sheet??null,state.item.context?.source_sha256]);
  if(key===nextKey){highlight();return;}const previousKey=key;key=nextKey;const request=++generation;available=false;
  if(grid)positions.set(previousKey,{top:grid.element.scrollTop,left:grid.element.scrollLeft});grid?.dispose();grid=null;feedback.replaceChildren();canvas.replaceChildren(el('p','loading','正在读取原表…'));onReady();
  try{
   if(revision!==state.revision){revision=state.revision;draftPromise=null;inventories.clear();}
   draftPromise??=api('/api/table-drafts/'+encodeURIComponent(parentId));const draft=await draftPromise;if(!active()||request!==generation)return;
   if(draft.revision!==state.revision)throw Error('表格范围已变化，请载入服务端最新版。');
   const options=draft.reader_options?.[region.asset_id]||{},inventoryKey=JSON.stringify([region.asset_id,options]);
   if(!inventories.has(inventoryKey))inventories.set(inventoryKey,api('/api/assets/'+encodeURIComponent(region.asset_id)+'/tables',{method:'POST',json:options}));
   const inventory=await inventories.get(inventoryKey);if(!active()||request!==generation)return;
   const table=inventory.tables?.find(t=>(t.sheet??null)===(region.sheet??null));if(!table)throw Error('找不到当前工作表。');
   grid=createRegionSourceGrid({table:{...table,asset_id:region.asset_id},readerOptions:options,api,isCurrent:()=>active()&&request===generation,initialScroll:positions.get(key),initialRange:positions.has(key)?null:region.table_range,onCell:cell=>{if(contains(state.item.region.table_range,cell))pick(cell.column);},onColumn:pick,onStatus:status=>{if(active()&&request===generation&&available!==status.ready){available=status.ready;onReady();}}});
   canvas.replaceChildren(grid.element);highlight();
  }catch(e){if(active()&&request===generation){canvas.replaceChildren();feedback.replaceChildren(el('p','message error',e.message||'原表读取失败'),button('重试读取原表',()=>{key=null;draftPromise=null;inventories.clear();ensure();},'secondary'));onReady();}}
 }
 return {get available(){return available;},update(next){const changed=state?.item.region.id!==next.item.region.id,sameSheet=state?.item.region.sheet===next.item.region.sheet;state=next;highlight();if(changed&&sameSheet&&grid)grid.locate(next.item.region.table_range);ensure();},locate(source){if(!grid)return;const address=columnName(source.column)+source.row;const range=address+':'+address;grid.setSelection(range);return grid.locate(range);},dispose(){disposed=true;generation++;grid?.dispose();}};
}
