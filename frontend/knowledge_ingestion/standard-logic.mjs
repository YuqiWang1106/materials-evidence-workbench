/** Source-only helpers for the Step83 finite meaning form. No values are inferred. */
export const ROW_KINDS=[['unknown','尚不清楚'],['sample_record','样品 / 材料配置'],['test_record','逐次实验 / 测试记录'],
 ['curve_point','曲线点（本次不支持）'],['statistic','统计汇总（本次不支持）'],['run_log','运行日志（本次不支持）']];
export const COLUMN_ROLES=[['unknown','尚不清楚'],['identity','身份 / 编号'],['result','测量结果'],['condition','实验条件'],
 ['method','方法'],['metric','项目名称列'],['unit','单位列'],['reference','引用 / DOI'],['ignored','本次不整理']];
export const STANDARD_KINDS=new Set(['sample_record','test_record']);

export function headerPath(context,column){
 const origins=new Map((context.headers||[]).flatMap(row=>row.cells||[]).map(cell=>[cell.address,cell]));
 const seen=new Set(),labels=[];
 for(const row of context.headers||[]){
  const cell=(row.cells||[]).find(cell=>cell.column===column);
  if(!cell)continue;
  const origin=origins.get(cell.merged_origin)||cell;
  if(seen.has(origin.address))continue;
  seen.add(origin.address);
  const value=origin.raw_value;
  if(value!==null&&value!==undefined&&String(value).trim())labels.push(String(value).trim());
 }
 return labels.join(' / ')||'表头未注明';
}

export function isHeaderCell(context,row,column){
 return (context.headers||[]).some(line=>line.row===row&&(line.cells||[]).some(cell=>cell.column===column));
}

export function initialForm(context){
 const columns=[...new Set((context.headers||[]).flatMap(row=>(row.cells||[]).map(cell=>cell.column)))].sort((a,b)=>a-b);
 return {version:'2',row_kind:'unknown',layout:'wide_rows',identity_mode:'row_independent',number_format:'source',
  header_conditions:[],column_roles:columns.map(column=>({column,role:'unknown',reason:'尚未判断'})),measurements:[]};
}

export function setColumnRole(form,column,role,reason){
 const next=structuredClone(form),entry=next.column_roles.find(item=>item.column===column);
 if(!entry)throw Error('所选列不在当前表区');
 entry.role=role;entry.reason=reason?.trim()||'待研究者核对';
 next.measurements=next.measurements.filter(item=>next.column_roles.some(r=>r.column===item.result_column&&r.role==='result'));
 if(role==='result'&&!next.measurements.some(item=>item.result_column===column))
  next.measurements.push({result_column:column,value_type:'number',metric:{kind:'header'},unit:{kind:'absent'}});
 next.measurements.sort((a,b)=>a.result_column-b.result_column);
 return next;
}

export function addMeasurementDraft(form,column){
 const next=structuredClone(form),role=next.column_roles.find(item=>item.column===column);
 if(!role||role.role!=='result')throw Error('请先将一列标为测量结果');
 if(next.measurements.some(item=>item.result_column===column))return next;
 const metric=next.layout==='wide_rows'?{kind:'header'}:
  {kind:'unresolved'};
 next.measurements.push({result_column:column,value_type:'unresolved',metric,unit:{kind:'unresolved'}});
 next.measurements.sort((a,b)=>a.result_column-b.result_column);
 return next;
}

export function selectUnitText(cell,start,end){
 if(!cell||!Array.isArray(cell.cell)||!cell.cell.every(n=>Number.isInteger(n)&&n>0)||
   !Number.isInteger(start)||!Number.isInteger(end)||start<0||end<=start||end>String(cell.raw_value??'').length||
   !String(cell.raw_value).slice(start,end).trim())throw Error('请在原格文字中选中单位');
 return {kind:'cell_span',cell:cell.cell,span:[start,end]};
}

export function formIssue(form){
 if(!form||form.version!=='2'||!Array.isArray(form.column_roles)||!Array.isArray(form.measurements))return '读法表单不完整';
 if(!STANDARD_KINDS.has(form.row_kind))return null;
 const roles=form.column_roles,identities=roles.filter(r=>r.role==='identity'),results=roles.filter(r=>r.role==='result');
 if(identities.length<1||identities.length>3)return '请选 1–3 列身份 / 编号；逐次编号不等于物理样品。';
 if(!results.length)return '请标出至少一列测量结果。';
 if(roles.some(r=>r.role==='unknown'||!r.reason?.trim()))return '请给每列选定职责并简述原因；可明确选择本次不整理。';
 if(roles.filter(r=>r.role==='method').length>1)return '方法列最多一列。';
 if(form.identity_mode==='unresolved')return '请明确同名编号的归属方式。';
 if(form.layout==='wide_rows'&&form.identity_mode!=='row_independent')return '宽表按每行独立记录核对。';
 if(form.layout==='long_rows'&&(results.length!==1||roles.filter(r=>r.role==='metric').length!==1))
  return '长表需一列结果和一列来源项目名称。';
 if(!['source','decimal_dot','decimal_comma'].includes(form.number_format))return '请选择数字书写方式。';
 if(form.measurements.length!==results.length||form.measurements.some(m=>!results.some(r=>r.column===m.result_column)))
  return '每列结果都需要一个数值类型与单位来源。';
 if(form.measurements.some(m=>m.value_type==='unresolved'||m.metric?.kind==='unresolved'||m.unit?.kind==='unresolved'))
  return '请补全结果类型、项目名称来源和单位来源；没有单位时可明确选择“原表未注明单位”。';
 if(form.measurements.some(m=>!['number','category'].includes(m.value_type)||!m.unit||!m.metric))return '请核对每列结果的类型和单位。';
 if(form.header_conditions?.length>8)return '表头固定条件最多 8 项；请缩小当前表区。';
 if(form.measurements.some(m=>form.layout==='wide_rows'&&m.metric.kind!=='header'))return '宽表指标名称应来自原表头。';
 if(form.measurements.some(m=>form.layout==='long_rows'&&m.metric.kind==='column'&&!roles.some(r=>r.column===m.metric.column&&r.role==='metric')))
  return '请从已标为项目名称的原列选择指标来源。';
 if(form.measurements.some(m=>m.unit.kind==='column'&&!roles.some(r=>r.column===m.unit.column&&r.role==='unit')))
  return '请从已标为单位的原列选择单位来源。';
 if(form.measurements.some(m=>m.unit.kind==='cell_span'&&(!Array.isArray(m.unit.span)||m.unit.span[1]<=m.unit.span[0])))return '请在原格文字中选中单位。';
 return null;
}
