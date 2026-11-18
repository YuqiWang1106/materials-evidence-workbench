// @ts-check
import {el,button} from './dom.js';

/**
 * @param {{
 *   host: HTMLElement,
 *   api: typeof import('./app.js').api,
 *   sourceId: string,
 *   title: string,
 *   kind: string,
 *   isCurrent?: () => boolean,
 *   onComplete?: (result: Record<string, any>) => void,
 *   onClose?: () => void,
 * }} options
 */
export function sourceActionDialog({host,api,sourceId,title,kind,isCurrent=()=>true,onComplete=()=>{},onClose=()=>{}}) {
  const deleting=kind==='delete',dialog=el('dialog','source-action-dialog');
  const heading=el('h2',null,deleting?'删除资料':'完整重新解析'),content=el('div'),message=el('p','source-action-message');
  const actions=el('div','source-action-controls');let closed=false,busy=false,preview=null,intent=null,controller=new AbortController();
  const active=()=>!closed&&isCurrent();
  const cancel=button('取消',()=>close(),'secondary'),confirm=button(deleting?'永久删除':'开始重新解析',submit,deleting?'danger':'primary');
  const refresh=button('刷新资料状态',()=>load(),'quiet');refresh.hidden=true;confirm.disabled=true;
  dialog.setAttribute('aria-label',deleting?'确认删除资料':'确认完整重新解析');dialog.setAttribute('aria-modal','true');
  message.setAttribute('role','status');actions.append(cancel,refresh,confirm);dialog.append(heading,el('p','source-action-title',title),content,message,actions);
  dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
  host.append(dialog);if(dialog.showModal)dialog.showModal();else{dialog.open=true;dialog.setAttribute('open','');}cancel.focus();
  function close(force=false){
    if(closed||busy&&!force)return;closed=true;controller.abort();dialog.close?.();host.replaceChildren();onClose();
  }
  async function load(){
    confirm.disabled=true;refresh.hidden=true;message.textContent='正在核对资料及关联内容…';
    try{
      const value=await api('/api/sources/'+encodeURIComponent(sourceId)+'/management',{signal:controller.signal});
      if(!active())return;preview=value;intent=null;content.replaceChildren();
      const impact=value.impact||{};
      if(deleting){
        content.append(el('p',null,`将清理 ${impact.sources ?? '未知'} 份资料（含派生资料）、${impact.tasks ?? '未知'} 个处理版本、${impact.originals ?? '未知'} 份专属原件。`),
          el('p',null,`删除 ${impact.wiki_deleted ?? '未知'} 个专属 Wiki，撤下 ${impact.wiki_invalidated ?? '未知'} 个关联 Wiki 的有效依据。`),
          el('p','source-action-warning','永久删除无法撤销。历史问答保留并标记来源已删除；其他资料仍使用的共享内容保留。'));
      } else content.append(el('p',null,'从原件完整重新读取与提取，不复用旧解析结果。开始后立即撤下旧结果，待新结果确认保存后恢复检索。'),
        el('p','source-action-warning','可能产生新的模型调用。表格将重新进入范围核对。'));
      const allowed=deleting?value.can_delete:value.can_reparse;
      confirm.disabled=!allowed;message.textContent=allowed?'':(deleting?value.delete_reason:value.reparse_reason)||'当前状态不允许此操作';
      if(!allowed)refresh.hidden=false;
    }catch(error){if(active()){message.textContent=error.message;refresh.hidden=false;}}
  }
  async function submit(){
    if(!active()||busy||!preview||confirm.disabled)return;
    if(!intent)intent={expected_generation:preview.generation,impact_token:preview.impact_token,
      request_id:globalThis.crypto?.randomUUID?.()||'source-'+Date.now()+'-'+Math.random().toString(36).slice(2)};
    busy=true;confirm.disabled=true;cancel.disabled=true;refresh.hidden=true;message.textContent='正在提交操作…';
    try{
      const result=await api('/api/sources/'+encodeURIComponent(sourceId)+'/'+kind,{method:'POST',json:intent,signal:controller.signal});
      if(!active())return;
      if(!result?.operation_id||result.source_id!==sourceId||result.kind!==kind)throw Error('操作回执不匹配，请查询资料状态');
      busy=false;close(true);onComplete(result);
    }catch(error){
      if(!active())return;busy=false;cancel.disabled=false;confirm.disabled=false;
      if(error.status===409||error.status===422){
        confirm.disabled=true;refresh.hidden=false;message.textContent=error.message;
      } else {
        confirm.textContent='重试原请求';
        message.textContent='操作结果尚未确认。'+error.message+' 可重试原请求，系统会沿用同一操作编号，避免重复执行。';
      }
    }
  }
  const ready=load();
  return {ready,isBusy:()=>busy,dispose:()=>close(true)};
}
