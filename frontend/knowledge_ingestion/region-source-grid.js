// @ts-check
import {el,button,textValue} from './dom.js';
import {clone,columnName} from './reading-logic.mjs';
import {bounds} from './region-review-logic.mjs';

const ROW_HEIGHT=40,COLUMN_WIDTH=132,HEADER_HEIGHT=32,ROW_HEADER_WIDTH=48;
const OVERSCAN_ROWS=8,OVERSCAN_COLUMNS=4,TILE_ROWS=64,TILE_COLUMNS=16,MAX_CACHED_TILES=32,MAX_REQUESTS=2;
const tileKey=(row,column)=>`${Math.floor((row-1)/TILE_ROWS)}:${Math.floor((column-1)/TILE_COLUMNS)}`;
const coordinateKey=(row,column)=>`${row}:${column}`;
function parseRange(range){try{return range?bounds(range):null;}catch{return null;}}
function highlightMap(cells){
 const result=new Map();
 for(const {row,column,payload_role}of cells){
  const name=payload_role==='table_header'?'region-payload-header':payload_role==='sample_row'?'region-payload-sample':null;
  if(name)result.set(coordinateKey(row,column),name);
 }
 return result;
}

/**
 * A persistent, read-only viewport over original source coordinates.
 * @param {{
 *   table: Record<string, any>,
 *   readerOptions?: Record<string, any>,
 *   api: typeof import('./app.js').api,
 *   isCurrent?: () => boolean,
 *   onCell?: (cell: Record<string, any>) => void,
 *   onColumn?: (column: number) => void,
 *   selection?: string,
 *   anchor?: {row: number, column: number},
 *   highlights?: any[],
 *   initialScroll?: {top: number, left: number},
 *   initialRange?: string,
 *   onScroll?: (position: {top: number, left: number}) => void,
 *   onStatus?: (status: {ready: boolean, loading: boolean, error: string | null}) => void,
 * }} options
 */
