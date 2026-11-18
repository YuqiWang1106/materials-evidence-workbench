import {ROW_KINDS,COLUMN_ROLES,headerPath,addMeasurementDraft,initialForm} from './standard-logic.mjs';

const clone=value=>structuredClone(value);
const columnName=value=>{let name='';while(value>0){value--;name=String.fromCharCode(65+value%26)+name;value=Math.floor(value/26);}return name;};
const unresolved=()=>({kind:'unresolved'});
const short=value=>Array.from(String(value??'')).slice(0,90).join('');
const referenceKey=reference=>reference?.kind==='cell'||reference?.kind==='cell_span'
  ?`${reference.kind==='cell_span'?'span':'cell'}:${reference.cell?.join(':')}`
  :reference?.kind==='column'?`column:${reference.column}`:reference?.kind||'unresolved';

function headerSources(context){
  const cells=(context?.headers||[]).flatMap(row=>row.cells||[]),byAddress=new Map(cells.map(cell=>[cell.address,cell]));
  return cells.flatMap(cell=>{
    const original=cell.merged_origin?byAddress.get(cell.merged_origin):cell;
    if(!original||original.raw_value==null||!String(original.raw_value).trim()||original.formula||['f','e','error'].includes(original.value_type))return [];
    const address=cell.address||`${columnName(cell.column)}${cell.row}`;
    return [{cell:[cell.row,cell.column],address,raw:String(original.raw_value),
      label:`${address}${cell.merged_origin?` → ${cell.merged_origin}`:''} · ${short(original.raw_value)}`}];
  });
}

