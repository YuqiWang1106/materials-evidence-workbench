import {el} from './dom.js';
import {modelOutcomeUnknown} from './document-processing-logic.mjs';


const STAGES = {queued:'等待处理', reading:'原文读取', extracting:'内容提取',
  visual_reading:'视觉区域识别', table_mapping:'表格结构映射', entity_extraction:'内容提取',
  validating:'结构核对', synonyms:'同义名称整理', identity:'配方身份整理', finalizing:'草稿与质量报告',
  publishing:'正在保存', needs_review:'处理结束，等待确认', saved:'已保存',
  failed:'处理失败', cancelled:'已取消', superseded:'历史版本', deleting:'正在删除资料'};
const TERMINAL = new Set(['needs_review','saved','failed','cancelled','superseded']);
export function progressValue(progress) {
  const {completed,total} = progress;
  return progress.determinate === true && Number.isInteger(completed) && Number.isInteger(total) &&
    total > 0 && completed >= 0 && completed <= total ? Math.floor(completed * 100 / total) : null;
}
export function processingText(progress) {
  let text = STAGES[progress.stage] || '处理中';
  const percent = progressValue(progress);
  if (percent !== null) text += ` · 当前阶段 ${progress.completed} / ${progress.total} ${progress.unit || '项'}（${percent}%）`;
  else if (progress.document_index && progress.documents_total) text += ` · 原件 ${progress.document_index}/${progress.documents_total}`;
  if (progress.failed) text += ` · ${progress.failed} 项未完成`;
  const batches = progress.model_batches;
  if (batches) text += ` · 模型批次：完成 ${batches.completed ?? '未知'}，处理中 ${batches.started ?? '未知'}，时间额度耗尽 ${batches.time_budget_exhausted ?? '未知'}`;
  if (batches?.failed) text += `，失败 ${batches.failed}`;
  return text + coverageText(progress.extraction_coverage);
}
/** What an extraction left out, as a suffix for one-line progress text; empty when nothing is missing. */
export function coverageText(coverage) {
  let text = '';
  if (coverage?.failed_windows) text += ` · 抽取窗口失败 ${coverage.failed_windows}/${coverage.windows}`;
  if (coverage?.skipped_pages) text += ` · 未抽取页码 ${coverage.skipped_pages} 项`;
  return text;
}
/** The notice above a draft whose extraction is incomplete; empty when nothing is missing. */
export function coverageWarning(coverage) {
  if (!coverage?.failed_windows && !coverage?.skipped_pages) return '';
  const parts = [];
  if (coverage.failed_windows) parts.push(`${coverage.failed_windows} / ${coverage.windows} 个窗口没有抽取成功`);
  if (coverage.skipped_pages) parts.push(`${coverage.skipped_pages} 项页码没有抽取`);
  return `本次抽取不完整：${parts.join('，')}。现在保存只会写入已抽取的部分，保存后可在结果页补抽失败窗口。`;
}
/** The status pill, title and explanation above a task that is still being processed or did not finish. */
export function processingSummary(task, statusLabel) {
  const unfinished=['failed','cancelled'].includes(task.state);
  const retriesExhausted=task.state==='failed' && task.error?.startsWith('model_response_lost_after_retries:');
  const description=modelOutcomeUnknown(task.error) && !retriesExhausted
    ? '模型请求结果尚未确认。原件与请求记录已保留；不会自动重发，请先核对记录。'
    : retriesExhausted ? '模型请求三次尝试后仍未取得结果。原件与请求记录已保留。'
    : unfinished ? '原件与草稿仍保留。请检查读法后重新确认。' : '读取原格、建立记录，并核对来源依据。';
  return [el('span','status-pill',statusLabel), el('h2',null,unfinished ? '这次整理尚未完成' : '正在按已确认的读法整理'),
    el('p','muted',description)];
}
export function processingView(progress) {
  const root=el('div','processing-progress'),label=el('p','processing-progress-label',processingText(progress));
  const percent=progressValue(progress),terminal=TERMINAL.has(progress.stage);
  label.setAttribute('role','status');root.append(label);
  if (!terminal || percent!==null) {
    const bar=el('progress','processing-progress-bar');bar.max=100;
    bar.setAttribute('aria-label','当前阶段：'+(STAGES[progress.stage]||'处理中'));
    if(percent!==null){bar.value=percent;bar.setAttribute('value',String(percent));bar.setAttribute('aria-valuetext',`${progress.completed}/${progress.total}，${percent}%`);}
    else bar.setAttribute('aria-valuetext','处理中，当前阶段总量尚不明确');
    root.append(bar);
  }
  if (progress.model_attempts?.length) {
    const detail = el('details'), records = el('ul');
    detail.append(el('summary', null, '查看模型请求记录'), records);
    for (const attempt of progress.model_attempts) {
      const label = {transport_unknown: '回包丢失或连接中断，结果未知', transport_write_failed: '发送中断，结果未知',
        transport_not_sent: '连接未建立', started: '尝试中', completed: '已收到结果'}[attempt.status] || attempt.status;
      const risk = attempt.possible_duplicate_charge ? ' · 可能重复计费' : '';
      records.append(el('li', null, `第 ${attempt.attempt} 次 · ${label}${risk} · ${attempt.attempt_id}`));
    }
    root.append(detail);
  }
  const timings=[];
  if(Number.isFinite(progress.reading_seconds))timings.push('本地解析：'+Math.round(progress.reading_seconds)+' 秒');
  if(Number.isFinite(progress.extraction_review_seconds))timings.push('提取核对：'+Math.round(progress.extraction_review_seconds)+' 秒');
  if(timings.length){
    const detail=el('details','processing-timing');detail.dataset.stateKey='processing-timing';
    detail.append(el('summary',null,'阶段耗时'),el('p',null,timings.join(' · ')));root.append(detail);
  }
  return root;
}
