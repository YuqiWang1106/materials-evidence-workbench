/** Pure projections of timeline events: correlation, grouping, row text and status icons. No DOM access. */
import {DIAGNOSTICS, REQUEST_STATE, ACTIVE, safeText, duration, stageName, statusName, displayStatus, readerName, errorName, finite, VISUAL_KINDS, VISUAL_REASONS, visualTargets, visualSummary} from './document-observation-labels.mjs';
import {visualStepLabel} from './document-observation-visual-logic.mjs';

export function currentStage(data) {
  if (!ACTIVE.has(data.state)) return null;
  const events = Array.isArray(data.events) ? data.events : [];
  const publishing = new Set(['review_validation','publishing','resolve','claims']);
  const active = new Set((Array.isArray(data.stages) ? data.stages : []).filter(row=>row.status==='started').map(row=>row.stage));
  const last = [...events].reverse().find(item => item.kind === 'stage' && item.status==='started' && active.has(item.stage) &&
    (data.state === 'publishing' ? publishing.has(item.stage) : !publishing.has(item.stage)));
  return last?.stage || null;
}

export function countText(counts, labels) {
  return Object.entries(labels).filter(([key]) => Number.isInteger(counts?.[key]))
    .map(([key,label]) => `${label} ${counts[key]}`).join(' · ') || '暂无结果';
}

export function correlationIndex(events) {
  const calls=new Map(), rounds=new Set();
  for(const row of events){
    if(row.kind==='table_round' && row.mapping_key && row.item_id && Number.isInteger(row.round))
      rounds.add(JSON.stringify([row.mapping_key,row.item_id,row.round]));
    if(row.kind==='model_call' && row.logical_id){
      if(!calls.has(row.logical_id))calls.set(row.logical_id,{parent_kind:new Set(),parent_id:new Set(),item_id:new Set()});
      const entry=calls.get(row.logical_id);
      for(const field of ['parent_kind','parent_id','item_id'])if(row[field])entry[field].add(row[field]);
    }
  }
  return {calls,rounds};
}

export function correlations(item, index) {
  const logical=typeof item.logical_id==='string' && item.logical_id ? item.logical_id : null;
  const matches=index.calls.get(logical);
  const unique=(key)=>{const values=matches?.[key];return values?.size===1?[...values][0]:null;};
  const directKind=item.parent_kind, directId=item.parent_id, inheritedKind=unique('parent_kind'), inheritedId=unique('parent_id');
  const conflict=(matches?.parent_id.size||0)>1 || (matches?.parent_kind.size||0)>1 || (matches?.item_id.size||0)>1 ||
    (directKind && inheritedKind && directKind!==inheritedKind) || (directId && inheritedId && directId!==inheritedId) ||
    (item.phase && unique('item_id') && item.phase!==unique('item_id'));
  const parentKind=conflict?null:(directKind||inheritedKind), parentId=conflict?null:(directId||inheritedId);
  const phase=item.phase||item.item_id||unique('item_id')||'';
  const match=typeof phase==='string' ? /^(table-[A-Za-z0-9_.:-]+)-mapping-(\d+)$/.exec(phase) : null;
  let round=null;
  if(match && parentKind==='table' && parentId){
    const n=Number(match[2])+1;
    if(Number.isSafeInteger(n) && index.rounds.has(JSON.stringify([match[1],parentId,n])))round=n;
  }
  return {logical,parentKind,parentId,round,isTable:parentKind==='table'||!!match};
}

export function correlationText(item, index) {
  const c=correlations(item,index);
  const parent=c.parentKind==='table'&&c.parentId?`表格 ${c.parentId}`:c.parentKind==='window'&&c.parentId?`窗口 ${c.parentId}`:'归属未知';
  return [parent,`逻辑调用 ${c.logical||'未知'}`,...(c.isTable ? [c.round ? `映射第 ${c.round} 轮` : '映射轮次未知'] : [])];
}

