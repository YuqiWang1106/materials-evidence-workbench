/* Source values and scientific relationships are never inferred in this view. */
export const KIND_LABELS = {source:'来源', sample:'样品', material:'材料', component:'组分',
  formulation:'配方', reported_test:'测试', instrument_run:'仪器记录', observation:'观测结果',
  series:'曲线', content_unit:'资料条目', reported_process:'制备记录', process_definition:'制备方法',
  method_definition:'测试方法', claim:'来源陈述', patent_claim:'权利要求', equipment:'设备', figure:'图'};
export const FIELD_LABELS = {'material.identity':'材料名称', 'material.description':'材料说明',
  'composition.amount':'组分用量', 'composition.ratio':'组成比例', 'observation.value':'结果原值',
  'condition.parameter':'测试条件', 'process.parameter':'制备条件', 'process.steps':'制备步骤',
  'process.route':'工艺路线', 'method.name':'测试方法', 'method.standard':'方法标准',
  'method.instrument':'仪器', 'claim.text':'来源陈述', 'source.title':'资料标题',
  'source.identifier':'来源标识', 'source.participant':'参与方', 'source.date':'日期',
  'series.point_count':'点数', 'series.last_time':'最后记录时间', 'series.unit':'曲线单位',
  'series.range':'曲线范围', 'series.data_asset':'曲线原件', 'instrument.parameter':'仪器参数',
  'activity.identifier':'运行标识', 'activity.scan_count':'扫描次数', 'activity.participant':'参与方',
  'activity.end_marker_time':'结束时间', 'activity.end_marker_scan_index':'结束扫描序号'};
const ISSUE_LABELS = {series_unit_unresolved:'曲线单位未确定',
  ambiguous_series_values_or_units:'曲线包含待确定的数值或单位', invalid_table_mapping:'部分内容未按读法完成整理',
  table_mapping_required:'部分表格尚未指定读法', missing_row_identity:'部分记录缺少明确的样品或测试标识',
  formula_cache_unavailable:'公式未计算，已保留原式', unsafe_table_structure:'文件的编码或分隔方式尚未确定',
  empty_series:'所选曲线没有可读取的数据', unparsed_metadata_number:'原文数值的含义尚未确定',
  run_label_conflict:'同一运行的来源标识不一致', instrument_run_grouping_unresolved:'仪器文件的运行归属未确定',
  instrument_run_identity_candidate:'仪器记录可能相关，实际运行关系仍需核对',
  ambiguous_observation_owner:'结果对应的测试不唯一', ambiguous_record_domain:'记录的来源范围不唯一',
  ambiguous_test_sample_owner:'同一测试对应多个样品，归属需核对', unproven_table_relation:'记录之间的关系缺少明确依据',
  conflicting_result_slot:'同一结果位置出现不同数值', conflicting_test_condition:'同一测试的条件不一致',
  conflicting_sample_field:'同一样品的资料不一致', conflicting_object_field:'同一记录的资料不一致',
  ambiguous_row_unit:'来源中的单位不一致', ambiguous_source_basis:'用量基准尚未确定',
  ambiguous_header_context:'表头含义或作用范围需要核对',
  absent_original_field:'所选位置在原件中不存在', xls_formula_expression_unavailable:'旧版表格只保留公式缓存，原式不可用'};
const HANDLING_LABELS = {source_blank:'原件为空', source_not_tested:'原件标明未测试', not_applicable:'不适用',
  source_not_reported:'原件未报告', source_not_detected:'原件标明未检出', source_not_applicable:'不适用', formula_requires_review:'公式保留，未当作实测结果',
  excel_error:'原表含错误值', ambiguous_placeholder:'占位文字含义未确定', unreadable:'原文无法读取',
  extraction_failed:'内容整理失败', numeric_precision_requires_review:'数值精度需要核对',
  unparsed_numeric:'数值格式需要核对', ambiguous_units:'单位需要核对'};
