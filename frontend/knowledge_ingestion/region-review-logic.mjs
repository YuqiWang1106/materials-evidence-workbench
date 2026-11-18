import {clone,columnName,columnNumber} from './reading-logic.mjs';

export const REGION_PROFILE='table_regions_v1';
export const ROLE_LABELS={header:'表头',data:'数据',title:'标题',note:'说明',uncertain:'待核对'};
export const emptyRecipe=()=>({version:'1',entry_profile:REGION_PROFILE,region_review:{version:'1',regions:[]},profiles:[],excluded_ranges:[],supplements:[]});
export function bounds(value){
 const m=/^([A-Z]+)([1-9]\d*):([A-Z]+)([1-9]\d*)$/.exec(value);
 if(!m)throw Error('请选择完整范围。');const b=[Number(m[2]),columnNumber(m[1]),Number(m[4]),columnNumber(m[3])];
 if(b[0]>b[2]||b[1]>b[3])throw Error('范围起止顺序不正确。');return b;
}
export const rangeName=b=>`${columnName(b[1])}${b[0]}:${columnName(b[3])}${b[2]}`;
export const cornerRange=(a,b)=>rangeName([Math.min(a.row,b.row),Math.min(a.column,b.column),Math.max(a.row,b.row),Math.max(a.column,b.column)]);
export const contains=(address,cell)=>{const b=bounds(address);return cell.row>=b[0]&&cell.row<=b[2]&&cell.column>=b[1]&&cell.column<=b[3];};
export function intersection(a,b){const x=bounds(a),y=bounds(b),r=[Math.max(x[0],y[0]),Math.max(x[1],y[1]),Math.min(x[2],y[2]),Math.min(x[3],y[3])];return r[0]<=r[2]&&r[1]<=r[3]?rangeName(r):null;}
export function subtractRange(source,cut){
 const overlap=intersection(source,cut);if(!overlap)return [source];const a=bounds(source),b=bounds(overlap),parts=[];
 if(a[0]<b[0])parts.push([a[0],a[1],b[0]-1,a[3]]);
 if(b[2]<a[2])parts.push([b[2]+1,a[1],a[2],a[3]]);
 if(a[1]<b[1])parts.push([b[0],a[1],b[2],b[1]-1]);
 if(b[3]<a[3])parts.push([b[0],b[3]+1,b[2],a[3]]);return parts.map(rangeName);
}
export function assignRole(region,role,range){
 const next=clone(region);bounds(range);
 if(['header','data'].includes(role)&&intersection(region.table_range,range)!==range)throw Error('表头和数据必须位于当前表区内，请先调整表区。');
 next.header_ranges=next.header_ranges.flatMap(r=>subtractRange(r,range));next.data_ranges=next.data_ranges.flatMap(r=>subtractRange(r,range));
 next.context_ranges=next.context_ranges.flatMap(c=>subtractRange(c.range,range).map(r=>({...c,range:r})));
 if(role==='header')next.header_ranges.push(range);else if(role==='data')next.data_ranges.push(range);
 else if(role!=='clear')next.context_ranges.push({range,role});return next;
}
export function resizeRegion(region,range){const r=clone(region);bounds(range);r.table_range=range;for(const key of ['header_ranges','data_ranges'])r[key]=r[key].map(x=>intersection(x,range)).filter(Boolean);return r;}
export function fromCandidate(candidate,id){
 if(candidate.region_kind==='annotation_candidate')throw Error('标题或说明候选需附到已选择的表区。');
 return {id,asset_id:candidate.asset_id,sheet:candidate.sheet??null,table_range:candidate.range,
  header_ranges:clone(candidate.suggested_header_ranges||[]),data_ranges:clone(candidate.suggested_data_ranges||[]),context_ranges:[]};
}
export function rolesAt(region,cell){if(!region)return [];return [...region.header_ranges.filter(r=>contains(r,cell)).map(()=> 'header'),...region.data_ranges.filter(r=>contains(r,cell)).map(()=> 'data'),...region.context_ranges.filter(c=>contains(c.range,cell)).map(c=>c.role)];}
export async function confirmReviewed({session,review,api,isCurrent}){
 if(!isCurrent())return null;
 if(session.dirty||session.conflict||!review?.ready||review.revision!==session.draft.revision||review.draft_sha256!==session.draft.draft_sha256)throw Error('范围有变化或尚未保存，请等待核对完成。');
 const epoch=session.change;
 const result=await api('/api/table-drafts/'+encodeURIComponent(session.draft.source_id)+'/confirm-regions',{method:'POST',json:{expected_revision:review.revision,operator:'本地使用者'}});
 return isCurrent()&&session.change===epoch?result:null;
}
