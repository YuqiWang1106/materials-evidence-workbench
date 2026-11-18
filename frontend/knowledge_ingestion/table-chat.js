// @ts-check
import {formatWebsiteTime} from './website-time-logic.mjs';
import {pageTitle} from './page-title.js';
import {el,button} from './dom.js';
import {renderChatMarkdown} from './table-chat-markdown.js';
import {mountChatSummary} from './table-chat-summary.js';
const enc=encodeURIComponent,clone=structuredClone;
const liveStatus=status=>status==='queued'||status==='running';
const same=(a,b)=>{if(a===b)return true;if(!a||!b||typeof a!=='object'||typeof b!=='object'||Array.isArray(a)!==Array.isArray(b))return false;const keys=Object.keys(a);return keys.length===Object.keys(b).length&&keys.every(k=>Object.hasOwn(b,k)&&same(a[k],b[k]));};
const text=value=>value===null||value===undefined?'':typeof value==='object'?JSON.stringify(value):String(value);
const requestId=()=>globalThis.crypto?.randomUUID?.()||'chat-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
const storageKey=id=>'sog.table-chat.v1:'+id;
const tableLabel=t=>[t.original_name||t.name||'表格',t.sheet,t.table_range].filter(Boolean).join(' · ');
function defaultStorage(){try{return globalThis.sessionStorage;}catch{return null;}}

/**
 * Durable request intent lives separately from the current selection and composer.
 * @param {{
 *   root: HTMLElement,
 *   chatId?: string,
 *   tableVersionId?: string,
 *   sourceId?: string,
 *   api: typeof import('./app.js').api,
 *   go: (hash: string) => void,
 *   replaceRoute?: (hash: string) => void,
 *   isCurrent?: () => boolean,
 *   onStateChange?: (state: {chatId: string, title: string, turns: any[], selected: string[], pending: boolean}) => void,
 *   storage?: Storage,
 *   pollInterval?: number,
 * }} options
 */
