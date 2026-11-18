// @ts-check
import {chatAxisLabel,renderRawTablePlots} from './raw-table-analysis.js';

const rawText=value=>value===null||value===undefined?'':typeof value==='object'?JSON.stringify(value):String(value);
const sameValue=(a,b)=>{if(a===b)return true;if(!a||!b||typeof a!=='object'||typeof b!=='object'||Array.isArray(a)!==Array.isArray(b))return false;const keys=Object.keys(a);return keys.length===Object.keys(b).length&&keys.every(key=>Object.hasOwn(b,key)&&sameValue(a[key],b[key]));};
const reasons={missing:'缺失',absent:'缺格',unparsed:'无法解析',formula:'公式未计算',non_finite:'非有限数值',non_scalar:'非单一数值',out_of_range:'超出数值能力'};

/** @typedef {{key: string, open: boolean, offset: number}} ChartViewState */

/**
 * One turn, one selected chart. All values remain bound to saved program output.
 * @param {{
 *   entries: any[],
 *   selectedKey?: string,
 *   onSelect?: (key: string) => void,
 *   dataViewState?: ChartViewState,
 *   onDataViewChange?: (state: ChartViewState) => void,
 *   onSource?: (tableVersionId: string, cell: Record<string, any>) => void,
 *   document?: Document,
 * }} options
 */
