// @ts-check
import {compareWebsiteTimes, isWebsiteTime} from './website-time-logic.mjs';
import {el,button} from './dom.js';

const enc=encodeURIComponent,copy=structuredClone;
const template='experimental-analysis-v1';
const legacyPolicy='table-summary-input-v1',currentPolicy='table-summary-input-v2';
const inputPolicy=value=>value?.input_policy_version===undefined?legacyPolicy:value.input_policy_version;
const knownPolicy=value=>value===legacyPolicy||value===currentPolicy;
const live=job=>!job?.page_deleted&&(job?.status==='queued'||job?.status==='running');
const requestId=()=>globalThis.crypto?.randomUUID?.()||'summary-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
const errors={SUMMARY_CONTEXT_LIMIT:'对话与关联证据合计超过 256 KB 上限，未删除任何消息，也未静默截断。',SUMMARY_WINDOW_NOT_COMPLETED:'最近对话含未完成的回复，暂时无法整理。',SUMMARY_INPUT_CHANGED:'对话或来源已变化，请核对后重新整理。',SUMMARY_SAVE_FAILED:'草稿保存失败，重试会复用已生成内容。',SUMMARY_INTERRUPTED:'整理已中断，可主动重试。',REQUEST_ID_CONFLICT:'请求与原记录不一致，请保留当前页面。'};
const errorText=e=>errors[e?.code]||e?.message||'整理未完成，请重试。';
const newer=(a,b)=>!b||a.attempt>b.attempt||(a.attempt===b.attempt&&compareWebsiteTimes(a.updated_at,b.updated_at)>=0);

