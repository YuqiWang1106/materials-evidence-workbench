import {saveDecision,saveReviewedResult,titleForResult,valueText,errorText,pagination} from './result-logic.js';
import {mountRowSource} from './row-source-view.js';
import {renderStandardCurve} from './standard-curve-view.js';

/** Small result surface over the immutable, server-verified result projection. */
export function renderRowResult({root,task:initialTask,api,onEdit,onRefresh,capabilities={},initialView=null,standardMode=false}) {
  const doc=root.ownerDocument;
  const node=(tag,cls,text)=>{const el=doc.createElement(tag);if(cls)el.className=cls;if(text!==undefined)el.textContent=String(text);return el;};
  const action=(label,handler,cls='button secondary')=>{const el=node('button',cls,label);el.type='button';el.addEventListener('click',handler);return el;};
  let task=initialTask,view=initialView,disposed=false,busy=false,notice='',error='',version=0,source=null,poll=null;
  const path=`/api/tasks/${encodeURIComponent(task.task_id)}`;
  async function load(offset=0){
    const token=++version;busy=true;draw();
    try{const [nextTask,nextView]=await Promise.all([api(path),api(`${path}/result-view?offset=${offset}&limit=50`)]);
      if(disposed||token!==version)return;task=nextTask;view=nextView;error='';
    }catch(e){if(!disposed&&token===version)error=errorText(e,'结果暂时无法载入，请刷新重试。');}
    finally{if(!disposed&&token===version){busy=false;draw();schedule();}}
  }
  function schedule(){clearTimeout(poll);if(!disposed&&['queued','extracting','checking','running'].includes(task.state))poll=setTimeout(()=>load(view?.offset||0),1500);}
  async function save(){
    if(busy||disposed)return;
    const decision=saveDecision(task,view,capabilities);
    if(!decision.action||(decision.action==='publish'&&!view?.completion?.selected_scope_complete))return;
    busy=true;notice='';error='';draw();
    try{
      const outcome=await saveReviewedResult({task,view,capabilities,api});if(disposed)return;
      task=outcome.task||task;
      if(outcome.kind==='version_changed'){view=outcome.view;notice='结果已更新，请重新核对后保存。';}
      else if(outcome.kind==='blocked')notice=outcome.reason;
      else if(task.state!=='saved')notice='已保留当前结果，请核对保存状态后再继续。';
    }catch(e){if(!disposed)error=errorText(e,'保存尚未确认，结果已保留。请核对保存状态后重试。');
      try{const latest=await api(path);if(!disposed)task=latest;}catch{}
    }finally{if(!disposed){busy=false;draw();}}
  }
  function draw(){
    if(disposed)return;source?.dispose();source=null;
    const wrapper=node('section','row-result-workspace'),top=node('section','panel');
    top.append(node('h2','',titleForResult(task,view||{})));
    top.append(node('p',task.state==='saved'?'status saved':'status',task.state==='saved'?'已归档':busy?'正在处理…':'核对后保存'));
    if(view){
      const completion=view.completion||{};
      const curveCount=standardMode?view.summary?.series_count:0;
      const summary=curveCount?`${curveCount} 条曲线${Number.isInteger(view.summary?.point_row_count)?` · ${view.summary.point_row_count} 行原始点`:''}`
        :`${view.total??view.rows?.length??0} 条${standardMode?'所选结果':'测试'}`;
      top.append(node('p','row-result-summary',`${summary}${completion.source_missing_results?` · ${completion.source_missing_results} 条原文缺失`:''}`));
      top.append(node('p','muted',completion.selected_scope_complete?(standardMode?'已核对读法中的结果列已完成程序整理，仍需研究者检查科学含义。':'所选范围已整理完成。'):'所选范围仍有问题，请返回修改。'));
      if(!completion.whole_file_complete)top.append(node('p','muted','文件中其他工作表、未选区域或未选列仍未整理，原件完整保留。'));
    }
    const controls=node('div','actions'),decision=saveDecision(task,view,capabilities);
    if(task.state!=='saved'){
      const saveButton=action(decision.action==='reconcile'?'核对保存状态':'确认保存',save,'button primary');
      saveButton.disabled=busy||!decision.action||(decision.action==='publish'&&!view?.completion?.selected_scope_complete);controls.append(saveButton);
      if(!decision.action&&decision.reason)top.append(node('p','muted',decision.reason));
    }
    if(onEdit)controls.append(action(task.state==='saved'?'重新整理':'修改设置',()=>onEdit(task)));
    const asset=view?.assets?.[0];if(asset){const link=node('a','button quiet','下载原件');link.href=`/api/assets/${encodeURIComponent(asset.asset_id)}/download`;link.download='';controls.append(link);}
    if(view){
      // Download the server bytes directly; parsing and stringifying here can
      // change original numeric representations beyond JavaScript precision.
      const link=node('a','button quiet','导出结果');link.href=`${path}/records`;link.download='整理结果.json';controls.append(link);
    }
    controls.append(action('刷新',()=>load(view?.offset||0),'button quiet'));top.append(controls);wrapper.append(top);
    if(notice)wrapper.append(node('p','message',notice));if(error)wrapper.append(node('p','message error',error));
    const layout=node('div','row-result-layout'),main=node('section','panel'),side=node('aside','row-result-source');side.hidden=true;
    source=mountRowSource({root:side,api,taskId:task.task_id});
    if(view?.rows?.length){
      const scroll=node('div','table-scroll'),table=node('table','row-result-table');table.setAttribute('aria-label','整理结果');
      const head=node('tr');for(const label of [standardMode?'样品 / 测试记录':'样品','指标','结果','单位','方法与条件'])head.append(node('th','',label));const thead=node('thead');thead.append(head);table.append(thead);
      const body=node('tbody');
      const linked=(row,label,binding)=>{const td=node('td');if(binding){const b=action(valueText(label,240),()=>source.show(row,{source:binding,label:'原格来源'}),'button quiet row-value');td.append(b);}else td.textContent=valueText(label,240);return td;};
      for(const row of view.rows){
        const tr=node('tr'),bindings=row.source_bindings||{},samples=[...new Set((row.contexts||[]).map(c=>c.sample_label||c.test_label).filter(Boolean))];
        const binding=value=>Array.isArray(value)?value[0]:value;
        const curve=standardMode&&row.series_preview;
        const result=curve?node('td','standard-curve-result'):linked(row,row.value,binding(bindings.value));
        if(curve){
          result.append(renderStandardCurve(row,{document:doc,onSource:(selected,original)=>source.show(selected,{source:original,label:'原格来源'})}));
          const first=binding(bindings.value);if(first)result.append(action('查看原格',()=>source.show(row,{source:first,label:'曲线原格'}),'button quiet'));
        }
        tr.append(node('td','',curve&&row.material_label||samples.join('、')||row.subject||'归属待核对'),linked(row,row.field,binding(bindings['qualifier:metric_label']||bindings['qualifier:property'])),result,linked(row,row.unit||'未注明',binding(bindings.unit)));
        const conditions=node('td');for(const [key,binding] of Object.entries(bindings))if(key.startsWith('qualifier:condition:')||key==='qualifier:method'){
          const rawLabel=key==='qualifier:method'?'方法':key.slice('qualifier:condition:'.length);
          const label=standardMode?(rawLabel==='stage'?'阶段':rawLabel.replace(/^header_(\d+)$/,'表头条件 $1')):rawLabel;
          const sourceBinding=Array.isArray(binding)?binding[0]:binding;
          const b=action(`${label}：${valueText(sourceBinding?.raw,100)}`,()=>source.show(row,{source:sourceBinding,label}),'button quiet');conditions.append(b);
        }
        if(!conditions.children.length)conditions.textContent=curve&&row.stage_label||'—';tr.append(conditions);body.append(tr);
      }table.append(body);scroll.append(table);main.append(scroll);
      const pages=pagination(view),nav=node('div','pagination');
      const previous=action('上一页',()=>load(pages.previous));previous.disabled=busy||pages.previous===null;
      const next=action('下一页',()=>load(pages.next));next.disabled=busy||pages.next===null;
      nav.append(previous,node('span','',`${pages.start}–${pages.end} / ${pages.total}`),next);main.append(nav);
    }else main.append(node('p','muted',busy?'正在读取整理结果…':'暂时没有可展示的测试结果。'));
    layout.append(main,side);wrapper.append(layout);root.replaceChildren(wrapper);
  }
  draw();if(!view)load();else schedule();
  return ()=>{disposed=true;version++;clearTimeout(poll);source?.dispose();};
}
