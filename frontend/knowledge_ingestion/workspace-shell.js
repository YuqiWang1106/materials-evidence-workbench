import {compareChatTimes} from './website-time-logic.mjs';
import {el,button} from './dom.js';

export function mountWorkspaceShell({root,go}){
 const layout=el('div','workspace-shell'),sidebar=el('aside','workspace-sidebar'),brand=el('a','workspace-brand'),mark=el('img','workspace-brand-mark'),brandCopy=el('span','workspace-brand-copy'),nav=el('nav','workspace-nav'),more=el('details','workspace-more'),moreSummary=el('summary',null,'更多'),moreNav=el('nav','workspace-more-nav'),historyHeading=el('h2','workspace-history-heading','最近对话'),history=el('div','workspace-history'),content=el('div','workspace-content'),message=el('div','message workspace-message'),chatRoot=el('section','workspace-chat-root'),pageRoot=el('main','workspace-page'),connection=el('span','workspace-connection','正在连接'),backdrop=button('关闭导航遮罩',()=>setSidebar(false,true),'quiet'),toggle=button('打开导航',()=>setSidebar(sidebar.hidden),'quiet');connection.id='connection-status';
 mark.src='/ui/workbench-mark.svg';mark.alt='';mark.width=32;mark.height=32;brand.setAttribute('aria-label','Materials Evidence Workbench');
 brand.href='#regions';brand.append(mark,brandCopy);brandCopy.append(el('strong',null,'Materials Evidence'),el('small',null,'Knowledge Base Service'));
 const links=[['research-chat','新建问答','#research-chat/new'],['library','资料库','#library'],['wiki','主题 Wiki','#wiki'],['graph','知识图谱','#graph']];
 const closeDrawerAfterNavigation=a=>a.addEventListener('click',()=>{if(narrow)setSidebar(false);});
 closeDrawerAfterNavigation(brand);
 for(const [key,label,href] of links){const a=el('a','workspace-nav-link',label);a.href=href;a.dataset.page=key;closeDrawerAfterNavigation(a);nav.append(a);}
 for(const [key,label,href] of [['regions','表格与范围','#regions'],['documents','论文与专利','#documents'],['query','查找数据','#query']]){const a=el('a','workspace-nav-link',label);a.href=href;a.dataset.page=key;closeDrawerAfterNavigation(a);moreNav.append(a);}
 more.append(moreSummary,moreNav);history.setAttribute('aria-label','最近对话');message.id='global-message';message.setAttribute('role','status');message.hidden=true;content.id='workspace';content.setAttribute('tabindex','-1');pageRoot.setAttribute('tabindex','-1');sidebar.append(brand,nav,more,historyHeading,history,connection);content.append(message,toggle,chatRoot,pageRoot);layout.append(sidebar,backdrop,content);root.replaceChildren(layout);
 backdrop.className='workspace-backdrop';backdrop.textContent='';backdrop.tabIndex=-1;toggle.className+=' workspace-menu-toggle';toggle.hidden=true;toggle.setAttribute('aria-expanded','false');toggle.setAttribute('aria-controls','workspace-sidebar');sidebar.id='workspace-sidebar';sidebar.setAttribute('aria-label','工作区导航');
 const media=globalThis.matchMedia?.('(max-width:760px)');let narrow=Boolean(media?.matches);
 function setSidebar(open,restore=false){sidebar.hidden=narrow&&!open;backdrop.hidden=!narrow||!open;toggle.setAttribute('aria-expanded',String(open));if(restore)toggle.focus();}
 function syncLayout(){narrow=Boolean(media?.matches);toggle.hidden=!narrow;setSidebar(!narrow);}
 const changed=()=>syncLayout();media?.addEventListener?.('change',changed);syncLayout();
 const eventRoot=root.ownerDocument.addEventListener?root.ownerDocument:root;const escape=e=>{if(e.key==='Escape'&&narrow&&!sidebar.hidden){e.preventDefault();setSidebar(false,true);}};eventRoot.addEventListener('keydown',escape);
 let currentChatId=null,currentChatKind='table',savedTableHistory=[],savedResearchHistory=[],historyEpoch=0,researchNext=null;
 function setCurrentPage(page,{newChat=false}={}){for(const a of [...nav.children,...moreNav.children]){const active=(a.dataset.page===page&&(!['table-chat','research-chat'].includes(a.dataset.page)||newChat))||(['task','draft','table-analysis','standard','meaning','standard-preview','sol-preview'].includes(page)&&a.dataset.page===(page==='task'?'library':'regions'));a.setAttribute('aria-current',active?'page':'false');}}
 function setHistory(chats=[]){history.replaceChildren();for(const item of chats){const kind=item.kind==='research'?'research':'table',label=item.title||(kind==='research'?'新研究对话':'新表格对话');const a=el('a','workspace-history-link',(kind==='research'?(item.scope_mode==='all_wiki'?'Wiki · ':'研究 · '):'表格 · ')+label);a.href=(kind==='research'?'#research-chat/':'#table-chat/')+encodeURIComponent(item.chat_id);a.dataset.chatKind=kind;closeDrawerAfterNavigation(a);if(item.chat_id===currentChatId&&kind===currentChatKind){a.setAttribute('aria-current','page');a.className+=' is-current';}history.append(a);}if(!chats.length)history.append(el('p','workspace-history-empty','暂无历史对话'));}
 function setCurrentChat(id,kind='table'){currentChatId=id||null;currentChatKind=kind;}
 function renderSavedHistory(api){
   setHistory([...savedTableHistory,...savedResearchHistory].sort((a,b)=>compareChatTimes(b,a)).slice(0,40));
   historyHeading.textContent=researchNext===null?'最近对话':'已加载的对话';
   if(researchNext!==null){const moreHistory=button('继续读取历史',async()=>{moreHistory.disabled=true;const epoch=++historyEpoch;try{const result=await researchHistory(api,epoch,researchNext,savedResearchHistory);if(epoch!==historyEpoch)return;savedResearchHistory=result.chats;researchNext=result.next;renderSavedHistory(api);}catch{if(epoch===historyEpoch){moreHistory.disabled=false;moreHistory.textContent='读取未完成，点击重试';}}},'quiet');history.append(moreHistory);}
 }
 async function researchHistory(api,epoch,offset=0,previous=[]){
   const rows=[...previous];let next=offset;
   for(let page=0;page<20;page++){
     const result=await api(next?'/api/research-chats?offset='+next+'&limit=50':'/api/research-chats');
     if(epoch!==historyEpoch)throw Error('superseded history');
     const items=result.chats||[];if(!Array.isArray(items))throw Error('invalid history');
     rows.push(...items.map(c=>({...c,kind:'research'})));
     if(!result.has_more)return {chats:[...new Map(rows.map(c=>[c.chat_id,c])).values()],next:null};
     if(!Number.isInteger(result.next_offset)||result.next_offset<=next)throw Error('invalid history cursor');
     next=result.next_offset;
   }
   return {chats:[...new Map(rows.map(c=>[c.chat_id,c])).values()],next};
 }
 async function refreshHistory(api){
   const epoch=++historyEpoch;
   const results=await Promise.allSettled([api('/api/table-chats'),researchHistory(api,epoch)]);
   if(epoch!==historyEpoch)return;
   if(results[0].status==='fulfilled')savedTableHistory=(results[0].value.chats||[]).map(c=>({...c,kind:'table'}));
   if(results[1].status==='fulfilled'){savedResearchHistory=results[1].value.chats;researchNext=results[1].value.next;}
   renderSavedHistory(api);
 }

 return {layout,sidebar,nav,history,content,message,chatRoot,pageRoot,setCurrentPage,setCurrentChat,setHistory,refreshHistory,setSidebar,dispose(){historyEpoch++;media?.removeEventListener?.('change',changed);eventRoot.removeEventListener('keydown',escape);}};
}