export function stageProgress(item) {
  if(item.kind!=='stage' || !Number.isInteger(item.completed) || !Number.isInteger(item.total) ||
      item.completed<0 || item.total<=0 || item.completed>item.total)return '';
  const unit={reading:'份原件',table_mapping:'张表格',entity_extraction:'个窗口'}[item.stage]||'项';
  const failed=Number.isInteger(item.failed) && item.failed>0 ? `（含未完成 ${item.failed}）` : '';
  return `已处理 ${item.completed} / ${item.total} ${unit}${failed}`;
}

// A call is ordered by its first recorded event. Its transport attempts remain
// inspectable, and the separate raw list retains the global event ordering.
export function projectCalls(events) {
  const groups=new Map(), attemptOwners=new Map();
  for(const item of events)if((item.kind==='model_call'||item.kind==='model_attempt') && item.logical_id){
    if(!groups.has(item.logical_id))groups.set(item.logical_id,[]);
    groups.get(item.logical_id).push(item);
    if(item.kind==='model_attempt' && item.attempt_id){
      if(!attemptOwners.has(item.attempt_id))attemptOwners.set(item.attempt_id,new Set());
      attemptOwners.get(item.attempt_id).add(item.logical_id);
    }
  }
  const merged=new Map(), skip=new Set();
  for(const group of groups.values()){
    const calls=group.filter(item=>item.kind==='model_call');
    const locationKeys=new Set(group.filter(item=>Array.isArray(item.visual_targets)).map(item=>JSON.stringify(
      item.visual_targets.map(t=>[t?.document_index,t?.page,t?.kind,t?.bbox]))));
    const hasConflict=locationKeys.size>1 || ['stage','item_id','phase','parent_kind','parent_id','protocol','round','requested_model'].some(field=>
      new Set(group.map(item=>item[field]).filter(value=>value!==undefined && value!==null && value!=='')).size>1);
    const starts=calls.filter(item=>item.status==='started'), ends=calls.filter(item=>item.status!=='started');
    const attempts=group.filter(item=>item.kind==='model_attempt' && item.attempt_id);
    const ids=new Map();for(const item of attempts){
      if(!ids.has(item.attempt_id))ids.set(item.attempt_id,[]);ids.get(item.attempt_id).push(item);
    }
    const reversed=items=>{
      const start=items.find(item=>item.status==='started'), end=items.find(item=>item.status!=='started');
      return !!start && !!end && end.seq<start.seq;
    };
    const ambiguous=[...ids.values()].some(items=>items.filter(item=>item.status==='started').length>1 ||
      items.filter(item=>item.status!=='started').length>1 || reversed(items) ||
      new Set(items.map(item=>item.attempt).filter(Number.isInteger)).size>1);
    if(!calls.length || hasConflict || starts.length>1 || ends.length>1 || ambiguous ||
      reversed(calls) || attempts.some(item=>attemptOwners.get(item.attempt_id).size>1) ||
      (starts[0] && attempts.some(item=>item.seq<starts[0].seq)) ||
      (ends[0] && attempts.some(item=>item.seq>ends[0].seq)))continue;
    const members=group.filter(item=>item.kind==='model_call' || item.attempt_id);
    if(members.length<2)continue;
    const first=members[0], latest=ends[0]||starts[0]||calls[0];
    const located=members.find(item=>visualTargets(item).length);
    const location=located?{visual_targets:located.visual_targets,
      ...(members.some(item=>item.visual_targets_truncated===true)?{visual_targets_truncated:true}:{})}:{};
    merged.set(first.seq,{...first,...latest,...location,seq:first.seq,at:first.at,kind:'model_call',_groupedEvents:members});
    for(const item of members.slice(1))skip.add(item.seq);
  }
  return events.filter(item=>!skip.has(item.seq)).map(item=>merged.get(item.seq)||item);
}

