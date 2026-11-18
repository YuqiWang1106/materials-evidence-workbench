// @ts-check
import {buildRowQuery,projectRowQuery,ROW_QUERY_OPERATORS} from './row-query-logic.mjs';
import {exclusionGroups} from './query-logic.mjs';
import {errorText,valueText} from './result-logic.js';

/** Read only the formal assertions explicitly selected by the existing server query. */
async function documentHits(response,sample,api,signal) {
  const invalid=()=>{throw Error('查询记录已变化或内容不完整，请重新查询。');};
  if(!Array.isArray(response?.results)||!Array.isArray(response?.exclusions))invalid();
  const wanted=sample.trim()?response.results.filter(hit=>hit?.sample_label===sample):response.results;
  const packages=new Map(),results=[];
  for(const hit of wanted){
    if(signal.aborted)throw Object.assign(Error('查询已取消'),{name:'AbortError'});
    if(!hit||typeof hit.task_id!=='string'||!Array.isArray(hit.assertion_refs))invalid();
    if(!packages.has(hit.task_id)){
      const path=`/api/tasks/${encodeURIComponent(hit.task_id)}`;
      const task=await api(path,{signal});
      if(signal.aborted)throw Object.assign(Error('查询已取消'),{name:'AbortError'});
      if(task?.state!=='saved')throw Error('此资料当前不是已保存版本，请重新查询。');
      packages.set(hit.task_id,await api(`${path}/records`,{signal}));
    }
    const pkg=packages.get(hit.task_id);
    if(!Array.isArray(pkg?.assertions)||hit.record_revision_id&&hit.record_revision_id!==pkg.header?.record_revision_id)invalid();
    const assertions=new Map(pkg.assertions.map(assertion=>[assertion.assertion_id,assertion]));
    if(hit.assertion_refs.some(ref=>typeof ref!=='string'||!assertions.has(ref)))invalid();
    const selected=[...new Set(hit.assertion_refs)].map(ref=>assertions.get(ref)).filter(assertion=>assertion.field_id==='observation.value');
    const matched=selected.slice(0,20).map(assertion=>{
      if(!assertion.value||!Object.hasOwn(assertion.value,'raw_value'))invalid();
      return {id:assertion.assertion_id,subject_ref:assertion.subject_ref,field_id:assertion.field_id,
        field:assertion.qualifiers?.metric_label||'结果原值',value:assertion.value.raw_value,unit:assertion.value.raw_unit,
        typed_value:assertion.value.typed_value,evidence_refs:assertion.evidence_refs,
        source_bindings:assertion.source_context?.bindings||{}};
    });
    results.push({...hit,matched_values:matched,matched_value_total:selected.length,matched_values_truncated:selected.length>20});
  }
  return projectRowQuery({...response,results});
}

/**
 * Document-only exact sample labels; numeric matching stays on the existing server.
 * @param {{
 *   root: HTMLElement,
 *   api: typeof import('./app.js').api,
 *   onOpenTask?: (taskId: string) => void,
 *   onSource?: (source: {taskId: string, row: Record<string, any>}) => void,
 * }} options
 */