export function renderChatArtifacts({entries,selectedKey=null,onSelect=()=>{},dataViewState=null,onDataViewChange=()=>{},onSource=()=>{},document=globalThis.document}){
 const el=(tag,className,text)=>{const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=String(text);return node;};
 const action=(text,callback)=>{const node=el('button','button quiet',text);node.type='button';node.addEventListener('click',callback);return node;};
 const wrapper=el('div','table-chat-artifact'),choices=[],usedKeys=new Set(),views=new Map();
 if(dataViewState&&typeof dataViewState.key==='string')views.set(dataViewState.key,{open:dataViewState.open===true,offset:Number.isInteger(dataViewState.offset)&&dataViewState.offset>=0?dataViewState.offset:0});
 for(const [entryIndex,entry]of (Array.isArray(entries)?entries:[]).entries()){
  const artifact=entry?.artifact,records=artifact?.kind==='table_analysis'?[artifact]:artifact?.kind==='table_comparison'?artifact.tables:null;
  if(!Array.isArray(records)||!records.length){wrapper.append(el('p','raw-skip','程序结果格式不可展示。'));continue;}
  for(const record of records){
   const id=record?.table_version_id,snapshot=entry.snapshots?.[id],result=record?.result,config=record?.config;
   if(!snapshot||snapshot.table_version_id!==id||result?.table_version_id!==id||!Array.isArray(result?.columns)||!config||!sameValue(config,result.config)){wrapper.append(el('p','raw-skip','原表版本或计算设置与程序结果不匹配，请重新读取。'));continue;}
   for(const column of result.columns){
    const key=`${entry.key??entryIndex}:${id}:${column.column_id}`;
    if(usedKeys.has(key)){wrapper.append(el('p','raw-skip','程序结果标识重复，请重新读取。'));continue;}
    usedKeys.add(key);choices.push({key,id,snapshot,result,config,column,label:`${column.column_id} · ${chatAxisLabel(column.label||column.column_id,column.unit)} — ${snapshot.original_name||snapshot.name||'原表'}${snapshot.sheet?' · '+snapshot.sheet:''} · ${snapshot.table_range||''}`});
   }
  }
 }
 if(!choices.length)return wrapper;
 const picker=el('label','table-chat-chart-picker'),select=el('select'),panel=el('div','table-chat-chart-panel');select.setAttribute('aria-label','选择图表');picker.append(el('span',null,'图表'),select);wrapper.append(picker,panel);
 for(const choice of choices){const option=el('option',null,choice.label);option.value=choice.key;select.append(option);}
 select.value=choices.some(choice=>choice.key===selectedKey)?selectedKey:choices[0].key;
 function draw(){
  const choice=choices.find(item=>item.key===select.value);if(!choice)return;select.title=choice.label;
  const {key,id,snapshot,config,column}=choice,rowMap=new Map(snapshot.rows.map(row=>[row.row,row]));
  const source=cell=>{const stored=rowMap.get(cell?.row)?.cells.find(item=>item.column===cell?.column);onSource(id,stored||{...cell,absent:true});};
  panel.replaceChildren(renderRawTablePlots(column,config,snapshot,source,{document,preferPrimary:true,compact:true}));
  const operation=config.line_plot?column.line:config.x_column?column.scatter:column.histogram,shownWarnings=new Set((operation?.warnings||[]).map(w=>w.reason));
  for(const warning of column.warnings||[])if(warning.reason&&!shownWarnings.has(warning.reason)){panel.append(el('p','raw-skip',warning.reason));shownWarnings.add(warning.reason);}
  if(column.statistics?.status==='skipped'&&column.statistics.reason&&!shownWarnings.has(column.statistics.reason))panel.append(el('p','raw-skip',column.statistics.reason));
  if(config.group_columns?.length&&column.group_statistics?.status==='skipped'&&column.group_statistics.reason)panel.append(el('p','raw-skip',column.group_statistics.reason));
  const state=views.get(key)||{open:false,offset:0};views.set(key,state);
  const details=el('details','table-chat-data'),content=el('div','table-chat-data-page');details.open=state.open;details.append(el('summary',null,'查看数据'),content);panel.append(details);
  const rows=snapshot.rows.slice(Math.max(0,config.header_row_count||0)),lastPage=Math.max(0,Math.floor((rows.length-1)/100)*100);state.offset=Math.min(Math.floor(state.offset/100)*100,lastPage);
  const selectedColumns=[...new Set([config.x_column,column.column_id,...(config.group_columns||[])].filter(Boolean))].map(id=>snapshot.columns.find(c=>c.column_id===id)).filter(Boolean);
  const issues=new Map();for(const issue of column.excluded||[]){const row=issue.source_row??issue.source?.row,list=issues.get(row)||[];list.push(`${issue.source?.address||''} ${reasons[issue.code]||issue.code||'未参与'}`.trim());issues.set(row,list);}
  const notify=()=>onDataViewChange({key,open:state.open,offset:state.offset});let drawn=false;
  function drawData(){
   drawn=true;const scroll=el('div','raw-table-scroll'),table=el('table','raw-data-table'),head=el('thead'),header=el('tr'),body=el('tbody');
   for(const title of ['原行',...selectedColumns.flatMap(c=>[`${c.column_id} 原格`,`${c.column_id} 原值`]),'说明'])header.append(el('th',null,title));head.append(header);
   for(const row of rows.slice(state.offset,state.offset+100)){
    const tr=el('tr');tr.append(el('td',null,row.row));
    for(const col of selectedColumns){const cell=row.cells.find(c=>c.column===col.column),coordinate=cell||{row:row.row,column:col.column,address:col.column_id+row.row,absent:true},ref=el('td');ref.append(action(coordinate.address||col.column_id+row.row,()=>source(coordinate)));tr.append(ref,el('td',null,cell?rawText(cell.raw_value):'缺格'));}
    tr.append(el('td',null,(issues.get(row.row)||[]).join(' · ')));body.append(tr);
   }
   table.append(head,body);scroll.append(table);content.replaceChildren();if(operation?.excluded_count)content.append(el('p',null,`未绘制：${operation.excluded_count} 行`));if(column.excluded?.length)content.append(el('p',null,`未参与统计：${column.excluded.length} 格`));content.append(scroll);
   if(rows.length>100){const pagination=el('div','raw-toolbar'),prev=action('上一页',()=>{state.offset=Math.max(0,state.offset-100);drawData();notify();}),next=action('下一页',()=>{state.offset=Math.min(lastPage,state.offset+100);drawData();notify();});prev.disabled=state.offset===0;next.disabled=state.offset>=lastPage;pagination.append(prev,el('span',null,`${state.offset+1}–${Math.min(state.offset+100,rows.length)} / ${rows.length}`),next);content.append(pagination);}
   if(!rows.length)content.append(el('p','raw-skip','表头之后没有数据行。'));
  }
  details.addEventListener('toggle',()=>{state.open=details.open;if(state.open&&!drawn)drawData();notify();});if(state.open)drawData();onSelect(key);notify();
 }
 select.addEventListener('change',draw);draw();return wrapper;
}

/** Compatibility for callers that have one stored artifact. */
export function renderChatArtifact({artifact,snapshots,...options}){
 return renderChatArtifacts({entries:[{key:'artifact',artifact,snapshots}],...options});
}