export function projectReaders(events) {
  const groups=new Map(), replacements=new Map(), skip=new Set();
  for(const event of events)if(event.kind==='reader_step' && event.item_id){
    if(!groups.has(event.item_id))groups.set(event.item_id,[]);
    groups.get(event.item_id).push(event);
  }
  for(const group of groups.values()){
    const starts=group.filter(e=>e.status==='started'), ends=group.filter(e=>e.status!=='started');
    if(starts.length!==1 || ends.length!==1 || starts[0].seq>=ends[0].seq ||
      !starts[0].reader || starts[0].reader!==ends[0].reader || starts[0].stage!==ends[0].stage)continue;
    const start=starts[0], end=ends[0];
    replacements.set(start.seq,{...start,...end,seq:start.seq,at:start.at,_readerEvents:[start,end]});
    skip.add(end.seq);
  }
  return events.filter(event=>!skip.has(event.seq)).map(event=>replacements.get(event.seq)||event);
}

export function rowText(item, index) {
  const time=finite(item.at) ? new Date(item.at*1000).toLocaleTimeString('zh-CN',{hour12:false}) : '未知';
  const parts = [`时间 ${time}`];
  if (item.kind === 'table_round') parts.push(`表格 ${safeText(item.item_id) || '未知'} · 映射第 ${item.round ?? '未知'} 轮`);
  else if (item.kind === 'model_call') parts.push(`模型调用 · ${stageName(item.stage)}`);
  else if (item.kind === 'model_attempt') parts.push(`模型传输 · 请求第 ${item.attempt ?? '未知'} 次`);
  else if (item.kind === 'embedding_batch') parts.push(`向量批次${Number.isInteger(item.batch_size) ? ` · ${item.batch_size} 项` : ''}`);
  else if (item.kind === 'embedding_summary') parts.push('向量候选汇总');
  else if (item.kind === 'table') parts.push(`表格 ${safeText(item.item_id) || '未知'}`);
  else if (item.kind === 'window') parts.push(`正文窗口 ${safeText(item.item_id) || '未知'}`);
  else if (item.kind === 'reader_step'){
    parts.push(readerName(item.reader));
    if(item.ocr_mode==='ocr')parts.push('解析模式 OCR');
    if(item.ocr_mode==='txt')parts.push('解析模式 文本');
    if(item.source==='reader_cache')parts.push('复用解析缓存');
  }
  else if (item.kind === 'visual_step') parts.push(visualStepLabel(item));
  else if (item.kind === 'stage') parts.push(stageName(item.stage));
  else parts.push('处理记录');
  if(item.kind==='model_call' || item.kind==='model_attempt')parts.push(...correlationText(item,index));
  parts.push(statusName(item.status));
  if(stageProgress(item))parts.push(stageProgress(item));
  if (item.kind==='model_attempt' && item.transport_status && item.transport_status!==item.status)
    parts.push(statusName(item.transport_status));
  if (item.error_code && !parts.includes(errorName(item.error_code))) parts.push(errorName(item.error_code));
  if (REQUEST_STATE[item.request_state]) parts.push('请求状态：'+REQUEST_STATE[item.request_state]);
  if (item.source==='journal_cache') parts.push('复用阶段缓存');
  if (item.source==='client_cache') parts.push('复用请求缓存');
  if (['responses','chat_completions','embeddings'].includes(item.protocol)) parts.push('接口：'+item.protocol);
  if (item.kind==='embedding_summary') for(const [key,label] of [['total','总数'],['completed','完成'],['failed','失败'],['cached','缓存'],['requests','请求']])
    if(Number.isInteger(item[key]))parts.push(`${label} ${item[key]}`);
  if (item.kind==='embedding_batch' && Number.isInteger(item.http_status)) parts.push(`HTTP ${item.http_status}`);
  if (item.diagnostic_codes && typeof item.diagnostic_codes==='object')
    for(const [code,count] of Object.entries(item.diagnostic_codes))
      if(DIAGNOSTICS[code] && Number.isInteger(count) && count>0)parts.push(`${DIAGNOSTICS[code]} ${count}`);
  if (item.will_retry) parts.push('将重试');
  if (item.possible_duplicate_charge) parts.push('可能重复计费');
  if (finite(item.elapsed_seconds)) parts.push(`耗时 ${duration(item.elapsed_seconds)}`);
  if (item.requested_model) parts.push(`请求模型 ${item.requested_model}`);
  if (item.returned_model) parts.push(`返回模型 ${item.returned_model}`);
  return parts.join(' · ');
}

