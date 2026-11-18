import {columnName} from './reading-logic.mjs';
import {initialForm,formIssue,STANDARD_KINDS} from './standard-logic.mjs';
export const sameForm=(a,b)=>JSON.stringify(canonical(a))===JSON.stringify(canonical(b));
function canonical(v){return Array.isArray(v)?v.map(canonical):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,canonical(v[k])])):v;}
export const isPlaceholder=(form,context)=>!form||sameForm(form,initialForm(context));
export const headerCells=context=>(context?.headers||[]).flatMap(r=>r.cells||[]);
function sourceCell(binding,context){const cells=headerCells(context),cell=cells.find(c=>c.row===binding.cell?.[0]&&c.column===binding.cell?.[1]);return cell?.merged_origin?cells.find(c=>c.address===cell.merged_origin):cell;}
export function bindingText(binding,context){
 if(binding?.kind==='absent')return '原表未注明';if(!binding||binding.kind==='unresolved')return '尚未确定';
 const cell=sourceCell(binding,context);
 if(!cell)return '来源不在本表表头';const raw=String(cell.raw_value??'');
 return binding.kind==='cell_span'?Array.from(raw).slice(...binding.span).join(''):raw;
}
export function curveRole(form,column){const c=form.curve||{};return c.x_column===column?'x':c.series?.some(s=>s.result_column===column)?'result':c.ignored_columns?.includes(column)?'ignored':'unresolved';}
function bindingIssue(value,context,required,label){
 if(value?.kind==='absent')return required?label+'需要选择原表文字':null;
 if(!['cell','cell_span'].includes(value?.kind))return label+'来源尚未确定';
 const cell=sourceCell(value,context);if(!cell)return label+'来源不在当前表头';
 if(cell.formula||['f','formula','e','error'].includes(cell.value_type)||typeof cell.raw_value==='string'&&cell.raw_value.trimStart().startsWith('='))return label+'不能使用公式或错误单元格';
 const raw=Array.from(String(cell.raw_value??''));if(!raw.join('').trim())return label+'原格没有文字';
 if(value.kind==='cell_span'&&(typeof cell.raw_value!=='string'||!Array.isArray(value.span)||value.span.length!==2||!value.span.every(Number.isInteger)||value.span[0]<0||value.span[1]>Math.min(4096,raw.length)||value.span[1]<=value.span[0]||!raw.slice(...value.span).join('').trim()))return label+'选中文字无效';
 return null;
}
export function meaningIssue(form,context){
 if(form?.version!=='3'||form.row_kind!=='curve_point')return !STANDARD_KINDS.has(form?.row_kind)?'请确认行含义，或从原表补全可整理的曲线。':formIssue(form);
 const c=form.curve||{},columns=[...new Set(headerCells(context).map(c=>c.column))];
 if(!columns.includes(c.x_column))return '请选择共享横轴列';
 if(!c.series?.length)return '请至少选择一列曲线结果';
 const assigned=[c.x_column,...c.series.map(s=>s.result_column),...(c.ignored_columns||[])];
 if(new Set(assigned).size!==assigned.length||assigned.some(col=>!columns.includes(col)))return '列职责重复或超出当前表格';
 if(columns.some(col=>!assigned.includes(col)))return columns.filter(col=>!assigned.includes(col)).map(columnName).join('、')+' 列职责尚未确定；不整理的列须明确忽略';
 if(!['source','decimal_dot','decimal_comma'].includes(form.number_format))return '请选择数字书写方式';
 for(const [value,required,label]of [[c.x_label,true,'横轴名称'],[c.x_unit,false,'横轴单位'],...c.series.flatMap(s=>[[s.material,false,columnName(s.result_column)+' 列材料'],[s.stage,false,columnName(s.result_column)+' 列阶段'],[s.metric,true,columnName(s.result_column)+' 列指标'],[s.unit,false,columnName(s.result_column)+' 列单位']])]){const issue=bindingIssue(value,context,required,label);if(issue)return issue;}
 return null;
}
