export const DOCUMENT_CATEGORIES = [
  ['composition','组成与配方'], ['process','制备过程'], ['testing','测试与条件'],
  ['results','测试结果'], ['insights','研究认识'], ['other','其他记录'],
];

export function categoryForDocumentRow(row) {
  const field = String(row?.field_id || '');
  if (/^(composition|material|formulation|sample)\./.test(field)) return 'composition';
  if (/^(process|preparation)\./.test(field)) return 'process';
  if (/^(observation|series|result|performance)\./.test(field)) return 'results';
  if (/^(method|condition|instrument|test|activity)\./.test(field)) return 'testing';
  if (/^(claim|mechanism|interpretation|conclusion|limitation|knowledge)\./.test(field)) return 'insights';
  return 'other';
}

/** Group only by typed ownership projected by the backend, never by matching labels. */
export function buildDocumentScopes(rows = []) {
  const samples = new Map(), units = new Map();
  const other = {key:'other', label:'其他未归属内容', kind:'other', rows:[]};
  const append = (map, key, label, kind, row) => {
    if (!map.has(key)) map.set(key,{key,label:label || '未命名',kind,rows:[]});
    const scope = map.get(key);
    if (!scope.rows.some(item=>item.id===row.id)) scope.rows.push(row);
  };
  for (const row of rows) {
    const owners = new Map();
    for (const context of row.contexts || []) {
      if (context?.sample_ref) owners.set(context.sample_ref,context.sample_label || '未命名样品');
    }
    if (row.kind === 'sample' && row.subject_ref) owners.set(row.subject_ref,row.subject || '未命名样品');
    for (const [key,label] of owners) append(samples,`sample:${key}`,label,'sample',row);
    if (row.contexts_truncated) other.rows.push(row);
    else if (!owners.size && row.kind === 'content_unit' && row.subject_ref) {
      append(units,`unit:${row.subject_ref}`,`${row.subject || '未命名实施例'} · 样品待关联`,'unit',row);
    } else if (!owners.size) other.rows.push(row);
  }
  const output=[...samples.values(),...units.values()];
  const occurrences = new Map();
  for (const scope of samples.values()) occurrences.set(scope.label,(occurrences.get(scope.label)||0)+1);
  const ordinals = new Map();
  for (const scope of samples.values()) {
    if (occurrences.get(scope.label)>1) {
      const ordinal=(ordinals.get(scope.label)||0)+1;
      ordinals.set(scope.label,ordinal);
      scope.label=`${scope.label}（记录 ${ordinal}）`;
    }
  }
  if (other.rows.length) output.push(other);
  return output;
}

export function documentRowsPage(scope, category, query='', page=0, pageSize=10) {
  const term=String(query).trim().toLocaleLowerCase();
  const filtered=(scope?.rows||[]).filter(row => categoryForDocumentRow(row)===category &&
    (!term || [row.field,row.value,row.unit,row.basis,row.subject, ...(row.contexts||[]).map(c=>c.test_label)]
      .some(value=>String(value ?? '').toLocaleLowerCase().includes(term))));
  const index=Math.max(0,Math.floor(page));
  return {rows:filtered.slice(index*pageSize,(index+1)*pageSize),total:filtered.length,
    page:index,pages:Math.max(1,Math.ceil(filtered.length/pageSize))};
}

function renderedRegion(c, page, legacy=false) {
  if (!Number.isInteger(page) || page<=0 || !c || typeof c!=='object' || Array.isArray(c) ||
      ((!legacy || Object.hasOwn(c,'page')) && c.page!==page) || c.unit!=='pt' || c.origin!=='top_left' ||
      c.coordinate_space!=='rendered_page' || !Array.isArray(c.bbox) || c.bbox.length!==4) return null;
  const [x0,y0,x1,y1]=c.bbox;
  const width=legacy?(c.width??c.page_width):c.width, height=legacy?(c.height??c.page_height):c.height;
  if (![x0,y0,x1,y1,width,height].every(Number.isFinite) || width<=0 || height<=0 ||
      x0<0 || y0<0 || x1<=x0 || y1<=y0 || x1>width || y1>height ||
      (x1-x0)/width>=0.95 && (y1-y0)/height>=0.95) return null;
  return {left:x0/width*100,top:y0/height*100,width:(x1-x0)/width*100,height:(y1-y0)/height*100};
}

/** A legacy region hint is shown only if its box belongs to the rendered page. */
export function evidenceRegion(anchor, page) {
  if (anchor?.kind!=='pdf' || !Array.isArray(anchor.page_numbers) || !anchor.page_numbers.includes(page)) return null;
  return renderedRegion(anchor.coordinate_region,page,true);
}

/** New boxes are independent rendered-page lines; an explicit empty result never uses the old union. */
export function evidenceRegions(location, page) {
  if (!location || typeof location!=='object') return [];
  if (Object.hasOwn(location,'highlight_regions')) {
    if (!Array.isArray(location.highlight_regions)) return [];
    return location.highlight_regions.map(region=>renderedRegion(region,page)).filter(Boolean);
  }
  const region=evidenceRegion(location.anchor,page);
  return region?[region]:[];
}

export function reviewState(reviews=[], blocked=false) {
  if (blocked || reviews.some(row=>['rejected','uncertain'].includes(row.decision))) return 'blocked';
  return reviews.some(row=>row.decision==='confirmed_source')?'confirmed':'unreviewed';
}
