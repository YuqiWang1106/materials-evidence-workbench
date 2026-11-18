import {pageTitle} from './page-title.js';
import {buildDocumentScopes, categoryForDocumentRow, documentRowsPage, DOCUMENT_CATEGORIES,
  evidenceRegion, reviewState} from './document-result-logic.mjs';
import {rowLabel, valueText, titleForResult, saveDecision, saveReviewedResult} from './result-logic.js';

const idPath=value=>encodeURIComponent(value);
const PAGE_SIZE=10;
const STAGES={needs_review:'候选 · 待核对',saved:'已归档',superseded:'历史版本',
  publishing:'正在保存',write_unknown:'保存状态待核对',failed:'整理未完成'};

/** Paper/patent candidate display. This view never infers a scientific relationship. */
export function renderDocumentResult({root,task,api,capabilities={},onRefresh}) {
  const doc=root.ownerDocument, controller=new AbortController();
  let disposed=false,view=null,rows=[],scopes=[],scopeKey=null,category=null,page=0,query='';
  let blockedRefs=new Set(),reviewable=false,selected=null,selectedEvidence=null,selectedReviews=[];
  let selectedEvidenceRef=null,currentLocation=null,currentPage=null,pageReady=false,sourceToken=0,busy=false,notice='';
  let seenPages=new Set(),requiredPages=new Map();
  const node=(tag,className,text)=>{const n=doc.createElement(tag);if(className)n.className=className;
    if(text!==undefined)n.textContent=String(text);return n;};
  const button=(label,action,className='document-button')=>{const n=node('button',className,label);
    n.type='button';n.addEventListener('click',action);return n;};
  const path=`/api/tasks/${idPath(task.task_id)}`;
  const get=async (url,options={})=>{
    if(disposed)throw Error('视图已关闭');
    const value=await api(url,{...options,signal:controller.signal});
    if(disposed)throw Error('视图已关闭');
    return value;
  };
  const shell=node('section','document-result-page');
  const heading=node('header','document-result-heading');
  const headingText=node('div');const headingTitle=node('h2','',titleForResult(task));headingText.append(pageTitle(headingTitle,"资料整理"));
  const status=node('span','document-result-status',STAGES[task.state]||'候选');
  const summary=node('span','document-result-summary');
  const controls=node('div','document-result-controls');
  heading.append(headingText,status,summary,controls);
  const message=node('p','document-result-message');message.setAttribute('role','status');message.hidden=true;
  const workspace=node('div','document-result-workspace');
  const sidebar=node('nav','document-sample-sidebar');sidebar.setAttribute('aria-label','样品或实施例');
  const content=node('section','document-fact-content');
  workspace.append(sidebar,content);
  const source=node('section','document-source-view');source.hidden=true;source.setAttribute('aria-label','核对出处');
  shell.append(heading,message,workspace,source);root.replaceChildren(shell);

  const tell=(text,tone='')=>{notice=text;message.textContent=text;message.className=`document-result-message ${tone}`.trim();message.hidden=!text;};
  const scope=()=>scopes.find(item=>item.key===scopeKey);
  const activeCategories=()=>DOCUMENT_CATEGORIES.filter(([key])=>scope()?.rows.some(row=>categoryForDocumentRow(row)===key));

  function drawHeader() {
    status.textContent=task.state==='saved' && blockedRefs.size?'已归档 · 有疑点':STAGES[task.state]||'候选';
    summary.textContent=view?`${rows.length} 条记录 · ${view.issue_total||0} 项待处理`:'';
    controls.replaceChildren();
    if(!view)return;
    const decision=saveDecision(task,view,capabilities);
    if(decision.action && (decision.action==='reconcile' || !blockedRefs.size)){
      const save=button(busy?'正在处理…':decision.label,saveCurrent,'document-button primary');
      save.disabled=busy;controls.append(save);
    } else if(blockedRefs.size)controls.append(node('span','document-save-blocked',
      task.state==='saved'?'有疑点，受影响结果已停用':'已有疑点或错误，需重提取后保存'));
    const primaryAsset=task.asset_ids?.[0];
    if(primaryAsset){const link=node('a','document-download','下载原件');
      link.href=`/api/assets/${idPath(primaryAsset)}/download`;link.download='';controls.append(link);}
  }

  function drawSidebar() {
    sidebar.replaceChildren(node('h3','', '选择样品 / 实施例'));
    if(!scopes.length){sidebar.append(node('p','document-empty','尚无可展示的记录'));return;}
    for(const item of scopes){
      const pick=button(item.label,()=>{
        scopeKey=item.key;category=activeCategories()[0]?.[0]||null;query='';page=0;drawSidebar();drawContent();
      },'document-scope-button');
      pick.setAttribute('aria-current',String(item.key===scopeKey));
      sidebar.append(pick);
    }
  }

  function drawContent() {
    content.replaceChildren();
    const current=scope();
    if(!current){content.append(node('p','document-empty','这份资料还没有可展示的样品记录。'));return;}
    const tabs=node('div','document-category-tabs');tabs.setAttribute('role','tablist');
    for(const [key,label] of activeCategories()){
      const tab=button(label,()=>{category=key;page=0;query='';drawContent();},'document-tab');
      tab.setAttribute('role','tab');tab.setAttribute('aria-selected',String(key===category));tabs.append(tab);
    }
    content.append(tabs);
    const toolbar=node('div','document-fact-toolbar');
    const search=node('input','document-search');search.type='search';search.placeholder='搜索当前栏目';
    search.setAttribute('aria-label','搜索当前栏目');search.value=query;
    search.addEventListener('input',()=>{query=search.value;page=0;drawFactList();});
    toolbar.append(search);content.append(toolbar);
    const list=node('div','document-fact-list');const pager=node('div','document-fact-pager');
    content.append(list,pager);
    function drawFactList(){
      list.replaceChildren();pager.replaceChildren();
      const result=documentRowsPage(current,category,query,page,PAGE_SIZE);
      if(!result.rows.length){list.append(node('p','document-empty','当前栏目没有匹配的记录。'));return;}
      const label=node('p','document-result-count',`${result.total} 条 · 第 ${result.page+1}/${result.pages} 页`);
      list.append(label);
      for(const row of result.rows){
        const card=node('article','document-fact-card');
        const top=node('div','document-fact-top');
        top.append(node('strong','document-fact-name',rowLabel(row)),
          node('span','document-fact-value',row.series_preview?`曲线 ${row.point_count??''} 点`:valueText(row.value,160)+(row.unit?` ${row.unit}`:'')));
        const detail=node('div','document-fact-meta');
        const owner=(row.contexts||[]).find(context=>`sample:${context.sample_ref}`===scopeKey);
        if(owner?.test_label)detail.append(node('span','',owner.test_label));
        if(row.basis)detail.append(node('span','',valueText(row.basis,100)));
        if(blockedRefs.has(row.id))detail.append(node('span','document-needs-check','已标记疑点或错误'));
        else if(!row.usable || (view.issues||[]).some(issue=>issue.target_refs?.includes(row.id)))
          detail.append(node('span','document-needs-check','待核对'));
        if((row.contexts||[]).filter(c=>c.sample_ref).length>1)detail.append(node('span','', '适用于多个样品'));
        if(row.contexts_truncated)detail.append(node('span','document-needs-check','还有未显示的归属'));
        const sourceButton=button('查看原文',()=>openSource(row),'document-source-button');
        card.append(top,detail,sourceButton);list.append(card);
      }
      if(result.pages>1){
        const back=button('上一页',()=>{page--;drawFactList();},'document-page-button');back.disabled=page===0;
        const next=button('下一页',()=>{page++;drawFactList();},'document-page-button');next.disabled=page>=result.pages-1;
        pager.append(back,next);
      }
    }
    drawFactList();
  }

  async function saveCurrent(){
    if(busy || (blockedRefs.size && saveDecision(task,view,capabilities).action!=='reconcile'))return;
    busy=true;drawHeader();
    try{
      const result=await saveReviewedResult({task,view,capabilities,api:get});
      if(result.kind==='submitted'){
        tell('已提交保存；正在核对写入状态。','success');
        if(onRefresh)await onRefresh(result.task);
      }else tell(result.reason||'结果已变化，请刷新后重新核对。','warn');
    }catch(error){tell(error.message||'保存未完成，请稍后重试。','error');}
    finally{busy=false;if(!disposed)drawHeader();}
  }

  const sourceTop=node('div','document-source-head');
  const sourceBody=node('div','document-source-layout');
  const pagePanel=node('div','document-page-panel');
  const detailPanel=node('div','document-detail-panel');
  sourceBody.append(pagePanel,detailPanel);source.append(sourceTop,sourceBody);

  function openSource(row){
    selected=row;selectedEvidence=null;selectedReviews=[];currentLocation=null;currentPage=null;pageReady=false;
    selectedEvidenceRef=row.evidence_refs?.[0]||null;seenPages=new Set();requiredPages=new Map();
    workspace.hidden=true;source.hidden=false;
    sourceTop.replaceChildren(button('← 返回结果',()=>{sourceToken++;source.hidden=true;workspace.hidden=false;},'document-back'),
      node('h3','','核对出处'));
    detailPanel.replaceChildren();pagePanel.replaceChildren(node('p','document-empty','正在打开原文…'));
    loadSource(row,++sourceToken,selectedEvidenceRef);
  }

  async function loadSource(row,token,evidenceRef){
    try{
      const target=get(`${path}/document-reviews?target_ref=${idPath(row.id)}`);
      if(evidenceRef){
        const [location,evidence,reviews]=await Promise.all([
          get(`${path}/evidence/${idPath(evidenceRef)}/location`),
          get(`${path}/evidence/${idPath(evidenceRef)}`),target]);
        if(disposed||token!==sourceToken)return;
        if(reviews.package_sha256!==view.package_sha256)throw Error('结果版本已变化，请刷新页面后核对。');
        selectedEvidence=evidence;currentLocation=location;selectedReviews=reviews.reviews||[];
        selectedEvidenceRef=evidenceRef;
        requiredPages.set(evidenceRef,(location.anchor?.page_numbers||[]).filter(n=>Number.isInteger(n)&&n>0));
        reviewable=Boolean(reviews.reviewable) && !task.read_only && task.state!=='superseded';
        if(reviews.blocked)blockedRefs.add(row.id);
        renderSource();
      }else{
        const reviews=await target;if(disposed||token!==sourceToken)return;
        if(reviews.package_sha256!==view.package_sha256)throw Error('结果版本已变化，请刷新页面后核对。');
        selectedReviews=reviews.reviews||[];reviewable=Boolean(reviews.reviewable)&&!task.read_only&&task.state!=='superseded';
        renderSource();
      }
    }catch(error){if(!disposed&&token===sourceToken){pagePanel.replaceChildren(node('p','document-empty','原文暂时无法读取。'));
      detailPanel.replaceChildren(node('p','document-result-message error',error.message||'请稍后重试。'));}}
  }

  function renderSource(){
    const row=selected;
    pagePanel.replaceChildren();detailPanel.replaceChildren();pageReady=false;
    const location=currentLocation,anchor=location?.anchor||{};
    const pages=(anchor.page_numbers||[]).filter(n=>Number.isInteger(n)&&n>0);
    if((row.evidence_refs||[]).length>1){
      const choices=node('div','document-evidence-choices');
      row.evidence_refs.forEach((ref,index)=>{const pick=button(`依据 ${index+1}`,()=>{
        if(ref===selectedEvidenceRef)return;
        pagePanel.replaceChildren(node('p','document-empty','正在打开原文…'));
        detailPanel.replaceChildren();pageReady=false;currentLocation=null;selectedEvidence=null;
        loadSource(row,++sourceToken,ref);
      },'document-page-choice');
        pick.setAttribute('aria-pressed',String(ref===selectedEvidenceRef));choices.append(pick);});
      pagePanel.append(choices);
    }
    if(pages.length){
      const pageHead=node('div','document-page-head');
      const pageTitle=node('strong','',`${location.asset?.original_name||'原件'} · 第 ${pages[0]} 页`);
      pageHead.append(pageTitle);
      const picks=node('div','document-page-choices');pageHead.append(picks);pagePanel.append(pageHead);
      const imageWrap=node('div','document-page-image-wrap');pagePanel.append(imageWrap);
      const pageNote=node('p','document-page-note');pagePanel.append(pageNote);
      let pageGeneration=0;
      const showPage=(number)=>{
        currentPage=number;pageReady=false;picks.replaceChildren();imageWrap.replaceChildren();
        const evidenceAtRender=selectedEvidenceRef, tokenAtRender=sourceToken, generation=++pageGeneration;
        pageTitle.textContent=`${location.asset?.original_name||'原件'} · 第 ${number} 页`;
        for(const p of pages){const pick=button(`第 ${p} 页`,()=>showPage(p),'document-page-choice');
          pick.setAttribute('aria-pressed',String(p===number));picks.append(pick);}
        const image=node('img','document-page-image');image.alt=`原件第 ${number} 页`;
        const region=evidenceRegion(anchor,number);
        if(region){const highlight=node('div','document-source-region');highlight.setAttribute('aria-hidden','true');
          for(const [key,value] of Object.entries(region))highlight.style[key]=`${value}%`;
          imageWrap.append(image,highlight);pageNote.textContent='已定位到原文区域，请对照图中内容核对。';
        }else{imageWrap.append(image);pageNote.textContent='只定位到原件页，请在页面中核对原文。';}
        const stillCurrent=()=>currentPage===number && selectedEvidenceRef===evidenceAtRender &&
          sourceToken===tokenAtRender && pageGeneration===generation;
        image.addEventListener('load',()=>{if(stillCurrent()){pageReady=true;
          seenPages.add(`${evidenceAtRender}:${number}`);updateConfirmButton();}});
        image.addEventListener('error',()=>{if(stillCurrent()){pageReady=false;pageNote.textContent='原件页暂时无法预览，请下载原件核对。';updateConfirmButton();}});
        image.src=`${path}/evidence/${idPath(location.evidence_id)}/page-image?page=${number}`;
        updateConfirmButton();
      };
      showPage(pages[0]);
    }else pagePanel.append(node('p','document-empty','这条记录没有可定位的原件页。'));
    detailPanel.append(node('div','document-detail-label','抽取结果'),
      node('h4','',rowLabel(row)),
      node('div','document-detail-value',valueText(row.value,Infinity)+(row.unit?` ${row.unit}`:'')));
    if(row.basis)detailPanel.append(node('p','document-detail-basis',valueText(row.basis,Infinity)));
    const quote=typeof selectedEvidence?.content==='string'?selectedEvidence.content:selectedEvidence?.content?.text;
    if(quote)detailPanel.append(node('div','document-detail-label','原文摘录'),
      node('blockquote','document-detail-quote',valueText(quote,Infinity)));
    else detailPanel.append(node('p','document-empty','这条记录没有可展示的原文摘录。'));
    if(location?.registered_asset_id){const download=node('a','document-download','下载完整原件');
      download.href=`/api/assets/${idPath(location.registered_asset_id)}/download`;download.download='';detailPanel.append(download);}
    const reviews=node('div','document-review-status');detailPanel.append(reviews);
    const currentState=reviewState(selectedReviews,blockedRefs.has(row.id));
    if(currentState==='blocked')reviews.textContent='此条已标记为错误或疑点；确定事实使用已阻断。';
    else if(currentState==='confirmed')reviews.textContent='已核对原文一致；尚非科学真实性验证。';
    else reviews.textContent='尚未由使用者核对原文。';
    drawReviewActions();
  }

  let confirmButton=null;
  function allEvidenceSeen(){
    return Boolean(selected?.evidence_refs?.length) && selected.evidence_refs.every(ref=>{
      const pages=requiredPages.get(ref);
      return pages?.length && pages.every(number=>seenPages.has(`${ref}:${number}`));
    });
  }
  function updateConfirmButton(){if(confirmButton)confirmButton.disabled=!pageReady || !allEvidenceSeen() || busy || blockedRefs.has(selected?.id);}
  function drawReviewActions(){
    confirmButton=null;
    if(!reviewable){detailPanel.append(node('p','document-empty','此版本只读；来源核对需要重新整理后进行。'));return;}
    const actions=node('div','document-review-actions');
    const actionButtons=node('div','document-review-buttons');
    if(!blockedRefs.has(selected.id)){
      confirmButton=button('原文一致',()=>submitReview('confirmed_source','已对照所显示原件页确认该提取内容与原文一致'), 'document-button primary');
      updateConfirmButton();actionButtons.append(confirmButton);
    }
    actionButtons.append(button('修正提取',()=>drawReviewNote(actions,'rejected'),'document-button'),
      button('保留疑点',()=>drawReviewNote(actions,'uncertain'),'document-button'));
    actions.append(actionButtons);
    detailPanel.append(actions);
  }

  function drawReviewNote(actions,decision){
    const actionButtons=actions.children[0];
    const form=node('div','document-review-form');
    form.append(node('p','',decision==='rejected'
      ?'请写明错误和正确原文。此操作停用当前内容，不会直接改写原值；重提取后才有修正版。'
      :'请写明无法确定的原因。此条暂不作为确定事实使用。'));
    const note=node('textarea','document-review-note');note.setAttribute('aria-label',decision==='rejected'?'错误与修正意见':'疑点说明');
    note.rows=3;form.append(note);
    const save=button(decision==='rejected'?'记录修正意见':'保留疑点',()=>{
      if(!note.value?.trim()){tell('请先写明原因。','warn');return;}
      submitReview(decision,note.value.trim());
    },'document-button primary');
    form.append(save);actions.replaceChildren(actionButtons,form);note.focus?.();
  }

  async function submitReview(decision,note){
    if(busy||!selected||!reviewable||!view)return;
    if(decision==='confirmed_source' && !allEvidenceSeen()){
      tell('请先逐一打开并核对这条记录的全部原文依据。','warn');return;
    }
    busy=true;updateConfirmButton();
    try{
      await get(`${path}/document-reviews`,{method:'POST',json:{expected_package_sha256:view.package_sha256,
        target_refs:[selected.id],decision,note,operator:'本地使用者'}});
      selectedReviews.push({decision,note});
      if(decision!=='confirmed_source')blockedRefs.add(selected.id);
      tell(decision==='rejected'?'更正意见已记录，此条已停用；尚未生成修正版。':
        decision==='uncertain'?'疑点已保留，此条暂不作为确定事实使用。':'已记录原文一致；这不代表科学验证。','success');
      drawHeader();drawContent();renderSource();
    }catch(error){tell(error.status===409?'结果版本已变化，请刷新后重新核对。':error.message||'核对未保存。','error');}
    finally{busy=false;updateConfirmButton();}
  }

  async function load(){
    try{
      const first=await get(`${path}/result-view?offset=0&limit=100`);
      if(first.package_sha256!==task.package_sha256)throw Error('结果版本已变化，请刷新页面。');
      view=first;rows=[...first.rows];
      let offset=first.offset+first.rows.length;
      while(offset<first.total){
        const next=await get(`${path}/result-view?offset=${offset}&limit=100`);
        if(next.package_sha256!==first.package_sha256 || next.offset!==offset || !next.rows?.length)
          throw Error('结果分页已变化，请刷新页面。');
        rows.push(...next.rows);offset+=next.rows.length;
      }
      const reviews=await get(`${path}/document-reviews`);
      if(reviews.package_sha256!==first.package_sha256)throw Error('核对版本已变化，请刷新页面。');
      blockedRefs=new Set(reviews.blocked_target_refs||[]);reviewable=Boolean(reviews.reviewable);
      headingTitle.textContent=titleForResult(task,view);
      scopes=buildDocumentScopes(rows);scopeKey=scopes[0]?.key||null;
      category=activeCategories()[0]?.[0]||null;
      drawHeader();drawSidebar();drawContent();
    }catch(error){if(!disposed){tell(error.message||'结果暂时无法读取。','error');
      workspace.replaceChildren(node('p','document-empty','请刷新页面后重试。'));}}
  }
  content.append(node('p','document-empty','正在读取抽取结果…'));
  load();
  return ()=>{disposed=true;sourceToken++;controller.abort();};
}
