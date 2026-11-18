// @ts-check
import {el,button} from './dom.js';

const fields=['page_id','revision_id','content_hash','dependency_manifest_hash'];
const types={wiki_reference:'引用主题',shared_source:'共同来源',shared_entity:'共同样品或测试'};
const changed=()=>Error('主题已变化，请刷新关联图。');
const malformed=()=>Error('关联图读取结果不完整，请刷新重试。');
const validBinding=b=>b&&fields.every(k=>typeof b[k]==='string'&&b[k].trim());
export const sameBinding=(a,b)=>validBinding(a)&&validBinding(b)&&fields.every(k=>a[k]===b[k]);
export const wikiBinding=p=>({page_id:p?.page_id,revision_id:p?.revision?.revision_id,content_hash:p?.revision?.content_hash,dependency_manifest_hash:p?.revision?.dependency_manifest_hash});

/** Validate the server's bounded graph; no relations are inferred in the browser. */
export function validateGraph(value,pageId,expected=null){
 if(!value||!validBinding(value.root_binding)||value.root_binding.page_id!==pageId||value.depth!==1||!Array.isArray(value.nodes)||!value.nodes.length||value.nodes.length>30||!Array.isArray(value.edges)||value.edges.length>60||!value.versions||typeof value.truncated!=='boolean'||!Number.isInteger(value.unavailable_count)||value.unavailable_count<0||!Array.isArray(value.truncation_reasons))throw malformed();
 if(expected&&!sameBinding(value.root_binding,expected))throw changed();
 const nodes=new Map();
 for(const n of value.nodes){if(!n||typeof n.page_id!=='string'||nodes.has(n.page_id)||!validBinding(n.binding)||n.binding.page_id!==n.page_id||!sameBinding(value.versions[n.page_id],n.binding))throw malformed();nodes.set(n.page_id,n);}
 if(!sameBinding(nodes.get(pageId)?.binding,value.root_binding)||Object.keys(value.versions).length!==nodes.size)throw malformed();
 const edges=new Set();
 for(const e of value.edges){
  if(!e||typeof e.id!=='string'||!e.id||edges.has(e.id)||!Object.hasOwn(types,e.type)||e.directed!==(e.type==='wiki_reference')||e.from===e.to||!nodes.has(e.from)||!nodes.has(e.to)||!sameBinding(e.basis?.from?.binding,nodes.get(e.from).binding)||!sameBinding(e.basis?.to?.binding,nodes.get(e.to).binding)||typeof e.basis.anchor_key!=='string')throw malformed();
  for(const side of e.type==='wiki_reference'?['from']:['from','to']){const b=e.basis[side];if(typeof b.support_id!=='string'||!Array.isArray(b.claim_ids)||typeof b.ref_hash!=='string'||!b.ref||typeof b.ref.kind!=='string')throw malformed();}
  if(e.type==='wiki_reference'&&(e.basis.from.ref.kind!=='wiki_revision'||!sameBinding(e.basis.from.ref,nodes.get(e.to).binding)))throw malformed();
  edges.add(e.id);
 }
 return value;
}

/**
 * A graph node opens only the exact still-published, available revision.
 * @param {typeof import('./app.js').api} api
 * @param {Record<string, any>} binding
 * @param {{signal?: AbortSignal}} [options]
 */
export async function readPinnedWiki(api,binding,{signal}={}){
 if(!validBinding(binding))throw changed();
 const path='/api/wiki/pages/'+encodeURIComponent(binding.page_id),options={method:'GET',signal};
 let p=await api(path,options);
 if(signal?.aborted)throw changed();
 if(p?.page_id!==binding.page_id||p.published_revision_id!==binding.revision_id)throw changed();
 if(p.revision?.revision_id!==binding.revision_id){
  const revision=p.history?.find(v=>v.revision_id===binding.revision_id);
  if(!Number.isInteger(revision?.revision)||revision.revision<1)throw changed();
  p=await api(path+'?revision='+revision.revision,options);
  if(signal?.aborted)throw changed();
  if(p?.revision?.revision!==revision.revision)throw changed();
 }
 if(!sameBinding(wikiBinding(p),binding)||p.published_revision_id!==binding.revision_id||p.availability!=='current'||!Array.isArray(p.gaps)||p.gaps.length||!p.revision.content||!Array.isArray(p.revision.content.sections))throw changed();
 return p;
}

