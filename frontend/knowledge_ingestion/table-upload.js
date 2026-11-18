import {el,button,input,field} from './dom.js';
import {REGION_PROFILE} from './region-review-logic.mjs';

/** Shared upload form; opening it does not upload or confirm any table. */
export function mountTableUpload({root,api,onUploaded,isCurrent=()=>true,onCancel=null,submitLabel='上传'}){
 let disposed=false,busy=false;
 const abort=new AbortController(),active=()=>!disposed&&isCurrent();
 const form=el('form','table-upload-form'),file=input('',()=>{},{type:'file'}),status=el('p','table-upload-status'),actions=el('div','table-upload-actions');
 file.accept='.csv,.xlsx';file.setAttribute('aria-label','选择文件');
 status.setAttribute('role','alert');status.hidden=true;
 const cancel=onCancel?button('取消',()=>{if(active()&&!busy)onCancel();},'secondary'):null;
 const submit=button(submitLabel,upload,'primary');
 if(cancel)actions.append(cancel);actions.append(submit);
 form.append(field('选择文件',file),el('p','table-upload-types','CSV / XLSX · 最多 50 MB'),status,actions);root.append(form);
 file.addEventListener('change',()=>{if(active()&&!busy){status.textContent='';status.hidden=true;}});
 form.addEventListener('submit',event=>{event.preventDefault();upload();});
 function message(value){status.textContent=value;status.hidden=!value;}
 function setBusy(value){busy=value;submit.disabled=value;file.disabled=value;if(cancel)cancel.disabled=value;submit.textContent=value?'上传中…':submitLabel;form.setAttribute('aria-busy',String(value));}
 async function upload(){
  if(!active()||busy)return;const selected=file.files?.[0];
  if(!selected||file.files.length!==1||!/\.(csv|xlsx)$/i.test(selected.name)){message('请选择一个 CSV 或 XLSX 文件。');return;}
  if(!selected.size||selected.size>50_000_000){message('请选择非空且不超过 50 MB 的文件。');return;}
  message('');setBusy(true);
  try{
   const body=new FormData();body.append('file',selected);
   const asset=await api('/api/assets',{method:'POST',body,signal:abort.signal});
   if(!active())return;if(!asset?.asset_id)throw Error('文件上传未完成，请重试。');
   const draft=await api('/api/table-drafts',{method:'POST',json:{title:selected.name,asset_ids:[asset.asset_id],entry_profile:REGION_PROFILE},signal:abort.signal});
   if(!active())return;if(!draft?.source_id)throw Error('范围草稿创建未完成，请重试。');
   onUploaded(draft.source_id);
  }catch(e){if(active())message(e.message||'上传未完成，请重试。');}
  finally{if(active())setBusy(false);}
 }
 return {file,isBusy:()=>busy,dispose(){disposed=true;abort.abort();}};
}