const KNOWN_ABSENCE = new Set(['source_blank','source_not_tested','source_not_detected','not_applicable','source_not_applicable','source_not_reported']);

export function valueText(value, max = 180) {
  let text;
  if (value === null || value === undefined) text = '未报告';
  else if (value === '') text = '空字符串';
  else if (typeof value === 'object') text = JSON.stringify(value);
  else text = String(value);
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

export function rowLabel(row, catalogue = {}) {
  const label = row.field;
  if (typeof label === 'string' && label && !/^(source_(?:column|key|calibration):|EXT-|[A-Z]-\d)/.test(label)
      && label !== row.field_id) return label;
  return catalogue[row.field_id] || FIELD_LABELS[row.field_id] || '来源字段';
}

const HEADER_ROLE_LABELS = {material:'材料标签',stage:'测试阶段',metric:'指标名称',note:'原表说明'};

function headerSource(source) {
  if (!source || typeof source.asset_ref !== 'string' || !source.asset_ref
      || (source.sheet != null && typeof source.sheet !== 'string')
      || ![source.row, source.column].every(n => Number.isInteger(n) && n > 0 && n <= 1000000)) return null;
  let value = source.column, column = '';
  while (value > 0) {value--; column = String.fromCharCode(65 + value % 26) + column; value = Math.floor(value / 26);}
  return {asset_ref:source.asset_ref, sheet:source.sheet ?? null, row:source.row, column:source.column,
    address:`${column}${source.row}`};
}

/** Display source labels as labels; never derive physical ownership or units. */
export function headerGroups(row = {}) {
  if (!Array.isArray(row.header_context)) return [];
  return row.header_context.slice(0, 8).map(context => {
    const roles = Array.isArray(context.roles) ? context.roles : [];
    const labels = roles.slice(0, 32).filter(role => HEADER_ROLE_LABELS[role.role]).map(role => ({
      role:role.role, name:HEADER_ROLE_LABELS[role.role], text:role.raw_value,
      unresolved:role.status !== 'value', statusLabel:HANDLING_LABELS[role.status] || '含义未确定',
      source:headerSource(role.source)}));
    const paths = [...(context.header_path || []), ...(context.scope_cells || [])];
    const seen = new Set(), headers = [];
    for (const entry of paths.slice(0, 52)) {
      const source = headerSource(entry.source), key = JSON.stringify(source);
      if (seen.has(key)) continue;
      seen.add(key); headers.push({text:entry.raw_value, source, unresolved:entry.status !== 'value',
        statusLabel:HANDLING_LABELS[entry.status] || '含义未确定'});
    }
    const unit = context.unit || {};
    return {axis:context.axis, axisLabel:context.axis === 'x' ? '横轴' : context.axis === 'y' ? '纵轴' : '结果', labels, headers,
      blocked:context.status === 'blocked' || Boolean(context.issues?.length),
      unitText:unit.value == null || unit.ambiguous ? '单位未确定' : valueText(unit.value, 80),
      unitCandidates:(unit.ambiguous ? unit.candidates || [] : []).slice(0, 128).map(v => valueText(v, 80)),
      unitSources:(unit.sources || []).slice(0, 32).map(headerSource).filter(Boolean),
      truncated:Boolean(context.header_path_truncated || context.scope_cells_truncated || context.roles_truncated
        || context.issues_truncated || unit.sources_truncated)};
  });
}

/** Reuse immutable decoding metadata from the matching original table only. */
export function headerLocation(source, locations = []) {
  const target = headerSource(source);
  if (!target) return null;
  const location = locations.find(item => item.registered_asset_id === target.asset_ref
    && (item.anchor?.sheet ?? null) === target.sheet && ['csv','xlsx','xls'].includes(item.anchor?.format));
  if (!location) return null;
  return {...location, content_kind:'table_cells', anchor:{...location.anchor,
    row_range:[target.row,target.row],columns:[target.column]}};
}

export function titleForResult(task = {}, view = {}) {
  for (const title of [task.title, view.title]) {
    if (title && title !== task.source_id && title !== view.source_id && title !== task.task_id) return title;
  }
  return view.assets?.map(a => a.original_name).filter(Boolean).join('、') || '整理结果';
}

export function publishPayload(view) {
  if (!/^[0-9a-f]{64}$/.test(view?.package_sha256 || '')) throw new Error('请重新载入当前结果后再保存');
  return {expected_package_sha256: view.package_sha256};
}

export function saveDecision(task = {}, view, capabilities = {}) {
  if (task.read_only) return {action: null, label: '历史结果只读', reason: '查看和下载原件可用；重新整理需要确认新的读法。'};
  if (task.state === 'saved') return {action: null, label: '已归档', reason: '当前结果已完成归档。'};
  if (task.state === 'superseded') return {action: null, label: '已有更新版本', reason: '请从我的资料打开最新结果。'};
  if (!capabilities?.archive_configured) return {action: null, label: '归档不可用', reason: '当前结果保留在本地；归档恢复可用后可继续保存。'};
  if (['publishing', 'write_unknown'].includes(task.state)) return {action: 'reconcile', label: '核对保存状态', reason: '先核对上次写入，不重复发送结果。'};
  if (task.state !== 'needs_review') return {action: null, label: '暂不可保存', reason: '等待整理完成后核对结果。'};
  if (!/^[0-9a-f]{64}$/.test(view?.package_sha256 || '')) return {action: null, label: '请先载入结果', reason: '需要核对当前版本后保存。'};
  return {action: 'publish', label: view.summary?.outcome === 'partial' ? '保存当前部分结果到知识库' : '保存到知识库',
    reason: view.summary?.outcome === 'partial' ? '未确定事项会随结果保留；保存不会将其视为已确认。' : '保存当前核对的结果。'};
}

/** Refresh the state while keeping the exact package the user clicked frozen. */
export async function saveReviewedResult({task, view, capabilities, api}) {
  const decision = saveDecision(task, view, capabilities);
  if (!decision.action) return {kind:'blocked', task, reason:decision.reason};
  const payload = decision.action === 'publish' ? publishPayload(view) : null;
  const offset = view?.offset || 0;
  const path = `/api/tasks/${encodeURIComponent(task.task_id)}`;
  const latest = await api(path);
  if (saveDecision(latest, view, capabilities).action !== decision.action) return {kind:'state_changed', task:latest};
  if (decision.action === 'publish') {
    const current = await api(`${path}/result-view?offset=${offset}&limit=50`);
    if (current.package_sha256 !== payload.expected_package_sha256) return {kind:'version_changed', task:latest, view:current};
    return {kind:'submitted', task:await api(`${path}/publish`, {method:'POST', json:payload, timeout:600000})};
  }
  return {kind:'submitted', task:await api(`${path}/reconcile`, {method:'POST', timeout:600000})};
}

export function curveSegments(preview = {}) {
  const segments = []; let current = [];
  const close = () => {if (current.length) segments.push(current); current = [];};
  for (const point of preview.points || []) {
    if (point?.[3] === 'gap_before') close();
    if (!Array.isArray(point) || !Number.isFinite(point[0]) || !Number.isFinite(point[1])) {close(); continue;}
    current.push(point.slice(0, 3));
  }
  close(); return segments;
}

export function previewRequest(location, readerOptions = {}, focusColumn = null) {
  const anchor = location?.anchor;
  if (!anchor || !['csv','xlsx','xls'].includes(anchor.format)) return null;
  const range = anchor.row_range, columns = anchor.columns;
  if (!Array.isArray(range) || range.length !== 2 || !range.every(n => Number.isInteger(n) && n > 0)
      || range[1] < range[0] || !Array.isArray(columns) || !columns.length
      || !columns.every(n => Number.isInteger(n) && n > 0)) return null;
  const focus = columns.includes(focusColumn) ? focusColumn : Math.min(...columns);
  const rowStart = Math.max(1, range[0] - 2), columnStart = Math.max(1, focus - 2);
  return {sheet: anchor.sheet ?? null, row_start: rowStart, row_end: Math.min(range[1], rowStart + 19),
    column_start: columnStart, column_end: Math.min(Math.max(...columns), columnStart + 11), reader_options: readerOptions};
}

export function readerOptionsForLocation(location) {
  if (!['confirmed','format_default'].includes(location?.reader_settings_status) || !location.reader_options
      || typeof location.reader_options !== 'object' || Array.isArray(location.reader_options)) return null;
  return location.reader_options;
}

export function pagination(view) {
  const offset = view.offset || 0, total = view.total || 0, count = view.rows?.length || 0, limit = view.limit || 50;
  return {start: count ? offset + 1 : 0, end: offset + count, total,
    previous: offset > 0 ? Math.max(0, offset - limit) : null, next: view.has_more ? offset + limit : null};
}

export function groupNotices(view = {}) {
  const groups = new Map();
  const add = (key, label, tone, item) => {
    if (!groups.has(key)) groups.set(key, {key, label, tone, items: []});
    groups.get(key).items.push(item);
  };
  for (const issue of view.issues || []) add(`issue:${issue.code || 'unknown'}`,
    ISSUE_LABELS[issue.code] || '此处需要进一步核对', issue.severity === 'error' ? 'error' : 'warn', issue);
  for (const assessment of view.assessments || []) add(`assessment:${assessment.handling || 'unknown'}`,
    HANDLING_LABELS[assessment.handling] || '原件状态尚未确定', KNOWN_ABSENCE.has(assessment.handling) ? 'info' : 'warn', assessment);
  return {groups: [...groups.values()],
    unshownIssues: Math.max(0, (view.issue_total || 0) - (view.issues?.length || 0)),
    unshownAssessments: Math.max(0, (view.assessment_total || 0) - (view.assessments?.length || 0))};
}

export function coverageText(detail) {
  const scope = typeof detail.scope === 'string' ? detail.scope : detail.scope?.scope;
  if (scope === 'sample_identity_and_metric_semantics') return '数值原格已保留；实际样品对应关系与指标含义尚未确认。';
  if (scope === 'physical_run_identity') return '数值原格已保留；不同记录是否属于同一次实际运行尚未确认。';
  if (scope === 'unmapped_cells') {
    const cells = (detail.cell_addresses || []).filter(address => /^[A-Z]+[1-9]\d*$/.test(address)).slice(0, 20);
    const sheet = typeof detail.scope?.sheet === 'string' ? `工作表“${detail.scope.sheet}”：` : '';
    const count = Number.isInteger(detail.cell_count) ? `${detail.cell_count} 个` : '';
    return `${sheet}仍有 ${count}原格未整理，原件已保留。${cells.length ? `位置示例：${cells.join('、')}。` : ''}`;
  }
  if (detail.scope?.mapping_id) return '一组读法尚未完成，原件已保留；可返回修改读法。';
  if (detail.scope?.row) return `第 ${detail.scope.row} 行的归属尚未确定，原件已保留。`;
  if (detail.scope?.visual_page) return `原件第 ${detail.scope.visual_page} 页的图片内容尚未整理。`;
  return '这部分资料的含义或对应关系仍需确认，原件已保留。';
}

export function errorText(error, fallback = '暂时无法读取，请重试。') {
  return ({401:'本地会话已失效，请重新打开页面。',403:'当前页面没有访问权限，请从本地入口打开。',
    404:'该结果或原件已不可用，请返回我的资料刷新。',409:'内容或状态已变化，请重新载入并核对。',
    422:'当前来源位置或读法无法使用，请核对读法或下载原件。',
    503:'归档不可用；已保留本地结果，请稍后核对归档状态。'})[error?.status] || fallback;
}

const VALUE_KIND_LABELS = {decimal:'数值',interval:'范围',bound:'上限 / 下限',uncertainty:'带误差数值',
  category:'类别',text:'文字',date:'日期',time:'时间',datetime:'日期与时间',duration:'持续时间',
  missing:'缺失状态',formula_cached:'公式与缓存',unparsed:'含义待确定'};
const MISSING_LABELS = {blank:'原件为空',not_tested:'未测试',not_detected:'未检出',not_applicable:'不适用',
  not_reported:'未报告',error:'原表错误值',ambiguous_placeholder:'占位符含义未确定'};

/** A display explanation, never a new interpretation or numeric conversion. */
export function typedValueInfo(typed) {
  if (!typed || typed.version !== '2') return null;
  let label = VALUE_KIND_LABELS[typed.kind] || '含义待确定', note = '';
  if (typed.kind === 'missing') {
    label = MISSING_LABELS[typed.status] || '缺失状态待确定'; note = '保留原件的报告状态，不计作数值 0。';
  } else if (typed.kind === 'formula_cached') {
    note = typed.cached == null ? '原件没有已保存的公式缓存；保留原式，不执行公式。'
      : `原件保存的缓存：${valueText(typed.cached.raw, 100)}。未执行或复核公式，不视为实测值；查询需单独启用。`;
  } else if (typed.status === 'ambiguous') note = '小数点或分组符含义有歧义，需确认数值格式后才能比较。';
  else if (typed.status !== 'valid') note = '原值已保留，当前解释尚不可用于定量比较。';
  else if (typed.kind === 'uncertainty') note = '查询默认不取中心值。误差含义未指定，不能推断为标准差或置信区间。';
  else if (['interval','bound'].includes(typed.kind)) note = '只有所有可能值都满足条件时才命中；保留端点及开闭边界。';
  else if (['date','time','datetime','duration'].includes(typed.kind)) note = '按确认的时间类型比较；未报告的时区不补齐。';
  return {label,note,raw:typed.raw};
}

/** Flatten only explicit proof wrappers; declared semantics have no original cell. */
export function bindingChoices(bindings) {
  const entries = Array.isArray(bindings) ? bindings : bindings ? [bindings] : [];
  return entries.slice(0, 64).map(original => {
    let binding = original, depth = 0;
    while (binding?.binding && depth++ < 4) binding = binding.binding;
    if (binding?.kind === 'declared_semantic_mapping' || binding?.not_source_fact) return {
      source:null,label:'读法中的含义说明',note:'这是人为指定的含义，不是原格报告的事实。'};
    const metadata = binding?.metadata === 'sheet_name' && typeof binding.asset_ref === 'string'
      && typeof binding.sheet === 'string';
    const source = metadata ? {asset_ref:binding.asset_ref,sheet:binding.sheet,metadata:'sheet_name'} : headerSource(binding);
    const label = metadata ? `工作表名称 · ${valueText(binding.sheet, 100)}`
      : source ? `${source.sheet ? `${source.sheet} · ` : ''}${source.address} · ${valueText(binding.raw, 100)}` : '未提供可定位的原格';
    const notes = [];
    if (metadata) notes.push(`使用工作表名称中的文字：${valueText(binding.raw, 160)}。这不是单元格测量值。`);
    if (binding?.merged_target) notes.push('读法明确使用合并区域的起始原格。');
    if (binding?.selector?.span || binding?.span) notes.push('使用原文中明确指定的片段；下方展示完整来源。');
    if (original?.kind === 'source_vocabulary_code') notes.push('来源文字附有读法声明的分类标记。');
    if (!source) notes.push('未提供可定位的原格；请在完整记录或原件中核对。');
    return {source,label,note:notes.join(' ')};
  });
}

export function bindingLocation(source, locations = []) {
  if (source?.metadata !== 'sheet_name') return headerLocation(source, locations);
  const location = locations.find(item => item.registered_asset_id === source.asset_ref
    && item.anchor?.sheet === source.sheet && ['xlsx','xls'].includes(item.anchor?.format));
  if (!location) return null;
  return {...location,content_kind:'table_sheet_metadata',anchor:{format:location.anchor.format,
    sheet:source.sheet,metadata_key:'sheet_name'}};
}

export const RELATION_LABELS = {has_formulation:'使用配方',recipe_for:'对应记录',contains_component:'含有组分',
  refers_to_material:'使用材料',tested_by:'接受测试',test_of:'测试对象',produces_observation:'产生结果',
  observes_subject:'观测对象',has_series:'记录曲线',uses_method:'采用方法',processed_by:'经历制备',
  process_of:'制备对象',produced_by:'由此制备',uses_process:'采用制备方法',has_step:'包含步骤',
  derived_from:'来源于',has_component:'含有组分',uses_equipment:'使用设备'};
export function objectLabel(object = {}) {
  const label = object.label;
  if (label == null || label === object.object_id || /^local \S+ \d+:\d+:\d+$/.test(String(label)))
    return `${KIND_LABELS[object.kind] || '资料记录'}（当前记录）`;
  return valueText(label,160);
}
export function relationText(relation) {
  const left = relation.from_label == null ? '未提供名称的记录' : objectLabel({label:relation.from_label,kind:relation.from_kind,object_id:relation.from_ref});
  const right = relation.to_label == null ? '未提供名称的记录' : objectLabel({label:relation.to_label,kind:relation.to_kind,object_id:relation.to_ref});
  return `${left} → ${RELATION_LABELS[relation.predicate] || '来源声明的关系'} → ${right}`;
}

const STRUCTURAL_REASONS = {missing_endpoint:'缺少已关联的记录',blocked_object:'关联记录已被阻止或撤回',
  revision_mismatch:'关联记录来自不同来源或版本',invalid_structural_endpoint_kind:'关联记录类型与归属要求不符',
  missing_structural_reference:'记录缺少必需的归属引用',invalid_structural_reference:'归属引用无法读取',
  structural_cycle:'归属依赖存在循环',structural_check_limit:'归属依赖检查达到上限，尚不能确认可用'};
export function structuralReasonText(reason) {
  return STRUCTURAL_REASONS[reason] || '归属尚不可用，请核对已保存记录。';
}
const structuralEndpointLabel = (link, side) => link?.[`${side}_label`] == null ? '未提供名称的记录'
  : objectLabel({object_id:link[`${side}_ref`],kind:link[`${side}_kind`],label:link[`${side}_label`]});
export function structuralLinkText(link) {
  const role = {owner_ref:'所属记录',material_ref:'所用材料'}[link.reference_field] || '结构引用';
  return `${structuralEndpointLabel(link,'from')} → ${role} → ${structuralEndpointLabel(link,'to')}`;
}

/** Direct stored references only; a matching label or object field cannot create a link. */
export function structuralContext(graph, subjectRef) {
  const links=(graph?.structural_links || []).filter(link=>link.from_ref===subjectRef
    && ['owner_ref','material_ref'].includes(link.reference_field));
  const ownerLink=links.find(link=>link.reference_field==='owner_ref'),materialLink=links.find(link=>link.reference_field==='material_ref');
  const endpoint=link=>link ? (graph?.objects || []).find(object=>object.object_id===link.to_ref) || null : null;
  const owner=endpoint(ownerLink),material=endpoint(materialLink);
  const usable=links.length>0 && links.every(link=>link.usable===true) && [owner,material].every(object=>object?.usable!==false);
  const reasons=[...new Set([...links,...[owner,material].filter(Boolean)].flatMap(item=>item.unusable_reasons || []).map(structuralReasonText))];
  if(links.length && !usable && !reasons.length)reasons.push('归属尚不可用，请核对已保存记录。');
  const label=ownerLink&&materialLink ? `${structuralEndpointLabel(ownerLink,'to')} → ${structuralEndpointLabel(materialLink,'to')}`
    : ownerLink ? `归属：${structuralEndpointLabel(ownerLink,'to')}` : materialLink ? `原料：${structuralEndpointLabel(materialLink,'to')}` : '';
  return {links,owner,material,usable,label,reasons};
}
