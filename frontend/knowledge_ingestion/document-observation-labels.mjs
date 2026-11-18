/** Display names and small formatters for the document timeline. No DOM access. */
export const STAGES = {task_preflight:'任务准备', reading:'本地读取', visual_reading:'视觉区域识别',
  table_mapping:'表格结构映射', entity_extraction:'正文实体抽取', validating:'结构核对',
  synonyms:'同义名称整理', identity:'配方身份整理', finalizing:'草稿与质量报告',
  candidate_validation:'候选校验', candidate_save:'保存候选', review_validation:'审核证据校验',
  publishing:'确认写入', resolve:'实体身份解析', claims:'跨来源陈述整理'};
export const STATES = {queued:'等待处理',reading:'读取中',extracting:'提取中',validating:'核对中',
  needs_review:'等待人工核对',publishing:'正在确认保存',saved:'已保存',failed:'处理失败',
  cancelled:'已取消',superseded:'历史版本',write_unknown:'保存结果待核实'};
export const OUTCOMES = {started:'已开始，状态待核实',completed:'已完成',mapped:'映射成功',partial:'部分成功',
  failed:'失败',empty:'空结果',not_processed:'未处理',invalid:'校验未通过',unknown:'结果未知',
  interrupted:'已中断',cache_hit:'使用缓存',transport_unknown:'结果未知',transport_write_failed:'结果未知',
  invalid_json:'JSON 格式错误',invalid_json_object:'JSON 不是对象',incomplete:'响应未完成',
  returned_model_mismatch:'返回模型不匹配',refusal_or_empty:'拒绝或空响应',transport_connect_failed:'连接失败',
  cancelled_after_dispatch:'发送后取消，结果待核实',model_not_dispatched:'未派发',
  project_budget_not_dispatched:'未派发',transport_not_sent:'请求未发送',time_budget_exhausted:'时间额度耗尽'};
export const ERRORS = {mapping_invalid:'表格映射未通过校验',
  quote_not_found:'未找到精确原文',quote_not_unique:'原文引用不唯一',invalid_source_grid:'表格结构不符合要求',
  table_no_attributes:'未形成可接受属性',table_partial:'表格仅部分覆盖',request_allowance:'请求额度不足',
  reading_incomplete:'本地读取不完整',visual_read_incomplete:'视觉识别不完整',window_partial:'正文窗口未完整处理',
  model_credentials_missing:'未配置模型凭据',embedding_unavailable:'向量候选不可用',embedding_timeout:'向量请求超时',
  embedding_transport_error:'向量请求网络错误',embedding_invalid_response:'向量响应格式错误',embedding_cache_unavailable:'向量缓存不可用',
  response_invalid:'模型响应未通过校验',model_error:'模型调用失败',model_outcome_unknown:'模型结果未知',
  transport_not_sent:'请求未发送',transport_unknown:'传输结果未知',transport_write_failed:'请求发送结果未知',
  time_budget_exhausted:'时间额度耗尽',pipeline_configuration_changed:'任务配置与当前代码不一致',
  original_integrity_failure:'原件完整性核对失败',review_evidence_invalid:'审核修改的证据未通过校验',
  task_processing_error:'任务处理失败',publication_error:'知识库写入未完成',local_io_error:'本地读写失败',
  ocr_reader_not_configured:'本地 OCR 读取器未配置',ocr_second_not_configured:'二次识别器未配置',
  ocr_queue_timeout:'等待本地识别资源超时',ocr_reader_timeout:'首次识别超时',ocr_reader_start_failed:'无法启动首次识别',
  ocr_reader_output_invalid:'首次识别结果格式异常',ocr_reader_incomplete:'首次识别结果不完整',ocr_reader_failed:'首次识别失败',
  ocr_second_timeout:'二次识别超时',ocr_second_incomplete:'二次识别结果不完整',ocr_second_failed:'二次识别失败',
  ocr_second_capped:'超出二次识别数量上限',ocr_render_failed:'扫描页准备失败',ocr_comparison_failed:'识别结果整理失败',
  ocr_cache_write_failed:'缓存保存失败，本次识别结果仍可使用',mineru_queue_timeout:'等待 MinerU 资源超时',
  visual_render_failed:'页面图像准备失败',visual_coverage_invalid:'模型识别结果未通过校验',
  visual_empty_page:'模型未返回页面文字',visual_empty_transcript:'模型返回空文字',visual_batch_not_sent:'批次未发送，区域未处理',
  model_request_binding_invalid:'请求绑定不一致，需核对',
  observation_evidence_conflict:'记录冲突，需核对',cancelled:'已取消',unknown_error:'原因未知'};
export const READERS={pymupdf:'PyMuPDF 原生读取',mineru:'原件解析（PyMuPDF 与 MinerU）',mineru_queue:'MinerU 资源等待',ocr_render:'扫描页准备',
  ocr_prepare:'识别结果预处理',ocr_first_queue:'首次识别资源等待',ocr_first:'本地 OCR 识别',
  ocr_second_queue:'二次识别资源等待',ocr_second:'OCR 二次识别',ocr_compare:'识别结果整理',
  ocr_cache:'OCR 缓存',ocr_limit:'二次识别数量限制'};
