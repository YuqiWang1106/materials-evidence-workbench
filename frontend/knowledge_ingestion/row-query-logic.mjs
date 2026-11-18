import {queryRecordLabel} from './query-logic.mjs';

export const ROW_QUERY_OPERATORS = {gte:'大于或等于',gt:'大于',lte:'小于或等于',lt:'小于',eq:'等于'};
const present = value => typeof value === 'string' && value.trim().length > 0;

/** Send scientific matching to the common backend; preserve source spellings. */
export function buildRowQuery({sample='',metric='',mode='any',operator='gte',value='',unit=''} = {}) {
  if (!['any','number','category'].includes(mode)) throw new Error('请选择数值或等级筛选。');
  if (mode !== 'any' && !present(metric)) throw new Error('请填写要比较的指标。');
  const qualifiers = present(metric) ? {metric_label:metric} : undefined;
  const observation = {field_id:'observation.value',...(qualifiers ? {qualifiers} : {})};
  const filters = [];
  if (present(sample)) filters.push({field_id:'material.identity',op:'eq',value_type:'text',value:sample});
  // Without this filter, sample-only searches do not include matched test values.
  filters.push({...observation,op:'exists'});
  if (mode === 'number') {
    if (!present(unit)) throw new Error('数值比较需要填写单位。');
    if (!Object.hasOwn(ROW_QUERY_OPERATORS,operator)) throw new Error('请选择有效的数值比较操作。');
    const raw = String(value ?? '').trim();
    if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(raw))
      throw new Error('请填写数值，使用小数点，不使用千位分隔符。');
    filters.push({...observation,op:operator,value_type:'number',value:raw,unit});
  } else if (mode === 'category') {
    if (!present(value)) throw new Error('请按原文填写等级。');
    filters.push({...observation,op:'eq',value_type:'category',value});
  }
  return {include_history:false,filters};
}

/** Accept only bounded, server-matched values. No record expansion or joins. */
export function projectRowQuery(response) {
  const invalid = () => { throw new Error('查询服务返回内容不完整，请重新查询。'); };
  if (!Array.isArray(response?.results) || !Array.isArray(response?.exclusions)) invalid();
  const hits = response.results.map(hit => {
    if (!hit || typeof hit !== 'object') invalid();
    const base = {taskId:hit.task_id,sample:queryRecordLabel(hit).label};
    if (!Object.hasOwn(hit,'matched_values')) return {...base,legacy:true,values:[],total:0,truncated:false};
    if (!Array.isArray(hit.matched_values) || !Array.isArray(hit.assertion_refs)) invalid();
    const refs = new Set(hit.assertion_refs);
    if (hit.matched_values.some(row => !row || row.field_id !== 'observation.value' || !refs.has(row.id))) invalid();
    const total = Number.isInteger(hit.matched_value_total) && hit.matched_value_total >= hit.matched_values.length
      ? hit.matched_value_total : hit.matched_values.length;
    return {...base,legacy:false,values:hit.matched_values.slice(0,20),total,
      truncated:hit.matched_values_truncated === true || total > Math.min(hit.matched_values.length,20)};
  });
  if (response.exclusions.some(item => !item || typeof item !== 'object')) invalid();
  return {hits,exclusions:response.exclusions};
}