export function mountTableChat({root,chatId=null,tableVersionId=null,sourceId=null,api,go,replaceRoute=go,isCurrent=()=>true,onStateChange=()=>{},storage=defaultStorage(),pollInterval=1500}){
 let disposed=false,chat=null,catalog=[],available=false,capKnown=false,catalogKnown=false,loaded=false,verified=false,error='',bootstrapError='',submissionError='',pending=null,busy=false,cancelling=false,timer=null,refreshing=false,selectionRestored=false;
 let history=[],historyFailed=false,historySignature='';
 let catalogRefreshing=false,catalogError='',catalogEpoch=0,tableInputs=[];
 let currentId=chatId,selected=tableVersionId?[tableVersionId]:[],querySucceeded=false;
 const initialKey=storageKey(chatId||'new:'+(tableVersionId||'')),abort=new AbortController(),turnNodes=new Map(),snapshotCache=new Map(),snapshotRequests=new Map(),artifactCache=new Map();
 const active=()=>!disposed,path=()=>'/api/table-chats/'+enc(currentId);
 const call=(url,options={})=>api(url,{...options,signal:abort.signal});
 const readLocal=key=>{try{const value=JSON.parse(storage?.getItem(key)||'null');return value&&value.version===1?value:null;}catch{return null;}};
 const local=readLocal(initialKey);
 if(local){if((!tableVersionId||chatId)&&Array.isArray(local.selected)&&local.selected.every(id=>typeof id==='string')&&local.selected.length<=10){selected=local.selected;selectionRestored=true;}if(local.pending&&typeof local.pending.request_id==='string'&&typeof local.pending.message==='string'&&Array.isArray(local.pending.table_version_ids)&&Number.isInteger(local.pending.expected_revision)){pending=local.pending;currentId=pending.chat_id||currentId;selected=[...pending.table_version_ids];}sourceId=sourceId||local.sourceId;}
 root.className=[...new Set((root.className||'').split(/\s+/).filter(Boolean).concat(['region-workspace','table-chat-workspace']))].join(' ');
 const icons={panel:'M9 3v18M4 3h16a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z',brand:'M3 8h18M8 8v13M4 3h16a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z',new:'M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7M16 3l5 5M11 13l-1 4 4-1 8-8-3-3-8 8Z',close:'M6 6l12 12M6 18 18 6',library:'M4 3v18M9 3v18M14 3l6 18M3 3h3M8 3h3M13 3h3',table:'M3 9h18M3 15h18M9 3v18M4 3h16a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z',down:'m6 9 6 6 6-6',send:'M12 19V5m-6 6 6-6 6 6',stop:'M6 6h12v12H6Z',latest:'M12 5v14m-6-6 6 6 6-6'};
 function icon(name){const svg=root.ownerDocument.createElementNS('http://www.w3.org/2000/svg','svg'),path=root.ownerDocument.createElementNS('http://www.w3.org/2000/svg','path');svg.setAttribute('class','chat-icon');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('width','18');svg.setAttribute('height','18');svg.setAttribute('aria-hidden','true');svg.setAttribute('focusable','false');svg.setAttribute('fill',name==='stop'?'currentColor':'none');svg.setAttribute('stroke','currentColor');svg.setAttribute('stroke-width','1.7');svg.setAttribute('stroke-linecap','round');svg.setAttribute('stroke-linejoin','round');path.setAttribute('d',icons[name]);svg.append(path);return svg;}
 function control(label,action,name,className,labelClass='chat-control-label'){const node=button(label,action,'quiet');node.className+=' '+className;node.setAttribute('aria-label',label);node.title=label;node.replaceChildren(icon(name),el('span',labelClass,label));return node;}
 const newChat=()=>go('#table-chat/new'+(sourceId?'?source='+enc(sourceId):''));
 const layout=el('div','chat-layout'),sidebar=el('aside','chat-sidebar'),brandRow=el('div','chat-sidebar-brand'),brand=control('材料知识库',()=>go(sourceId?'#regions/'+enc(sourceId):'#regions'),'brand','chat-brand-link','chat-brand-label'),sideClose=control('关闭侧栏',()=>setSidebar(false,true),'close','chat-sidebar-close'),newButton=control('新对话',newChat,'new','chat-new-button','chat-new-label'),historyTitle=el('h2','chat-sidebar-title','最近对话'),historyList=el('div','chat-history-list'),workspace=control('数据工作区',()=>go('#library'),'library','chat-workspace-link','chat-workspace-label');
 const main=el('section','chat-main'),header=el('header','chat-header'),headerLeft=el('div','chat-header-left'),title=el('h1','region-step-title workspace-page-title',currentId?'对话分析':'新对话'),historyToggle=control('切换侧栏',toggleHistory,'panel','chat-sidebar-toggle'),newHeader=control('新对话',newChat,'new','chat-header-new');
 const messages=el('div','chat-messages'),status=el('div','chat-status'),composerWrap=el('div','chat-composer-wrap'),jump=control('回到最新消息',scrollLatest,'latest','chat-jump'),composer=el('form','chat-composer'),box=el('textarea'),composeActions=el('div','chat-composer-actions'),tablePicker=el('details','chat-table-picker'),tableSummary=el('summary','chat-table-summary'),tableSummaryLabel=el('span','chat-table-summary-label'),tableList=el('div','chat-table-list'),pickerHeader=el('div','chat-picker-header'),pickerClose=control('关闭选表',()=>closePicker(true),'close','chat-picker-close'),tableOptions=el('div','chat-table-options'),tableSearch=el('input','chat-table-search'),composeButtons=el('div','chat-composer-buttons'),sendButton=control('发送',send,'send','chat-send-button'),stopButton=control('停止',stop,'stop','chat-stop-button'),modal=el('div'),backdrop=button('关闭侧栏遮罩',()=>setSidebar(false,true),'quiet');
 backdrop.className='chat-sidebar-backdrop';backdrop.setAttribute('aria-label','关闭侧栏遮罩');backdrop.textContent='';backdrop.tabIndex=-1;
 sidebar.id='chat-sidebar';sidebar.setAttribute('aria-label','历史对话');brandRow.append(brand,sideClose);sidebar.append(brandRow,newButton,historyTitle,historyList,workspace);
 historyToggle.hidden=true;newHeader.hidden=true;historyToggle.setAttribute('aria-controls','workspace-sidebar');headerLeft.append(pageTitle(title,"研究工作区"));const summaryRoot=el('div');header.append(headerLeft,summaryRoot);
 const summary=mountChatSummary({root:summaryRoot,api,go,chatId:currentId,storage,pollInterval});
 pickerHeader.append(el('span',null,'选择表格'),pickerClose);tableSearch.type='search';tableSearch.placeholder='搜索文件或表格';tableSearch.setAttribute('aria-label','搜索文件或表格');tableSearch.addEventListener('input',drawTables);tablePicker.addEventListener('toggle',()=>{if(tablePicker.open)refreshCatalog();});tableList.append(pickerHeader,tableSearch,tableOptions);tableSummary.append(icon('table'),tableSummaryLabel,icon('down'));tableSummary.setAttribute('aria-label','选择分析表格');tablePicker.append(tableSummary,tableList);tablePicker.setAttribute('aria-label','选择分析表格');
 box.setAttribute('aria-label','分析需求');box.placeholder='想分析什么？';box.rows=2;box.maxLength=8000;box.value=local?.draft||pending?.message||'';messages.setAttribute('aria-label','对话记录');messages.setAttribute('role','region');status.setAttribute('role','status');composeButtons.append(sendButton,stopButton);composeActions.append(tablePicker,composeButtons);composer.append(box,composeActions);composerWrap.append(jump,composer);main.append(header,messages,status,composerWrap);layout.replaceChildren(main);root.replaceChildren(layout,modal);
 const media=globalThis.matchMedia?.('(max-width:760px)'),eventRoot=root.ownerDocument.addEventListener?root.ownerDocument:root;let narrow=Boolean(media?.matches),pinned=true,scrollFrame=null;
 function setSidebar(open,restoreFocus=false){sidebar.hidden=!open;backdrop.hidden=!narrow||!open;sideClose.hidden=!narrow;historyToggle.setAttribute('aria-expanded',String(open));root.className=root.className.split(/\s+/).filter(name=>name!=='chat-sidebar-collapsed').concat(open?[]:['chat-sidebar-collapsed']).join(' ');if(restoreFocus)historyToggle.focus();}
 function changed(){try{onStateChange({chatId:currentId,title:chat?.title||'新对话',turns:chat?.turns||[],selected:[...selected],pending:Boolean(pending)});}catch{}}
 function getContext(){return {chatId:currentId,sourceId,selected:[...selected],pending:Boolean(pending),turns:chat?.turns||[]};}
 async function addTables(ids){if(pending){error='发送待确认期间不能更改本轮选表。';drawStatus();return false;}const requested=Array.isArray(ids)?ids:[],catalogRevision=++catalogEpoch;try{const refreshed=await call('/api/raw-tables');if(!active()||catalogRevision!==catalogEpoch)return false;catalog=refreshed.tables||catalog;catalogKnown=true;loaded=true;catalogRefreshing=false;catalogError='';}catch(e){if(active()&&catalogRevision===catalogEpoch){catalogRefreshing=false;error=e.message||'刷新表格目录失败。';drawStatus();}return false;}if(pending){error='发送待确认期间不能更改本轮选表。';drawStatus();return false;}if(requested.some(id=>!catalog.some(table=>table.table_version_id===id))){error='所选表格已不在当前目录中，请刷新后重试。';drawStatus();return false;}let next=[...selected];for(const id of requested){const incoming=catalog.find(table=>table.table_version_id===id);next=next.filter(existing=>catalog.find(table=>table.table_version_id===existing)?.table_id!==incoming.table_id);next.push(id);}next=[...new Set(next)];if(next.length>10){error='最多选择 10 张表格。';drawStatus();return false;}selected=next;error='';submissionError='';selectionRestored=true;persist();drawTables();changed();return true;}
 function setVisible(value){root.hidden=!value;}
 function toggleHistory(){setSidebar(sidebar.hidden);if(narrow&&!sidebar.hidden)sideClose.focus();}
 const resize=event=>{narrow=event.matches;setSidebar(!narrow);followMessages();};setSidebar(!narrow);media?.addEventListener?.('change',resize);
 function closePicker(restoreFocus=false){tablePicker.open=false;if(restoreFocus)tableSummary.focus();}
 const within=(target,container)=>{for(let node=target;node;node=node.parentNode)if(node===container)return true;return false;};
 const outside=event=>{if(tablePicker.open&&!within(event.target,tablePicker))closePicker();};
 const escape=event=>{if(event.key!=='Escape'||modal.children.length)return;if(tablePicker.open){event.preventDefault();closePicker(true);}else if(narrow&&!sidebar.hidden){event.preventDefault();setSidebar(false,true);}};
 eventRoot.addEventListener('click',outside);eventRoot.addEventListener('keydown',escape);
 function sizeInput(){box.style.height='48px';box.style.height=Math.min(104,Math.max(48,box.scrollHeight||48))+'px';}
 function scrollLatest(){messages.scrollTop=messages.scrollHeight||0;pinned=true;jump.hidden=true;}
 function followMessages(){if(!active())return;const follow=()=>{scrollFrame=null;if(!active())return;if(pinned)scrollLatest();else jump.hidden=false;};if(globalThis.requestAnimationFrame){if(scrollFrame===null)scrollFrame=requestAnimationFrame(follow);}else follow();}
 const inspectContent=event=>{for(let node=event.target;node&&node!==messages;node=node.parentNode)if(['select','summary','details'].includes(node.tagName?.toLowerCase())){pinned=false;return;}};messages.addEventListener('click',inspectContent);messages.addEventListener('change',inspectContent);
 messages.addEventListener('scroll',()=>{pinned=(messages.scrollHeight||0)-(messages.scrollTop||0)-(messages.clientHeight||0)<48;jump.hidden=pinned;});
 const contentObserver=globalThis.ResizeObserver?new ResizeObserver(followMessages):null;contentObserver?.observe(messages);jump.hidden=true;sizeInput();
 box.addEventListener('input',()=>{submissionError='';sizeInput();persist();drawStatus();});
 box.addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();send();}});
 composer.addEventListener('submit',event=>{event.preventDefault();send();});
 function persist(){if(!storage){error='无法保存发送状态，请恢复浏览器存储后重试。';return false;}const value={version:1,sourceId,selected:[...selected],draft:box.value,pending:pending?clone(pending):null};try{storage?.setItem(initialKey,JSON.stringify(value));if(currentId)storage?.setItem(storageKey(currentId),JSON.stringify(value));return Boolean(storage);}catch{error='无法保存发送状态，请恢复浏览器存储后重试。';return false;}}
 async function refreshCatalog(){
  if(!active())return;
  const catalogRevision=++catalogEpoch;catalogRefreshing=true;catalogError='';drawStatus();
  try{const value=await call('/api/raw-tables');if(!active()||catalogRevision!==catalogEpoch)return;if(!Array.isArray(value?.tables))throw Error('表格目录读取失败。');catalog=value.tables;catalogKnown=true;loaded=true;}
   catch(e){if(active()&&catalogRevision===catalogEpoch)catalogError=e.message||'表格目录读取失败。';}
   finally{if(active()&&catalogRevision===catalogEpoch){catalogRefreshing=false;drawTables();}}
 }
 function drawTables(){
  const focus=root.ownerDocument.activeElement,focusedId=tableInputs.includes(focus)?/** @type {HTMLInputElement} */ (focus).value:null,scroll=tableOptions.scrollTop;
  tableOptions.replaceChildren();tableInputs=[];
  const files=new Map(),query=tableSearch.value.trim().toLocaleLowerCase();
  for(const table of catalog){const key=JSON.stringify([table.source_id||'',table.asset_id||table.original_name||'原件']);if(!files.has(key))files.set(key,{name:table.original_name||'原件',tables:[]});files.get(key).tables.push(table);}
  const counts=new Map(),seen=new Map();for(const file of files.values())counts.set(file.name,(counts.get(file.name)||0)+1);
  let visible=0;
  for(const file of files.values()){
   const ordinal=(seen.get(file.name)||0)+1;seen.set(file.name,ordinal);
   const name=file.name+(counts.get(file.name)>1?' · 文件 '+ordinal:''),rows=file.tables.filter(t=>!query||[file.name,t.name,t.sheet,t.table_range].filter(Boolean).join(' ').toLocaleLowerCase().includes(query));
   if(!rows.length)continue;visible++;
   const group=el('section','chat-table-file'),heading=el('header','chat-table-file-heading');group.setAttribute('role','group');group.setAttribute('aria-label',name);
   heading.append(el('h3',null,name),el('span',null,new Set(file.tables.map(t=>t.table_id)).size+' 张'));group.append(heading);
   for(const table of rows){
    const label=el('label','chat-table'),input=el('input'),copy=el('span','chat-table-copy'),title=el('span','chat-table-name',table.name||'表格'),versions=file.tables.filter(t=>t.table_id===table.table_id),versionText=versions.length>1?formatWebsiteTime(table.created_at)+' · '+table.table_version_id.slice(-8):'';
    input.type='checkbox';input.value=table.table_version_id;input.setAttribute('aria-label','选择表格：'+name+' · '+[table.name,table.sheet,table.table_range].filter(Boolean).join(' · ')+(versionText?' · 版本 '+versionText:''));input.checked=selected.includes(table.table_version_id);title.title=tableLabel(table);copy.append(title);
    const location=[table.sheet,table.table_range].filter(Boolean).join(' · ');if(location)copy.append(el('span','chat-table-location',location));if(versionText)copy.append(el('span','chat-version',versionText));label.append(input,copy);
    input.addEventListener('change',()=>{
     if(pending||catalogRefreshing||catalogError){input.checked=selected.includes(table.table_version_id);return;}
     if(input.checked&&!selected.includes(table.table_version_id)){const sameTable=catalog.find(t=>t.table_id===table.table_id&&selected.includes(t.table_version_id));if(selected.length>=10&&!sameTable){input.checked=false;error='最多选择 10 张表格。';drawStatus();return;}selected=selected.filter(id=>catalog.find(t=>t.table_version_id===id)?.table_id!==table.table_id);selected.push(table.table_version_id);}
     else if(!input.checked)selected=selected.filter(id=>id!==table.table_version_id);
     error='';submissionError='';persist();drawStatus();changed();
    });tableInputs.push(input);group.append(label);
   }tableOptions.append(group);
  }
  if(catalogKnown&&!catalog.length)tableOptions.append(el('p','chat-empty','尚未保存表格'));else if(!visible&&query)tableOptions.append(el('p','chat-empty','没有匹配的表格'));
  if(Number.isFinite(scroll))tableOptions.scrollTop=scroll;if(focusedId)tableInputs.find(n=>n.value===focusedId)?.focus();drawStatus();
 }
 function drawStatus(){if(!active())return;if(!currentId)verified=loaded&&capKnown;summary.update({chatId:currentId,turns:chat?.turns||[],verified,pending:Boolean(pending)});tableSummaryLabel.textContent=selected.length?'表格 · '+selected.length:'选择表格';tableSummary.title=catalog.filter(table=>selected.includes(table.table_version_id)).map(tableLabel).join('\n');status.replaceChildren();const running=chat?.turns?.find(t=>t.turn_id===chat.active_turn_id&&liveStatus(t.status));sendButton.disabled=catalogRefreshing||Boolean(catalogError)||selected.some(id=>!catalog.some(t=>t.table_version_id===id))||!loaded||!available||!verified||Boolean(bootstrapError)||busy||Boolean(pending)||Boolean(running)||!selected.length||!box.value.trim();box.readOnly=Boolean(pending);sendButton.hidden=Boolean(running);stopButton.hidden=!running;stopButton.disabled=cancelling||busy||!verified;
  tableOptions.setAttribute('aria-busy',String(catalogRefreshing));for(const node of tableInputs){node.disabled=Boolean(pending)||catalogRefreshing||Boolean(catalogError);node.checked=selected.includes(node.value);}
  if(catalogError){status.append(el('p','message error',catalogError),button('重试选表',refreshCatalog,'quiet'));}
  if(!pending&&catalogKnown&&!catalogRefreshing&&!catalogError&&selected.some(id=>!catalog.some(t=>t.table_version_id===id))){status.append(el('p','message error','已选表格已不可用。'),button('移除不可用表格',()=>{selected=selected.filter(id=>catalog.some(t=>t.table_version_id===id));persist();drawTables();changed();},'quiet'));}
  if(!available&&capKnown)status.append(el('p','message error','尚未配置表格对话模型。'));
  if(pending){status.append(el('p','chat-pending',busy?'正在发送…':'发送结果待确认'));if(!busy){const query=button('查询发送状态',recover,'quiet');status.append(query);if(querySucceeded){const retry=button('重试发送',retryPending,'secondary');status.append(retry);}}}
  if(submissionError)status.append(el('p','message error',submissionError));
  if(bootstrapError)status.append(el('p','message error',bootstrapError));if(error)status.append(el('p','message error',error));if((error||bootstrapError)&&!pending){const retry=button('重试读取',()=>bootstrapError?load():loaded&&currentId?refresh():load(),'quiet');retry.disabled=busy;status.append(retry);}
 }
 function schedule(){clearTimeout(timer);if(active()&&(pending||chat?.active_turn_id)){timer=setTimeout(()=>refresh(),pollInterval);/** @type {number & {unref?: () => void}} */ (timer).unref?.();}}
 function validateChat(value){if(!value||value.chat_id!==currentId||!Number.isInteger(value.revision)||!Array.isArray(value.turns))throw Error('对话记录不匹配，请重新读取。');if(chat&&value.revision<chat.revision)return false;return true;}
 function acceptChat(value){if(!validateChat(value))return;chat=value;verified=true;title.textContent=chat.title||'对话分析';const historyIndex=history.findIndex(item=>item.chat_id===currentId);if(historyIndex<0)history.unshift({chat_id:currentId,title:chat.title});else history[historyIndex]={...history[historyIndex],title:chat.title};drawHistory();changed();
  if(!selectionRestored&&!tableVersionId&&!pending&&chat.turns.length){selected=[...chat.turns.at(-1).table_version_ids];selectionRestored=true;drawTables();}
  if(pending){const found=chat.turns.find(t=>t.request_id===pending.request_id);if(found){if(found.message!==pending.message||!same(found.table_version_ids,pending.table_version_ids))throw Error('发送记录与待确认内容不一致，请保留当前页面。');if(box.value===pending.message){box.value='';sizeInput();}pending=null;querySucceeded=false;persist();}}
  drawMessages();drawStatus();
 }
 function turnNode(turn){
  let item=turnNodes.get(turn.turn_id);
  if(!item){const element=el('article','chat-turn'),user=el('p','chat-user'),answer=el('div','chat-answer'),state=el('p','chat-turn-status'),artifacts=el('div','chat-artifacts');
   element.append(user,answer,state,artifacts);
   item={element,user,answer,state,artifacts,answerText:null,chartSlots:new Map(),chartOrder:[],selectedChartKey:null,dataViewState:null,galleryRevision:0};
   turnNodes.set(turn.turn_id,item);messages.append(element);contentObserver?.observe(element);
  }return item;
 }
 function drawMessages(){
  if(!chat?.turns.length){if(!turnNodes.size&&!messages.children.length){const empty=el('div','chat-empty-state'),suggestions=el('div','chat-suggestions');for(const prompt of ['查看表头','绘制曲线','比较两张表'])suggestions.append(button(prompt,()=>{box.value=prompt;sizeInput();persist();drawStatus();box.focus();},'quiet'));empty.append(el('h2','chat-empty-title','想从数据中了解什么？'),el('p','chat-empty-description','选择已保存的表格，查看字段、比较数据或生成图表。建议只填入问题，发送后才会开始分析。'),suggestions);messages.append(empty);}return;}
  if(!turnNodes.size)messages.replaceChildren();
  for(const turn of chat.turns){const item=turnNode(turn);item.user.textContent=turn.message;item.user.title=(turn.tables||[]).map(tableLabel).join(' / ');
   const answer=turn.assistant_text||'';
   if(answer!==item.answerText){item.answerText=answer;item.answer.replaceChildren(renderChatMarkdown(answer,{document:root.ownerDocument}));}
   item.answer.hidden=!answer;
   const labels={queued:'等待分析',running:'正在分析…',failed:'分析未完成',cancelled:'已停止',interrupted:'分析已中断'};
   const lastTool=(turn.tool_calls||[]).at(-1),unresolved=lastTool?.output?.error?.message;
   const sourceWarning=(turn.source_warnings||[]).map(w=>w.message).filter((v,i,a)=>a.indexOf(v)===i).join('；');
   item.state.textContent=sourceWarning ? sourceWarning+'。历史问答保留，原表及依据已不可用。' : turn.error?.message||labels[turn.status]||unresolved||'';item.state.hidden=!item.state.textContent;
   if(sourceWarning){for(const table of turn.tables||[])snapshotCache.delete(table.table_version_id);item.chartOrder=[];item.chartSlots.clear();item.artifacts.replaceChildren();item.galleryRevision++;continue;}
   item.chartOrder=(turn.tool_calls||[]).filter(tool=>tool.artifact_id).map(tool=>tool.artifact_id);
   let added=false;
   for(const tool of turn.tool_calls||[])if(tool.artifact_id&&!item.chartSlots.has(tool.artifact_id)){
    item.chartSlots.set(tool.artifact_id,{tool,status:'loading',entry:null,error:null});added=true;loadArtifact(turn,tool,item);
   }
   if(added)drawCharts(turn,item);
  }
  followMessages();
 }
 async function drawCharts(turn,item){
  const revision=++item.galleryRevision;
  const {renderChatArtifacts}=await import('./table-chat-artifact.js');
  if(!active()||revision!==item.galleryRevision)return;
  const entries=item.chartOrder.map(key=>item.chartSlots.get(key)?.entry).filter(Boolean);
  const children=[];
  if(entries.length)children.push(renderChatArtifacts({entries,selectedKey:item.selectedChartKey,onSelect:key=>{item.selectedChartKey=key;},dataViewState:item.dataViewState,onDataViewChange:value=>{item.dataViewState=value;},onSource:showSource,document:root.ownerDocument}));
  const slots=item.chartOrder.map(key=>item.chartSlots.get(key)).filter(Boolean);
  if(!entries.length&&slots.some(slot=>slot.status==='loading'))children.push(el('p','loading','正在读取图表…'));
  for(const slot of slots)if(slot.error){const issue=el('div','chat-chart-error');issue.append(el('p','message error',slot.error),button('重试图表',()=>{slot.error=null;slot.status='loading';drawCharts(turn,item);loadArtifact(turn,slot.tool,item);},'quiet'));children.push(issue);}
  item.artifacts.replaceChildren(...children);followMessages();
 }
 async function snapshot(id,turn){
  const identity=(turn.tables||[]).find(t=>t.table_version_id===id);
  const validate=original=>{if(original?.table_version_id!==id||!identity||original.source_id!==identity.source_id)throw Error('图表原表版本不匹配。');return original;};
  if(snapshotCache.has(id))return validate(snapshotCache.get(id));
  let request=snapshotRequests.get(id);if(!request){request=call('/api/raw-tables/'+enc(id));snapshotRequests.set(id,request);}
  try{const result=await request;if(result?.table_version_id!==id)throw Error('图表原表版本不匹配。');const original=validate(result.snapshot);if(active())snapshotCache.set(id,original);return original;}
  finally{if(snapshotRequests.get(id)===request)snapshotRequests.delete(id);}
 }

 function artifactVersions(artifact,turn){const scopes=artifact?.kind==='table_analysis'?[artifact]:artifact?.kind==='table_comparison'?artifact.tables:null;if(!Array.isArray(scopes)||!scopes.length||scopes.length>10)throw Error('图表结果格式不受支持。');const ids=scopes.map(s=>s.table_version_id);if(new Set(ids).size!==ids.length||ids.some(id=>!turn.table_version_ids.includes(id)))throw Error('图表不属于本轮选表。');for(const scope of scopes)if(!scope.config||scope.result?.table_version_id!==scope.table_version_id||!same(scope.result?.config,scope.config)||!Array.isArray(scope.result.columns))throw Error('图表与计算设置不匹配。');if(artifact.kind==='table_comparison'&&(artifact.joined!==false||artifact.unit_conversion!==false||artifact.comparison_mode!=='side_by_side'))throw Error('比较结果边界不匹配。');return ids;}
 async function loadArtifact(turn,tool,item){
  const id=currentId,key=id+'/'+turn.turn_id+'/'+tool.artifact_id,slot=item.chartSlots.get(tool.artifact_id);
  try{let result=artifactCache.get(key);
   if(!result){result=await call('/api/table-chats/'+enc(id)+'/turns/'+enc(turn.turn_id)+'/artifacts/'+enc(tool.artifact_id));
    if(result?.chat_id!==id||result?.turn_id!==turn.turn_id||result?.call_id!==tool.call_id||result?.artifact_id!==tool.artifact_id)throw Error('图表归属不匹配。');
    if(active())artifactCache.set(key,result);
   }
   const ids=artifactVersions(result.artifact,turn),snapshots={};
   await Promise.all(ids.map(async version=>{snapshots[version]=await snapshot(version,turn);}));
   if(!active()||currentId!==id)return;
   slot.entry={key:tool.artifact_id,artifact:result.artifact,snapshots};slot.status='ready';slot.error=null;
   await drawCharts(turn,item);
  }catch(e){artifactCache.delete(key);if(active()&&currentId===id){slot.status='failed';slot.entry=null;slot.error=e.message||'图表读取失败。';await drawCharts(turn,item);}}
 }
 function showSource(version,cell){const source=snapshotCache.get(version),stored=source?.rows.find(r=>r.row===cell.row)?.cells.find(c=>c.column===cell.column);modal.replaceChildren();const dialog=el('dialog','raw-source-dialog');dialog.setAttribute('aria-label','原格来源');dialog.setAttribute('aria-modal','true');dialog.open=true;dialog.setAttribute('open','');dialog.append(el('h2',null,[source?.original_name,source?.sheet,cell.address||'原格'].filter(Boolean).join(' · ')),stored?el('pre',null,text(stored.raw_value)):el('p',null,'此行没有保存的原格（缺格）'));for(const [key,label]of [['value_type','原值类型'],['missing_kind','缺失类型'],['formula','公式'],['source_formula','来源公式'],['cached_value','已保存计算值'],['number_format','显示格式']])if(stored?.[key]!==undefined&&stored[key]!==null)dialog.append(el('p',null,label+'：'+text(stored[key])));const close=()=>modal.replaceChildren();dialog.append(button('关闭',close,'secondary'));dialog.addEventListener('cancel',event=>{event.preventDefault();close();});modal.append(dialog);if(dialog.showModal){dialog.open=false;dialog.removeAttribute('open');dialog.showModal();}}
 async function refresh(){if(!active()||refreshing||!currentId)return;refreshing=true;try{const value=await call(path());if(!active())return;error='';querySucceeded=Boolean(pending);acceptChat(value);}catch(e){if(active()){verified=false;querySucceeded=false;error=e.message||'读取对话失败。';drawStatus();}}finally{refreshing=false;if(active())schedule();}}
 async function recover(){if(currentId)return refresh();querySucceeded=true;drawStatus();}
 function drawHistory(){const signature=JSON.stringify([historyFailed,currentId,history.map(item=>[item.chat_id,item.title])]);if(signature===historySignature)return;historySignature=signature;historyList.replaceChildren();for(const item of history){const entry=button(item.title||'对话',()=>go('#table-chat/'+enc(item.chat_id)+(sourceId?'?source='+enc(sourceId):'')),'quiet');entry.className+=' chat-history-item';entry.title=item.title||'对话';if(item.chat_id===currentId){entry.className+=' is-current';entry.setAttribute('aria-current','page');}historyList.append(entry);}if(historyFailed)historyList.append(button('重试历史',load,'quiet'));else if(!history.length)historyList.append(el('p','chat-empty','暂无历史对话'));}
 async function load(){const catalogRevision=++catalogEpoch;error='';bootstrapError='';drawStatus();const results=await Promise.allSettled([call('/api/capabilities'),call('/api/raw-tables'),call('/api/table-chats')]);if(!active())return;const [cap,tables,hist]=results;capKnown=cap.status==='fulfilled';available=capKnown&&/** @type {PromiseFulfilledResult<any>} */ (cap).value.table_chat_available===true;if(catalogRevision===catalogEpoch){catalogRefreshing=false;catalogKnown=tables.status==='fulfilled';loaded=catalogKnown;if(catalogKnown){catalog=/** @type {PromiseFulfilledResult<any>} */ (tables).value.tables||[];catalogError='';}}const failures=[];if(catalogRevision===catalogEpoch&&!catalogKnown)failures.push(/** @type {PromiseRejectedResult} */ (tables).reason?.message||'读取表格失败。');if(!capKnown)failures.push(/** @type {PromiseRejectedResult} */ (cap).reason?.message||'读取模型状态失败。');bootstrapError=failures.join(' · ');if(hist.status==='fulfilled'){history=hist.value.chats||[];historyFailed=false;}else historyFailed=true;drawHistory();drawTables();if(currentId)await refresh();else{verified=loaded&&cap.status==='fulfilled';if(pending)querySucceeded=true;drawStatus();}return chat;}
 async function submitIntent(){if(!pending||busy||!active())return;busy=true;querySucceeded=false;error='';submissionError='';drawStatus();const intent=pending;try{if(!currentId){const created=await call('/api/table-chats',{method:'POST',json:{request_id:intent.create_request_id}});if(!active())return;if(!created?.chat_id||!Number.isInteger(created.revision))throw Error('对话创建结果不完整。');currentId=created.chat_id;intent.chat_id=currentId;chat=created;verified=true;if(!persist())return;}
   const payload={request_id:intent.request_id,expected_revision:intent.expected_revision,message:intent.message,table_version_ids:[...intent.table_version_ids]};const value=await call(path()+'/turns',{method:'POST',json:payload});if(!active())return;if(typeof value.turn_id!=='string'||!['queued','running','completed','failed','cancelled','interrupted'].includes(value.status)||value.request_id!==intent.request_id||value.message!==intent.message||!same(value.table_version_ids,intent.table_version_ids))throw Error('发送回执与本次内容不一致。');
   const turns=[...(chat?.turns||[])],index=turns.findIndex(t=>t.turn_id===value.turn_id);if(index<0)turns.push(value);else turns[index]=value;
   acceptChat({...chat,chat_id:currentId,revision:Math.max(chat?.revision||0,intent.expected_revision+1),turns,active_turn_id:liveStatus(value.status)?value.turn_id:chat?.active_turn_id===value.turn_id?null:chat?.active_turn_id||null});await refresh();
  }catch(e){if(active()){error=e.message||'发送未返回。';if(Number.isInteger(e.status)&&e.status>=400&&e.status<500&&e.status!==408){submissionError=e.message||'发送未完成。';pending=null;persist();}if(currentId)await refresh();else querySucceeded=true;}}
  finally{if(active()){busy=false;drawStatus();persist();schedule();changed();if(currentId&&!chatId&&!submissionError&&isCurrent())replaceRoute('#table-chat/'+enc(currentId)+(sourceId?'?source='+enc(sourceId):''));}}
 }
 function send(){if(!active()||sendButton.disabled||pending)return;closePicker();pinned=true;pending={request_id:requestId(),create_request_id:requestId(),chat_id:currentId,expected_revision:chat?.revision||0,message:box.value,table_version_ids:[...selected]};if(!persist()){pending=null;drawStatus();return;}submitIntent();}
 function retryPending(){if(!busy&&pending&&querySucceeded)submitIntent();}
 async function stop(){if(!currentId||cancelling||!chat?.active_turn_id||!verified)return;const id=chat.active_turn_id;cancelling=true;error='';drawStatus();try{await call(path()+'/turns/'+enc(id)+'/cancel',{method:'POST'});if(active())await refresh();}catch(e){if(active()){error=e.message||'停止结果待确认，请刷新查看。';verified=false;drawStatus();}}finally{if(active()){cancelling=false;drawStatus();schedule();}}}
 drawMessages();const ready=load();return {ready,dispose(){disposed=true;summary.dispose();clearTimeout(timer);media?.removeEventListener?.('change',resize);eventRoot.removeEventListener('click',outside);eventRoot.removeEventListener('keydown',escape);contentObserver?.disconnect();if(scrollFrame!==null)globalThis.cancelAnimationFrame?.(scrollFrame);abort.abort();modal.replaceChildren();},getContext,addTables,setVisible};
}
