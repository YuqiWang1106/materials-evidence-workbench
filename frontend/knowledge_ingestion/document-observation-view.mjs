/** The task page's timeline: an overview with a running clock and a detail drawer of recorded events. */
import {el} from './dom.js';
import {projectVisualSteps, distinguishVisualAreas} from './document-observation-visual-logic.mjs';
import {coverageText} from './document-processing.mjs';
import {STATES, ACTIVE, safeText, duration, elapsedClock, stageName, statusName, displayStatus, errorName, finite, readerDocumentIndex, phaseName} from './document-observation-labels.mjs';
import {currentStage, countText, correlationIndex, correlationText, projectCalls, projectReaders, rowText, eventLabel, statusIcon, activityKey, sameTimelineIdentity, detailItems} from './document-observation-events.mjs';

function details(label) {
  const root=el('details','observation-details'), summary=el('summary',null,label), content=el('div','observation-list');
  root.append(summary,content);return {root,content};
}

/** Coarse library projection: stage counts are processed items, never model successes. */
export function compactEntityProgress(progress, state) {
  const result=el('p','observation-library');
  const stage=state==='publishing' ? '正在确认保存' : ACTIVE.has(state) ? `最近记录阶段：${stageName(progress?.stage)}` : (STATES[state] || '任务状态未知');
  const p=progress || {}, valid=Number.isInteger(p.completed)&&Number.isInteger(p.total)&&p.total>0&&p.completed>=0&&p.completed<=p.total;
  const count=valid && state!=='publishing' ? ` · 已处理 ${p.completed} / ${p.total} ${p.unit || '项'}` : '';
  const failed=valid && Number.isInteger(p.failed)&&p.failed>0 ? ` · 含失败或不完整 ${p.failed}` : '';
  result.textContent=stage+count+failed+coverageText(p.extraction_coverage);return result;
}