export const DIAGNOSTICS={unsafe_source_token_boundary:'单位原文边界无法确认',unmapped_nonempty_cells:'未覆盖单元格',
  duplicate_identity:'对象身份重复',unresolved_object_dependency:'对象依赖未解决',unresolved_result_context:'结果上下文未解决',
  ambiguous_value:'数值不明确',unmapped_value:'数值未映射',unsupported_unit:'不支持的单位',duplicate_fact:'重复事实',
  cell_value_cut:'只取了单元格的一部分，该项未采用'};
export const REQUEST_STATE={pending:'待派发',sent:'已发送',unknown:'结果未知',failed:'失败',completed:'已完成',not_dispatched:'未发送'};
export const ACTIVE = new Set(['queued','reading','extracting','validating','publishing']);
export const safeText = value => typeof value === 'string' ? value : '';
export const duration = value => Number.isFinite(value) && value >= 0 ? `${Math.round(value)} 秒` : '未知';
export function elapsedClock(value) {
  if (!Number.isFinite(value) || value < 0) return {text:'—', label:'总耗时未知'};
  const total=Math.round(value), hours=Math.floor(total/3600), minutes=Math.floor(total/60)%60, seconds=total%60;
  const text=hours ? `${hours}:${String(minutes).padStart(2,'0')}:${String(seconds).padStart(2,'0')}`
    : `${String(minutes).padStart(2,'0')}:${String(seconds).padStart(2,'0')}`;
  const label=`总耗时 ${hours ? `${hours} 小时 ` : ''}${hours || minutes ? `${minutes} 分 ` : ''}${seconds} 秒`;
  return {text,label};
}
export const stageName = stage => STAGES[stage] || '阶段未知';
export const statusName = status => OUTCOMES[status] || (typeof status === 'string' && /^http_[1-5]\d\d$/.test(status) ? status.toUpperCase().replace('_',' ') : '状态未知');
export const displayStatus = item => item.kind==='visual_step' && item.visual_step==='result' && item.status==='partial'
  ? '有不确定项' : item.kind==='visual_step' && item.visual_step==='result' && item.status==='empty'
  ? '空文字' : item.kind==='reader_step' && item.status==='partial'
  ? item.reader==='ocr_cache' ? '缓存未保存' : item.reader==='ocr_limit' ? '部分内容未二次识别' : '解析不完整'
  : statusName(item.status);
export const readerName = reader => READERS[reader] || '读取器记录';
export const errorName = code => ERRORS[code] || (typeof code === 'string' && /^http_[1-5]\d\d$/.test(code) ? `HTTP ${code.slice(5)}` : '原因未知');
export const finite = n => typeof n === 'number' && Number.isFinite(n) && n >= 0;
export const VISUAL_KINDS={image:'图片',chart:'图表',figure:'图片',equation:'公式',formula:'公式',unread_page:'整页',handwriting_table:'手写表格'};
export const VISUAL_REASONS={usable_mineru_formula:'可复用 MinerU 公式',unread_page:'页面没有可用区域，需要整页识别',
  figure_requires_visual_reading:'图片或图表需要补充识别',formula_source_unresolved:'公式来源未能确认',
  formula_has_reading_risk:'公式存在读取风险',formula_text_not_reusable:'公式文字不适合直接复用',
  formula_body_incomplete_or_unsupported:'公式内容不完整或无法直接复用',handwriting_flagged:'手写单元格需要核对'};

export function visualTargets(item){
  return item.stage==='visual_reading' && Array.isArray(item.visual_targets)
    ? item.visual_targets.filter(t=>t && Number.isInteger(t.page) && t.page>0 && VISUAL_KINDS[t.kind]).slice(0,100) : [];
}
export function visualSummary(item){
  const targets=visualTargets(item);if(!targets.length)return '';
  const locations=[...new Map(targets.map(t=>{
    const name=`${Number.isInteger(t.document_index) && t.document_index>0?`原件 ${t.document_index} · `:''}第 ${t.page} 页`;
    return [name,name];
  })).values()];
  return `${locations.slice(0,2).join('、')}${locations.length>2 || item.visual_targets_truncated?'等':''}`;
}
export function readerDocumentIndex(item){
  if(item.kind!=='reader_step' || typeof item.reader!=='string' || typeof item.item_id!=='string')return null;
  const prefix=item.reader+'-';if(!item.item_id.startsWith(prefix))return null;
  const suffix=item.item_id.slice(prefix.length);if(!/^[1-9]\d{0,4}$/.test(suffix))return null;
  const index=Number(suffix);return Number.isSafeInteger(index)?index:null;
}
export function phaseName(phase) {
  if (typeof phase !== 'string') return '未知';
  if (phase.startsWith('table-')) return '表格映射';
  if (phase.startsWith('visual-')) return '视觉识别';
  if (phase.startsWith('entities-') || phase.startsWith('attributes-')) return '正文抽取';
  return STAGES[phase] || '其他调用';
}
