import {formatWebsiteTime} from './website-time-logic.mjs';
const sections = [
  ['research_object', '研究对象'], ['composition', '样品组成'],
  ['preparation', '制备过程'], ['testing', '测试方法与条件'],
  ['results_interpretation', '结果与作者解释'], ['gaps_use_scope', '记录缺口与使用范围'],
];
const kinds = {source_description:'来源描述', author_interpretation:'作者解释', editorial_note:'整理说明', gap:'缺口'};
const reviews = {not_reviewed:'尚未审核', confirmed:'已确认记录', corrected:'已修改记录', uncertain:'待核对', excluded:'已排除'};
const relations = {resolved:'已解析', consistent:'适用一致', multiple:'多个适用方法', conflict:'适用冲突', unresolved:'未解析', unknown:'未知引用', ambiguous:'引用有歧义', wrong_kind:'对象类型不符', invalid:'引用格式无效', not_specified:'未提供'};
const list = value => Array.isArray(value) ? value : [];
const methodNeedsReview = row => row.method_scope_status !== 'consistent' || (row.method_ref_status !== 'resolved' && !(row.method_ref_status === 'not_specified' && list(row.applicable_method_pointers).length === 1));
const text = value => value == null ? '未提供' : typeof value === 'object' ? JSON.stringify(value) : String(value);
const versionKeys = ['task_id', 'candidate_id', 'study_ref', 'candidate_sha256', 'review_sha256'];
const sameVersion = (a,b) => Boolean(a && b && versionKeys.every(key => a[key] === b[key]));
const versionFor = context => ({task_id:context.taskId,candidate_id:context.candidateId,study_ref:context.studyRef,candidate_sha256:context.candidateHash,review_sha256:context.reviewHash});
const validHash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const invalidReport = () => Error('报告内容无法核对，请关闭后重新读取；不会自动重新生成。');
const reportVersions = new Set(['study-report-v3','study-report-v4','study-report-v5','study-report-v6']);
// Match Python str.strip(): its whitespace set includes NEL/control separators, not BOM.
const sourceLabel = value => typeof value === 'string' && /[^\t-\r\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/.test(value) && !value.includes('{{') && !value.includes('}}');

/** Validate before mounting any success content. Values only come from this report snapshot. */
export function studyReportContent(report) {
  if (!report || !reportVersions.has(report.prompt_version) || report.status !== 'draft' || report.scientific_validation !== false || !Array.isArray(report.structured_table) || !Array.isArray(report.citations) || !Array.isArray(report.gaps)) throw invalidReport();
  const facts = new Map(), references = new Map();
  for (const row of report.structured_table) {
    const id = row?.id || row?.fact_id;
    if (!id || facts.has(id) || row.id && row.fact_id && row.id !== row.fact_id || typeof row.rendered_text !== 'string') throw invalidReport();
    facts.set(id,row); references.set(id,{type:'fact',row});
  }
  for (const [type,rows] of [['citation',report.citations],['gap',report.gaps]]) for (const row of rows) {
    if (!row?.id || references.has(row.id)) throw invalidReport();
    references.set(row.id,{type,row});
  }
  if (report.output?.sections?.length !== sections.length) throw invalidReport();
  const content = sections.map(([id,title],index) => {
    const section = report.output.sections[index];
    if (section?.id !== id || !Array.isArray(section.paragraphs) || !section.paragraphs.length) throw invalidReport();
    const paragraphs = section.paragraphs.map(paragraph => {
      if (typeof paragraph?.text !== 'string' || !Object.hasOwn(kinds,paragraph.kind) || !Array.isArray(paragraph.refs) || paragraph.refs.some(ref => !references.has(ref))) throw invalidReport();
      const embeddedFacts = [];
      let rendered='',plain='',end=0;
      // Scan the original template only; inserted source values are never interpreted.
      for (const match of paragraph.text.matchAll(/\{\{(fact|label):([a-z0-9_-]+)(?::([a-z_]+))?\}\}/g)) {
        const [literal,kind,ref,field]=match;
        if (!facts.has(ref) || !paragraph.refs.includes(ref)) throw invalidReport();
        const fact=facts.get(ref);let replacement;
        if(kind==='fact'){
          if(field!==undefined)throw invalidReport();
          embeddedFacts.push(ref);replacement=fact.rendered_text;
        }else{
          const expected=Object.fromEntries(['name','material'].filter(key=>Object.hasOwn(fact,key)&&sourceLabel(fact[key])).map(key=>[key,`{{label:${ref}:${key}}}`]));
          const actual=fact.label_placeholders;
          if(!['study-report-v5','study-report-v6'].includes(report.prompt_version)||!actual||typeof actual!=='object'||Array.isArray(actual)
              ||Object.keys(actual).length!==Object.keys(expected).length
              ||Object.entries(expected).some(([key,value])=>!Object.hasOwn(actual,key)||actual[key]!==value)
              ||!Object.hasOwn(expected,field)||expected[field]!==literal)throw invalidReport();
          replacement=fact[field];
        }
        const prefix=paragraph.text.slice(end,match.index);plain+=prefix;rendered+=prefix+replacement;end=match.index+literal.length;
      }
      const tail=paragraph.text.slice(end);plain+=tail;rendered+=tail;
      if(plain.includes('{{')||plain.includes('}}')||(['study-report-v5','study-report-v6'].includes(report.prompt_version)&&/[{}]/.test(plain)))throw invalidReport();
      return {...paragraph,text:rendered,embeddedFacts};
    });
    return {id,title,paragraphs};
  });
  for (const row of facts.values()) if (list(row.evidence_refs).some(ref => references.get(ref)?.type !== 'citation')) throw invalidReport();
  return {sections:content,facts,references};
}

/** A report owns only its overlay, abort controller and polling timer. */
export function createDocumentStudyReport({layer,api,current,pollInterval=1500,schedule=setTimeout,cancel=clearTimeout}) {
  const doc=layer.ownerDocument;
  let session=null,disposed=false;
  const el=(tag,cls,value)=>{const node=doc.createElement(tag);if(cls)node.className=cls;if(value!==undefined)node.textContent=text(value);return node;};
  const button=(label,action)=>{const node=el('button','',label);node.type='button';node.addEventListener('click',()=>{if(!node.disabled)action();});return node;};
  const inside=(node,parent)=>{for(let p=node;p;p=p.parentNode)if(p===parent)return true;return false;};
  const contextChanged=(s,next)=>!next || s.context.taskId!==next.taskId || s.context.revision!==next.revision || s.context.generation!==next.generation || s.context.candidateHash!==next.candidateHash || s.context.reviewHash!==next.reviewHash;
  const alive=s=>!disposed&&session===s&&!s.controller.signal.aborted&&!contextChanged(s,current());
  const path=s=>`/api/tasks/${encodeURIComponent(s.context.taskId)}/document-reports`;
  const payload=s=>({candidate_id:s.context.candidateId,study_ref:s.context.studyRef});
  const query=s=>`candidate_id=${encodeURIComponent(s.context.candidateId)}&study_ref=${encodeURIComponent(s.context.studyRef)}`;
  const readablePath=s=>`${path(s)}?${query(s)}`;
  const revisionPath=(s,id)=>`${path(s)}/revisions/${encodeURIComponent(id)}`;
  const get=(s,url)=>api(url,{method:'GET',signal:s.controller.signal});
  const storage=row=>row?.storage_status||row?.status;
  const live=row=>['queued','running'].includes(storage(row));
  const stateLabels={not_generated:'尚未生成',queued:'正在排队',running:'正在生成',ready:'已生成',failed:'生成失败',cancelled:'已取消',outcome_unknown:'结果未知',stale:'已失效'};

  function close(restoreFocus=true) {
    if(!session)return;
    const previous=session;session=null;
    previous.controller.abort();if(previous.timer!==null)cancel(previous.timer);
    layer.hidden=true;layer.replaceChildren();
    for(const [node,wasInert] of previous.backgrounds)node.inert=wasInert;
    if(restoreFocus)previous.trigger?.focus({preventScroll:true});
  }
  function note(s,message) {
    s.notice.textContent=message;s.notice.hidden=!message;
    s.body.setAttribute('aria-busy','false');
  }
  function error(s,problem) {
    if(!alive(s))return;
    s.message=problem?.status===409?'报告输入或状态已变化，请刷新核对；未自动重发。':'暂时无法确认小报告状态，请刷新核对；未自动重发。';
    note(s,s.message);
  }
  function explainDrafts(s) {
    if(current()?.hasDrafts)s.body.append(el('p','dsr-warning','此报告依据服务端记录，不包含当前未保存的本地草稿。'));
  }
  function disclosure(parent,label,children) {
    const details=el('details','dsr-details'),summary=el('summary','',label);
    details.append(summary,...children);parent.append(details);return details;
  }
  function ownerLabel(report,pointer) {
    const ancestry=list(report.context).filter(row=>typeof row.source_pointer==='string'&&(pointer===row.source_pointer||pointer?.startsWith(row.source_pointer+'/'))).sort((a,b)=>a.source_pointer.length-b.source_pointer.length);
    return ancestry.map(row=>row.label||row.ref).filter(Boolean).join(' / ')||pointer||'归属未提供';
  }
  function gapNode(gap,className='dsr-warning') {
    const match=gap.reason==='missing_category'&&typeof gap.text==='string'
      ? gap.text.match(/^当前单元记录中没有可纳入的(composition|preparation|results)条目，不能据此判断原文是否报告。$/) : null;
    if(!match)return el('p',className,gap.text);
    const labels={composition:'样品组成',preparation:'制备过程',results:'测试结果'};
    return el('p',className,`当前单元记录中没有可纳入的${labels[match[1]]}条目，不能据此判断原文是否报告。`);
  }

  function renderRelations(report,body) {
    const rows=list(report.context),nodes=[];
    const describe=pointer=>ownerLabel(report,pointer);
    for(const row of rows){
      const article=el('div','dsr-relation');article.append(el('h4','',row.label||row.ref||'对象'),el('p','dsr-muted',row.source_pointer));
      if(Object.hasOwn(row,'method_ref_status')){
        article.append(el('p','',`引用方法：${text(row.method_ref)}；解析：${relations[row.method_ref_status]||'待核对'}；${relations[row.method_scope_status]||'适用范围待核对'}`));
        if(row.method_pointer)article.append(el('p','',`对应方法：${describe(row.method_pointer)}`));
        if(Object.hasOwn(row,'applicable_method_pointers'))article.append(el('p','',`明确适用方法：${list(row.applicable_method_pointers).map(pointer=>`${describe(pointer)}（${pointer}）`).join('；')||'无'}`));
        if(row.method_resolution_note)article.append(el('p',methodNeedsReview(row)?'dsr-warning':'dsr-muted',row.method_resolution_note));
      }
      if(Object.hasOwn(row,'applies_to_status')){
        article.append(el('p','',`方法适用关系：${relations[row.applies_to_status]||'待核对'}`));
        article.append(el('p','',`原始适用引用：${text(row.applies_to)}`));
        article.append(el('p','',`全部已解析对象：${list(row.applies_to_pointers).map(describe).join('；')||'无'}`));
        article.append(el('p','',`本研究单元内对象：${list(row.study_applies_to_pointers).map(describe).join('；')||'无'}`));
        for(const resolution of list(row.applies_to_resolution))article.append(el('p','dsr-muted',`${text(resolution.ref)} → ${resolution.pointer?describe(resolution.pointer):'未解析'}（${relations[resolution.status]||'待核对'}）`));
        if(row.applies_to_note)article.append(el('p',row.applies_to_status==='resolved'?'dsr-muted':'dsr-warning',row.applies_to_note));
      }
      nodes.push(article);
    }
    if(nodes.length)disclosure(body,`对象与方法关系（${nodes.length}）`,nodes);
  }
  function renderTable(report,content,body) {
    const area=el('section','dsr-structure');
    const groups=new Map();for(const fact of content.facts.values()){const owner=fact.owner_pointer||'';if(!groups.has(owner))groups.set(owner,[]);groups.get(owner).push(fact);}
    const types={measurement:'实测',calculation:'计算',interpretation:'解释',claim:'主张',description:'描述'},attributions={authors:'作者',cited_work:'引用研究',product:'产品资料'};
    for(const [owner,rows] of groups){
      area.append(el('h4','dsr-owner',ownerLabel(report,owner)));
      const scroll=el('div','dsr-table-scroll');scroll.tabIndex=0;scroll.setAttribute('aria-label',`${ownerLabel(report,owner)}的结构化记录`);
      const table=el('table','dsr-table'),head=el('thead'),header=el('tr'),tbody=el('tbody');
      for(const label of ['项目','原值','单位','比例基准','原始条件','审核状态'])header.append(el('th','',label));head.append(header);
      for(const fact of rows){
        const row=el('tr'),name=el('td','',fact.name||fact.material||'记录');
        name.append(el('small','dsr-muted',`${types[fact.fact_type]||text(fact.fact_type)} · ${attributions[fact.attribution]||text(fact.attribution)}`));
        if(fact.statement_status)name.append(el('small','dsr-muted',`陈述状态：${fact.statement_status}`));
        row.append(name,el('td','',text(fact.value)),el('td','',text(fact.unit)),el('td','',text(fact.basis)),el('td','',list(fact.conditions).join('；')||'未提供'));
        const audit=el('td','',reviews[fact.review_status]||'状态待核对');
        if(fact.review_note)audit.append(el('p','dsr-muted',fact.review_note));
        if(fact.scope)audit.append(el('p','dsr-muted',`陈述范围：${fact.scope==='cross_study'?'跨研究单元':'本研究单元'}；适用对象：${list(fact.applies_to_pointers).map(p=>ownerLabel(report,p)).join('；')||'未提供'}`));
        row.append(audit);tbody.append(row);
      }
      table.append(head,tbody);scroll.append(table);area.append(scroll);
    }
    if(!groups.size)area.append(el('p','dsr-muted','当前报告没有可纳入的结构化事实。'));
    body.append(area);
  }
  function reportRisks(report,content) {
    const risks=new Map();
    for(const fact of content.facts.values()){
      if(fact.review_status==='uncertain'||!Object.hasOwn(reviews,fact.review_status))risks.set(`fact:${fact.id||fact.fact_id}`,`${ownerLabel(report,fact.owner_pointer)} · ${fact.name||fact.material||'记录'}：${reviews[fact.review_status]||'审核状态待核对'}。`);
    }
    for(const row of list(report.context)){
      const notes=[];
      if(Object.hasOwn(row,'method_ref_status')&&methodNeedsReview(row))notes.push(row.method_resolution_note||`方法引用：${relations[row.method_ref_status]||'待核对'}；适用关系：${relations[row.method_scope_status]||'待核对'}。`);
      if(Object.hasOwn(row,'applies_to_status')&&row.applies_to_status!=='resolved')notes.push(row.applies_to_note||`方法适用关系：${relations[row.applies_to_status]||'待核对'}。`);
      if(notes.length)risks.set(`method:${row.source_pointer}`,`${ownerLabel(report,row.source_pointer)}：${[...new Set(notes)].join(' ')}`);
    }
    return risks;
  }
  function renderReport(s,report) {
    let content;try{content=studyReportContent(report);}catch{s.body.replaceChildren(el('p','dsr-status',invalidReport().message));return;}
    s.body.replaceChildren();s.body.setAttribute('aria-busy','false');
    s.body.append(el('p','dsr-draft','待复核草稿 · 尚未独立科学核验'),el('h2','dsr-study-title',report.study_label||'当前研究单元'));
    explainDrafts(s);
    const risks=reportRisks(report,content),shownRisks=new Set(),shownConditions=new Set();
    const paragraphs=content.sections.flatMap(section=>section.paragraphs),visibleCopy=new Set(paragraphs.map(paragraph=>paragraph.text));
    let gapSection;
    const showRisk=(parent,key)=>{
      if(shownRisks.has(key)||!risks.has(key))return;shownRisks.add(key);
      const message=risks.get(key);if(visibleCopy.has(message))return;visibleCopy.add(message);
      parent.append(el('p','dsr-local-risk',message));
    };
    for(const section of content.sections){
      const block=el('section','dsr-section');block.append(el('h3','dsr-section-title',section.title));
      if(section.id==='gaps_use_scope')gapSection=block;
      for(const paragraph of section.paragraphs){
        const item=el('div','dsr-paragraph');
        if(paragraph.kind==='author_interpretation')item.append(el('span','dsr-kind dsr-kind-author_interpretation',kinds.author_interpretation));
        item.append(el('p','dsr-prose',paragraph.text));
        for(const ref of paragraph.refs){
          const entry=content.references.get(ref);
          if(entry.type==='fact'){
            const fact=entry.row,missing=[];
            for(const condition of list(fact.conditions)){
              const key=JSON.stringify([ref,condition]);if(shownConditions.has(key))continue;
              // Only this fact's own rendered placeholder establishes that its condition is already visible.
              if(!paragraph.embeddedFacts.includes(ref)||!fact.rendered_text.includes(text(condition)))missing.push(text(condition));
              shownConditions.add(key);
            }
            if(missing.length)item.append(el('p','dsr-condition',`${ownerLabel(report,fact.owner_pointer)} · ${fact.name||fact.material||'记录'}的原始条件：${missing.join('；')}`));
            showRisk(item,`fact:${ref}`);showRisk(item,`method:${fact.owner_pointer}`);
          }
        }
        block.append(item);
      }
      s.body.append(block);
    }
    for(const gap of report.gaps){
      // The fixed scope message is already covered by the draft notice and missing-records note.
      if(gap.id==='gap_scope'&&gap.reason==='scope'&&gap.text==='仅依据当前候选与审核记录整理；记录缺口不代表原文未报告；未经独立科学验证。')continue;
      if(!visibleCopy.has(gap.text)){gapSection.append(gapNode(gap,'dsr-record-gap'));visibleCopy.add(gap.text);}
    }
    for(const key of risks.keys())showRisk(gapSection,key);
    if(![...visibleCopy].some(value=>value.includes('记录缺口不代表原文未报告')))gapSection.append(el('p','dsr-muted','记录缺口不代表原文未报告。'));
    const records=disclosure(s.body,`完整结构化记录（${content.facts.size}）`,[]);records.className='dsr-details dsr-appendix';
    renderTable(report,content,records);renderRelations(report,records);
    const date=formatWebsiteTime(report.generated_at,'未提供');
    const metadata=[el('p','dsr-muted',`生成时间：${date}；模型：${text(report.model)}；提示版本：${text(report.prompt_version)}`)];
    const counts=report.input_summary;
    if(counts)metadata.push(el('p','dsr-muted',`输入记录：${text(counts.fact_count)} 项；排除记录：${text(counts.excluded_count)} 项；相关问题：${text(counts.issue_count)} 项。`));
    const trace=disclosure(s.body,'生成与追溯信息',metadata);trace.className='dsr-details dsr-appendix';
  }
  function checked(s,row) {
    const expected=versionFor(s.context),identities=[row?.current_input?.identity,row?.input_version].filter(Boolean);
    if(!row||!identities.length||identities.some(value=>['task_id','candidate_id','study_ref'].some(key=>value[key]!==expected[key])))throw invalidReport();
    if(row.report&&(!sameVersion(row.report.input_version,row.input_version)||(row.input_sha256&&row.report.input_sha256!==row.input_sha256)))throw invalidReport();
    return row;
  }
  function retryable(row,currentInput) {
    const execution=row?.legacy?row.legacy_job:row;
    return ['failed','cancelled'].includes(storage(row))&&row.input_sha256===currentInput?.sha256
      &&(execution?.request_started===false||['settled','released','not_reserved'].includes(execution?.budget_receipt?.state));
  }
  function render(s) {
    if(!alive(s))return;
    const row=s.viewId?s.viewRow:(s.work||s.currentRow),input=s.currentRow?.current_input;
    s.actions.replaceChildren();
    const add=(label,fn)=>{const b=button(label,fn);b.disabled=s.submitting;s.actions.append(b);return b};
    add('刷新状态',()=>read(s));
    if(s.viewId)add('返回当前报告',()=>{s.viewEpoch++;s.viewId=null;s.viewRow=null;render(s);read(s)});
    let message=row?stateLabels[row.status]||'状态待确认':'正在读取小报告…';
    if(row?.stale)message=row.stale_reason==='report_rules_changed'?'报告规则已更新；历史记录已保留。':row.stale_reason==='superseded'?'正在查看历史修订，不代表当前报告。':row.stale_reason==='current_input_unavailable'?'当前输入不可用；历史记录仍可阅读。':'报告输入记录已变化；历史记录已保留。';
    if(storage(row)==='outcome_unknown')message='本次结果未知，可能已产生费用。仅可读取状态，不会重发生成。';
    if(s.viewId)message='历史修订 · '+message;
    if(current()?.hasDrafts)message+=' 请先保存当前修改或恢复本地草稿，再生成。';
    if(current()?.busy)message+=' 正在保存，请稍后再生成。';
    if(s.capabilities?.model_available!==true)message+=' 报告模型尚未配置或不可用；现有报告仍可阅读。';
    note(s,[message,s.message].filter(Boolean).join(' '));
    const content=row?.report||(!s.viewId?s.currentRow?.report:null);
    // Revisions are immutable. Poll status without replacing the user's open
    // appendix, focus target or scroll position inside an unchanged report.
    const contentKey=content?JSON.stringify([s.viewId,content.revision_id||content.report_id,content.input_sha256,content.prompt_version]):null;
    if(contentKey!==s.contentKey){s.contentKey=contentKey;if(content)renderReport({...s,body:s.content},content);else s.content.replaceChildren();}
    if(!s.viewId&&row){
      const readyInput=input?.available===true&&validHash(input.sha256);
      const mayCreate=readyInput&&s.capabilities?.model_available===true&&!current()?.busy&&!current()?.hasDrafts&&!s.uncertain&&!s.loading&&!live(row)&&storage(row)!=='outcome_unknown';
      if(mayCreate&&retryable(row,input))add('重试生成',()=>submit(s,'create',row.revision_id));
      else if(mayCreate&&(row.status==='not_generated'||row.stale&&row.stale_reason!=='superseded'))add(row.status==='not_generated'?'生成小报告':'生成新修订',()=>submit(s,'create'));
      if(live(row)&&row.revision_id){
        if(storage(row)==='running'&&validHash(row.response_sha256))add('继续保存',()=>submit(s,'persist',row.revision_id));
        add('取消生成',()=>submit(s,'cancel',row.revision_id));
      }
    }
  }
  function poll(s) {
    if(s.timer!==null)cancel(s.timer);s.timer=null;
    if(alive(s)&&live(s.work||s.currentRow))s.timer=schedule(()=>{s.timer=null;read(s)},pollInterval);
  }
  async function read(s,initial=false) {
    if(!alive(s))return;
    const epoch=++s.readEpoch;s.loading=true;
    if(s.timer!==null)cancel(s.timer);s.timer=null;
    try{
      const [row,capabilities]=await Promise.all([get(s,readablePath(s)),initial?get(s,path(s)+'/capabilities').catch(()=>({model_available:false})):s.capabilities]);
      if(!alive(s)||epoch!==s.readEpoch)return;
      s.capabilities=capabilities;s.currentRow=checked(s,row);
      const workId=row.pending_revision_id||s.trackedId;
      const work=workId?(workId===row.revision_id?row:await get(s,revisionPath(s,workId)+'?'+query(s))):null;
      if(!alive(s)||epoch!==s.readEpoch)return;
      s.work=work?checked(s,work):null;
      if(s.work?.is_current){s.currentRow=s.work;s.work=null;s.trackedId=null;}
      if(s.uncertain&&[s.currentRow,s.work].some(value=>value?.revision_id&&value.input_sha256===s.pendingInput))s.uncertain=false;
      s.loading=false;render(s);poll(s);
    }catch(problem){if(alive(s)&&epoch===s.readEpoch){s.loading=false;error(s,problem);render(s);poll(s)}}
  }
  async function submit(s,action,revisionId=null) {
    if(!alive(s)||s.submitting||s.loading)return;
    const input=s.currentRow?.current_input;
    if(action==='create'&&(s.uncertain||s.capabilities?.model_available!==true||!input?.available||!validHash(input.sha256)||current()?.busy||current()?.hasDrafts))return;
    s.submitting=true;s.message='';s.pendingInput=input?.sha256;
    const body=action==='create'?{...payload(s),request_id:globalThis.crypto.randomUUID(),expected_input_sha256:input.sha256,...(revisionId?{retry_of:revisionId}:{})}:payload(s);
    render(s);
    try{
      const result=await api(action==='create'?path(s)+'/revisions':revisionPath(s,revisionId)+'/'+action,{method:'POST',json:body,signal:s.controller.signal});
      if(!alive(s))return;
      s.work=checked(s,result);s.trackedId=result.revision_id;s.uncertain=false;
    }catch(problem){
      if(!alive(s))return;
      if(action==='create'&&(!problem?.status||problem.status>=500||problem.status===408))s.uncertain=true;
      error(s,problem);
    }finally{
      if(alive(s)){s.submitting=false;await read(s);if(s.history.open)loadHistory(s,false)}
    }
  }
  async function selectRevision(s,id) {
    if(!alive(s))return;
    const epoch=++s.viewEpoch;s.viewId=id;s.viewRow=null;render(s);
    try{const row=await get(s,revisionPath(s,id)+'?'+query(s));if(!alive(s)||epoch!==s.viewEpoch)return;s.viewRow=checked(s,row);render(s)}catch(problem){if(alive(s)&&epoch===s.viewEpoch)error(s,problem)}
  }
  function historyRows(s) {
    s.historyBody.replaceChildren();
    for(const row of s.historyRows){
      const time=formatWebsiteTime(row.created_at);
      const label=row.is_current?'当前':row.stale?'失效历史':'历史';
      s.historyBody.append(button(`查看修订 · ${time} · ${label} · ${stateLabels[row.status]||'状态待确认'}`,()=>selectRevision(s,row.revision_id)));
    }
    if(!s.historyRows.length)s.historyBody.append(el('p','dsr-muted','尚无历史修订。'));
    if(s.historyNext!==null)s.historyBody.append(button('更多历史',()=>loadHistory(s,true)));
  }
  async function loadHistory(s,append=false) {
    if(!alive(s)||s.historyLoading&&append)return;
    const epoch=++s.historyEpoch,offset=append?s.historyNext:0;
    if(offset===null)return;s.historyLoading=true;
    try{
      const result=await get(s,`${path(s)}/revisions?${query(s)}&offset=${offset}&limit=20`);
      if(!alive(s)||epoch!==s.historyEpoch)return;
      if(!Array.isArray(result.revisions)||result.has_more&&(!Number.isInteger(result.next_offset)||result.next_offset<=offset))throw invalidReport();
      const rows=result.revisions.map(row=>checked(s,row));
      s.historyRows=[...new Map([...(append?s.historyRows:[]),...rows].map(row=>[row.revision_id,row])).values()];
      s.historyNext=result.has_more?result.next_offset:null;s.historyLoaded=true;historyRows(s);
    }catch(problem){if(alive(s)&&epoch===s.historyEpoch){s.historyBody.append(el('p','dsr-warning','历史暂时无法读取。'),button('重读历史',()=>loadHistory(s,false)))}}
    finally{if(alive(s)&&epoch===s.historyEpoch)s.historyLoading=false}
  }
  function focusTargets(s) {
    const nodes=[s.close,s.body];
    const visit=node=>{
      if(node.hidden)return;
      const tag=node.tagName.toLowerCase();
      if(!node.disabled&&(['button','summary'].includes(tag)||node.tabIndex===0))nodes.push(node);
      if(tag==='details'&&!node.open){if(node.children[0])visit(node.children[0]);return;}
      for(const child of node.children)visit(child);
    };
    for(const child of s.body.children)visit(child);return nodes;
  }
  function open(context,trigger) {
    if(disposed||context.busy||!context.studyRef||!validHash(context.candidateHash)||!validHash(context.reviewHash))return;
    const key=JSON.stringify([context.taskId,context.candidateId,context.studyRef]);
    if(session?.key===key)return;close(false);
    const dialog=el('section','dsr-dialog'),header=el('header','dsr-header'),body=el('div','dsr-body');
    dialog.setAttribute('role','dialog');dialog.setAttribute('aria-modal','true');dialog.setAttribute('aria-label','研究单元小报告');
    const closer=button('关闭',()=>close());closer.setAttribute('aria-label','关闭小报告');header.append(el('h2','','研究单元小报告'),closer);
    body.tabIndex=0;body.setAttribute('aria-label','小报告内容');body.setAttribute('aria-live','polite');dialog.append(header,body);
    const backgrounds=[];for(let node=layer;node?.parentNode;node=node.parentNode){for(const sibling of node.parentNode.children)if(sibling!==node){backgrounds.push([sibling,Boolean(sibling.inert)]);sibling.inert=true;}if(node.parentNode===doc.body)break;}
    const notice=el('p','dsr-status'),actions=el('div','dsr-actions'),content=el('div','dsr-content'),history=el('details','dsr-details dsr-history'),historyBody=el('div','dsr-history-list');
    notice.setAttribute('role','status');history.append(el('summary','','历史修订'),historyBody);body.append(notice,actions,content,history);
    const s={key,context:{...context},trigger,dialog,body,notice,actions,content,history,historyBody,close:closer,backgrounds,controller:new AbortController(),timer:null,loading:false,submitting:false,readEpoch:0,viewEpoch:0,historyEpoch:0,historyRows:[],historyNext:null,historyLoaded:false,historyLoading:false,currentRow:null,work:null,trackedId:null,viewId:null,viewRow:null,contentKey:null,capabilities:{model_available:false},message:'',uncertain:false};session=s;
    history.addEventListener('toggle',()=>{if(history.open&&!s.historyLoaded&&!s.historyLoading)loadHistory(s)});
    dialog.addEventListener('keydown',event=>{
      if(event.key==='Escape'){event.preventDefault();event.stopPropagation();close();}
      else if(event.key==='Tab'){event.preventDefault();const targets=focusTargets(s),index=targets.indexOf(doc.activeElement);targets[(index+(event.shiftKey?-1:1)+targets.length)%targets.length]?.focus({preventScroll:true});}
    });
    layer.replaceChildren(dialog);layer.hidden=false;closer.focus({preventScroll:true});read(s,true);
  }
  return {open,close,get isOpen(){return Boolean(session);},contains:node=>session?inside(node,session.dialog):false,sync(next=current()){if(session&&contextChanged(session,next))close(false);},dispose(){disposed=true;close(false);}};
}
