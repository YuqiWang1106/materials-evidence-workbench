import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {DraftSession} from '../reading-logic.mjs';
const url=new URL('../region-review-logic.mjs',import.meta.url);
async function logic(){assert.ok(fs.existsSync(url),'The independent region-review logic is missing');return import(url);}
const region={id:'r1',asset_id:'a',sheet:null,table_range:'A1:D6',header_ranges:['A1:D2'],data_ranges:['A3:D6'],context_ranges:[]};
test('rectangle subtraction retains the four surrounding pieces without overlap',async()=>{
 const m=await logic();assert.deepEqual(m.subtractRange('A1:D6','B2:C4'),['A1:D1','A5:D6','A2:A4','D2:D4']);
 assert.deepEqual(m.subtractRange('A1:D6','A1:D6'),[]);assert.deepEqual(m.subtractRange('A1:D6','F1:F4'),['A1:D6']);
});
test('marking title removes its cells from old headers and data without modifying original region',async()=>{
 const m=await logic(),r=m.assignRole(region,'title','A1:D1');
 assert.deepEqual(r.header_ranges,['A2:D2']);assert.deepEqual(r.context_ranges,[{range:'A1:D1',role:'title'}]);
 assert.deepEqual(region.header_ranges,['A1:D2']);
 const again=m.assignRole(r,'data','A1:D1');assert.deepEqual(again.context_ranges,[]);assert.ok(again.data_ranges.includes('A1:D1'));
});
test('resizing clips header/data while retaining original external context',async()=>{
 const m=await logic(),r=m.resizeRegion({...region,context_ranges:[{range:'F1:F1',role:'note'}]},'B2:C4');
 assert.deepEqual(r.header_ranges,['B2:C2']);assert.deepEqual(r.data_ranges,['B3:C4']);assert.equal(r.context_ranges[0].range,'F1:F1');
 assert.throws(()=>m.assignRole(r,'data','A1:A1'),/表区/);
});
test('two corners normalize reversed selections and can span pages',async()=>{
 const m=await logic();assert.equal(m.cornerRange({row:28,column:22},{row:2,column:1}),'A2:V28');
 assert.equal(m.cornerRange({row:3,column:2},{row:3,column:2}),'B3:B3');
});
test('annotation candidates never become data tables; title suggestions are not silently applied',async()=>{
 const m=await logic();assert.throws(()=>m.fromCandidate({region_kind:'annotation_candidate',range:'A1:B1'},'r1'),/标题|说明/);
 const r=m.fromCandidate({asset_id:'a',sheet:null,region_kind:'uncertain',range:'A1:D6',suggested_header_ranges:['A1:D2'],suggested_data_ranges:['A3:D6'],context_candidates:[{range:'A1:D1',role:'title'}]},'r2');
 assert.deepEqual(r.context_ranges,[]);assert.equal(r.table_range,'A1:D6');assert.deepEqual(r.header_ranges,['A1:D2']);
});
test('confirmation uses only the exact visible revision and never calls extraction',async()=>{
 const m=await logic(),calls=[],draft={source_id:'s',revision:3,draft_sha256:'hash',recipe:m.emptyRecipe(),reader_options:{}};
 const session=new DraftSession(draft,async()=>{}),review={ready:true,revision:3,draft_sha256:'hash'};
 const result=await m.confirmReviewed({session,review,isCurrent:()=>true,api:async(path,options)=>{calls.push([path,options]);return {range_confirmed:true,revision:3};}});
 assert.equal(result.range_confirmed,true);assert.equal(calls[0][0],'/api/table-drafts/s/confirm-regions');assert.equal(calls[0][1].json.expected_revision,3);
 session.edit({recipe:m.emptyRecipe()});await assert.rejects(()=>m.confirmReviewed({session,review,isCurrent:()=>true,api:async()=>assert.fail()}),/保存|变化/);
});
test('late confirmation is not displayed after an edit or route change',async()=>{
 const m=await logic(),session=new DraftSession({source_id:'s',revision:3,draft_sha256:'h'},async()=>{});
 let resolve;const pending=m.confirmReviewed({session,review:{ready:true,revision:3,draft_sha256:'h'},isCurrent:()=>true,api:()=>new Promise(r=>resolve=r)});
 session.edit({title:'new'});resolve({range_confirmed:true});assert.equal(await pending,null);
});
