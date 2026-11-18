import {KIND_LABELS,objectLabel} from './result-logic.js';

export const QUERY_TYPES = {number:'数值',text:'文字',category:'类别',date:'日期',time:'时间',datetime:'日期与时间',duration:'持续时间'};
export const QUERY_OPERATORS = {gte:'大于或等于',gt:'大于',lte:'小于或等于',lt:'小于',eq:'等于',contains:'包含文字',exists:'已报告值'};
export function operatorsFor(type) {
  return ['text','category'].includes(type) ? ['eq','contains','exists'] : ['gte','gt','lte','lt','eq','exists'];
}

/** Preserve decimal spelling; JavaScript Number must not round source thresholds. */
export function buildQuery(rows) {
  if (!Array.isArray(rows) || !rows.length || rows.length > 20) throw new Error('请设置 1–20 条筛选条件。');
  return {include_history:false,filters:rows.map((row,index) => {
    if (typeof row.field_id !== 'string' || !row.field_id) throw new Error(`第 ${index+1} 条请选择字段。`);
    if (!Object.hasOwn(QUERY_TYPES,row.type) || !operatorsFor(row.type).includes(row.op)) throw new Error(`第 ${index+1} 条的类型与操作不匹配。`);
    const query = {field_id:row.field_id,op:row.op,value_type:row.type};
    if (row.op !== 'exists') {
      const raw = String(row.value ?? '');
      if (!raw.trim()) throw new Error(`第 ${index+1} 条请输入比较值。`);
      if (row.type === 'number' && !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(raw.trim()))
        throw new Error(`第 ${index+1} 条请使用小数点数值，不使用千位分隔符。`);
      query.value = row.type === 'number' ? raw.trim() : raw;
      if (row.type === 'number' && row.allowCenter === true) query.allow_uncertainty_center = true;
    }
    if (row.allowFormula === true) query.allow_formula_cached = true;
    for (const key of ['unit','basis']) if (typeof row[key] === 'string' && row[key].trim()) query[key] = row[key];
    const qualifiers = {};
    if (row.metric?.trim()) qualifiers.metric_label = row.metric;
    if (row.parameter?.trim()) qualifiers.parameter_label = row.parameter;
    if (Object.keys(qualifiers).length) query.qualifiers = qualifiers;
    return query;
  })};
}

const REASON_LABELS = {
  invalid_query_value_type:'该查询类型暂不支持',value_type_mismatch:'原值类型与所选类型不同',
  legacy_type_evidence_missing:'历史记录缺少可靠的类型解释依据；未重新猜测原文',
  no_values_of_requested_type:'曲线中没有可比较的所选类型值',
  ambiguous_number_format:'数值格式有歧义，需先确认小数点和分组符',
  formula_cache_not_enabled:'未启用公式缓存比较',formula_cache_missing:'原件没有可用的公式缓存',
  uncertainty_center_not_enabled:'未启用误差中心值比较',invalid_typed_value:'原值尚未得到可比较的解释',
  unsupported_typed_value_version:'该值的类型版本暂不支持比较',
  unit_mismatch:'单位不一致；没有进行单位换算',basis_mismatch:'用量基准不一致',
  series_requires_point_selection:'整条曲线不能作为单个数值比较',no_present_series_values:'曲线没有可用的已报告值',
  timezone_not_comparable:'时间的时区信息不一致，不能直接比较',invalid_temporal_query:'输入的日期或时间格式不可比较',
  invalid_temporal_value:'原件的日期或时间不可比较',invalid_query_number:'输入的数值不可比较',
  contains_requires_text:'包含文字操作只用于文字',inactive_assertion:'该项已被阻止或替代，暂不用于筛选',
  not_definitely_satisfied:'无法确定所有可能值都满足条件',not_equal:'原值不等于条件值',not_contained:'原文不包含条件文字'};
const MISSING_REASONS = {blank:'原件为空',not_tested:'原件标明未测试',not_detected:'原件标明未检出',
  not_applicable:'原件标明不适用',not_reported:'原件未报告',error:'原表含错误值',ambiguous_placeholder:'占位符含义未确定'};
const SOURCE_REASONS = {ambiguous_units:'来源单位未确定',unreadable:'来源无法读取',extraction_failed:'该项整理失败'};
export function reasonText(reason) {
  if (REASON_LABELS[reason]) return REASON_LABELS[reason];
  if (reason?.startsWith('missing:')) return MISSING_REASONS[reason.slice(8)] || '原件报告为缺失状态';
  if (reason?.startsWith('source_status:')) return SOURCE_REASONS[reason.slice(14)] || '来源状态尚不可比较';
  if (reason?.startsWith('non_numeric_kind:')) return `${QUERY_TYPES[reason.slice(17)] || '该来源类型'}不能按数值直接比较`;
  return '该项暂时不能比较，请打开相关资料核对。';
}
export function exclusionGroups(exclusions = []) {
  const groups = new Map();
  for (const item of exclusions) {
    const key = item.reason || 'unknown';
    if (!groups.has(key)) groups.set(key,{label:reasonText(key),count:0,taskIds:[],comparable:['not_definitely_satisfied','not_equal','not_contained'].includes(key)});
    const group = groups.get(key);group.count++;
    if (item.task_id && !group.taskIds.includes(item.task_id)) group.taskIds.push(item.task_id);
  }
  return [...groups.values()];
}
export function queryRecordLabel(row) {
  const ids = [row.source_id,row.task_id,row.record_ref,row.sample_ref,row.test_ref,row.run_ref,row.series_ref];
  const label = [row.record_label,row.sample_label].find(value => typeof value === 'string' && value && !ids.includes(value));
  return {label:label ? objectLabel({label,kind:row.record_kind,object_id:row.record_ref}) : '来源未提供显示名称',kind:KIND_LABELS[row.record_kind] || '资料记录'};
}
