import {studyEvidenceView} from './study-unit-wiki.js';
import { el } from './dom.js';
import { renderChatMarkdown } from './table-chat-markdown.js';
import { renderRawTablePlots } from './raw-table-analysis.js';

const scalar = value => value === null || value === undefined ? '—' : typeof value === 'object' ? null : String(value);
const valueText = value => Array.isArray(value) ? value.map(valueText).join('、') : value && typeof value === 'object' ? Object.entries(value).map(([key, item]) => `${key}：${valueText(item)}`).join('；') : scalar(value);
const statements = {source_report:'来源报告',measured:'实测',source_reported_measurement:'来源报告的测量',declared_range:'声明范围',scope_claim:'范围声明',source_claim:'来源声明',author_interpretation:'作者解释',source_reported_calculation:'来源计算',unknown:'性质未注明'};
const fields = {'material.description':'材料描述','material.specification':'材料规格','composition.amount':'配方用量','condition.parameter':'测试条件','method.standard':'测试方法','observation.value':'测试结果','process.parameter':'制备参数','process.description':'制备过程','sample.description':'样品描述'};
const labels = {table_cells:'原表单元格',table_analysis:'描述统计',record_fact:'已保存事实',document_evidence:'文档证据',source_passage:'来源原段'};
const contentLabels = {parsed:'解析文本',model_transcription:'模型转写',verbatim:'来源逐字内容',unknown:'来源类型待核对'};
function table(headings, rows) {
  const scroll=el('div','research-data-scroll'), node=el('table','research-data-table'), head=el('thead'), tr=el('tr'), body=el('tbody');
  for(const heading of headings) tr.append(el('th',null,heading));
  head.append(tr);
  for(const values of rows){const row=el('tr');for(const value of values)row.append(el('td',null,value??'—'));body.append(row);}
  node.append(head,body);scroll.append(node);return scroll;
}
function context(root, entry) {
  for(const [key,label] of [['conditions','适用条件'],['uncertainties','不确定性']]) {
    if(entry[key]?.length)root.append(el('p','research-evidence-context',label+'：'+entry[key].join('；')));
    else if(entry.context_status?.[key]==='read_separately')root.append(el('p','research-evidence-context',label+'尚未完整读取。'));
  }
}
function cells(root, payload) {
  const rows=[];
  for(const row of payload.rows||[])for(const cell of row.cells||[]) {
    const column=payload.columns?.find(c=>c.column===cell.column);
    const value=cell.raw_value === '' ? '空白' : cell.raw_value === null ? '空值' : valueText(cell.raw_value);
    rows.push([cell.address||`${column?.column_id||cell.column}:${row.row}`,row.row,column?.column_id||cell.column,value,cell.formula||cell.source_formula||cell.missing_kind||cell.value_type||'']);
  }
  root.append(table(['原格','源行','原列','原值','类型／说明'],rows));
  const coverage=payload.coverage;
  if(coverage?.sampled||coverage?.omitted_columns)root.append(el('p','research-evidence-context',`已读取 ${coverage.returned_rows} / ${coverage.total_rows} 行${coverage.omitted_columns ? `，另有 ${coverage.omitted_columns} 列未读取` : ''}。`));
}
function analyses(root, payload, program) {
  const records=payload.tables||[payload];
  const artifact=program?.artifact;
  const full=artifact?.kind==='table_analysis'?[artifact]:artifact?.kind==='table_comparison'?artifact.tables:[];
  const stats=['n','min','max','mean','median','sample_std','q1','q3'];
  const statLabels=['有效 n','最小值','最大值','平均值','中位数','样本标准差','Q1','Q3'];
  const readable=value=>value?.omitted===true ? value.reason||'摘要已省略该内容' : valueText(value);
  const name=column=>`${column.column_id} · ${readable(column.label||column.column_id)}`;
  const groupName=g=>(g.key||[]).map(k=>`${k.column_id}：${readable(k.value)}`).join(' · ');
  const coverage=payload.analysis_coverage;
  if(coverage?.part_count>1||coverage?.complete===false)root.append(el('p','research-evidence-context',`分析结果 ${coverage.part_index+1} / ${coverage.part_count} 页，当前仅展示部分结果。`));
  for(const [index,record] of records.entries()) {
    const group=el('section','research-analysis'),saved=full?.find(item=>item.table_version_id===record.table_version_id);
    const groupedMode=Boolean((record.config||saved?.config)?.group_columns?.length);
    if(records.length>1)group.append(el('h3',null,record.original_name||`表 ${index+1}`));
    if(!groupedMode)group.append(table(['列','单位',...statLabels],(record.columns||[]).map(c=>[name(c),c.unit?readable(c.unit):'未注明',...stats.map(k=>readable(c.statistics?.[k]))])));
    for(const column of record.columns||[]) {
      if(column.statistics?.status==='skipped')group.append(el('p','research-evidence-context',`${column.column_id}：${column.statistics.reason?readable(column.statistics.reason):'未计算统计'}`));
      if(!groupedMode&&column.statistics?.sample_std_reason)group.append(el('p','research-evidence-context',`${column.column_id} 样本标准差：${readable(column.statistics.sample_std_reason)}`));
      const counts=column.counts||{},missing=(counts.missing||0)+(counts.absent||0);
      const excluded=[[missing,'缺失'],[counts.unparsed,'不可解析'],[counts.formula,'公式未计算']].filter(([n])=>n>0).map(([n,label])=>`${label} ${n}`);
      if(excluded.length)group.append(el('p','research-evidence-context',`${column.column_id}：${excluded.join(' · ')}。`));
      for(const warning of column.warnings||[])if(warning.reason)group.append(el('p','research-evidence-context',`${column.column_id}：${readable(warning.reason)}`));
      const grouped=column.group_statistics;
      if(groupedMode) {
        if(grouped?.groups?.length)group.append(table(['列','单位','分组',...statLabels],grouped.groups.map(g=>[name(column),column.unit?readable(column.unit):'未注明',groupName(g),...stats.map(k=>readable(g.statistics?.[k]))])));
        else group.append(el('p','research-evidence-context',`${name(column)}：${grouped?.reason?readable(grouped.reason):'当前未展示分组统计'}`));
        for(const g of grouped?.groups||[])for(const reason of [g.statistics?.reason,g.statistics?.sample_std_reason].filter(Boolean))group.append(el('p','research-evidence-context',`${column.column_id} · ${groupName(g)}：${readable(reason)}`));
        if(grouped?.excluded_count)group.append(el('p','research-evidence-context',`${column.column_id}：${grouped.excluded_count} 行未纳入本次分组统计。`));
        const page=grouped?.page_coverage;
        if(page?.complete===false)group.append(el('p','research-evidence-context',`${column.column_id}：本页分组 ${page.offset+1}–${page.offset+page.returned} / ${page.summary_groups}，其余摘要分组尚未在本页显示。`));
      }
      if(grouped?.omitted_groups)group.append(el('p','research-evidence-context',`另有 ${grouped.omitted_groups} 组未在摘要中显示。`));
    }
    if(saved?.result?.table_version_id===record.table_version_id && saved.config)for(const column of saved.result.columns||[]) {
      // Compact plots use persisted program coordinates only; no source rows are fetched or fabricated.
      if(groupedMode&&!saved.config.x_column&&!saved.config.line_plot)group.append(el('p','research-evidence-context',`${name(column)} · 全部分组的总体分布`));
      group.append(renderRawTablePlots(column,saved.config,{rows:[]},()=>{},{preferPrimary:true,compact:true}));
    }
    root.append(group);
  }
  if(payload.comparison_mode==='side_by_side')root.append(el('p','research-evidence-context','各表并排计算，未联表或换算单位。'));
}