/** A separate durable intent; chat selection, composer and message nodes are never owned here. */
export function mountChatSummary({root,api,go,chatId=null,storage,pollInterval=1500}){
 let disposed=false,context={chatId,turns:[],verified:false,pending:false},jobs=[],pending=null,cap=null,loaded=false,busy=false,error='',timer=null,epoch=0,readRevision=0,reading=null,ready=Promise.resolve();
 const abort=new AbortController(),active=()=>!disposed,key=()=>'sog.wiki-summary.v1:'+context.chatId;
 const call=(path,options={})=>api(path,{...options,signal:abort.signal});
 root.className='chat-summary';root.setAttribute('aria-label','整理对话为 Wiki');
 function validJob(value){return value&&typeof value.job_id==='string'&&value.job_id.length>0&&value.chat_id===context.chatId&&typeof value.end_turn_id==='string'&&value.end_turn_id.length>0&&['queued','running','completed','failed','interrupted'].includes(value.status)&&value.template_version===template&&knownPolicy(inputPolicy(value))&&Number.isInteger(value.attempt)&&value.attempt>0&&isWebsiteTime(value.updated_at)&&isWebsiteTime(value.created_at)&&(value.page_deleted===undefined||typeof value.page_deleted==='boolean')&&(value.status!=='completed'||typeof value.page_id==='string'&&value.page_id.length>0);}
 function validIntent(value){if(!value||!knownPolicy(inputPolicy(value))||value.chat_id!==context.chatId||typeof value.end_turn_id!=='string'||!value.end_turn_id||typeof value.body?.request_id!=='string'||!value.body.request_id)return false;
  return value.operation==='create'&&value.url==='/api/wiki/summaries'&&value.body.chat_id===context.chatId&&value.body.end_turn_id===value.end_turn_id&&Object.keys(value.body).length===3||value.operation==='retry'&&typeof value.job_id==='string'&&value.url==='/api/wiki/summaries/'+enc(value.job_id)+'/retry'&&Number.isInteger(value.baseAttempt)&&value.baseAttempt>0&&Object.keys(value.body).length===1;
 }
 function restore(){pending=null;if(!context.chatId)return;try{const value=JSON.parse(storage?.getItem(key())||'null');if(value?.version===1&&validIntent(value.pending))pending=value.pending;}catch{}}
 function persist(){try{if(!storage)throw Error();storage.setItem(key(),JSON.stringify({version:1,pending}));return true;}catch{error='无法保存整理状态，请恢复浏览器存储后重试。';return false;}}
 function latest(end,policy=inputPolicy(cap)){return jobs.filter(j=>j.end_turn_id===end&&inputPolicy(j)===policy).reduce((best,j)=>newer(j,best)?j:best,null);}
 function windows(){const result=new Map();for(const j of jobs){const window=JSON.stringify([j.end_turn_id,inputPolicy(j)]);if(newer(j,result.get(window)))result.set(window,j);}return [...result.values()].sort((a,b)=>compareWebsiteTimes(b.created_at,a.created_at)||b.attempt-a.attempt);}
 function reconcile(){if(!pending)return;const found=latest(pending.end_turn_id,inputPolicy(pending));if(found&&(pending.operation==='create'||found.attempt>pending.baseAttempt||found.status==='completed'||found.page_deleted)){pending=null;error='';persist();}}
 function merge(job){const previous=jobs.find(j=>j.job_id===job.job_id);if(previous?.status==='completed'&&job.status!=='completed'||previous?.page_deleted&&!job.page_deleted)return;if(!previous||compareWebsiteTimes(job.updated_at,previous.updated_at)>=0)jobs=[...jobs.filter(j=>j.job_id!==job.job_id),job];}
 function blocked(){if(!context.chatId||!context.turns.length)return '请先完成一轮对话。';if(context.pending)return '请先确认当前消息的发送结果。';if(!context.verified)return '请先重新读取对话。';if(context.turns.some(t=>['queued','running'].includes(t.status)))return '请等待当前回复完成。';if(context.turns.slice(-20).some(t=>t.status!=='completed'||!t.assistant_text?.trim()))return '最近对话含未完成的回复，暂时无法整理。';if(!loaded)return '正在读取整理状态。';if(cap?.available!==true)return 'Wiki 整理服务暂不可用。';return '';}
 function action(label,fn,disabled=false,title=''){const n=button(label,fn,'quiet');n.className+=' chat-summary-action';n.disabled=disabled;if(title)n.title=title;root.append(n);return n;}
 function draw(){if(!active())return;root.replaceChildren();const end=context.turns.at(-1)?.turn_id,current=end?latest(end):null,all=windows(),running=all.find(live),completed=current?.status==='completed'&&!current.page_deleted?current:all.find(j=>j.status==='completed'&&!j.page_deleted),failed=current&&!current.page_deleted&&['failed','interrupted'].includes(current.status)?current:!current?all.find(j=>!j.page_deleted&&['failed','interrupted'].includes(j.status)):null;
  if(pending){action(busy?'正在提交…':'重试原请求',sendPending,busy);if(!busy)action('查询状态',()=>refresh(),false);}
  else if(running)action(running.status==='queued'?'整理已排队':'整理中…',()=>{},true);
  else if(current?.page_deleted)action('Wiki 已删除',()=>{},true);
  else if(current?.status!=='completed'){
   if(failed)action('重试整理',()=>beginRetry(failed),busy||!loaded||cap?.available!==true);
   if(!failed||failed.end_turn_id!==end||inputPolicy(failed)!==inputPolicy(cap)){const reason=blocked();action('整理为 Wiki',beginCreate,busy||Boolean(reason),reason);}
  }
  if(completed)action('查看 Wiki',()=>go('#wiki/'+enc(completed.page_id)));
  const issue=error||(!pending&&!running&&failed?errorText(failed.error):'')||(!pending&&!running&&cap?.available===false&&current?.status!=='completed'&&!current?.page_deleted?'Wiki 整理服务尚未配置。':'');
  if(issue){const state=el('span','chat-summary-error',issue);state.setAttribute('role','status');root.append(state);if(!pending&&!busy)action('重新读取',()=>refresh(),false);}
  else if(pending&&!busy){const state=el('span','chat-summary-state','结果待确认');state.setAttribute('role','status');root.append(state);}
 }
 function schedule(){clearTimeout(timer);if(active()&&context.chatId&&(pending||windows().some(live))){timer=setTimeout(()=>refresh(),pollInterval);/** @type {number & {unref?: () => void}} */ (timer).unref?.();}}
 async function refresh(){if(!active()||busy)return;if(reading)return reading;const generation=epoch,revision=++readRevision,id=context.chatId;
  const work=(async()=>{try{
   const reads=await Promise.allSettled([call('/api/wiki/summaries/capabilities'),id?call('/api/wiki/summaries?chat_id='+enc(id)):Promise.resolve({jobs:[]})]);
   if(!active()||generation!==epoch||revision!==readRevision)return;
   const [capResult,listResult]=reads;
   if(capResult.status==='fulfilled'){const value=capResult.value;if(value?.template_version!==template||value.max_messages!==40||value.max_turns!==20||typeof value.available!=='boolean'||!knownPolicy(inputPolicy(value))||(inputPolicy(value)===currentPolicy&&value.max_context_bytes!==256000))throw Error('Wiki 整理能力不匹配，请重新读取。');cap=value;}
   else throw capResult.reason;
   if(listResult.status!=='fulfilled')throw listResult.reason;
   if(!Array.isArray(listResult.value?.jobs)||listResult.value.jobs.some(j=>!validJob(j)))throw Error('整理记录不匹配，请重新读取。');
   for(const value of listResult.value.jobs)merge(value);loaded=true;error='';reconcile();
  }catch(e){if(active()&&generation===epoch&&revision===readRevision){loaded=false;error=errorText(e);}}
  finally{if(active()&&generation===epoch){draw();schedule();}}
  })();reading=work;try{await work;}finally{if(reading===work)reading=null;}
 }
 function beginCreate(){if(!active()||busy||pending||blocked()||windows().some(live))return;const end=context.turns.at(-1)?.turn_id;if(!end||latest(end))return;
  begin({operation:'create',url:'/api/wiki/summaries',chat_id:context.chatId,end_turn_id:end,input_policy_version:inputPolicy(cap),body:{request_id:requestId(),chat_id:context.chatId,end_turn_id:end}});
 }
 function beginRetry(job){if(!active()||busy||pending||!loaded||cap?.available!==true||windows().some(live)||job.page_deleted||!['failed','interrupted'].includes(job.status))return;
  begin({operation:'retry',url:'/api/wiki/summaries/'+enc(job.job_id)+'/retry',chat_id:context.chatId,end_turn_id:job.end_turn_id,job_id:job.job_id,baseAttempt:job.attempt,input_policy_version:inputPolicy(job),body:{request_id:requestId()}});
 }
 function begin(intent){pending=copy(intent);error='';if(!persist()){pending=null;draw();return;}sendPending();}
 async function sendPending(){if(!active()||!pending||busy)return;const intent=copy(pending),generation=epoch;let refreshCapability=false;busy=true;error='';readRevision++;clearTimeout(timer);draw();
  try{const value=await call(intent.url,{method:'POST',json:copy(intent.body)});if(!active()||generation!==epoch)return;
   // Old cached creates may first reach the server after an upgrade; only their explicit receipt may choose the policy.
   const expectedPolicy=intent.input_policy_version||(intent.operation==='retry'?legacyPolicy:null);
   const upgradedCreate=intent.operation==='create'&&expectedPolicy===legacyPolicy&&inputPolicy(value)===currentPolicy;
   if(!validJob(value)||value.end_turn_id!==intent.end_turn_id||(expectedPolicy&&inputPolicy(value)!==expectedPolicy&&!upgradedCreate)||(intent.operation==='retry'&&value.attempt<intent.baseAttempt))throw Error('整理回执不匹配，请重试原请求。');
   merge(value);pending=null;loaded=true;persist();
   if(intent.operation==='create'&&inputPolicy(value)!==inputPolicy(cap)){cap=null;loaded=false;refreshCapability=true;}
  }catch(e){if(active()&&generation===epoch){error=errorText(e);if(Number.isInteger(e.status)&&e.status>=400&&e.status<500&&![408,429].includes(e.status)){pending=null;persist();}}}
  finally{if(active()&&generation===epoch){busy=false;draw();schedule();if(refreshCapability){reading=null;refresh();}}}
 }
 function update(value){if(!active())return;const changed=value.chatId!==context.chatId;context={...context,...value};if(changed){epoch++;readRevision++;clearTimeout(timer);jobs=[];pending=null;loaded=false;busy=false;error='';reading=null;restore();ready=refresh();}draw();}
 restore();draw();ready=refresh();
 return {get ready(){return ready;},update,dispose(){disposed=true;epoch++;readRevision++;clearTimeout(timer);abort.abort();}};
}