export function zoomViewBox(box,factor,anchor){
 const width=Math.max(40,Math.min(8000,box.width*factor)),ratio=width/box.width;
 return {x:anchor.x-(anchor.x-box.x)*ratio,y:anchor.y-(anchor.y-box.y)*ratio,width,height:box.height*ratio};
}
const title=n=>typeof n?.title==='string'&&n.title.trim()?n.title:'未命名主题';
function relationLabel(e){const kind=e.basis.from.entity_kind;return e.type==='shared_entity'&&kind===e.basis.to.entity_kind?({sample:'共同样品',test:'共同测试'}[kind]||types.shared_entity):types[e.type];}
let graphNumber=0;

export function mountWikiRelations({root,api,binding,onOpenWiki,isCurrent=()=>true}){
 const document=root.ownerDocument,initial=structuredClone(binding),markerId='wiki-relation-arrow-'+(++graphNumber);
 let disposed=false,generation=0,request=null,data=null,selected=null,busy=false,removeCanvas=()=>{};
 const active=()=>!disposed&&isCurrent();
 root.className='wiki-relations';
 const toolbar=el('div','wiki-relations-toolbar'),heading=el('h2',null,'关联图'),state=el('p','wiki-relations-status'),body=el('div','wiki-relations-body');
 state.setAttribute('role','status');heading.tabIndex=-1;
 const refresh=button('刷新',()=>load(data?.root_binding||initial,true),'quiet');toolbar.append(heading,refresh);root.replaceChildren(toolbar,state,body);
 function svgNode(tag,attributes={},text){const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [key,value]of Object.entries(attributes))n.setAttribute(key,value);if(text!==undefined)n.textContent=text;return n;}
 async function load(target,acceptLatest=false){
  if(!active())return;
  const mine=++generation;request?.abort();request=new AbortController();busy=true;state.textContent='正在读取关联…';
  try{
   const value=await api('/api/wiki/pages/'+encodeURIComponent(target.page_id)+'/relations?depth=1',{method:'GET',signal:request.signal});
   if(!active()||mine!==generation)return;
   data=validateGraph(value,target.page_id,acceptLatest?null:target);selected=data.root_binding.page_id;busy=false;render();
   state.textContent=data.truncated||data.unavailable_count? '部分关联暂不可用或未显示。' : '';
  }catch(e){if(active()&&mine===generation){busy=false;state.textContent=(e?.message?.startsWith('主题已变化')||e?.message?.startsWith('关联图读取结果')?e.message:'关联图读取失败，请刷新重试。')+(data?' 保留上次读取的关联图。':'');}}
 }
 function render(){
  removeCanvas();body.replaceChildren();
  const nodes=new Map(data.nodes.map(n=>[n.page_id,n])),ordered=[nodes.get(data.root_binding.page_id),...data.nodes.filter(n=>n.page_id!==data.root_binding.page_id).sort((a,b)=>title(a).localeCompare(title(b),'zh')||a.page_id.localeCompare(b.page_id))];
  heading.textContent=title(ordered[0]);
  const canvas=el('div','wiki-relations-canvas'),controls=el('div','wiki-relations-zoom'),aside=el('aside','wiki-relations-sidebar'),list=el('div','wiki-relations-list'),preview=el('div','wiki-relations-preview');
  list.setAttribute('aria-label','关联主题');aside.append(list,preview);body.append(canvas,aside);
  const radius=Math.max(190,(ordered.length-1)*30),extent=radius+160,fit={x:-extent,y:-extent,width:extent*2,height:extent*2};let box={...fit};
  const svg=svgNode('svg',{class:'wiki-relations-svg',role:'group','aria-label':'Wiki 关联图',tabindex:'0'}),defs=svgNode('defs'),marker=svgNode('marker',{id:markerId,viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:7,markerHeight:7,orient:'auto-start-reverse'});
  marker.append(svgNode('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'#778972'}));defs.append(marker);svg.append(defs);canvas.append(svg,controls);
  const setBox=value=>{box=value;svg.setAttribute('viewBox',[box.x,box.y,box.width,box.height].join(' '));};setBox(box);
  const zoom=factor=>setBox(zoomViewBox(box,factor,{x:box.x+box.width/2,y:box.y+box.height/2}));
  for(const [label,fn]of [['放大',()=>zoom(.8)],['缩小',()=>zoom(1.25)],['适合视图',()=>setBox({...fit})]]){const b=button(label,fn,'quiet');b.setAttribute('aria-label',label);controls.append(b);}
  const positions=new Map(ordered.map((n,i)=>{const angle=-Math.PI/2+(i-1)*Math.PI*2/Math.max(1,ordered.length-1);return[n.page_id,i?{x:Math.cos(angle)*radius,y:Math.sin(angle)*radius}:{x:0,y:0}];}));
  const nodeElements=new Map(),edgeElements=new Map(),rows=new Map();
  function basisView(e){
   const area=el('div','wiki-relations-basis'),from=nodes.get(e.from),to=nodes.get(e.to);
   area.append(el('p',null,title(from)+(e.directed?' → ':' ↔ ')+title(to)));
   const meanings={wiki_reference:'此主题引用了另一主题的固定发布版本。',shared_source:'两个主题使用了同一来源；不表示结论相同。',shared_entity:'两个主题关联到同一固定身份的样品或测试。'};area.append(el('p',null,meanings[e.type]));
   for(const side of ['from','to']){const b=e.basis[side],ref=b.ref;if(!ref)continue;const kind={table_snapshot:'原表',record_revision:'论文或专利记录',sample_test_context:'样品与测试',assertion:'记录结论',evidence:'记录依据',source_snapshot:'来源快照',table_analysis_artifact:'分析结果',wiki_revision:'Wiki 发布版本',source_passage:'来源原段'}[ref.kind]||'已绑定来源';const details=[kind,ref.table_range,b.entity_kind==='sample'?'样品':b.entity_kind==='test'?'测试':'',b.claim_ids.length?'用于 '+b.claim_ids.length+' 条正文':''].filter(Boolean);area.append(el('p',null,title(nodes.get(e[side]))+' · '+details.join(' · ')));}
   return area;
  }
  function choose(id,edgeId=null){
   selected=id;
   for(const [nid,n]of nodeElements)n.setAttribute('class','wiki-relations-node'+(nid===id?' is-selected':'')+(nid===data.root_binding.page_id?' is-root':''));
   for(const [nid,row]of rows)row.setAttribute('aria-pressed',String(nid===id));
   for(const e of data.edges)edgeElements.get(e.id).setAttribute('class','wiki-relations-edge '+e.type+(e.from===id||e.to===id?' is-selected':''));
   preview.replaceChildren(el('h3',null,title(nodes.get(id))));
   if(id===data.root_binding.page_id)preview.append(el('p','wiki-relations-root-label','当前中心'));
   const actions=el('div','wiki-relations-node-actions'),open=button('查看 Wiki',event=>{if(active()&&!busy)onOpenWiki(structuredClone(nodes.get(id).binding),event.currentTarget);},'quiet'),center=button('以此为中心',()=>load(nodes.get(id).binding),'quiet');center.disabled=id===data.root_binding.page_id;actions.append(open,center);preview.append(actions);
   const incident=data.edges.filter(e=>e.from===id||e.to===id);
   if(!incident.length&&!data.truncated&&!data.unavailable_count)preview.append(el('p','wiki-relations-empty','暂无可展示的关联主题。'));
   for(const e of incident){const relation=el('div','wiki-relations-relation'),other=nodes.get(e.from===id?e.to:e.from),description=e.type==='wiki_reference'?(e.from===id?'引用了此主题':'被此主题引用'):relationLabel(e);relation.append(el('span','wiki-relations-tag '+e.type,description),el('span',null,title(other)));const detail=el('div');let expanded=e.id===edgeId;const toggle=button('关联依据',()=>{expanded=!expanded;toggle.setAttribute('aria-expanded',String(expanded));detail.replaceChildren(...(expanded?[basisView(e)]:[]));},'quiet');toggle.setAttribute('aria-expanded',String(expanded));if(expanded)detail.append(basisView(e));relation.append(toggle,detail);preview.append(relation);}
  }
  const parallel=new Map();
  for(const e of data.edges){const a=positions.get(e.from),b=positions.get(e.to),dx=b.x-a.x,dy=b.y-a.y,length=Math.hypot(dx,dy),key=[e.from,e.to].sort().join('|'),index=parallel.get(key)||0;parallel.set(key,index+1);const offset=index*26,ax=a.x+dx/length*25,ay=a.y+dy/length*25,bx=b.x-dx/length*25,by=b.y-dy/length*25;
   const path=svgNode('path',{d:`M ${ax} ${ay} Q ${(ax+bx)/2-dy/length*offset} ${(ay+by)/2+dx/length*offset} ${bx} ${by}`,fill:'none',class:'wiki-relations-edge '+e.type,tabindex:'0',role:'button','aria-label':title(nodes.get(e.from))+' '+relationLabel(e)+' '+title(nodes.get(e.to))});if(e.directed)path.setAttribute('marker-end','url(#'+markerId+')');path.append(svgNode('title',{},relationLabel(e)));const pick=()=>choose(e.from,e.id);path.addEventListener('click',pick);path.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();pick();}});edgeElements.set(e.id,path);svg.append(path);
  }
  for(const n of ordered){const p=positions.get(n.page_id),g=svgNode('g',{transform:`translate(${p.x} ${p.y})`,tabindex:'0',role:'button','aria-label':title(n),class:'wiki-relations-node'});g.append(svgNode('title',{},title(n)),svgNode('circle',{r:n.page_id===data.root_binding.page_id?25:21}),svgNode('text',{y:43,'text-anchor':'middle'},title(n).length>13?title(n).slice(0,12)+'…':title(n)));const pick=()=>{choose(n.page_id);rows.get(n.page_id)?.scrollIntoView?.({block:'nearest'});};g.addEventListener('click',pick);g.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();pick();}});nodeElements.set(n.page_id,g);svg.append(g);const row=button(title(n),()=>choose(n.page_id),'quiet');row.className+=' wiki-relations-topic';row.setAttribute('aria-pressed','false');rows.set(n.page_id,row);list.append(row);}
  const legend=el('div','wiki-relations-legend');for(const [kind,label]of Object.entries(types))legend.append(el('span','wiki-relations-tag '+kind,label));canvas.append(legend);
  const point=event=>{try{const matrix=svg.getScreenCTM();if(!matrix)return null;const p=svg.createSVGPoint();p.x=event.clientX;p.y=event.clientY;return p.matrixTransform(matrix.inverse());}catch{return null;}};
  let drag=null;
  const down=event=>{if(event.target!==svg||event.button!==0)return;const p=point(event);if(!p)return;drag={id:event.pointerId,point:p};svg.setPointerCapture(event.pointerId);event.preventDefault();};
  const move=event=>{if(!drag||drag.id!==event.pointerId)return;const p=point(event);if(p)setBox({...box,x:box.x+drag.point.x-p.x,y:box.y+drag.point.y-p.y});};
  const up=()=>{if(drag){if(svg.hasPointerCapture?.(drag.id))svg.releasePointerCapture(drag.id);drag=null;}};
  const wheel=event=>{const p=point(event);if(!p)return;event.preventDefault();setBox(zoomViewBox(box,event.deltaY>0?1.12:1/1.12,p));};
  const keys=event=>{if(event.key==='+'||event.key==='='){event.preventDefault();zoom(.8);}else if(event.key==='-'){event.preventDefault();zoom(1.25);}else if(event.key==='0'){event.preventDefault();setBox({...fit});}};
  const listeners=[['pointerdown',down],['pointermove',move],['pointerup',up],['pointercancel',up],['wheel',wheel],['keydown',keys]];for(const [name,fn]of listeners)svg.addEventListener(name,fn,name==='wheel'?{passive:false}:undefined);
  removeCanvas=()=>{up();for(const [name,fn]of listeners)svg.removeEventListener(name,fn);};choose(selected);heading.focus();
 }
 const ready=load(initial);
 return {ready,dispose(){if(disposed)return;disposed=true;++generation;request?.abort();removeCanvas();}};
}