function subjectText(value, values) {
  const label=value.subject_label, ref=value.subject_ref;
  const kind={sample:'样品',reported_test:'测试',instrument_run:'仪器运行'}[value.subject_kind]||value.subject_kind;
  const ambiguous=label&&values.some(other=>other.subject_label===label&&(other.subject_ref!==ref||other.subject_kind!==value.subject_kind));
  return [label||ref||'未注明对象',kind,label&&ref&&ambiguous?ref:null].filter(Boolean).join(' · ');
}
function documentContent(root,payload) {
  const evidence=payload.evidence_data||payload;
  const kind=evidence.effective_content_kind||payload.content_kind;
  if(kind)root.append(el('p','research-evidence-context',contentLabels[kind]||contentLabels.unknown));
  if(typeof evidence.quote==='string')root.append(el('blockquote','research-source-quote',evidence.quote));
  const sourceTitle=evidence.source_title||payload.label||payload.attribution;
  if(sourceTitle)root.append(el('p','research-evidence-label',sourceTitle));
  if(payload.values?.length)root.append(table(['对象','字段','原值','单位','基准／条件','性质'],payload.values.map(v=>[subjectText(v,payload.values),fields[v.field_id]||v.field_id||v.label||'字段',valueText(v.raw_value),v.unit||'未注明',[v.basis,...Object.entries(v.qualifiers||{}).map(([k,value])=>`${k}：${valueText(value)}`)].filter(Boolean).join(' · '),statements[v.statement_kind]||statements.unknown])));
  const coverage=payload.quote_coverage;
  if(coverage?.complete===false)root.append(el('p','research-evidence-context',`原段 ${coverage.part_index+1} / ${coverage.part_count}，当前未展示全文。`));
}