// Expanded rows show only recorded facts; diagnostic IDs remain in raw history.
export function detailItems(item, index, elapsed) {
  const items=[];
  const started=item.status==='started' && !Number.isInteger(item.completed);
  const time=value=>finite(value) ? new Date(value*1000).toLocaleTimeString('zh-CN',{hour12:false}) : '未记录';
  if(item._groupedEvents){
    const first=item._groupedEvents.find(e=>e.kind==='model_call' && e.status==='started');
    const last=item._groupedEvents.find(e=>e.kind==='model_call' && e.status!=='started');
    items.push(`开始时间：${time(first?.at)}`);
    if(!first && last)items.push(`结束时间：${time(last.at)}`);
  }else{
    const label=item._readerEvents || item._visualEvents || started ? '开始时间'
      : item.kind==='visual_step' && ['selection','reuse'].includes(item.visual_step) ? '记录时间'
      : item.kind==='visual_step' && item.visual_step==='result' ? '结果记录时间'
      : item.kind==='reader_step' && (['cache_hit','not_processed'].includes(item.status) || ['ocr_limit','ocr_cache'].includes(item.reader)) ? '记录时间'
      : item.kind==='stage' && item.status==='started' ? '记录时间' : '结束时间';
    items.push(`${label}：${time(item.at)}`);
  }
  if(item.kind==='reader_step'){
    if(item.reader==='pymupdf')items.push('工具：PyMuPDF');
    if(item.reader==='mineru')items.push('工具：PyMuPDF 与 MinerU');
    if(item.ocr_mode==='ocr')items.push('解析模式：OCR');
    if(item.ocr_mode==='txt')items.push('解析模式：文本模式');
    if(item.reader==='mineru' && item.status!=='started' && !item.ocr_mode &&
      ['completed','partial','cache_hit'].includes(item.status))items.push('解析模式：未报告');
    if(item.source==='reader_cache' || item.status==='cache_hit')items.push('复用解析缓存');
    if(Number.isInteger(item.page_count) && item.page_count>=0)items.push(`页面数量：${item.page_count} 页`);
    if(Number.isInteger(item.item_count) && item.item_count>=0)
      items.push(`${item.reader==='ocr_limit'?'未二次识别':'识别项目'}：${item.item_count} 项`);
    if(Number.isInteger(item.failed) && item.failed>0 && item.reader==='ocr_first')items.push(`未完整读取：${item.failed} 页`);
    if(Number.isInteger(item.review_count) && item.review_count>=0)items.push(`待核对：${item.review_count} 项`);
    if(Number.isInteger(item.disagreement_count) && item.disagreement_count>=0)items.push(`识别不一致：${item.disagreement_count} 项`);
  }
  if(item.kind==='model_call' || item.kind==='model_attempt' || item.kind==='visual_step'){
    const c=correlations(item,index);
    if(c.parentKind==='table' && c.parentId)items.push(`所属表格：${c.parentId}`);
    else if(c.parentKind==='window' && c.parentId)items.push(`所属正文窗口：${c.parentId}`);
    if(c.round)items.push(`映射第 ${c.round} 轮`);
    for(const target of visualTargets(item)){
      const source=Number.isInteger(target.document_index) && target.document_index>0?`原件 ${target.document_index} · `:'';
      const box=Array.isArray(target.bbox) && target.bbox.length===4 && target.bbox.every(finite)
        ? ` · 区域坐标：[${target.bbox.join(', ')}]` : '';
      const why=VISUAL_REASONS[target.reason] ? ` · ${VISUAL_REASONS[target.reason]}` : '';
      items.push(`${source}第 ${target.page} 页 · ${VISUAL_KINDS[target.kind]}${box}${why}`);
    }
    if(visualTargets(item).length && item.visual_targets_truncated===true)items.push('还有更多区域，未全部列出');
  }
  if(item.kind==='visual_step' && item.visual_step==='result'){
    const handwriting=visualTargets(item).some(target=>target.kind==='handwriting_table');
    if(Number.isInteger(item.item_count))items.push(`核对单元格：${item.item_count} 个`);
    if(Number.isInteger(item.failed) && item.failed>0)items.push(`无法读取：${item.failed} 个`);
    if(Number.isInteger(item.review_count) && item.review_count>0)
      items.push(handwriting?`待人工核对：${item.review_count} 项`:`模型提示不确定：${item.review_count} 项`);
    if(['completed','partial'].includes(item.status))items.push('模型识别结果尚未人工核对');
  }
  if(['failed','invalid','partial','empty','not_processed','unknown','interrupted'].includes(item.status) &&
     !(item.kind==='visual_step' && item.visual_step==='selection' && item.status==='empty'))
    items.push(`结果：${displayStatus(item)}`);
  if(item.error_code && !(item.kind==='model_attempt' && item.transport_status &&
     errorName(item.error_code)===statusName(item.transport_status)))items.push(`原因：${errorName(item.error_code)}`);
  if(item.kind==='model_attempt' && item.transport_status && item.transport_status!==item.status)
    items.push(`请求结果：${statusName(item.transport_status)}`);
  if(item.source==='journal_cache')items.push('复用阶段缓存');
  if(item.source==='client_cache')items.push('复用请求缓存');
  if(REQUEST_STATE[item.request_state] && item.request_state!=='completed')items.push(`请求状态：${REQUEST_STATE[item.request_state]}`);
  if(['responses','chat_completions','embeddings'].includes(item.protocol))items.push(`接口：${item.protocol}`);
  if(item.requested_model)items.push(`请求模型：${item.requested_model}`);
  if(item.returned_model)items.push(`返回模型：${item.returned_model}`);
  if(item.kind==='embedding_batch' && Number.isInteger(item.http_status))items.push(`HTTP ${item.http_status}`);
  if(item.kind==='embedding_summary') for(const [key,name] of [['total','总数'],['completed','完成'],['failed','失败'],['cached','缓存'],['requests','请求']])
    if(Number.isInteger(item[key]))items.push(`${name} ${item[key]}`);
  for(const [key,name] of [['attributes','属性'],['requests','请求'],['unread_cells','未覆盖单元格']])
    if(Number.isInteger(item[key]) && !(item.kind==='embedding_summary' && key==='requests'))items.push(`${name} ${item[key]}`);
  if(item.diagnostic_codes && typeof item.diagnostic_codes==='object')
    for(const [code,count] of Object.entries(item.diagnostic_codes))
      if(DIAGNOSTICS[code] && Number.isInteger(count) && count>0)items.push(`${DIAGNOSTICS[code]} ${count}`);
  if(finite(elapsed))items.push(`自记录开始已过 ${Math.round(elapsed)} 秒`);
  else if(finite(item.elapsed_seconds))items.push(`耗时 ${duration(item.elapsed_seconds)}`);
  if(item.possible_duplicate_charge)items.push('可能重复计费');
  if(item.will_retry)items.push('将重试');
  if(item._groupedEvents){
    const attempts=new Map();
    for(const event of item._groupedEvents)if(event.kind==='model_attempt' && event.attempt_id)attempts.set(event.attempt_id,event);
    for(const attempt of attempts.values()){
      const outcome=attempt.transport_status ? statusName(attempt.transport_status) : statusName(attempt.status);
      const name=`请求第 ${attempt.attempt??'未知'} 次`;
      items.push(`${name}：${attempt.status==='started'?'已开始':outcome}`);
      if(attempt.error_code && errorName(attempt.error_code)!==outcome)items.push(`原因：${errorName(attempt.error_code)}`);
      if(attempt.requested_model && attempt.requested_model!==item.requested_model)items.push(`请求模型：${attempt.requested_model}`);
      if(attempt.returned_model && attempt.returned_model!==item.returned_model)items.push(`返回模型：${attempt.returned_model}`);
      if(attempt.protocol && attempt.protocol!==item.protocol)items.push(`接口：${attempt.protocol}`);
      if(attempt.will_retry)items.push('将重试');
      if(attempt.possible_duplicate_charge)items.push('可能重复计费');
      if(finite(attempt.elapsed_seconds))items.push(`请求耗时 ${duration(attempt.elapsed_seconds)}`);
    }
  }
  if(item.kind==='model_attempt' && Number.isInteger(item.attempt))items.push(`请求第 ${item.attempt} 次`);
  return items;
}

