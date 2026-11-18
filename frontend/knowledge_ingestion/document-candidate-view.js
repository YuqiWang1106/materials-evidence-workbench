// @ts-check
import {pageTitle} from './page-title.js';
import {mountStudyUnitWiki} from './study-unit-wiki.js';
import {createDocumentStudyReport} from './document-study-report.mjs';
import {saveDocumentLatest, savedDocumentTime, readDocumentCandidates} from './document-save-latest.mjs';
import {localizedGroups, presentFact} from './document-candidate-display.mjs';
import {evidenceRegions} from './document-result-logic.mjs';
import {array, object, text, excluded, issuesAcknowledged, summary, displayFact, buildReviewModel, pagesFor} from './document-candidate-logic.mjs';

const enc=encodeURIComponent;
const categories=[['composition','组成'],['preparation','制备'],['tests','测试与结果'],['statements','研究陈述']];
const fields=[['name','名称'],['value','内容'],['unit','单位'],['basis','比例基准'],['conditions','条件（每行一项）'],['fact_type','内容性质'],['attribution','来源性质'],['value_representation','表达方式'],['statement_status','陈述状态']];
const enums={fact_type:[['measurement','实测'],['calculation','计算'],['interpretation','解释'],['claim','主张'],['description','描述']],attribution:[['authors','作者'],['cited_work','引用研究'],['product','产品资料']],value_representation:[['reported_literal','原文值'],['paraphrase','释义']]};
const labelFor=(key,value)=>enums[key]?.find(([id])=>id===value)?.[1]||text(value);
const initialState=()=>({scope:null,category:null,positions:{},sourceKey:null,sourceRef:null,drafts:{},exclusions:{}});

