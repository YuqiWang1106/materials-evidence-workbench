import {FIELD_LABELS, errorText, valueText} from './result-logic.js';
import {QUERY_TYPES,QUERY_OPERATORS,operatorsFor,buildQuery,exclusionGroups,queryRecordLabel} from './query-logic.mjs';

/** User-entered filters query current published records; no client scientific joins. */
export function mountQuery({root,api,catalogue,onOpenTask}) {
  const doc=root.ownerDocument;
  let disposed=false,version=0,busy=false,request=null,result=null,page=0;
  const fields=(catalogue?.fields?.length ? catalogue.fields : Object.entries(FIELD_LABELS).map(([field_id,label])=>({field_id,label})))
    .filter(field=>field.field_id&&field.label);
  const initialField=fields.find(field=>field.field_id==='observation.value')?.field_id || fields[0]?.field_id || '';
  const newFilter=()=>({field_id:initialField,type:'number',op:'gte',value:'',unit:'',basis:'',metric:'',parameter:'',allowCenter:false,allowFormula:false});
  const filters=[newFilter()];
  const node=(tag,className,text)=>{const element=doc.createElement(tag);if(className)element.className=className;if(text!==undefined)element.textContent=String(text);return element;};
  const button=(label,handler,className='button secondary')=>{const element=node('button',className,label);element.type='button';element.addEventListener('click',handler);return element;};
  const wrapper=node('section','query-workspace');
  wrapper.append(node('p','query-intro','从已保存的当前版本中筛选数据。多个条件必须由同一明确归属中的记录共同满足。'));
  const form=node('form','query-form panel'),rows=node('div','query-filter-list');
  form.setAttribute('aria-label','数据查询条件');
  const message=node('p','message');message.hidden=true;message.setAttribute('role','status');
  const results=node('div','query-results');
  const add=button('添加条件',()=>{if(filters.length<20){filters.push(newFilter());invalidate();drawFilters();}});
  const search=button('查询数据',event=>{event.preventDefault();run();},'button primary');search.type='submit';
  const actions=node('div','query-actions');actions.append(add,search);
  form.append(rows,node('p','help','范围和上下限必须全部满足才命中。单位只按已知同名写法匹配，不进行换算。'),actions);
  form.addEventListener('submit',event=>{event.preventDefault();run();});
  wrapper.append(form,message,results);root.replaceChildren(wrapper);

  function tell(text,tone='') {message.textContent=text;message.hidden=!text;message.className=`message ${tone}`.trim();}
  function invalidate() {
    version++;request?.abort();request=null;busy=false;search.disabled=false;search.textContent='查询数据';
    if(result || results.textContent){result=null;results.replaceChildren();tell('条件已变更，请重新查询。');}
  }
  function changed(row,key,value,redraw=false) {row[key]=value;invalidate();if(redraw)drawFilters();}
  function field(label,control,help='') {const wrap=node('label','query-field');wrap.append(node('span','',label),control);if(help)wrap.append(node('small','help',help));return wrap;}
  function select(label,options,value,onChange) {
    const control=node('select');control.setAttribute('aria-label',label);
    for(const [key,title] of options){const option=node('option','',title);option.value=key;control.append(option);}
    control.value=value;control.addEventListener('change',()=>onChange(control.value));return control;
  }
  function textInput(label,value,onInput,placeholder='') {
    const control=node('input');control.type='text';control.value=value;control.placeholder=placeholder;control.setAttribute('aria-label',label);
    control.addEventListener('input',()=>onInput(control.value));return control;
  }
  function checkbox(label,checked,onChange) {
    const control=node('input');control.type='checkbox';control.checked=checked;
    control.addEventListener('change',()=>onChange(control.checked));return field(label,control);
  }
  function drawFilters() {
    rows.replaceChildren();add.disabled=filters.length>=20;
    filters.forEach((row,index)=>{
      const title=`第 ${index+1} 条`,box=node('fieldset','query-filter');box.append(node('legend','',`${title}条件`));
      const grid=node('div','query-filter-grid');
      const selectedField=select(`${title}字段`,fields.map(item=>[item.field_id,item.label]),row.field_id,value=>{
        row.field_id=value;row.metric='';row.parameter='';
        const kinds=fields.find(item=>item.field_id===value)?.value_kinds || [];
        if(kinds.length && kinds.every(kind=>['text','category'].includes(kind))){row.type='text';row.op='eq';}
        invalidate();drawFilters();
      });
      grid.append(field('查找字段',selectedField),field('值的类型',select(`${title}值的类型`,Object.entries(QUERY_TYPES),row.type,value=>{
        row.type=value;if(!operatorsFor(value).includes(row.op))row.op=operatorsFor(value)[0];
        row.allowCenter=false;invalidate();drawFilters();
      })),field('满足条件',select(`${title}比较操作`,operatorsFor(row.type).map(key=>[key,QUERY_OPERATORS[key]]),row.op,value=>changed(row,'op',value,true))));
      if(row.op!=='exists') {
        const placeholders={number:'如 12.5（小数点）',text:'按原文填写',category:'按原类别填写',date:'2024-02-29',time:'14:30:00',datetime:'2024-02-29T14:30:00',duration:'PT90M（90 分钟）'};
        const value=textInput(`${title}比较值`,row.value,value=>changed(row,'value',value),placeholders[row.type]);
        if(row.type==='number')value.inputMode='decimal';
        grid.append(field('比较值',value));
      }
      box.append(grid);
      const more=node('details','query-options');more.append(node('summary','','限定原文名称、单位与比较方式'));
      const extras=node('div','query-filter-grid');
      if(row.field_id.startsWith('observation.') || row.field_id.startsWith('series.')) extras.append(field('原指标名称（可选）',
        textInput(`${title}原指标名称`,row.metric,value=>changed(row,'metric',value)),'与已整理的原指标名称完全相同；不进行模糊匹配。'));
      if(['condition.parameter','process.parameter','instrument.parameter'].includes(row.field_id)) extras.append(field('原条件名称（可选）',
        textInput(`${title}原条件名称`,row.parameter,value=>changed(row,'parameter',value)),'与已整理的条件名称完全相同。'));
      extras.append(field('单位（可选）',textInput(`${title}单位`,row.unit,value=>changed(row,'unit',value),'如 MPa')),
        field('用量基准（可选）',textInput(`${title}用量基准`,row.basis,value=>changed(row,'basis',value),'按原文填写')));
      more.append(extras);
      const opts=node('div','query-checks');
      if(row.type==='number'&&row.op!=='exists')opts.append(checkbox('允许用带误差数值的中心值比较',row.allowCenter,value=>changed(row,'allowCenter',value)));
      opts.append(checkbox('允许用原件保存的公式缓存比较',row.allowFormula,value=>changed(row,'allowFormula',value)));
      more.append(opts,node('p','help',row.op==='exists'
        ? '公式缓存默认不参与存在判断。启用后按缓存值的类型判断；不代表实测，也不会执行或重算公式。'
        : '默认不启用。中心值不会补充误差含义；公式缓存不代表实测，也不会执行或重算公式。'));
      if(row.op==='exists')more.open=true;
      box.append(more);
      if(filters.length>1)box.append(button('移除此条件',()=>{filters.splice(index,1);invalidate();drawFilters();},'button quiet'));
      rows.append(box);
    });
  }
  async function run() {
    if(disposed||busy)return;
    let payload;try{payload=buildQuery(filters);}catch(error){tell(error.message,'error');return;}
    const token=++version;request?.abort();request=new AbortController();busy=true;search.disabled=true;search.textContent='正在查询…';
    result=null;results.replaceChildren(node('p','muted','正在查找已保存的数据…'));tell('');
    try {
      const found=await api('/api/search/detailed',{method:'POST',json:payload,signal:request.signal});
      if(disposed||token!==version)return;
      if(!Array.isArray(found?.results)||!Array.isArray(found?.exclusions))throw new Error('查询服务返回内容不完整，请重试。');
      result=found;page=0;drawResults();
    } catch(error) {if(!disposed&&token===version){results.replaceChildren();tell(errorText(error,'查询未完成，请重试。'),'error');}}
    finally {if(!disposed&&token===version){busy=false;search.disabled=false;search.textContent='查询数据';}}
  }
  function taskButton(label,taskId) {
    const open=button(label,()=>onOpenTask?.(taskId),'button quiet');open.disabled=!taskId||!onOpenTask;return open;
  }
  function drawResults() {
    if(!result)return;
    results.replaceChildren();const hits=node('section','panel');
    hits.append(node('h3','',`符合条件的记录 · ${result.results.length.toLocaleString('zh-CN')}`));
    if(!result.results.length)hits.append(node('p','muted','当前已保存版本中没有符合全部条件的记录。未保存结果不会出现在这里。'));
    const start=page*50,shown=result.results.slice(start,start+50);
    for(const row of shown) {
      const display=queryRecordLabel(row),item=node('article','query-hit');
      item.append(node('div','query-hit-kind',display.kind),node('h4','',valueText(display.label,240)),
        node('p','help',`共同满足条件的结果项：${row.assertion_refs?.length || 0}。打开资料核对原值、归属及来源。`));
      if(row.series_ref)item.append(node('p','help','包含明确归属的曲线上下文。'));
      else if(row.test_ref||row.run_ref)item.append(node('p','help','包含明确归属的测试或运行上下文。'));
      item.append(taskButton('查看记录与来源',row.task_id));hits.append(item);
    }
    if(result.results.length>50) {
      const controls=node('div','query-actions'),prev=button('上一页',()=>{page--;drawResults();}),next=button('下一页',()=>{page++;drawResults();});
      prev.disabled=page===0;next.disabled=start+shown.length>=result.results.length;
      controls.append(prev,node('span','',`${start+1}–${start+shown.length} / ${result.results.length}`),next);hits.append(controls);
    }
    results.append(hits);
    const groups=exclusionGroups(result.exclusions);
    if(!groups.length)return;
    const diagnostics=node('section','panel');diagnostics.append(node('h3','','未命中与不能比较'),
      node('p','help','以下按比较记录计数；同一原值可能被多条条件检查。它们不代表资料总数。'));
    for(const comparable of [false,true]) {
      const selected=groups.filter(group=>group.comparable===comparable);if(!selected.length)continue;
      diagnostics.append(node('h4','',comparable?'值未满足条件':'当前不能比较'));
      for(const group of selected) {
        const line=node('details','query-exclusion');line.append(node('summary','',`${group.label} · ${group.count} 条`));
        group.taskIds.slice(0,20).forEach((taskId,index)=>line.append(taskButton(`打开第 ${index+1} 份相关资料`,taskId)));
        if(group.taskIds.length>20)line.append(node('p','help',`共 ${group.taskIds.length} 份相关资料；当前提供前 20 份入口。可增加条件缩小范围。`));
        diagnostics.append(line);
      }
    }
    results.append(diagnostics);
  }
  drawFilters();
  return ()=>{disposed=true;version++;request?.abort();};
}