export function eventLabel(item) {
  if(item.kind==='stage')return item.status==='started' ? (stageProgress(item) || '开始处理')
    : item.error_code ? errorName(item.error_code) : statusName(item.status);
  if(item.kind==='table')return `表格 ${safeText(item.item_id)||'未知'}`;
  if(item.kind==='window')return `正文窗口 ${safeText(item.item_id)||'未知'}`;
  if(item.kind==='table_round')return `表格 ${safeText(item.item_id)||'未知'} · 映射第 ${item.round??'未知'} 轮`;
  if(item.kind==='reader_step')return `${readerName(item.reader)}${item._multipleDocuments && item._documentIndex ? ` · 原件 ${item._documentIndex}` : ''}`;
  if(item.kind==='visual_step')return visualStepLabel(item);
  if(item.kind==='model_call'){
    const visual=visualSummary(item);if(visual)return `视觉识别 · ${visual} · 模型请求`;
    const parent=item.parent_kind==='table'&&item.parent_id?` · 表格 ${item.parent_id}`
      :item.parent_kind==='window'&&item.parent_id?` · 正文窗口 ${item.parent_id}`:'';
    return `模型调用${parent}`;
  }
  if(item.kind==='model_attempt'){
    const visual=visualSummary(item);return `${visual?`视觉识别 · ${visual} · `:''}模型请求第 ${item.attempt??'未知'} 次`;
  }
  if(item.kind==='embedding_batch')return '向量批次';
  if(item.kind==='embedding_summary')return '向量候选汇总';
  return '处理记录';
}