export function mountDocumentQuery({root,api,onOpenTask,onSource}) {
  const doc=root.ownerDocument;
  const el=(tag,cls,text)=>{const node=doc.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=String(text);return node;};
  const button=(label,action,cls='button secondary')=>{const node=el('button',cls,label);node.type='button';node.addEventListener('click',action);return node;};
  const state={sample:'',metric:'',mode:'any',operator:'gte',value:'',unit:''};
  let disposed=false,version=0,request=null,busy=false,result=null,page=0;
  const wrapper=el('section','query-workspace row-query-workspace'),form=el('form','query-form panel'),fields=el('div','query-filter-grid row-query-fields'),actions=el('div','query-actions'),results=el('div','query-results'),message=el('p','message');
  const search=button('查询',event=>{event.preventDefault();run();},'button primary');search.type='submit';
  form.setAttribute('aria-label','测试查询条件');form.addEventListener('submit',event=>{event.preventDefault();run();});actions.append(search);form.append(fields,actions);message.hidden=true;message.setAttribute('role','status');
  wrapper.append(el('p','query-intro','查询已保存的论文 / 专利测试结果。样品与指标按原文精确匹配。'),form,message,results);root.replaceChildren(wrapper);
  const tell=text=>{message.textContent=text;message.hidden=!text;};
  function invalidate(){const had=busy||result!==null;version++;request?.abort();busy=false;search.disabled=false;search.textContent='查询';result=null;results.replaceChildren();tell(had?'条件已变更，请重新查询。':'');}
  function control(label,key,options){const input=el(options?'select':'input');input.setAttribute('aria-label',label);if(options)for(const [value,title] of options){const option=el('option','',title);option.value=value;input.append(option);}else input.type='text';input.value=state[key];input.addEventListener(options?'change':'input',()=>{state[key]=input.value;invalidate();if(key==='mode')drawFields();});const wrap=el('label','query-field');wrap.append(el('span','',label),input);fields.append(wrap);return input;}
  function drawFields(){fields.replaceChildren();control('样品','sample');control('指标','metric');control('值筛选','mode',[['any','不限'],['number','数值'],['category','等级']]);if(state.mode==='any')return;const op=control('操作','operator',state.mode==='number'?Object.entries(ROW_QUERY_OPERATORS):[['eq','等于']]);if(state.mode==='category'){op.value='eq';op.disabled=true;}control('值','value');if(state.mode==='number')control('单位','unit');}
  async function run(){
    if(disposed||busy)return;let payload;try{payload=buildRowQuery({...state,sample:''});}catch(error){tell(error.message);return;}
    const token=++version,sample=state.sample;request?.abort();request=new AbortController();const signal=request.signal;busy=true;search.disabled=true;search.textContent='正在查询…';result=null;tell('');results.replaceChildren(el('p','muted','正在查找已保存的测试…'));
    try{const response=await api('/api/search/detailed',{method:'POST',json:payload,signal});if(disposed||token!==version)return;const projected=await documentHits(response,sample,api,signal);if(disposed||token!==version)return;result=projected;page=0;drawResults();}
    catch(error){if(!disposed&&token===version){results.replaceChildren();tell(error?.status===503?'查询服务暂时不可用，请稍后重试。':errorText(error,'查询未完成，请检查连接后重试。'));}}
    finally{if(!disposed&&token===version){busy=false;search.disabled=false;search.textContent='查询';}}
  }
  function drawResults(){
    results.replaceChildren();const panel=el('section','panel');panel.append(el('h3','','查询结果'));const entries=result.hits.flatMap(hit=>hit.values.length?hit.values.map(row=>({hit,row})):[{hit,row:null}]);
    if(!entries.length)panel.append(el('p','muted','没有符合条件的已保存测试结果。'));
    else{const scroll=el('div','table-scroll row-query-table-scroll'),table=el('table','row-query-table'),head=el('thead'),heading=el('tr'),body=el('tbody');for(const label of ['样品','指标','结果','单位','来源'])heading.append(el('th','',label));head.append(heading);table.append(head);const shown=entries.slice(page*50,page*50+50);
      for(const {hit,row} of shown){const tr=el('tr');tr.append(el('td','',valueText(hit.sample,240)),el('td','',row?valueText(row.field,240):'结果暂不可展示'),el('td','',row?valueText(row.value,512):'—'),el('td','',row?valueText(row.unit||'未注明',80):'—'));const source=el('td'),open=button(onSource?'查看原文':'查看资料',()=>{if(onSource&&row)onSource({taskId:hit.taskId,row});else onOpenTask?.(hit.taskId);},'button quiet');open.disabled=!onSource&&!onOpenTask;source.append(open);tr.append(source);body.append(tr);}table.append(body);table.setAttribute('aria-label','测试查询结果');scroll.append(table);panel.append(scroll);
      for(const hit of new Set(shown.map(entry=>entry.hit)))if(hit.truncated)panel.append(el('p','help',`${valueText(hit.sample,100)}：命中 ${hit.total} 项，当前返回 ${hit.values.length} 项；打开原文查看完整已保存记录。`));
      if(entries.length>50){const pager=el('div','query-actions'),previous=button('上一页',()=>{page--;drawResults();}),next=button('下一页',()=>{page++;drawResults();});previous.disabled=page===0;next.disabled=page*50+shown.length>=entries.length;pager.append(previous,el('span','',`${page*50+1}–${page*50+shown.length} / ${entries.length}`),next);panel.append(pager);}
    }results.append(panel);const exclusions=exclusionGroups(result.exclusions);if(exclusions.length){const notes=el('section','panel');notes.append(el('h3','','本次指标查询的排除说明'));for(const item of exclusions)notes.append(el('p','',`${item.label} · ${item.count}`));results.append(notes);}
  }
  drawFields();return {dispose(){disposed=true;version++;request?.abort();}};
}