export function createObservationView({taskId, now=()=>Date.now()}) {
  const root=el('section','document-observation');root.setAttribute('aria-label','文献处理进度');
  const overview=el('div','observation-overview'), header=el('div','observation-header');
  const heading=el('h2','observation-heading','正在读取任务状态'), connection=el('p','observation-connection','正在连接');
  const stats=el('div','observation-stats'), clock=el('div','observation-elapsed');
  const clockIcon=el('span','observation-elapsed-icon'), clockLabel=el('span','observation-elapsed-label','总耗时');
  const clockValue=el('time','observation-elapsed-value','—'), progress=el('span','observation-stage-progress');
  clockIcon.setAttribute('aria-hidden','true');clockValue.setAttribute('aria-label','总耗时未知');
  clock.append(clockIcon,clockLabel,clockValue);stats.append(clock,progress);
  const button=el('button','button secondary observation-toggle','查看详细日志');button.type='button';
  button.setAttribute('aria-expanded','false');button.setAttribute('aria-controls',`observation-detail-${taskId}`);
  const panel=el('aside','observation-panel');panel.id=`observation-detail-${taskId}`;panel.hidden=true;
  panel.setAttribute('aria-label','处理详细日志');
  const panelTitle=el('h3','observation-panel-title','处理详情'), history=el('p','observation-history');
  history.hidden=true;
  const panelHeader=el('div','observation-panel-header'), close=el('button','button quiet','关闭详情');close.type='button';
  panelHeader.append(panelTitle,close);
  const orderNote=el('p','observation-order','按首次记录排序');orderNote.hidden=true;
  const timeline=el('div','observation-timeline'), rawEvents=details('原始事件（按记录顺序）');
  rawEvents.root.hidden=true;
  const supplemental=details('结果汇总与历史补充（顺序未知）');
  supplemental.root.hidden=true;
  const summary=el('p','observation-summary'), indexScope=el('p','observation-note'), supplementalRows=el('div','observation-list');
  supplemental.content.append(summary,indexScope,supplementalRows);
  panel.append(panelHeader,history,orderNote,timeline,rawEvents.root,supplemental.root);
  header.append(heading,button);overview.append(header,connection,stats);root.append(overview,panel);
  let data=null, link='connecting', reason=null, received=0, clocks=[], timelineClocks=[];
  let timelineScope=null, timelineItems=[], timelineTail=null;
  const clearEntryAnimations=()=>{for(const item of timelineItems)delete item.node.dataset.new;};
  button.addEventListener('click',()=>{panel.hidden=!panel.hidden;if(panel.hidden)clearEntryAnimations();button.setAttribute('aria-expanded',String(!panel.hidden));});
  close.addEventListener('click',()=>{panel.hidden=true;clearEntryAnimations();button.setAttribute('aria-expanded','false');button.focus();});
  root.addEventListener('keydown',event=>{if(event.key==='Escape'&&!panel.hidden){panel.hidden=true;clearEntryAnimations();button.setAttribute('aria-expanded','false');button.focus();}});
  function renderConnection(){
    const labels={connecting:'正在连接',live:'实时连接',stale:'状态暂未确认',disconnected:'连接中断，正在尝试恢复',polling:'定期刷新',paused:'页面隐藏，已暂停连接',unavailable:'状态暂不可用'};
    const reasons={verification_timeout:'状态核对超时',history_regression:'历史记录出现缺口，正在核对',
      history_resynchronized:'历史记录存在缺口，已重新同步',observation_unavailable:'后台观测暂不可用',
      stream_unverified:'实时流长时间未确认',backend_unverified:'后台状态长时间未确认'};
    const last=finite(data?.observed_at) ? new Date(data.observed_at*1000).toLocaleTimeString('zh-CN',{hour12:false}) : '未知';
    connection.hidden=link==='live';
    connection.textContent=`${labels[link] || '连接状态未知'}${reasons[reason]?' · '+reasons[reason]:''} · 最近更新 ${last}`;
  }
  function renderStats(){
    if(!data)return;
    const stage=currentStage(data);
    const stagePrefix=link==='live' && data.history==='recorded' ? '当前阶段' : '最近记录阶段';
    heading.textContent=stage ? `${stagePrefix}：${stageName(stage)}` : ACTIVE.has(data.state) && data.state!=='queued'
      ? '阶段切换中，状态待同步' : (STATES[data.state] || '任务状态未知');
    const extra=link==='live' && ACTIVE.has(data.state) ? Math.max(0,(now()-received)/1000) : 0;
    const total=elapsedClock(finite(data.elapsed_seconds) ? data.elapsed_seconds+extra : null);
    clockValue.textContent=total.text;clockValue.setAttribute('aria-label',total.label);
    const p=data.progress || {};
    if(stage && p.stage===stage && Number.isInteger(p.completed) && Number.isInteger(p.total) && p.total>0 && p.completed<=p.total)
      progress.textContent=`${p.completed} / ${p.total} ${p.unit || '项'}`;
    else progress.textContent='';
    for(const update of clocks)update();
    for(const update of timelineClocks)update();
  }
  function elapsedText(item, startKey, durationKey){
    if(finite(item[durationKey]))return `耗时 ${duration(item[durationKey])}`;
    if(item.status==='started' && finite(item[startKey]) && finite(data?.observed_at)){
      const delta=link==='live' && ACTIVE.has(data.state)?Math.max(0,(now()-received)/1000):0;
      return `自记录开始已过 ${Math.round(Math.max(0,data.observed_at-item[startKey])+delta)} 秒，状态待核实`;
    }
    return '耗时 未知';
  }
  function timedRow(item,startKey,durationKey,format){
    const node=el('p','observation-row');const update=()=>{node.textContent=format(elapsedText(item,startKey,durationKey));};
    if(item.status==='started' && finite(item[startKey]))clocks.push(update);update();return node;
  }
  function refreshTimelineItems(){
    if(!data)return;
    timelineClocks=[];
    // Polling is set only after a successful read; the UI labels it separately
    // from the live stream and drops the indicator as soon as it becomes stale.
    const activeStage=(link==='live' || link==='polling') && data.history==='recorded' && finite(data.observed_at) ? currentStage(data) : null;
    const index=correlationIndex(Array.isArray(data.events)?data.events:[]);
    const latest=new Map();
    for(const entry of timelineItems){const key=activityKey(entry.event);if(key)latest.set(key,entry);}
    const currentStart=activeStage ? [...timelineItems].reverse().find(entry=>
      entry.event.kind==='stage' && entry.event.stage===activeStage && entry.event.status==='started') : null;
    for(const entry of timelineItems){
      const event=entry.event, key=activityKey(event);
      const trackedWork=['model_call','model_attempt','table','window','table_round','reader_step','visual_step'].includes(event.kind);
      const active=entry===currentStart || (trackedWork && event.status==='started' &&
        activeStage===event.stage && key && latest.get(key)===entry);
      const icon=statusIcon(event,active);
      entry.node.dataset.activity=icon.kind;
      entry.icon.className='observation-timeline-icon observation-timeline-icon-'+icon.kind;
      entry.icon.textContent=icon.text;entry.icon.setAttribute('aria-label',icon.label);
      entry.label.textContent=eventLabel(event);
      const open=event.status==='started' && finite(event.at) && ACTIVE.has(data.state)
        && (!key || latest.get(key)===entry);
      const startedAt=event.kind==='stage' ? (data.stages||[]).find(item=>item.stage===event.stage && item.status==='started')?.started_at : null;
      const elapsedEvent=finite(startedAt) ? {...event,at:startedAt} : event;
      const update=()=>{
        if(trackedWork && key && event.status==='started' && ACTIVE.has(data.state) && latest.get(key)===entry){
          if(finite(event.at) && finite(data.observed_at)){
            const fresh=link==='live'||link==='polling';
            const delta=fresh?Math.max(0,(now()-received)/1000):0;
            if(fresh || !finite(entry.lastElapsed))entry.lastElapsed=Math.max(0,data.observed_at-event.at)+delta;
          }
          const isVisualCall=event.kind==='model_call' && event.stage==='visual_reading';
          const attempts=(event._groupedEvents||[]).filter(row=>row.kind==='model_attempt');
          const lastAttempt=attempts.at(-1);
          const state=active && isVisualCall ? !lastAttempt ? '等待或准备请求'
            : lastAttempt.status==='started' ? '请求进行中'
            : lastAttempt.will_retry ? '等待重试'
            : lastAttempt.status==='unknown' ? '请求结果待核实' : '整理请求结果'
            : active?'已开始':'待核实';
          const elapsed=finite(entry.lastElapsed)?elapsedClock(entry.lastElapsed).text:'耗时未知';
          entry.meta.textContent=`${state} · ${isVisualCall?'调用已耗时 ':''}${elapsed}`;
        }else entry.meta.textContent=event.error_code && event.kind==='stage' ? (finite(event.elapsed_seconds)?duration(event.elapsed_seconds):'')
          : event.error_code ? errorName(event.error_code)
          : event.kind==='visual_step' && event.visual_step==='selection' && event.status==='empty' ? ''
          : icon.kind==='cached' ? '使用缓存'
          : ['failed','invalid','unknown','partial','empty','not_processed','interrupted'].includes(event.status) ? displayStatus(event)
          : finite(event.elapsed_seconds) ? duration(event.elapsed_seconds) : '';
        const elapsed=open && trackedWork && finite(entry.lastElapsed) ? entry.lastElapsed : null;
        entry.detail.replaceChildren(...detailItems(event,index,elapsed).map(text=>el('li',null,text)));
      };
      update();if(open)timelineClocks.push(update);
    }
  }
  function renderTimeline(){
    const raw=Array.isArray(data.events)?data.events:[];
    const seen=new Set(), events=[];
    for(const event of raw){
      if(!Number.isSafeInteger(event?.seq)||event.seq<1||seen.has(event.seq))continue;
      seen.add(event.seq);events.push(event);
    }
    events.sort((a,b)=>a.seq-b.seq);
    const documents=new Set(events.map(readerDocumentIndex).filter(index=>index!==null));
    const multiple=documents.size>1 || events.some(item=>item.kind==='stage' && item.stage==='reading'
      && Number.isInteger(item.total) && item.total>1) || (data.progress?.stage==='reading'
      && Number.isInteger(data.progress.total) && data.progress.total>1);
    const projected=distinguishVisualAreas(projectCalls(projectVisualSteps(projectReaders(events)))).filter(item=>item.kind!=='stage' || item.status!=='started')
      .map(item=>item.kind==='reader_step'?{...item,_documentIndex:readerDocumentIndex(item),_multipleDocuments:multiple}:item);
    const index=correlationIndex(events);
    rawEvents.root.hidden=!events.length;
    orderNote.hidden=!projected.some(item=>item._groupedEvents || item._readerEvents || item._visualEvents);
    if(!rawEvents.root.hidden)rawEvents.content.replaceChildren(...events.map(item=>{
      const row=el('p','observation-raw-event',`#${item.seq} · ${rowText(item,index)}`);row.dataset.seq=String(item.seq);return row;
    }));
    const scope=`${data.task_id}:${data.generation}:${data.attempt}`;
    const same=scope===timelineScope;
    const prefix=same && timelineItems.length<=projected.length && timelineItems.every((entry,i)=>sameTimelineIdentity(entry.event,projected[i]));
    const animate=prefix && timelineScope!==null && !panel.hidden;
    const follow=animate && projected.length>timelineItems.length && finite(panel.scrollHeight) && finite(panel.clientHeight)
      && finite(panel.scrollTop) && panel.scrollHeight-panel.clientHeight-panel.scrollTop<=24;
    const reusable=new Map();let focused=null,previousScroll;
    if(!prefix){
      if(same){
        const active=/** @type {HTMLElement|null} */ (document.activeElement);
        for(const entry of timelineItems){
          reusable.set(entry.event.seq,entry);
          if(active && (entry.node===active || entry.heading===active || entry.node.contains?.(active)))focused=active;
        }
        previousScroll=panel.scrollTop;
      }
      clearEntryAnimations();timeline.replaceChildren();timelineItems=[];timelineTail=null;
    }
    let animationIndex=0;
    if(prefix)for(let i=0;i<timelineItems.length;i++)timelineItems[i].event=projected[i];
    for(const event of projected.slice(timelineItems.length)){
      if(!timelineTail || timelineTail.stage!==event.stage){
        const group=el('section','observation-timeline-group'), title=el('h4','observation-timeline-title',stageName(event.stage));
        const list=el('div','observation-timeline-list');group.append(title,list);timeline.append(group);
        timelineTail={stage:event.stage,list};
      }
      let entry=reusable.get(event.seq);
      if(entry && !sameTimelineIdentity(entry.event,event))entry=null;
      if(entry)entry.event=event;
      if(!entry){
        const node=el('details','observation-timeline-entry'), heading=el('summary','observation-timeline-heading');
        const icon=el('span','observation-timeline-icon'), label=el('span','observation-timeline-label'), meta=el('span','observation-timeline-meta');
        const detail=el('ul','observation-row');node.dataset.seq=String(event.seq);icon.setAttribute('role','img');
        if(animate){node.dataset.new='true';node.style.animationDelay=`${Math.min(animationIndex++,6)*50}ms`;
          node.addEventListener('animationend',()=>{delete node.dataset.new;},{once:true});}
        heading.append(icon,label,meta);node.append(heading,detail);entry={event,node,heading,icon,label,meta,detail};
      }
      timelineTail.list.append(entry.node);timelineItems.push(entry);
    }
    if(!prefix && same){
      if(previousScroll!==undefined)panel.scrollTop=previousScroll;
      if(focused && timelineItems.some(entry=>entry.node===focused || entry.heading===focused || entry.node.contains?.(focused)))
        focused.focus?.({preventScroll:true});
    }
    else if(follow)panel.scrollTop=panel.scrollHeight;
    timelineScope=scope;
    refreshTimelineItems();
  }
  function renderSupplemental(){
    const events=Array.isArray(data.events)?data.events:[], index=correlationIndex(events);
    const hasStage=new Set(events.filter(item=>item.kind==='stage').map(item=>`${item.stage}:${item.status}`));
    const latest=(kind,key)=>new Map(events.filter(item=>item.kind===kind && item[key]).map(item=>[item[key],item]));
    const tables=latest('table','item_id'), windows=latest('window','item_id'), attempts=latest('model_attempt','attempt_id');
    const represented=(item,event,fields,model=false)=>{
      if(['artifact_recovered','artifact_conflict'].includes(item.source) || !event || (model ? event.transport_status||event.status : event.status)!==item.status)return false;
      return fields.every(([recordKey,eventKey])=>item[recordKey]===null || item[recordKey]===undefined ||
        item[recordKey]===false || item[recordKey]===event[eventKey]);
    };
    const workFields=[['elapsed_seconds','elapsed_seconds'],['error_code','error_code'],['attributes','attributes'],
      ['requests','requests'],['unread_cells','unread_cells']];
    const modelFields=[['attempt','attempt'],['requested_model','requested_model'],['returned_model','returned_model'],
      ['wall_seconds','elapsed_seconds'],['error_code','error_code'],['will_retry','will_retry'],
      ['possible_duplicate_charge','possible_duplicate_charge']];
    const stageRows=(data.stage_history||[]).filter(item=>!hasStage.has(`${item.stage}:${item.status}`)).map(item=>
      timedRow(item,'started_at','elapsed_seconds',elapsed=>`${stageName(item.stage)} · ${statusName(item.status)} · ${elapsed}${item.error_code?' · '+errorName(item.error_code):''}`));
    const provenance=item=>item.source==='artifact_conflict'
      ? `事件：${statusName(item.observed_status)}；产物：${statusName(item.artifact_status)}；时间顺序未知`
      : item.source==='artifact_recovered'?'来自处理产物，时间顺序未知':'结果记录';
    const tableRows=(data.table_results||[]).filter(item=>!represented(item,tables.get(item.item_id),workFields)).map(item=>
      timedRow(item,'at','elapsed_seconds',elapsed=>`表格 ${safeText(item.item_id)||'未知'} · ${statusName(item.status)} · ${elapsed}${item.error_code?' · '+errorName(item.error_code):''}${Number.isInteger(item.unread_cells)?` · 未覆盖 ${item.unread_cells} 个单元格`:''} · ${provenance(item)}`));
    const windowRows=(data.window_results||[]).filter(item=>!represented(item,windows.get(item.item_id),workFields)).map(item=>
      timedRow(item,'at','elapsed_seconds',elapsed=>`正文窗口 ${safeText(item.item_id)||'未知'} · ${statusName(item.status)} · ${elapsed}${item.error_code?' · '+errorName(item.error_code):''} · ${provenance(item)}`));
    const modelRows=(data.model_attempts||[]).filter(item=>!represented(item,attempts.get(item.attempt_id),modelFields,true)).map(item=>{
      const name=item.requested_model||'未知',returned=item.returned_model||'未报告';
      const status=item.status==='started'?'已开始，状态待核实':statusName(item.status);
      const correlation=correlationText(item,index).join(' · ');
      return timedRow(item,'started_at','wall_seconds',elapsed=>`调用阶段：${phaseName(item.phase)} · ${correlation} · 请求第 ${item.attempt??'未知'} 次 · ${status} · 请求模型 ${name} · 返回模型 ${returned} · ${elapsed}${item.will_retry?' · 将重试':''}${item.possible_duplicate_charge?' · 可能重复计费':''}${item.error_code?' · '+errorName(item.error_code):''} · 模型账本`);
    });
    const totals=[data.summary?.tables,data.summary?.windows].some(counts=>counts && Object.values(counts).some(n=>Number.isInteger(n)&&n>0));
    const showIndex=data.state==='saved' && data.embedding_index?.status==='not_observed';
    indexScope.hidden=!showIndex;
    indexScope.textContent=showIndex?'保存后的后台检索索引属于独立流程，当前未观测。':'';
    supplemental.root.hidden=!(showIndex||totals||stageRows.length||tableRows.length||windowRows.length||modelRows.length);
    summary.textContent='表格：'+countText(data.summary?.tables,{mapped:'成功',partial:'部分',empty:'空结果',failed:'失败',not_processed:'未处理',started:'已开始待核实',unknown:'结果未知',interrupted:'已中断'})+
      '；正文窗口：'+countText(data.summary?.windows,{completed:'完成',failed:'失败',not_processed:'未处理',started:'已开始待核实',unknown:'结果未知',interrupted:'已中断'});
    supplementalRows.replaceChildren(...stageRows,...tableRows,...windowRows,...modelRows);
  }
  function render(){
    if(!data){renderConnection();return;}
    clocks=[];renderStats();
    history.textContent=data.history==='recorded' ? '' : '历史记录不完整，未记录的耗时和结果显示为未知。';
    if(data.ledger_incomplete) history.textContent+=' 模型请求记录存在缺口。';
    if([...(data.table_results||[]),...(data.window_results||[])].some(item=>item.source==='artifact_conflict'))
      history.textContent+=' 处理记录与产物记录冲突，需核对。';
    history.hidden=!history.textContent;
    renderTimeline();renderSupplemental();
    renderConnection();
  }
  return {root,update(snapshot){data=snapshot;received=now();render();},
    updateClock(snapshot){if(!data || snapshot.cursor!==data.cursor)return;
      data={...data,observed_at:snapshot.observed_at,elapsed_seconds:snapshot.elapsed_seconds};
      received=now();renderConnection();renderStats();},
    setConnection(state, detail=null){link=state;reason=detail;renderConnection();renderStats();refreshTimelineItems();},
    tick:renderStats,dispose(){data=null;received=0;}, get receivedAt(){return received;}};
}