export function statusIcon(item, active) {
  if(active)return {kind:'active', label:'已开始，状态待核实', text:''};
  if(item.status==='started')return {kind:'unconfirmed',label:'已开始，状态待核实',text:'·'};
  if(item.kind==='visual_step' && item.visual_step==='selection' && item.status==='empty')return item.visual_reason==='regular_visual_no_targets'
    ? {kind:'success',label:'无需处理',text:'✓'} : {kind:'unknown',label:'选择结果未记录',text:'?'};
  if(item.status==='cache_hit' || (item.status==='completed' && ['client_cache','journal_cache'].includes(item.source)))
    return {kind:'cached',label:'使用缓存',text:'↻'};
  if(['completed','mapped'].includes(item.status))return {kind:'success',label:statusName(item.status),text:'✓'};
  if(['failed','invalid'].includes(item.status))return {kind:'failure',label:statusName(item.status),text:'!'};
  if(['partial','empty','not_processed','interrupted'].includes(item.status))return {kind:'warning',label:displayStatus(item),text:'△'};
  return {kind:'unknown',label:statusName(item.status),text:'?'};
}

export function activityKey(item) {
  if(item.kind==='stage')return `stage:${item.stage}`;
  if((item.kind==='table'||item.kind==='window') && item.item_id)return `${item.kind}:${item.item_id}`;
  if(item.kind==='reader_step' && item.item_id)return `reader_step:${item.item_id}`;
  if(item.kind==='visual_step' && item.item_id)return `visual_step:${item.item_id}`;
  if(item.kind==='table_round' && item.item_id && Number.isInteger(item.round))return `table_round:${item.item_id}:${item.round}`;
  if(item.kind==='model_attempt' && item.attempt_id)return `model_attempt:${item.attempt_id}`;
  if((item.kind==='model_call'||item.kind==='embedding_batch') && item.logical_id)return `${item.kind}:${item.logical_id}`;
  return null;
}

export function sameTimelineIdentity(a,b){
  return ['seq','stage','kind','item_id','parent_kind','parent_id','logical_id','attempt_id','round','reader','visual_step']
    .every(field=>a[field]===b[field]);
}