export function renderDocumentCandidates({root,task,api,capabilities=/** @type {{archive_configured?: boolean}} */ ({}),onTaskChange,onBusyChange,storage,go=path=>{globalThis.location.hash=path;}}) {
  const doc=root.ownerDocument;
  if(storage===undefined){try{storage=globalThis.localStorage;}catch{storage=null;}}
  let groups=[],candidateHash=null,reviewHash=null,model={scopes:[],facts:[],targets:[]},state=initialState();
  let busy=false,conflict=false,disposed=false,sourceToken=null,editSession=null,successSession=null;
  let rowPreviews=new Map(),issueDialog=null,issueTrigger=null;
  let uiScopes=new Map(),uiFacts=new Map(),uiDocumentIssues=[],renderedPageKey=null;
  const path=()=>`/api/tasks/${enc(task.task_id)}`;
  const el=(tag,cls,value)=>{const node=doc.createElement(tag);if(cls)node.className=cls;if(value!==undefined)node.textContent=text(value);return node;};
  const button=(label,action,cls='')=>{const node=el('button',cls,label);node.type='button';node.addEventListener('click',event=>{event.stopPropagation();if(canInteract(node))action();});return node;};
  const iconButton=(label,icon,action)=>{const node=button('',action,'rr-icon-button');node.setAttribute('aria-label',label);node.setAttribute('title',label);const glyph=el('span',`rr-icon rr-icon-${icon}`);glyph.setAttribute('aria-hidden','true');node.append(glyph);return node;};
  const shell=el('section','document-candidate-review');shell.setAttribute('aria-label','研究资料审核');
  const top=el('header','rr-top'),toolbar=el('div','rr-toolbar');
  const save=button('保存最新',publish,'rr-primary');
  const title=el('h2','',task.title&&task.title!==task.source_id?task.title:'核对提取结果'),status=el('span','rr-subtle'),titleGroup=el('div','rr-title-group');
  const documentLabel=task.source_type==='patent'?'专利说明':'论文说明';
  const documentIssuesButton=button(documentLabel,()=>showIssues({title:documentLabel,warnings:uiDocumentIssues.map(issue=>issue.description)},documentIssuesButton),'rr-issue-trigger rr-scope-issue-trigger');documentIssuesButton.setAttribute('aria-haspopup','dialog');documentIssuesButton.hidden=true;titleGroup.append(pageTitle(title,'资料整理'),documentIssuesButton);top.append(titleGroup,status,toolbar);toolbar.append(save);
  const layout=el('div','rr-shell'),nav=el('nav','rr-nav'),main=el('div','rr-main');nav.setAttribute('aria-label','研究和样品');
  const heading=el('div','rr-heading'),tabs=el('div','rr-tabs'),workspace=el('div','rr-workspace'),content=el('section','rr-content'),contentBody=el('div','rr-content-body'),source=el('aside','rr-source');
  tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','提取内容分类');content.setAttribute('aria-label','提取内容');source.setAttribute('aria-label','原文对照');
  content.append(contentBody);workspace.append(content,source);main.append(heading,tabs,workspace);layout.append(nav,main);
  const notice=el('div','rr-message'),noticeText=el('span','','正在读取候选内容…'),reload=button('重新读取',()=>load(true),'rr-link');notice.setAttribute('role','status');notice.setAttribute('aria-live','polite');notice.append(noticeText,reload);reload.hidden=true;
  const issueLayer=el('div','rr-issue-overlay');issueLayer.hidden=true;
  const editLayer=el('div','rr-edit-overlay');editLayer.hidden=true;
  const successLayer=el('div','rr-save-overlay');successLayer.hidden=true;
  const reportLayer=el('div','dsr-overlay');reportLayer.hidden=true;
  shell.append(top,layout,notice,issueLayer,editLayer,successLayer,reportLayer);root.replaceChildren(shell);
  const storageKey=()=>`kb-document-review:${task.task_id}:${candidateHash}`;
  const writable=()=>['needs_review','failed','saved'].includes(task.state);
  const editable=()=>!busy&&!conflict&&writable();
  const lookup=key=>model.facts.find(fact=>fact.key===key);
  const activeScope=()=>model.scopes.find(scope=>scope.key===state.scope)||model.scopes[0];
  const positionKey=()=>`${state.scope}:${state.category}`;
  const activePages=()=>pagesFor(activeScope(),state.category);
  const activePage=()=>{const pages=activePages();const at=Math.max(0,Math.min(Number(state.positions[positionKey()])||0,pages.length-1));state.positions[positionKey()]=at;return pages[at];};
  const hasDraft=fact=>Object.hasOwn(state.drafts,fact.key);
  const pendingExclusion=fact=>state.exclusions[fact.key]===true;
  const draftKeys=()=>[...new Set([...Object.keys(state.drafts),...Object.keys(state.exclusions)])];
  const reportContext=()=>({taskId:task.task_id,revision:task.revision,generation:task.generation,candidateHash,reviewHash,hasDrafts:draftKeys().length>0,busy});
  const reports=createDocumentStudyReport({
    layer:reportLayer, api, current:reportContext,
    // @ts-expect-error The existing report controller ignores this callback.
    onConflict:()=>{conflict=true;message('候选或审核版本已变化，请关闭小报告后重新读取。');},
  });
  const reportButton=button('研究单元小报告',()=>{const scope=activeScope();if(!scope?.studyRef||busy)return;closeIssue(false);closeEditor(false);reports.open({...reportContext(),candidateId:scope.group.candidate_id,studyRef:scope.studyRef},reportButton);});
  reportButton.setAttribute('aria-haspopup','dialog');reportButton.hidden=true;toolbar.append(reportButton);
  const wikiHost=el('span');toolbar.append(wikiHost);
  const unitWiki=mountStudyUnitWiki({root:wikiHost,api,go,current:()=>({sourceId:task.source_id,taskId:task.task_id,candidateId:activeScope()?.group?.candidate_id,studyRef:activeScope()?.studyRef,interactionBlocked:busy||Boolean(editSession)||Boolean(successSession)||reports.isOpen||Boolean(issueDialog)})});
  const infoFor=fact=>fact.malformed?{name:'结构异常内容',value:JSON.stringify(fact.rawValue)??text(fact.rawValue)}:summary(presentFact(fact,state.drafts));
  function message(value){noticeText.textContent=value;notice.hidden=!value&&!conflict;reload.hidden=!conflict;}
  function updateSaveAvailability(){save.disabled=busy||conflict||!candidateHash||!capabilities.archive_configured||!['needs_review','failed','saved','publishing','write_unknown'].includes(task.state);status.textContent=busy?'正在保存…':task.state==='saved'?'已归档':!writable()?'当前版本只读':'';status.hidden=!status.textContent;}
  function persist(){if(!candidateHash)return;try{storage?.setItem(storageKey(),JSON.stringify(state));}catch{message('浏览器无法保存本地草稿，请在离开前保存最新。');}}
  function canInteract(node){
    if(reports.isOpen)return reports.contains(node);
    const modal=successSession||editSession;
    if(!modal)return true;
    for(let current=node;current;current=current.parentNode)if(current===modal.dialog)return true;
    return false;
  }
  function restore(){try{const saved=JSON.parse(storage?.getItem(storageKey())||'null');if(saved&&typeof saved==='object'&&!Array.isArray(saved)){state={...initialState(),scope:typeof saved.scope==='string'?saved.scope:null,category:typeof saved.category==='string'?saved.category:null,sourceKey:typeof saved.sourceKey==='string'?saved.sourceKey:null,sourceRef:typeof saved.sourceRef==='string'?saved.sourceRef:null,positions:object(saved.positions),drafts:object(saved.drafts),exclusions:Object.fromEntries(Object.entries(object(saved.exclusions)).filter(([,value])=>value===true))};}}catch{state=initialState();}}
  function refreshModel(){
    model=buildReviewModel(groups);const uiModel=buildReviewModel(localizedGroups(groups));uiScopes=new Map(uiModel.scopes.map(scope=>[scope.key,scope]));uiFacts=new Map(uiModel.facts.map(fact=>[fact.key,fact]));uiDocumentIssues=uiModel.documentIssues;let cleaned=false;
    for(const key of draftKeys()){const fact=lookup(key);if(!fact||excluded(fact)){delete state.drafts[key];delete state.exclusions[key];cleaned=true;}else if(fact.malformed&&hasDraft(fact)){delete state.drafts[key];cleaned=true;}}
    if(cleaned)persist();
    const scope=activeScope();state.scope=scope?.key||null;if(!scope?.blocks.some(block=>block.category===state.category))state.category=categories.find(([key])=>scope?.blocks.some(block=>block.category===key))?.[0]||null;
    if(!array(activePage()?.rows).some(fact=>fact.key===state.sourceKey)){state.sourceKey=activePage()?.rows[0]?.key||null;state.sourceRef=null;}
  }
  function resetPanels(){closeEditor(false);}
  function navigate(scopeKey,category){state.scope=scopeKey;state.category=category;resetPanels();render();persist();}
  function selectFact(fact,{keepEditor=false}={}){if(state.sourceKey!==fact.key){state.sourceRef=null;if(!keepEditor)resetPanels();}state.sourceKey=fact.key;for(const [key,preview] of rowPreviews)preview.row.setAttribute('data-selected',String(key===fact.key));renderSource();persist();}
  function originalLink(){const asset=array(task.asset_ids).find(id=>typeof id==='string'&&id);if(!asset)return el('span','rr-subtle','完整原件暂不可用');const link=el('a','rr-link','打开完整原件');link.href=`/api/assets/${enc(asset)}/download`;link.target='_blank';link.rel='noopener';return link;}

  function closeIssue(restoreFocus=true){
    if(!issueDialog)return;
    const trigger=issueTrigger;issueDialog=null;issueTrigger=null;
    issueLayer.hidden=true;issueLayer.replaceChildren();
    for(const background of [top,layout,notice])background.inert=false;
    if(restoreFocus)trigger?.focus();
  }
  function updateIssueTrigger(fact,trigger=rowPreviews.get(fact.key)?.issueTrigger){
    if(!trigger)return;const reviewed=issuesAcknowledged(fact,state.drafts,state.exclusions);
    trigger.textContent=reviewed?'说明':'需核对';trigger.setAttribute('data-issue-state',reviewed?'reviewed':'pending');
  }
  function showIssue(fact,trigger){
    showIssues({name:infoFor(fact).name,warnings:(uiFacts.get(fact.key)||fact).warnings,fact,reviewed:issuesAcknowledged(fact,state.drafts,state.exclusions)},trigger);
  }
  function showIssues({title='疑点说明',name='',warnings=[],fact=null,reviewed=false},trigger){
    closeIssue(false);if(fact)selectFact(fact,{keepEditor:true});
    const dialog=el('section','rr-issue-dialog'),header=el('header','rr-issue-header'),body=el('div','rr-issue-body');
    dialog.setAttribute('role','dialog');dialog.setAttribute('aria-modal','true');dialog.setAttribute('aria-label',title);dialog.setAttribute('data-issue-state',reviewed?'reviewed':'pending');
    const close=button('关闭',()=>closeIssue(),'rr-issue-close');close.setAttribute('aria-label','关闭疑点说明');
    header.append(el('h3','',title),close);body.tabIndex=0;body.setAttribute('aria-label','疑点内容');if(name)body.append(el('p','rr-issue-name',name));
    const list=el('ul','rr-issue-list');for(const warning of warnings)list.append(el('li','',warning));body.append(list);dialog.append(header,body);
    dialog.addEventListener('keydown',event=>{
      if(event.key==='Escape'){event.preventDefault();event.stopPropagation();closeIssue();}
      if(event.key==='Tab'){event.preventDefault();const targets=[close,body],index=targets.indexOf(doc.activeElement),next=(index+(event.shiftKey?-1:1)+targets.length)%targets.length;targets[next].focus();}
    });
    issueDialog=dialog;issueTrigger=trigger;
    for(const background of [top,layout,notice])background.inert=true;
    issueLayer.replaceChildren(dialog);issueLayer.hidden=false;close.focus();
  }

  function render(){
    if(disposed)return;
    // Clearing or reattaching a scroll container lets the browser clamp it to zero.
    const contentTop=contentBody.scrollTop||0,navTop=nav.scrollTop||0;
    renderView();
    const pageKey=JSON.stringify([candidateHash,state.scope,state.category,activePage()?.block.key,state.positions[positionKey()]||0]);
    contentBody.scrollTop=renderedPageKey===pageKey?contentTop:0;
    nav.scrollTop=navTop;
    renderedPageKey=pageKey;
  }

  function renderView(){
    closeIssue(false);closeEditor(false);
    rowPreviews=new Map();refreshModel();
    const scope=activeScope();
    title.textContent=(task.title!==task.source_id?text(task.title):'')||groups.map(group=>text(object(object(group.raw).source).title)).find(Boolean)||'核对提取结果';
    unitWiki.sync();reports.sync();reportButton.hidden=!scope?.studyRef||scope.group.raw_status!=='available'||array(scope.group.raw?.studies).filter(study=>study?.ref===scope.studyRef).length!==1;reportButton.disabled=busy||conflict||!candidateHash;
    updateSaveAvailability();documentIssuesButton.hidden=!uiDocumentIssues.length;
    nav.replaceChildren();let previous='';
    for(const original of model.scopes){const item=uiScopes.get(original.key)||original;const study=`${item.group.candidate_id}:${item.studyPointer||'other'}`;if(study!==previous){nav.append(el('p','rr-nav-heading',item.studyLabel||'资料内容'));previous=study;}const pick=button('',()=>navigate(item.key,null));pick.append(el('span','',item.label));pick.setAttribute('aria-current',String(item.key===scope?.key));nav.append(pick);}
    heading.replaceChildren();tabs.replaceChildren();content.replaceChildren(contentBody);contentBody.replaceChildren();
    if(!scope){contentBody.append(el('p','rr-empty','还没有可展示的候选内容。'));renderSource();return;}
    const uiScope=uiScopes.get(scope.key)||scope;heading.append(el('h3','',[uiScope.studyLabel,uiScope.label].filter(Boolean).join(' / ')));
    if(array(uiScope.sampleIssues).length){const sampleIssuesButton=button('样品说明',()=>showIssues({title:'样品说明',name:uiScope.label,warnings:uiScope.sampleIssues.map(issue=>issue.description)},sampleIssuesButton),'rr-issue-trigger rr-scope-issue-trigger');sampleIssuesButton.setAttribute('aria-haspopup','dialog');heading.append(sampleIssuesButton);}
    if(!scope.reviewScope)heading.append(el('p','rr-note','请更新服务以提供完整保存范围；当前内容可只读查看。'));
    for(const [id,label] of categories){if(!scope.blocks.some(block=>block.category===id))continue;const tab=button(label,()=>navigate(scope.key,id));tab.setAttribute('role','tab');tab.setAttribute('aria-selected',String(id===state.category));tabs.append(tab);}
    for(const link of array(uiScope.relatedScopes))contentBody.append(button(`相关样品：${link.label}`,()=>navigate(link.scopeKey,null),'rr-link'));
    const page=activePage(),pages=activePages();
    if(!page){contentBody.append(el('p','rr-empty',scope.group.raw_status==='unreadable'?'候选文件暂时无法读取。':'这组内容暂时无法归入样品或字段。'));renderSource();return;}
    const uiBlock=uiScope.blocks.find(block=>block.key===page.block.key)||page.block;
    const groupHead=el('div','rr-group-head');groupHead.append(el('h3','',uiBlock.title),el('span','rr-subtle',`${(state.positions[positionKey()]||0)+1}/${pages.length} 页`));contentBody.append(groupHead);
    if(uiBlock.context)contentBody.append(el('p','rr-context',uiBlock.context));
    for(const method of array(uiBlock.methods))contentBody.append(button(`共用方法：${method.label}`,()=>{navigate(method.scopeKey,'tests');const next=activePages().findIndex(p=>p.block.key===method.blockKey);if(next>=0){state.positions[positionKey()]=next;render();persist();}},'rr-link'));
    if(!page.rows.length)contentBody.append(el('p','rr-empty','此测试尚无结果；可通过共用方法继续核对。'));
    if(page.rows.length){
      const table=el('table','rr-table'),head=el('thead'),tr=el('tr');tr.append(el('th','',state.category==='composition'?'材料':'内容'),el('th','','提取结果'));head.append(tr);table.append(head);const body=el('tbody');
      for(const fact of page.rows){
        const display=presentFact(fact,state.drafts),info=infoFor(fact),row=el('tr'),nameCell=el('td'),valueCell=el('td');
        row.setAttribute('data-fact-ref',fact.pointer);row.setAttribute('data-selected',String(state.sourceKey===fact.key));row.setAttribute('data-excluded',excluded(fact)?'locked':pendingExclusion(fact)?'pending':'false');
        row.addEventListener('click',()=>{if(editSession)return;selectFact(fact);render();});
        const select=button(info.name,()=>{selectFact(fact);render();},'rr-value');nameCell.append(select);
        if(fact.role==='conditions')nameCell.append(el('span','rr-row-status','测试条件'));
        const value=button(info.value,()=>{selectFact(fact);render();},'rr-value'),valueHead=el('div','rr-result-head');valueHead.append(value);valueCell.append(valueHead);rowPreviews.set(fact.key,{value,row});
        if(display.basis)valueCell.append(el('span','rr-row-status',`基准：${text(display.basis)}`));
        if(array(display.conditions).length)valueCell.append(el('span','rr-row-status',`条件：${array(display.conditions).map(text).join('；')}`));
        if(Object.keys(object(fact.review?.correction)).length||hasDraft(fact))valueCell.append(el('span','rr-row-status',`原提取：${summary(presentFact({...fact,review:null},{})).value}`));
        if(fact.review?.ownership_target_ref)valueCell.append(el('span','rr-row-status','已人工调整归属'));
        if(fact.warnings.length){const warning=button('需核对',()=>showIssue(fact,warning),'rr-row-status rr-issue-trigger');warning.setAttribute('aria-haspopup','dialog');nameCell.append(warning);rowPreviews.get(fact.key).issueTrigger=warning;updateIssueTrigger(fact,warning);}
        if(!fact.evidence.some(entry=>['located','page_only'].includes(entry.location?.status)))nameCell.append(el('span','rr-row-status rr-uncertain','没有找到原文位置'));
        const actions=el('div','rr-row-icons');
        const edit=iconButton(`编辑 ${info.name}`,'edit',()=>showEditor(fact,edit));edit.setAttribute('aria-haspopup','dialog');edit.disabled=!editable()||!scope.reviewScope||excluded(fact)||pendingExclusion(fact)||fact.malformed;rowPreviews.get(fact.key).edit=edit;
        const excludeLabel=excluded(fact)?'已排除':pendingExclusion(fact)?'恢复核对':'暂不确认';
        const exclude=iconButton(`${excludeLabel} ${info.name}`,pendingExclusion(fact)?'restore':excluded(fact)?'lock':'exclude',()=>{selectFact(fact);if(!editable()||excluded(fact))return;if(pendingExclusion(fact))delete state.exclusions[fact.key];else state.exclusions[fact.key]=true;resetPanels();render();persist();});exclude.disabled=!editable()||!scope.reviewScope||excluded(fact);exclude.setAttribute('aria-pressed',String(excluded(fact)||pendingExclusion(fact)));
        actions.append(edit,exclude);valueHead.append(actions);row.append(nameCell,valueCell);body.append(row);
      }
      table.append(body);contentBody.append(table);
    }
    const footer=el('div','rr-footer'),pager=el('div','rr-pager');
    const move=direction=>{state.positions[positionKey()]=(state.positions[positionKey()]||0)+direction;resetPanels();render();persist();};
    if(pages.length>1){const prev=button('上一页',()=>move(-1)),next=button('下一页',()=>move(1));prev.disabled=busy||state.positions[positionKey()]===0;next.disabled=busy||state.positions[positionKey()]===pages.length-1;pager.append(prev,next);}footer.append(pager);
    if(pages.length>1)content.append(footer);
    if(draftKeys().length){contentBody.append(el('p','rr-note',`有 ${draftKeys().length} 项本地修改或暂不确认草稿；点击右上角保存最新可一并保存。`));for(const key of draftKeys()){const fact=lookup(key),target=model.scopes.find(item=>item.blocks.some(block=>block.rows.some(row=>row.key===key)));if(!fact||!target)continue;contentBody.append(button(`定位草稿：${infoFor(fact).name}`,()=>{state.scope=target.key;state.category=target.blocks.find(block=>block.rows.includes(fact)).category;state.positions[positionKey()]=activePages().findIndex(page=>page.rows.includes(fact));state.sourceKey=key;state.sourceRef=null;render();persist();if(hasDraft(fact)&&!pendingExclusion(fact))showEditor(fact,rowPreviews.get(key)?.edit);},'rr-link'));}}
    renderSource();
  }

  function closeEditor(restoreFocus=true){
    if(!editSession)return;
    const {trigger,backgrounds}=editSession;editSession=null;
    editLayer.hidden=true;editLayer.replaceChildren();
    for(const [node,wasInert] of backgrounds)node.inert=wasInert;
    if(restoreFocus)trigger?.focus({preventScroll:true});
  }

  function showEditor(fact,trigger){
    if(editSession||!editable()||!activeScope()?.reviewScope||excluded(fact)||pendingExclusion(fact)||fact.malformed)return;
    closeIssue(false);selectFact(fact);
    const title=`修改 ${infoFor(fact).name}`,dialog=el('section','rr-edit-dialog'),header=el('header','rr-edit-header');
    dialog.setAttribute('role','dialog');dialog.setAttribute('aria-modal','true');dialog.setAttribute('aria-label',title);
    const close=button('关闭',()=>closeEditor(),'rr-edit-close');close.setAttribute('aria-label','关闭修改');header.append(el('h3','',title),close);
    const form=el('form','rr-editor'),body=el('div','rr-edit-body'),grid=el('div','rr-editor-grid'),advanced=el('div','rr-editor-grid');
    const current=displayFact(fact,state.drafts),presented=presentFact(fact,state.drafts);
    // Form values are private to this modal. Only saveEdit may update persisted drafts.
    const sourceConditions=array(current.conditions).map(text),translatedConditions=array(presented.conditions).map(text);
    const editConditions=translatedConditions.map((line,index)=>new Set(translatedConditions.flatMap((other,i)=>other===line?[sourceConditions[i]]:[])).size>1?sourceConditions[index]:line);
    const readConditions=value=>value.split('\n').map(line=>line.trim()).filter(Boolean).map(line=>{const matches=editConditions.flatMap((shown,index)=>shown.trim()===line?[sourceConditions[index]]:[]);return new Set(matches).size===1?matches[0]:line;});
    const reader=[];
    const saveEdit=()=>{
      if(!editSession||editSession.dialog!==dialog||!editable())return;
      const result={};for(const [key,input,initialDisplay] of reader){let value=input.value===initialDisplay?current[key]:key==='conditions'?readConditions(input.value):input.value;const original=fact.item[key];if(value===''&&(original===null||['unit','basis','statement_status'].includes(key)))value=null;if(key==='value'&&typeof original==='number'&&String(original)===value)value=original;if(!(key in fact.item)&&(value===''||value===null||Array.isArray(value)&&!value.length))continue;if(JSON.stringify(value)!==JSON.stringify(original))result[key]=value;}
      const savedCorrection=object(fact.review?.correction);const changed=[...new Set([...Object.keys(result),...Object.keys(savedCorrection)])].some(key=>JSON.stringify(result[key])!==JSON.stringify(savedCorrection[key]));if(changed)state.drafts[fact.key]=result;else delete state.drafts[fact.key];
      closeEditor(false);persist();render();rowPreviews.get(fact.key)?.edit?.focus({preventScroll:true});
    };
    form.addEventListener('submit',event=>event.preventDefault());
    for(const [key,label] of [...(fact.role==='composition'?[['material','材料']]:[]),...fields]){
      const wrap=el('label','',label);let input;
      if(enums[key]){input=el('select');const empty=el('option','','未提供');empty.value='';input.append(empty);for(const [value,title] of enums[key]){const option=el('option','',title);option.value=value;input.append(option);}}
      else input=el(key==='conditions'||key==='value'&&text(current[key]).length>80?'textarea':'input');
      input.name=key;input.value=key==='conditions'?editConditions.join('\n'):text(presented[key]);input.disabled=!editable();
      reader.push([key,input,input.value]);wrap.append(input);(enums[key]||key==='statement_status'?advanced:grid).append(wrap);
    }
    advanced.hidden=true;
    const details=button('更多字段',()=>{advanced.hidden=!advanced.hidden;details.textContent=advanced.hidden?'更多字段':'收起更多字段';details.setAttribute('aria-expanded',String(!advanced.hidden));},'rr-link');details.setAttribute('aria-expanded','false');
    body.append(grid,details,advanced,el('p','rr-subtle',`原提取：${summary(presentFact({...fact,review:null},{})).value}`));
    const actions=el('div','rr-editor-actions rr-edit-actions'),cancel=button('取消',()=>closeEditor()),done=button('保存修改',saveEdit,'rr-primary');actions.append(cancel,done);form.append(body,actions);dialog.append(header,form);
    const focusTargets=()=>[close,...reader.filter(([key])=>!enums[key]&&key!=='statement_status').map(([,input])=>input),details,...(!advanced.hidden?reader.filter(([key])=>enums[key]||key==='statement_status').map(([,input])=>input):[]),cancel,done].filter(node=>!node.disabled);
    dialog.addEventListener('keydown',event=>{
      if(event.key==='Escape'){event.preventDefault();event.stopPropagation();closeEditor();}
      if(event.key==='Tab'){event.preventDefault();const targets=focusTargets(),index=targets.indexOf(doc.activeElement),next=(index+(event.shiftKey?-1:1)+targets.length)%targets.length;targets[next].focus();}
    });
    // Include sibling app navigation outside this view, and restore existing inert states.
    const backgrounds=[];for(let node=editLayer;node?.parentNode;node=node.parentNode){for(const sibling of node.parentNode.children)if(sibling!==node){backgrounds.push([sibling,Boolean(sibling.inert)]);sibling.inert=true;}if(node.parentNode===doc.body)break;}
    editSession={dialog,trigger,backgrounds};editLayer.replaceChildren(dialog);editLayer.hidden=false;reader.find(([,input])=>!input.disabled)?.[1].focus({preventScroll:true});
  }

  function renderSource(){
    const fact=lookup(state.sourceKey);const entries=fact?.evidence||[];const selected=entries.find(entry=>entry.ref===state.sourceRef)||entries[0];
    const token=JSON.stringify([candidateHash,fact?.key,selected?.ref]);if(sourceToken===token)return;sourceToken=token;source.replaceChildren();
    const head=el('div','rr-source-head');head.append(el('strong','','原文对照'),originalLink());source.append(head);const body=el('div','rr-source-body');source.append(body);
    if(!fact){body.append(el('p','rr-empty','选择内容，即可在这里对照原文。'));return;}
    if(entries.length>1){const label=el('label','','来源片段'),select=el('select');select.setAttribute('aria-label','来源片段');for(const [index,entry] of entries.entries()){const option=el('option','',`片段 ${index+1}${entry.location?.page?` · 第 ${entry.location.page} 页`:''}`);option.value=entry.ref;select.append(option);}select.value=selected?.ref;select.addEventListener('change',()=>{state.sourceRef=select.value;renderSource();persist();});label.append(select);body.append(label);}
    const location=selected?.location;const hasPage=location&&['located','page_only'].includes(location.status)&&Number.isInteger(location.page)&&location.page>0;
    if(!hasPage)body.append(el('p','rr-notice','没有找到原文位置。提取内容仍保留，请在完整原件中查找。'));
    else{
      const regions=evidenceRegions(location,location.page);
      const precision=regions.length?(Object.hasOwn(location,'highlight_regions')&&location.precision==='text'?'text':'region'):'page';
      const locationLabel=el('p','rr-source-label',`原件第 ${location.page} 页`);body.append(locationLabel);
      const wrap=el('div','document-page-image-wrap'),image=el('img','document-page-image');image.alt=`原件第 ${location.page} 页`;image.src=`${path()}/document-candidates/${enc(fact.group.candidate_id)}/evidence/${enc(selected.ref)}/page-image?candidate_sha256=${enc(candidateHash)}`;wrap.append(image);
      for(const region of regions){const highlight=el('div','document-source-region');highlight.setAttribute('data-precision',precision);highlight.setAttribute('aria-hidden','true');for(const [key,value] of Object.entries(region))highlight.style[key]=`${value}%`;wrap.append(highlight);}
      image.addEventListener('load',()=>{if(disposed||sourceToken!==token||!regions.length)return;const target=wrap.getBoundingClientRect().top-body.getBoundingClientRect().top+image.clientHeight*Math.min(...regions.map(region=>region.top))/100;body.scrollTop=Math.max(0,body.scrollTop+target-36);});
      image.addEventListener('error',()=>{locationLabel.textContent=`原件第 ${location.page} 页 · 预览不可用`;wrap.replaceChildren(el('p','rr-notice','原文页暂时无法预览，请打开完整原件核对。'));});body.append(wrap);
    }
  }

  function failure(error){if(error.status===409&&(error.documentSavePhase!=='publish'||error.documentSaveConflict)){conflict=true;message(`${error.message||'审核版本已变化。'} 你的本地草稿已保留，请重新读取后核对，再提交。`);}else message(error.message||'操作未完成，候选和修改草稿已保留。');}
  function closeSuccess(restoreFocus=true){
    if(!successSession)return;
    const {backgrounds}=successSession;successSession=null;successLayer.hidden=true;successLayer.replaceChildren();
    for(const [node,wasInert] of backgrounds)node.inert=wasInert;
    if(restoreFocus)save.focus({preventScroll:true});
  }
  function showSuccess(result){
    if(disposed)return;
    const time=savedDocumentTime(result,task);closeIssue(false);closeEditor(false);closeSuccess(false);
    const dialog=el('section','rr-save-dialog');dialog.setAttribute('role','dialog');dialog.setAttribute('aria-modal','true');dialog.setAttribute('aria-label','保存成功');
    const close=button('关闭',()=>closeSuccess(),'rr-primary');
    dialog.append(el('h3','','保存成功'),el('p','','已归档。'),el('p','rr-save-time',`更新时间：${time}`),close);
    dialog.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();event.stopPropagation();closeSuccess();}if(event.key==='Tab'){event.preventDefault();close.focus({preventScroll:true});}});
    const backgrounds=[];for(let node=successLayer;node?.parentNode;node=node.parentNode){for(const sibling of node.parentNode.children)if(sibling!==node){backgrounds.push([sibling,Boolean(sibling.inert)]);sibling.inert=true;}if(node.parentNode===doc.body)break;}
    successSession={dialog,backgrounds};successLayer.replaceChildren(dialog);successLayer.hidden=false;close.focus({preventScroll:true});
  }
  function adoptTask(next){Object.assign(task,next);if(!disposed)onTaskChange?.({...task});}
  async function publish(){
    if(busy||conflict||disposed||!candidateHash||!capabilities.archive_configured)return;
    busy=true;onBusyChange?.(true);render();message('');let result;
    try{
      result=await saveDocumentLatest({api,task:{...task},groups,candidateHash,reviewHash,drafts:state.drafts,exclusions:state.exclusions,isActive:()=>!disposed,
        onSnapshot:next=>{groups=next.groups;candidateHash=next.candidateHash;reviewHash=next.reviewHash;Object.assign(task,next.task);refreshModel();persist();},
        onRevision:next=>{persist();groups=next.groups;candidateHash=next.candidateHash;reviewHash=next.reviewHash;sourceToken=null;adoptTask(next.task);refreshModel();persist();},
        onReview:({groupId,keys,response})=>{reviewHash=response.review_sha256;const group=groups.find(item=>item.candidate_id===groupId);if(Array.isArray(response.review_scopes))group.review_scopes=response.review_scopes;group.reviews=[...array(group.reviews).filter(prior=>!response.reviews.some(next=>next.candidate_ref===prior.candidate_ref)),...response.reviews];for(const key of keys){delete state.drafts[key];delete state.exclusions[key];}persist();},
        onTask:adoptTask});
    }catch(error){if(!disposed)failure(error);}
    finally{busy=false;if(!disposed){render();unitWiki.refresh();onBusyChange?.(false);}}
    if(result&&!disposed)showSuccess(result);
  }
  async function load(reloading=false){
    if(busy)return;closeEditor(false);busy=true;reload.disabled=true;
    try{
      const next=await readDocumentCandidates(api,task,()=>!disposed);
      const changed=candidateHash&&candidateHash!==next.candidateHash;candidateHash=next.candidateHash;reviewHash=next.reviewHash;groups=next.groups;
      if(!reloading||changed){state=initialState();restore();resetPanels();}conflict=false;sourceToken=null;
      message(changed?'候选版本已更新；旧版草稿单独保留。':!writable()?'当前任务状态不允许修改审核。候选与来源仍可只读查看。':!capabilities.archive_configured?'归档不可用，当前可以核对；草稿保留，恢复可用后可继续保存。':reloading?'已读取最新审核。保留的草稿请对照原文后再提交。':'');
    }catch(error){failure(error);reload.hidden=false;}
    finally{busy=false;reload.disabled=false;render();}
  }
  load();
  const dispose=()=>{disposed=true;unitWiki.dispose();reports.dispose();closeSuccess(false);closeEditor(false);persist();closeIssue(false);root.replaceChildren();};
  dispose.updateTask=next=>{if(disposed||busy)return;if(next?.task_id!==task.task_id||next.source_id!==task.source_id){reports.close(false);return;}reports.sync({...reportContext(),revision:next.revision??task.revision,generation:next.generation??task.generation,candidateHash:next.candidate_sha256??candidateHash,reviewHash:next.review_sha256??reviewHash});Object.assign(task,next);if(!successSession&&!editSession&&!reports.isOpen)render();};
  return dispose;
}