/** Edits one private form copy; only an explicit save calls the parent. */
export function renderMeaningEditor({document:doc=globalThis.document,form,context,region,column,mode='column',onSave,onCancel}){
  let draft=clone(form),finished=false,error='',transitionNotice='';
  const family=value=>value.row_kind==='curve_point'?'curve':'records';
  const variants=new Map([[family(draft),clone(draft)]]);
  const node=(tag,cls,text)=>{const el=doc.createElement(tag);if(cls)el.className=cls;if(text!==undefined)el.textContent=String(text);return el;};
  const dialog=node('dialog','standard-meaning-dialog');dialog.open=true;dialog.setAttribute('open','');
  dialog.setAttribute('aria-modal','true');dialog.setAttribute('aria-label',mode==='table'?'编辑表格含义':`编辑 ${columnName(column)} 列含义`);
  const sources=headerSources(context);
  const act=(label,handler,cls='button secondary')=>{const button=node('button',cls,label);button.type='button';button.addEventListener('click',handler);return button;};
  const input=(label,value,update,{readonly=false}={})=>{const field=node('input');field.type='text';field.value=String(value??'');field.readOnly=readonly;field.setAttribute('aria-label',label);
    if(update){const handler=()=>update(field.value);field.addEventListener('input',handler);field.addEventListener('change',handler);}return field;};
  const select=(label,options,value,update)=>{const field=node('select');field.setAttribute('aria-label',label);
    for(const [key,text] of options){const option=node('option','',text);option.value=String(key);field.append(option);}
    field.value=String(value??'unresolved');field.addEventListener('change',()=>{if(options.some(([key])=>String(key)===field.value))update(field.value);});return field;};
  const labeled=(label,field)=>{const wrapper=node('label','standard-meaning-field');wrapper.append(node('span','',label),field);return wrapper;};
  const update=change=>{change();error='';draw();};
  const cancel=()=>{if(finished)return;finished=true;onCancel?.();};
  dialog.addEventListener('cancel',event=>{event.preventDefault();cancel();});
  dialog.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();cancel();}});

  function sourceEditor(label,reference,setReference,{absent=true,span=true,header=false,columns=[]}={}){
    const group=node('section','standard-meaning-source');
    const options=[['unresolved','尚未确认来源'],...(absent?[['absent','原表未说明']]:[]),...(header?[['header','本列表头']]:[]),
      ...columns.map(value=>[`column:${value}`,`${columnName(value)} 列 · ${headerPath(context,value)}`]),
      ...sources.map(source=>[`cell:${source.cell.join(':')}`,`${source.label} · 整格`]),
      ...(span?sources.filter(source=>Array.from(source.raw).length>1).map(source=>[`span:${source.cell.join(':')}`,`${source.label} · 文字片段`]):[])];
    group.append(labeled(label,select(label,options,referenceKey(reference),value=>update(()=>{
      if(value==='absent'||value==='unresolved'||value==='header')setReference({kind:value});
      else if(value.startsWith('column:'))setReference({kind:'column',column:Number(value.slice(7))});
      else {const parts=value.split(':'),cell=parts.slice(1).map(Number);setReference(parts[0]==='span'?{kind:'cell_span',cell,span:[0,0]}:{kind:'cell',cell});}
    }))));
    if(reference?.kind==='cell_span'){
      const source=sources.find(item=>item.cell.join(':')===reference.cell?.join(':'));
      if(source){
        const text=input(label+'原文',source.raw,null,{readonly:true});
        group.append(labeled('在原文中选择片段',text),act('使用选中文字',()=>{
          const start=text.selectionStart,end=text.selectionEnd,raw=source.raw;
          const boundary=index=>!(index>0&&index<raw.length&&/[\uD800-\uDBFF]/.test(raw[index-1])&&/[\uDC00-\uDFFF]/.test(raw[index]));
          if(!Number.isInteger(start)||!Number.isInteger(end)||start<0||end<=start||end>raw.length||!boundary(start)||!boundary(end)||!raw.slice(start,end).trim()){
            error=`${label}：请选中完整且非空的原文片段`;draw();return;
          }
          update(()=>setReference({kind:'cell_span',cell:clone(source.cell),
            span:[Array.from(raw.slice(0,start)).length,Array.from(raw.slice(0,end)).length]}));
        }));
        const selected=Array.isArray(reference.span)?Array.from(source.raw).slice(...reference.span).join(''):'';
        group.append(node('p','standard-meaning-selection',selected?`当前选中：${selected}`:'尚未选择原文片段'));
      }else group.append(node('p','message warn',`${label}：所选原格不在当前表头中，请重新选择`));
    }
    return group;
  }

  function numberFormat(panel){panel.append(labeled('数值写法',select('数值写法',[
    ['source','按原表原生格式'],['decimal_dot','小数点（0.81）'],['decimal_comma','小数逗号（0,81）']],
    draft.number_format,value=>{draft.number_format=value;})));}

  function switchRowKind(value){
    const previous=family(draft),next=value==='curve_point'?'curve':'records';
    if(previous!==next){
      variants.set(previous,clone(draft));
      if(variants.has(next))draft=clone(variants.get(next));
      else if(draft.version==='3'){
        const source=draft;draft=initialForm(context);draft.number_format=source.number_format;
        for(const entry of draft.column_roles)if(source.curve.ignored_columns.includes(entry.column)){
          entry.role='ignored';entry.reason='保留已明确的不整理列';
        }
      }
      // A v2 curve proposal retains all source fields for the parent's bounded
      // candidate preparation. No source mapping is inferred in this editor.
      transitionNotice=next==='curve'?'保存后请核对横轴、材料和阶段来源。切回原类型可恢复本次编辑。'
        :'切换类型后请重新核对列映射；曲线材料和阶段不作为行身份。切回原类型可恢复本次编辑。';
    }
    draft.row_kind=value;
  }

  function tableEditor(panel){
    panel.append(labeled('每行代表什么',select('每行代表什么',ROW_KINDS.map(([key,text])=>[key,key==='curve_point'?'曲线点':text]),draft.row_kind,value=>update(()=>switchRowKind(value)))));
    if(transitionNotice)panel.append(node('p','standard-meaning-transition',transitionNotice));
    if(draft.row_kind==='curve_point'){panel.append(node('p','', '每行代表曲线点，各结果列共用横轴'));numberFormat(panel);return;}
    panel.append(labeled('结果排列方式',select('结果排列方式',[['wide_rows','每列一个结果（宽表）'],['long_rows','项目列 + 结果列（长表）']],draft.layout,value=>update(()=>{
        if(draft.layout===value)return;draft.layout=value;
        for(const measurement of draft.measurements||[])measurement.metric=value==='wide_rows'?{kind:'header'}:unresolved();
      }))),
      labeled('同名编号如何解释',select('同名编号如何解释',[['row_independent','每行独立记录'],['same_id_in_scope','同一表区同编号归为一个对象'],['unresolved','尚未确定']],
        draft.identity_mode,value=>{draft.identity_mode=value;})));
    numberFormat(panel);
    const conditions=node('details','standard-meaning-conditions');conditions.append(node('summary','',`表头固定条件（${draft.header_conditions?.length||0}）`));
    for(const source of sources){
      const existing=(draft.header_conditions||[]).find(item=>item.cell.join(':')===source.cell.join(':'));
      const checkbox=node('input');checkbox.type='checkbox';checkbox.checked=Boolean(existing);checkbox.setAttribute('aria-label','固定条件 '+source.address);
      checkbox.addEventListener('change',()=>update(()=>{
        draft.header_conditions=(draft.header_conditions||[]).filter(item=>item.cell.join(':')!==source.cell.join(':'));
        if(checkbox.checked)draft.header_conditions.push({cell:clone(source.cell),reason:'表头声明的固定条件'});
      }));
      conditions.append(labeled(source.label,checkbox));
      if(existing)conditions.append(labeled('判断依据',input('固定条件 '+source.address+' 判断依据',existing.reason,value=>{existing.reason=value;})));
    }
    panel.append(conditions);
  }

  function ordinaryColumn(panel){
    const entry=draft.column_roles?.find(item=>item.column===column);
    if(!entry){panel.append(node('p','message warn','请选择当前表范围内的一列'));return;}
    panel.append(labeled('这列是',select('这列是',COLUMN_ROLES,entry.role,value=>update(()=>{
      entry.role=value;
      if(value!=='result')draft.measurements=(draft.measurements||[]).filter(item=>item.result_column!==column);
      else draft=addMeasurementDraft(draft,column);
    }))),labeled('判断依据',input('判断依据',entry.reason,value=>{entry.reason=value;})));
    if(entry.role!=='result')return;
    const measurement=draft.measurements?.find(item=>item.result_column===column);
    if(!measurement){panel.append(act('补充结果设置',()=>update(()=>{draft=addMeasurementDraft(draft,column);})));return;}
    panel.append(labeled('原值类型',select('原值类型',[['unresolved','尚未确认'],['number','数字'],['category','类别 / 等级']],
      measurement.value_type,value=>{measurement.value_type=value;})));
    if(draft.layout==='wide_rows')panel.append(labeled('指标来源',select('指标来源',[['header','本列表头'],['unresolved','尚未确认']],measurement.metric?.kind,
      value=>{measurement.metric={kind:value};})));
    else panel.append(sourceEditor('指标来源',measurement.metric,value=>{measurement.metric=value;},
      {absent:false,span:false,columns:(draft.column_roles||[]).filter(item=>item.role==='metric').map(item=>item.column)}));
    panel.append(sourceEditor('单位来源',measurement.unit,value=>{measurement.unit=value;},
      {columns:(draft.column_roles||[]).filter(item=>item.role==='unit').map(item=>item.column)}));
  }

  function curveColumn(panel){
    const curve=draft.curve;
    const role=curve.x_column===column?'x':curve.series.some(item=>item.result_column===column)?'result':curve.ignored_columns.includes(column)?'ignored':'unresolved';
    panel.append(labeled('这列是',select('这列是',[['unresolved','尚未确认'],['x','共享横轴'],['result','曲线结果'],['ignored','本次不整理']],role,value=>update(()=>{
      curve.ignored_columns=curve.ignored_columns.filter(item=>item!==column);
      if(value!=='result')curve.series=curve.series.filter(item=>item.result_column!==column);
      if(curve.x_column===column&&value!=='x'){curve.x_column=null;curve.x_label=unresolved();curve.x_unit=unresolved();}
      if(value==='x'&&curve.x_column!==column){curve.x_column=column;curve.x_label=unresolved();curve.x_unit=unresolved();}
      if(value==='ignored')curve.ignored_columns.push(column);
      if(value==='result'&&!curve.series.some(item=>item.result_column===column))curve.series.push({result_column:column,
        material:unresolved(),stage:unresolved(),metric:unresolved(),unit:unresolved()});
      curve.ignored_columns.sort((a,b)=>a-b);curve.series.sort((a,b)=>a.result_column-b.result_column);
    }))));
    if(role==='x')panel.append(sourceEditor('横轴名称来源',curve.x_label,value=>{curve.x_label=value;},{absent:false}),
      sourceEditor('横轴单位来源',curve.x_unit,value=>{curve.x_unit=value;}));
    if(role==='result'){
      const series=curve.series.find(item=>item.result_column===column);
      for(const [key,label] of [['material','材料来源'],['stage','阶段来源'],['metric','指标来源'],['unit','单位来源']])
        panel.append(sourceEditor(label,series[key],value=>{series[key]=value;},{absent:key!=='metric'}));
    }
  }

  function draw(){
    const panel=node('section','standard-meaning-editor');panel.append(node('h2','',mode==='table'?'编辑表格含义':`${columnName(column)} 列 · ${headerPath(context,column)}`));
    if(mode==='table')tableEditor(panel);else if(draft.version==='3')curveColumn(panel);else ordinaryColumn(panel);
    if(error)panel.append(node('p','message error',error));
    const controls=node('div','actions');controls.append(act('取消',cancel),act('保存修改',()=>{
      if(finished)return;finished=true;onSave?.(clone(draft));
    },'button primary'));panel.append(controls);dialog.replaceChildren(panel);
  }
  draw();return dialog;
}