export function createRegionSourceGrid({table,readerOptions={},api,isCurrent=()=>true,onCell=()=>{},onColumn=null,selection=null,anchor=null,highlights=[],initialScroll={top:0,left:0},initialRange=null,onScroll=()=>{},onStatus=()=>{}}){
 const source={...table},options=clone(readerOptions),nrows=Math.max(0,source.nrows||0),ncols=Math.max(0,source.ncols||0);
 const element=el('div','region-grid-wrap region-source-grid'),extent=el('div','region-source-extent');
 const columnHeaders=el('div','region-source-columns'),corner=el('div','region-source-corner'),rowHeaders=el('div','region-source-rows');
 const cells=el('div','region-source-cells'),status=el('div','region-source-status');
 let disposed=false,frame=null,currentWindow=null,currentKey=null,renderedKey=null,cacheVersion=0,lastStatus=null;
 const cache=new Map(),inFlight=new Map();let needed=new Map(),waiters=[];
 const requestFrame=globalThis.requestAnimationFrame?.bind(globalThis)||((callback)=>setTimeout(callback,16));
 const cancelFrame=globalThis.cancelAnimationFrame?.bind(globalThis)||clearTimeout;
 let selectedBounds=parseRange(selection),selectedAnchor=anchor,visibleCells=[],highlighted=highlightMap(highlights);
 const active=()=>!disposed&&isCurrent();
 element.tabIndex=0;element.setAttribute('role','region');element.setAttribute('aria-label',`原始工作表 ${source.sheet||'CSV'}，只读，可横向和纵向滚动`);
 extent.style.width=`${ROW_HEADER_WIDTH+ncols*COLUMN_WIDTH}px`;
 extent.style.height=`${HEADER_HEIGHT+nrows*ROW_HEIGHT}px`;
 columnHeaders.style.height=`${HEADER_HEIGHT}px`;rowHeaders.style.height=`${nrows*ROW_HEIGHT}px`;
 status.setAttribute('role','status');status.setAttribute('aria-live','polite');corner.append(status);
 columnHeaders.append(corner);extent.append(cells,columnHeaders,rowHeaders);element.append(extent);

 function showStatus(message,error=false){
  status.replaceChildren();status.hidden=!message;status.className='region-source-status'+(error?' region-source-error':'');
  element.setAttribute('aria-busy',String(Boolean(message&&!error&&nrows&&ncols)));
  if(message)status.append(el('span',null,message));
  if(error)status.append(button('重试读取原表',()=>loadWindow(true),'quiet'));
 }
 function viewport(){
  const top=Math.max(0,Number(element.scrollTop)||0),left=Math.max(0,Number(element.scrollLeft)||0);
  const height=Math.max(ROW_HEIGHT,(element.clientHeight||480)-HEADER_HEIGHT),width=Math.max(COLUMN_WIDTH,(element.clientWidth||900)-ROW_HEADER_WIDTH);
  return {
   row_start:Math.max(1,Math.floor(top/ROW_HEIGHT)+1-OVERSCAN_ROWS),
   row_end:Math.min(nrows,Math.ceil((top+height)/ROW_HEIGHT)+OVERSCAN_ROWS),
   column_start:Math.max(1,Math.floor(left/COLUMN_WIDTH)+1-OVERSCAN_COLUMNS),
   column_end:Math.min(ncols,Math.ceil((left+width)/COLUMN_WIDTH)+OVERSCAN_COLUMNS),
  };
 }
 function tiles(window){
  const result=new Map();
  for(let row=Math.floor((window.row_start-1)/TILE_ROWS)*TILE_ROWS+1;row<=window.row_end;row+=TILE_ROWS){
   for(let column=Math.floor((window.column_start-1)/TILE_COLUMNS)*TILE_COLUMNS+1;column<=window.column_end;column+=TILE_COLUMNS){
    result.set(tileKey(row,column),{row_start:row,row_end:Math.min(nrows,row+TILE_ROWS-1),column_start:column,column_end:Math.min(ncols,column+TILE_COLUMNS-1)});
   }
  }
  return result;
 }
 function sourceStatus(window=viewport()){
  if(!nrows||!ncols)return {ready:false,loading:false,error:null};
  const entries=[...tiles(window).keys()].map(key=>cache.get(key)),failed=entries.find(entry=>entry&&!entry.cells);
  const loading=entries.some(entry=>!entry),error=failed?String(failed.error?.message||'原表读取失败，请重试。'):null;
  return {ready:!loading&&!error,loading,error};
 }
 function publishStatus(){
  if(!active())return;
  const next=sourceStatus();
  if(lastStatus&&next.ready===lastStatus.ready&&next.loading===lastStatus.loading&&next.error===lastStatus.error)return;
  // Record first: a consumer may synchronously refresh this same source window.
  lastStatus=next;onStatus({...next});
 }
 function updateSelection(){
  for(const {node,row,column,physical,loaded}of visibleCells){
   const included=selectedBounds&&row>=selectedBounds[0]&&row<=selectedBounds[2]&&column>=selectedBounds[1]&&column<=selectedBounds[3];
   const anchored=selectedAnchor?.row===row&&selectedAnchor?.column===column;
   const payloadClass=highlighted.get(coordinateKey(row,column));
   node.className='region-source-cell'+(!loaded?' region-loading':!physical?' region-absent':'')+(payloadClass?' '+payloadClass:'')+(included?' region-included':'')+(anchored?' region-anchor':'');
  }
 }
 function render(window){
  const signature=currentKey+':'+[...needed.keys()].map(key=>cache.get(key)?.version||0).join(',');
  if(signature===renderedKey)return;renderedKey=signature;
  const focused=visibleCells.some(cell=>cell.node.children[0]===document.activeElement)?document.activeElement:null;
  const newCells=[],newRows=[],newColumns=[];visibleCells=[];
  for(let column=window.column_start;column<=window.column_end;column++){
   const label=onColumn?button(columnName(column),()=>onColumn(column),'region-source-column'):el('div','region-source-column',columnName(column));if(onColumn)label.setAttribute('aria-label','选择列 '+columnName(column));label.style.left=`${ROW_HEADER_WIDTH+(column-1)*COLUMN_WIDTH}px`;newColumns.push(label);
  }
  for(let row=window.row_start;row<=window.row_end;row++){
   const label=el('div','region-source-row',row);label.style.top=`${(row-1)*ROW_HEIGHT}px`;newRows.push(label);
   for(let column=window.column_start;column<=window.column_end;column++){
    const entry=cache.get(tileKey(row,column)),loaded=Boolean(entry?.cells),cell=entry?.cells?.get(coordinateKey(row,column)),slot=el('div');slot.dataset.row=String(row);slot.dataset.column=String(column);
    slot.style.top=`${HEADER_HEIGHT+(row-1)*ROW_HEIGHT}px`;slot.style.left=`${ROW_HEADER_WIDTH+(column-1)*COLUMN_WIDTH}px`;
    visibleCells.push({node:slot,row,column,physical:Boolean(cell),loaded});
    if(!loaded){slot.setAttribute('aria-label',`${columnName(column)}${row} · 正在读取原格`);}
    else if(!cell){slot.textContent='无原格';slot.title=`${columnName(column)}${row} · 原件中不存在此格`;}
    else{
     const control=button(cell.raw_value==null?'':String(cell.raw_value),()=>{if(active())onCell(cell);},'region-cell');
     control.setAttribute('aria-label','原格 '+cell.address);
     control.title=`${cell.address} · 原值：${textValue(cell.raw_value)}${cell.merged_origin?' · 合并原点 '+cell.merged_origin+'；不填充值':''}${cell.formula?' · 原公式：'+cell.formula+'；未计算':''}${cell.comment?' · 原批注：'+(cell.comment.text??cell.comment):''}`;
     slot.append(control);
     const markers=[];if(cell.comment)markers.push('批注');if(cell.formula)markers.push('公式');if(cell.merged_origin)markers.push('合并 '+cell.merged_origin);
     if(markers.length){slot.append(el('span','region-source-marker',markers.join(' · ')));control.className+=' region-cell-marked';}
    }
    newCells.push(slot);
   }
  }
  // Only the window changes; the scroller, status, and any outside input remain mounted.
  cells.replaceChildren(...newCells);rowHeaders.replaceChildren(...newRows);columnHeaders.replaceChildren(corner,...newColumns);updateSelection();
  // Restore keyboard focus only when its original coordinate remains in this window.
  if(focused?.className?.includes('region-cell')){
   const row=Number(/** @type {HTMLElement} */ (focused.parentNode)?.dataset?.row),column=Number(/** @type {HTMLElement} */ (focused.parentNode)?.dataset?.column);
   visibleCells.find(cell=>cell.row===row&&cell.column===column)?.node.children[0]?.focus?.({preventScroll:true});
  }
 }
 function settle(){const pending=waiters;waiters=[];for(const resolve of pending)resolve();}
 function trimCache(){
  for(const key of cache.keys()){
   if(cache.size<=Math.max(MAX_CACHED_TILES,needed.size))break;
   if(!needed.has(key))cache.delete(key);
  }
 }
 function abortObsolete(){
  for(const [key,request]of inFlight)if(!needed.has(key)){
   inFlight.delete(key);request.controller.abort();
  }
 }
 function pump(){
  for(const [key,json]of needed){
   if(inFlight.size>=MAX_REQUESTS)break;
   if(cache.has(key)||inFlight.has(key))continue;
   const request={controller:new AbortController()};inFlight.set(key,request);
   Promise.resolve().then(()=>{
    if(!active()||request.controller.signal.aborted)return null;
    return api('/api/assets/'+encodeURIComponent(source.asset_id)+'/table-preview',{method:'POST',json:{sheet:source.sheet??null,...json,reader_options:clone(options)},signal:request.controller.signal});
   }).then(page=>{
    if(!active()||inFlight.get(key)!==request)return;
    const physical=new Map();
    for(const row of page?.rows||[])for(const cell of row.cells||[])physical.set(coordinateKey(row.row,cell.column),cell);
    cache.set(key,{cells:physical,version:++cacheVersion});
   },error=>{
    if(active()&&inFlight.get(key)===request)cache.set(key,{error,version:++cacheVersion});
   }).finally(()=>{
    if(inFlight.get(key)!==request)return;
    inFlight.delete(key);
    if(active())refresh();else settle();
   });
  }
 }
 function refresh(force=false){
  if(!active()){settle();return;}
  if(!nrows||!ncols){showStatus('空工作表，没有原格。');settle();publishStatus();return;}
  const window=viewport(),key=JSON.stringify(window);
  if(key!==currentKey){settle();currentKey=key;currentWindow=window;needed=tiles(window);}
  abortObsolete();
  for(const key of needed.keys()){
   const entry=cache.get(key);
   if(force&&entry&&!entry.cells)cache.delete(key);
   else if(entry){cache.delete(key);cache.set(key,entry);}
  }
  trimCache();pump();render(currentWindow);
  const state=sourceStatus(currentWindow);
  if(state.error)showStatus(state.error,true);
  else showStatus(state.loading?'正在读取原表…':'');
  if(!state.loading)settle();
  publishStatus();
 }
 function loadWindow(force=false){
  if(frame!==null){cancelFrame(frame);frame=null;}
  refresh(force);
  if(!active()||!nrows||!ncols||[...needed.keys()].every(key=>cache.has(key)))return Promise.resolve();
  return new Promise(resolve=>waiters.push(resolve));
 }
 function schedule(){
  if(!active())return;
  // Revoke an old window's readiness immediately, before the next render frame.
  publishStatus();if(!active())return;
  onScroll({top:element.scrollTop||0,left:element.scrollLeft||0});
  // Keep the first scheduled frame: continuous scrolling must never starve loading.
  if(frame===null)frame=requestFrame(()=>{frame=null;refresh();});
 }
 function positionRange(range){
  const target=parseRange(range);if(!target)return false;
  element.scrollTop=Math.max(0,Math.min((target[0]-1)*ROW_HEIGHT,HEADER_HEIGHT+nrows*ROW_HEIGHT-(element.clientHeight||480)));
  element.scrollLeft=Math.max(0,Math.min((target[1]-1)*COLUMN_WIDTH,ROW_HEADER_WIDTH+ncols*COLUMN_WIDTH-(element.clientWidth||900)));
  return true;
 }
 element.addEventListener('scroll',schedule,{passive:true});
 const resize=typeof globalThis.ResizeObserver==='function'?new ResizeObserver(schedule):null;resize?.observe(element);
 const ready=Promise.resolve().then(()=>{
  if(!active())return;
  // Mounting is synchronous in the caller; the initial position is applied only once.
  if(!positionRange(initialRange)){
   element.scrollTop=Math.max(0,Number(initialScroll?.top)||0);element.scrollLeft=Math.max(0,Number(initialScroll?.left)||0);
  }
  return loadWindow();
 });
 return {element,ready,
  setSelection(range,nextAnchor=null){if(!active())return;selectedBounds=parseRange(range);selectedAnchor=nextAnchor;updateSelection();},
  setHighlights(cells=[]){if(!active())return;highlighted=highlightMap(cells);updateSelection();},
  locate(range){
   if(!active()||!positionRange(range))return Promise.resolve();
   onScroll({top:element.scrollTop||0,left:element.scrollLeft||0});return loadWindow();
  },
  dispose(){
   disposed=true;if(frame!==null)cancelFrame(frame);frame=null;needed.clear();abortObsolete();cache.clear();settle();
   element.removeEventListener('scroll',schedule);resize?.disconnect();
  },
 };
}
