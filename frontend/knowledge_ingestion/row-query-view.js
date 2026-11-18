// @ts-check
import {buildRowQuery,projectRowQuery,ROW_QUERY_OPERATORS} from './row-query-logic.mjs';
import {exclusionGroups} from './query-logic.mjs';
import {errorText,valueText} from './result-logic.js';

/**
 * Fixed form over the existing scientific query, with a bounded result table.
 * @param {{
 *   root: HTMLElement,
 *   api: typeof import('./app.js').api,
 *   onOpenTask?: (taskId: string) => void,
 *   onSource?: (source: {taskId: string, row: Record<string, any>}) => void,
 * }} options
 */
export function mountRowQuery({root,api,onOpenTask,onSource}) {
  const doc=root.ownerDocument;
  const node=(tag,className,text)=>{const element=doc.createElement(tag);if(className)element.className=className;if(text!==undefined)element.textContent=String(text);return element;};
  const action=(label,handler,className='button secondary')=>{const element=node('button',className,label);element.type='button';element.addEventListener('click',handler);return element;};
  const field=(label,control)=>{const element=node('label','query-field');element.append(node('span','',label),control);return element;};
  let disposed=false,version=0,busy=false,request=null,result=null,page=0,sourceVersion=0,source=null,sourceTask=null;
  const state={sample:'',metric:'',mode:'any',operator:'gte',value:'',unit:''};
  const wrapper=node('section','query-workspace row-query-workspace');
  wrapper.append(node('p','query-intro','查询已保存的测试结果。样品、指标和等级按原文精确匹配。'));
  const form=node('form','query-form panel'),fields=node('div','query-filter-grid row-query-fields');
  form.setAttribute('aria-label','测试查询条件');
  const search=action('查询',event=>{event.preventDefault();run();},'button primary');search.type='submit';
  const actions=node('div','query-actions');
  actions.append(search);form.append(fields,actions);
  form.addEventListener('submit',event=>{event.preventDefault();run();});
  const message=node('p','message');message.hidden=true;message.setAttribute('role','status');
  const layout=node('div','row-query-layout'),results=node('div','query-results'),sourceRoot=node('aside','row-query-source');
  sourceRoot.hidden=true;sourceRoot.setAttribute('aria-label','查询结果来源');
  layout.append(results,sourceRoot);wrapper.append(form,message,layout);root.replaceChildren(wrapper);

  function tell(text,tone='') {message.textContent=text;message.hidden=!text;message.className=`message ${tone}`.trim();}
  function clearSource() {sourceVersion++;source?.dispose();source=null;sourceTask=null;sourceRoot.hidden=true;sourceRoot.replaceChildren();}
  function invalidate() {
    const hadResult=busy||result!==null;
    version++;request?.abort();request=null;busy=false;search.disabled=false;search.textContent='查询';
    result=null;results.replaceChildren();clearSource();tell(hadResult?'条件已变更，请重新查询。':'');
  }
  function edit(key,value,redraw=false) {state[key]=value;invalidate();if(redraw)drawFields();}
  function textInput(label,key,placeholder='') {
    const input=node('input');input.type='text';input.value=state[key];input.placeholder=placeholder;input.setAttribute('aria-label',label);
    input.addEventListener('input',()=>edit(key,input.value));return input;
  }
  function select(label,key,options,redraw=false) {
    const input=node('select');input.setAttribute('aria-label',label);
    for(const [value,text] of options){const option=node('option','',text);option.value=value;input.append(option);}
    input.value=state[key];input.addEventListener('change',()=>edit(key,input.value,redraw));return input;
  }
  function drawFields() {
    fields.replaceChildren();
    const comparing=state.mode!=='any',metric=textInput('指标','metric','按原指标名称填写');
    metric.setAttribute('aria-required',String(comparing));
    fields.append(field('样品',textInput('样品','sample','如 001')),field(comparing?'指标（必填）':'指标',metric),
      field('值筛选',select('值筛选','mode',[['any','不限'],['number','数值'],['category','等级']],true)));
    if(!comparing)return;
    const operator=select('操作','operator',state.mode==='number'?Object.entries(ROW_QUERY_OPERATORS):[['eq','等于']]);
    if(state.mode==='category'){operator.value='eq';operator.disabled=true;}
    const value=textInput('值','value',state.mode==='number'?'如 12.5':'如 V-0');value.setAttribute('aria-required','true');
    if(state.mode==='number')value.inputMode='decimal';
    fields.append(field('操作',operator),field('值（必填）',value));
    if(state.mode==='number'){
      const unit=textInput('单位','unit','如 MPa');unit.setAttribute('aria-required','true');fields.append(field('单位（必填）',unit));
    }
  }
  async function run() {
    if(disposed||busy)return;
    let payload;try{payload=buildRowQuery(state);}catch(error){tell(error.message,'error');return;}
    const token=++version;request?.abort();request=new AbortController();busy=true;search.disabled=true;search.textContent='正在查询…';
    result=null;clearSource();results.replaceChildren(node('p','muted','正在查找已保存的测试…'));tell('');
    let received=false;
    try {
      const response=await api('/api/search/detailed',{method:'POST',json:payload,signal:request.signal});
      if(disposed||token!==version)return;
      received=true;result=projectRowQuery(response);page=0;drawResults();
    } catch(error) {
      if(!disposed&&token===version){
        results.replaceChildren();
        tell(received?'查询服务返回内容不完整，请重新查询。':error?.status===503
          ?'查询服务暂时不可用，请稍后重试。':errorText(error,'查询未完成，请检查连接后重试。'),'error');
      }
    } finally {if(!disposed&&token===version){busy=false;search.disabled=false;search.textContent='查询';}}
  }
  async function showSource(hit,row) {
    if(disposed)return;
    if(onSource){onSource({taskId:hit.taskId,row});return;}
    const token=++sourceVersion;sourceRoot.hidden=false;
    try {
      const {mountRowSource}=await import('./row-source-view.js');
      if(disposed||token!==sourceVersion)return;
      if(sourceTask!==hit.taskId){source?.dispose();source=mountRowSource({root:sourceRoot,api,taskId:hit.taskId});sourceTask=hit.taskId;}
      await source.show(row);
    } catch(error) {
      if(!disposed&&token===sourceVersion)sourceRoot.replaceChildren(node('p','message error',errorText(error,'暂时无法查看原格，请重试。')));
    }
  }
  function openTask(label,taskId) {
    const button=action(label,()=>onOpenTask?.(taskId),'button quiet');button.disabled=!taskId||!onOpenTask;return button;
  }
  function drawResults() {
    if(!result)return;
    results.replaceChildren();clearSource();
    const panel=node('section','panel');panel.append(node('h3','','查询结果'));
    const entries=result.hits.flatMap(hit=>hit.values.length?hit.values.map(row=>({hit,row})):[{hit,row:null}]);
    if(!entries.length)panel.append(node('p','muted','没有符合条件的已保存测试结果。'));
    else {
      const start=page*50,shown=entries.slice(start,start+50),scroll=node('div','table-scroll row-query-table-scroll'),table=node('table','row-query-table');
      const head=node('thead'),heading=node('tr');
      for(const title of ['样品 / 测试记录','指标','结果','单位','方法与条件']){const cell=node('th','',title);cell.setAttribute('scope','col');heading.append(cell);}
      head.append(heading);table.append(head);
      const body=node('tbody');
      for(const {hit,row} of shown){
        const tr=node('tr');tr.append(node('td','',valueText(hit.sample,240)),node('td','',row?valueText(row.field,240):hit.legacy?'旧版记录':'待查看'));
        const value=node('td');
        if(row){
          const button=action(valueText(row.value,512),()=>showSource(hit,row),'button quiet row-query-value');
          button.setAttribute('aria-label',`查看 ${valueText(hit.sample,100)} · ${valueText(row.field,100)} 的原格`);value.append(button);
          if(row.value_truncated||String(row.value??'').length>512)value.append(node('span','help','（文字已截断，点击查看原格）'));
        } else {
          value.append(openTask(hit.legacy?'打开旧版记录':'打开记录',hit.taskId),
            node('small','help',hit.legacy?'旧版结果需在资料页查看。':'此记录未提供可直接展示的测试值。'));
        }
        tr.append(value,node('td','',row?valueText(row.unit||'未注明',80):'—'));
        const conditions=node('td');for(const [key,binding] of Object.entries(row?.source_bindings||{})){
          if(!key.startsWith('qualifier:condition:')&&key!=='qualifier:method')continue;
          const label=key==='qualifier:method'?'方法':key.slice('qualifier:condition:'.length).replace(/^header_(\d+)$/,'表头条件 $1');
          for(const source of (Array.isArray(binding)?binding:[binding]).filter(Boolean))
            conditions.append(node('p','',`${label}：${valueText(source.raw,100)}`));
        }if(!conditions.children.length)conditions.textContent='—';tr.append(conditions);body.append(tr);
      }
      table.append(body);table.setAttribute('aria-label','测试查询结果');scroll.append(table);panel.append(scroll);
      const visibleHits=new Set(shown.map(entry=>entry.hit));
      for(const hit of visibleHits)if(hit.truncated){
        const note=node('p','help',`${valueText(hit.sample,100)}：此记录命中 ${hit.total} 项，当前返回 ${hit.values.length} 项，结果已截断。`);
        note.append(openTask('查看完整记录',hit.taskId));panel.append(note);
      }
      if(entries.length>50){
        const pager=node('div','query-actions'),previous=action('上一页',()=>{page--;drawResults();}),next=action('下一页',()=>{page++;drawResults();});
        previous.disabled=page===0;next.disabled=start+shown.length>=entries.length;
        pager.append(previous,node('span','',`${start+1}–${start+shown.length} / ${entries.length}`),next);panel.append(pager);
      }
    }
    results.append(panel);
    const groups=exclusionGroups(result.exclusions);
    if(groups.length){
      const notes=node('section','panel row-query-notes');notes.append(node('h3','','未命中或不能比较'),node('p','help','以下为原值的比较次数，同一原值可能计入多次。'));
      const list=node('ul');for(const group of groups)list.append(node('li','',`${group.label} · ${group.count}`));notes.append(list);results.append(notes);
    }
  }
  drawFields();
  return {dispose(){disposed=true;version++;request?.abort();clearSource();}};
}