/** Roles are relative to the answering turn, never the mutable composer selection. */
export function researchSourceRole(entry, focus) {
  if (!focus) return null;
  const ref=entry.provenance?.research_artifact_ref || entry.program?.artifact_ref || entry.ref || entry.support?.ref || entry.payload?.ref || entry.context_ref || {};
  const sid=ref.source_id || ref.source_snapshot_ref?.source_id;
  const versions=ref.table_version_ids || (ref.table_version_id?[ref.table_version_id]:entry.payload?.table_version_id?[entry.payload.table_version_id]:[]);
  return focus.document_source_ids?.includes(sid) || versions.some(v=>focus.table_version_ids?.includes(v)) ? '重点资料' : '全库补充';
}
function sourceDetails(root, entry, {preview, focus}) {
  const role=researchSourceRole(entry,focus);
  if(role)root.append(el('p','research-source-role',role));
  if(entry.status?.source_review==='unreviewed_candidate'||entry.payload?.use_status==='unreviewed_candidate')root.append(el('p','research-evidence-context','研究单元候选 · 尚未人工审核 · 未作科学验证'));
  if(!preview)return;
  const ref=entry.provenance?.research_artifact_ref || entry.program?.artifact_ref || entry.ref || entry.support?.ref || entry.payload?.ref || {};
  if(ref.kind==='table_analysis_artifact'&&entry.program?.artifact) {
    if(entry.program.version!=='wiki-local-analysis-v1' && ['chat_id','turn_id','artifact_id'].some(k=>entry.program[k]!==ref[k]))throw Error('程序结果归属不匹配');
    const link=el('a','research-evidence-link','返回计算所属对话');link.href=(ref.chat_kind==='research'?'#research-chat/':'#table-chat/')+encodeURIComponent(ref.chat_id);root.append(link);
  }
}
function studyContent(root,payload) {
  root.append(studyEvidenceView(payload));
  const nav=payload.navigation||{};
  if(nav.object_label||nav.owner_label)root.append(el('p','research-evidence-context',[nav.object_label,nav.owner_label].filter(Boolean).join(' · ')));
  for(const item of payload.owner_context||[]) {
    const details=[item.label,item.method_name,item.standard,item.description].filter(x=>typeof x==='string'&&x);
    if(details.length)root.append(el('p','research-evidence-context',details.join(' · ')));
  }
}

/** Typed persisted answer/support view. The caller revalidates supports before opening a preview. */
export function renderWikiSupport(entry, { preview=false, focus=null }={}) {
  const dependency=['unified_support','wiki_support'].includes(entry.kind);
  if(dependency) {
    const projection=entry.support_preview;
    if(!projection || !['table_cells','table_analysis','record_fact','document_evidence','source_passage','study_unit_context','wiki_content'].includes(projection.evidence_type) || !projection.payload || typeof projection.payload!=='object')throw Error('依据预览类型尚未就绪');
    // Preserve the saved dependency kind; this is a fresh read-only projection,
    // never an assertion that the model previously read these raw values.
    entry={...entry,evidence_type:projection.evidence_type,payload:projection.payload,program:projection.program||entry.program};
  }
  const root=el('div','research-wiki-content');
  sourceDetails(root,entry,{preview,focus});
  if(dependency)root.append(el('p','research-evidence-context','来源依赖预览 · 不代表模型已读取全部原始内容'));
  if(entry.kind==='wiki_content'||dependency&&entry.evidence_type==='wiki_content') {
    if(dependency)entry={...entry,...entry.payload};
    root.append(el('p','research-evidence-label',['Wiki',entry.title,entry.heading].filter(Boolean).join(' · ')));
    root.append(renderChatMarkdown(entry.body||''));
    context(root,entry);
  } else if(entry.kind==='wiki_evidence'||dependency) {
    const payload=entry.payload||{};
    root.append(el('p','research-evidence-label',[labels[entry.evidence_type]||'依据',payload.original_name,payload.sheet,payload.table_range].filter(Boolean).join(' · ')));
    if(payload.ref?.kind==='study_unit_snapshot'||payload.kind?.startsWith('study_unit_'))studyContent(root,payload);
    else if(entry.evidence_type==='table_cells')cells(root,payload);
    else if(entry.evidence_type==='table_analysis')analyses(root,payload,preview?entry.program:null);
    else documentContent(root,payload);
    // A record citation may also resolve to a selected original paragraph on open.
    if(preview&&entry.location&&entry.location!==entry.payload&&entry.evidence_type==='record_fact'){if(entry.location.ref?.kind==='study_unit_snapshot')studyContent(root,entry.location);else documentContent(root,entry.location);}
  }
  if(preview&&entry.part_count>1)root.append(el('p','research-evidence-context',`段落 ${entry.part_index+1} / ${entry.part_count}`));
  return root;
}
