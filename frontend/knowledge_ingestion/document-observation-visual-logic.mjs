/** Pure projections and labels for visual-reading timeline events. */
import {finite, visualTargets, visualSummary, VISUAL_KINDS} from './document-observation-labels.mjs';

export function projectVisualSteps(events) {
  const groups=new Map(), replacements=new Map(), skip=new Set();
  for(const event of events)if(event.kind==='visual_step' && event.visual_step==='render' && event.item_id){
    if(!groups.has(event.item_id))groups.set(event.item_id,[]);
    groups.get(event.item_id).push(event);
  }
  for(const group of groups.values()){
    const starts=group.filter(e=>e.status==='started'),ends=group.filter(e=>e.status!=='started');
    if(starts.length!==1 || ends.length!==1 || starts[0].seq>=ends[0].seq || starts[0].stage!==ends[0].stage)continue;
    const start=starts[0],end=ends[0];
    if(Array.isArray(start.visual_targets) && Array.isArray(end.visual_targets) &&
      JSON.stringify(start.visual_targets)!==JSON.stringify(end.visual_targets))continue;
    replacements.set(start.seq,{...start,...end,seq:start.seq,at:start.at,_visualEvents:[start,end]});
    skip.add(end.seq);
  }
  return events.filter(event=>!skip.has(event.seq)).map(event=>replacements.get(event.seq)||event);
}

export function distinguishVisualAreas(events) {
  const groups=new Map(), coordinates=new Map();
  for(const event of events){
    if(event.kind!=='visual_step' || event.visual_step!=='result' || visualTargets(event).length!==1)continue;
    const target=visualTargets(event)[0],box=target.bbox;
    if(!Array.isArray(box) || box.length!==4 || !box.every(finite))continue;
    const key=JSON.stringify([target.document_index||null,target.page,target.kind]);
    if(!groups.has(key))groups.set(key,[]);
    groups.get(key).push({event,box:JSON.stringify(box)});
  }
  for(const group of groups.values()){
    if(group.length<2 || new Set(group.map(x=>x.box)).size!==group.length ||
       new Set(group.map(x=>x.event.item_id)).size!==group.length || group.some(x=>!x.event.item_id))continue;
    group.forEach(item=>coordinates.set(item.event.seq,JSON.parse(item.box)));
  }
  return events.map(event=>coordinates.has(event.seq)?{...event,_visualCoordinates:coordinates.get(event.seq)}:event);
}

export function visualStepLabel(item) {
  const location=visualSummary(item), target=visualTargets(item)[0], kind=VISUAL_KINDS[target?.kind]||'区域';
  if(item.visual_step==='selection'){
    if(item.status==='empty')return item.visual_reason==='regular_visual_no_targets'
      ? '常规图片、图表、公式和整页无需补充识别' : '视觉区域选择结果未记录';
    const count=Number.isInteger(item.item_count) && item.item_count>=0 ? `${item.item_count} 项` : '数量未知';
    return `视觉区域选择 · 已选 ${count}`;
  }
  if(item.visual_step==='reuse')return `${location||'位置未记录'} · ${kind} · 复用 MinerU 结果`;
  if(item.visual_step==='render')return `${location||'位置未记录'} · ${target?.kind==='handwriting_table'?'手写表格':'页面'}图像准备`;
  if(item.visual_step==='result')return `${location||'位置未记录'} · ${kind}${item._visualCoordinates?` · 位置 [${item._visualCoordinates.join(', ')}]`:''}识别结果`;
  return '视觉处理记录';
}
