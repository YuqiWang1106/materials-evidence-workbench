import test from 'node:test';
import assert from 'node:assert/strict';
import * as logic from '../document-candidate-logic.mjs';

const sample0='/studies/0/samples/0', sample1='/studies/0/samples/1';
const fact=(name,value,extra={})=>({ref:null,name,value,unit:'MPa',evidence:['e'],conditions:[],...extra});
const fixture=()=>({candidate_id:'g',reviews:[],source_locations:{e:{status:'page_only',page:2}},raw:{
  evidence:[{ref:'e',quote:'A measured result'}],issues:[],statements:[],
  shared_methods:[{ref:'method',label:'Shared protocol',applies_to:['t1'],details:[fact('Speed','3')]}],
  studies:[{ref:'study',label:'Study A',samples:[
    {ref:'s0',label:'Same',composition:Array.from({length:7},(_,i)=>fact(`Material ${i}`,i,{material:`Material ${i}`})),preparation:[],tests:[
      {ref:'t0',label:'Test',method_ref:'method',conditions:[fact('Temperature',23)],results:[fact('Strength',10,{conditions:['23 C']})]},
      {ref:'t1',label:'Test',conditions:[],results:[fact('Strength',20,{conditions:['80 C']})]},
      {ref:'t2',label:'Empty',method_ref:'method',conditions:[],results:[]}]},
    {ref:'s1',label:'Same',composition:[],preparation:[],tests:[{ref:'t3',label:'Other test',conditions:[],results:[]}]}
  ]}]
}});

test('same-name samples and tests preserve separate original pointer identities',()=>{
  const group=fixture(),model=logic.buildReviewModel([group]);
  assert.deepEqual(model.scopes.filter(s=>s.samplePointer).map(s=>s.key),[`g:${sample0}`,`g:${sample1}`]);
  assert.equal(model.facts.length,11);
  assert.equal(new Set(model.facts.map(f=>f.key)).size,11);
  const tests=model.scopes[0].blocks.filter(b=>b.category==='tests');
  assert.equal(tests.length,3);
  assert.deepEqual(tests[0].rows.map(f=>f.pointer),[`${sample0}/tests/0/conditions/0`,`${sample0}/tests/0/results/0`]);
  assert.deepEqual(tests[0].rows[1].item.conditions,['23 C']);
  assert.deepEqual(tests[1].rows[0].item.conditions,['80 C']);
  assert.ok(model.facts.every(f=>f.group===group));
});

test('pages preserve each test boundary and show composition in one 25-row page',()=>{
  const scope=logic.buildReviewModel([fixture()]).scopes[0];
  const pages=logic.pagesFor(scope,'composition');
  assert.deepEqual(pages.map(p=>[p.rows.length,p.part,p.parts]),[[7,1,1]]);
  assert.equal(pages[0].rows[5].pointer,`${sample0}/composition/5`);
  const tests=logic.pagesFor(scope,'tests');
  assert.deepEqual(tests.map(p=>p.rows.length),[2,1,0]);
  assert.equal(tests[2].block.test.label,'Empty');
  assert.equal(logic.pagesFor(scope,'absent').length,0);
});

test('shared facts appear once and test links require method_ref or explicit applies_to',()=>{
  const group=fixture();group.raw.shared_methods.push({ref:'unknown',label:'Unknown',applies_to:[],details:[fact('Unknown detail',5)]});
  const model=logic.buildReviewModel([group]),sample=model.scopes[0],shared=model.scopes.find(s=>s.label==='共用方法');
  assert.equal(shared.blocks.flatMap(b=>b.rows).length,2);
  assert.equal(model.facts.filter(f=>f.pointer==='/shared_methods/0/details/0').length,1);
  const tests=sample.blocks.filter(b=>b.category==='tests');
  assert.deepEqual(tests.map(b=>b.methods.map(m=>m.label)),[['Shared protocol'],['Shared protocol'],['Shared protocol']]);
  assert.equal(tests[0].methods[0].scopeKey,shared.key);
  assert.ok(shared.blocks.some(b=>b.key===tests[0].methods[0].blockKey));
  assert.equal(model.scopes[1].blocks.find(b=>b.category==='tests').methods.length,0);
  assert.match(shared.blocks[0].context,/适用于：.*Same.*Test/);
  assert.doesNotMatch(shared.blocks[0].context,/Other test/);
  assert.equal(shared.blocks[1].context,'适用对象待确认');
});

test('ownership correction moves displayed rows without changing original facts or review pointers',()=>{
  const group=fixture(),original=structuredClone(group.raw);
  group.reviews=[{candidate_ref:`${sample0}/composition/0`,decision:'uncertain',correction:{value:99},ownership_target_ref:sample1},
    {candidate_ref:`${sample0}/tests/0/results/0`,decision:'corrected',correction:{unit:'GPa'},ownership_target_ref:`${sample1}/tests/0`}];
  const model=logic.buildReviewModel([group]),moved=model.facts.find(f=>f.pointer===`${sample0}/composition/0`);
  assert.equal(moved.originalOwnerPointer,sample0);assert.equal(moved.ownerPointer,sample1);
  assert.equal(moved.item.value,0);assert.equal(logic.displayFact(moved).value,99);
  assert.ok(!model.scopes[0].blocks.flatMap(b=>b.rows).includes(moved));
  assert.ok(model.scopes[1].blocks.flatMap(b=>b.rows).includes(moved));
  const result=model.scopes[1].blocks.find(b=>b.test).rows[0];
  assert.equal(result.pointer,`${sample0}/tests/0/results/0`);assert.equal(result.ownerPointer,`${sample1}/tests/0`);
  assert.deepEqual(group.raw,original);
});

test('ownership targets exclude missing identities, malformed objects, and other groups',()=>{
  const group=fixture();group.raw.studies[0].samples.push({label:'No identity',tests:[{label:'No test identity'}]},null,'bad');
  const model=logic.buildReviewModel([group]);
  assert.equal(model.targets.length,6);
  assert.ok(model.targets.every(t=>t.groupId==='g'&&['sample','test'].includes(t.kind)));
  assert.deepEqual(model.targets.filter(t=>t.kind==='sample').map(t=>t.pointer),[sample0,sample1]);
});

test('duplicate visible sample and test target names receive distinguishable labels without merging identities',()=>{
  const model=logic.buildReviewModel([fixture()]);
  const sampleScopes=model.scopes.filter(s=>s.samplePointer);
  assert.equal(new Set(sampleScopes.map(s=>s.label)).size,2);
  const sampleTargets=model.targets.filter(t=>t.kind==='sample');
  assert.equal(new Set(sampleTargets.map(t=>t.label)).size,2);
  const testTargets=model.targets.filter(t=>t.kind==='test');
  assert.equal(new Set(testTargets.map(t=>t.label)).size,4);
  assert.ok(testTargets.find(t=>t.pointer===`${sample0}/tests/2`).label.endsWith('Empty'));
  assert.deepEqual(sampleTargets.map(t=>t.pointer),[sample0,sample1]);
});

test('same-name studies retain distinct original pointers and distinguishable labels across scopes and ownership targets',()=>{
  const group=fixture();
  group.raw.studies.push({ref:'study2',label:'Study A',samples:[{ref:'other-sample',label:'Other sample',composition:[],preparation:[],tests:[]}]},
    {ref:'study3',label:'Unique study',samples:[{ref:'unique-sample',label:'Unique sample',composition:[],preparation:[],tests:[]}]});
  group.raw.statements=[fact('first study statement',1,{about:['study']}),fact('second study statement',2,{about:['study2']})];
  const model=logic.buildReviewModel([group]);
  const first=model.scopes.find(s=>s.samplePointer===sample0),second=model.scopes.find(s=>s.samplePointer==='/studies/1/samples/0');
  assert.equal(first.studyPointer,'/studies/0');assert.equal(second.studyPointer,'/studies/1');
  assert.notEqual(first.studyLabel,second.studyLabel);
  assert.equal(model.scopes.find(s=>s.samplePointer==='/studies/2/samples/0').studyLabel,'Unique study');
  const statementScopes=model.scopes.filter(s=>s.label==='研究陈述');
  assert.deepEqual(statementScopes.map(s=>s.studyPointer),['/studies/0','/studies/1']);
  assert.deepEqual(statementScopes.map(s=>s.studyLabel),[first.studyLabel,second.studyLabel]);
  assert.ok(model.targets.find(t=>t.pointer===sample0).label.startsWith(first.studyLabel+' · '));
  assert.ok(model.targets.find(t=>t.pointer==='/studies/1/samples/0').label.startsWith(second.studyLabel+' · '));
});

test('statements use explicit sample, test, study and method references; unknowns remain unclassified',()=>{
  const group=fixture();group.raw.statements=[fact('sample',1,{about:['s0']}),fact('test',2,{about:['t3']}),fact('study',3,{about:['study']}),fact('method',4,{about:['method']}),fact('unknown',5,{about:[]}),fact('unresolved',6,{about:['absent']})];
  const model=logic.buildReviewModel([group]);
  const statements=scope=>scope.blocks.filter(b=>b.category==='statements').flatMap(b=>b.rows).map(f=>f.item.name);
  assert.deepEqual(statements(model.scopes[0]),['sample']);
  assert.deepEqual(statements(model.scopes[1]),['test']);
  assert.deepEqual(statements(model.scopes.find(s=>s.label==='研究陈述')),['study']);
  assert.deepEqual(statements(model.scopes.find(s=>s.label==='共用方法')),['method']);
  assert.deepEqual(statements(model.scopes.find(s=>s.label==='未归类陈述')),['unknown','unresolved']);
  assert.equal(model.facts.filter(f=>f.role==='statements').length,6);
});

test('sample explanations keep their original sample while explicit field explanations follow the exact fact',()=>{
  const group=fixture();group.raw.issues=[{description:'Original sample warning',refs:['s0']},{description:'Evidence warning',refs:['e']},{description:'Exact field warning',refs:['s0']}];
  group.reviews=[{candidate_ref:`${sample0}/composition/0`,decision:'uncertain',ownership_target_ref:sample1}];
  annotate(group,[[0,'sample',[sample0]],[2,'field',[`${sample0}/composition/0`]]]);
  const model=logic.buildReviewModel([group]);
  assert.deepEqual(model.scopes.find(s=>s.samplePointer===sample0).sampleIssues.map(i=>i.description),['Original sample warning']);
  assert.deepEqual(model.scopes.find(s=>s.samplePointer===sample1).sampleIssues,[]);
  assert.deepEqual(model.facts[0].warnings,['Exact field warning']);
  assert.deepEqual(model.facts.find(f=>f.role==='details').warnings,[]);
  assert.deepEqual(model.documentIssues.map(i=>i.description),['Evidence warning']);
  group.raw.issues=[{description:'Whole candidate uncertain',refs:[]}];delete group.issue_annotations;
  const legacy=logic.buildReviewModel([group]);
  assert.ok(legacy.facts.every(f=>!f.warnings.length));
  assert.deepEqual(legacy.documentIssues.map(i=>i.description),['Whole candidate uncertain']);
});

test('targeted issue evidence does not warn unrelated facts sharing the same quote',()=>{
  const group=fixture();group.raw.studies[0].samples[0].composition[0].ref='f1';
  group.raw.issues=[{description:'Only f1 is uncertain',refs:['f1'],evidence:['e']}];
  annotate(group,[[0,'field',[`${sample0}/composition/0`]]]);
  const model=logic.buildReviewModel([group]);
  assert.deepEqual(model.facts[0].warnings,['Only f1 is uncertain']);
  assert.deepEqual(model.facts[1].warnings,[]);
});

test('malformed arrays and objects retain a safe fallback without crashing the entire candidate page',()=>{
  const group={candidate_id:'broken',raw:{studies:'bad',shared_methods:{},statements:1,evidence:null,issues:'bad'},reviews:{},source_locations:[]};
  const model=logic.buildReviewModel([null,group]);assert.equal(model.facts.length,0);assert.equal(model.scopes.length,1);assert.equal(model.scopes[0].group,group);assert.equal(model.scopes[0].rawOnly,true);
  assert.deepEqual(logic.buildReviewModel(null),{scopes:[],facts:[],targets:[],documentIssues:[]});
  const nested=fixture();nested.raw.studies[0].samples[0].tests=[null,{ref:'bad-test',results:'bad',conditions:{}}];nested.raw.studies[0].samples[0].composition=[null,17,[],{value:{nested:true},evidence:'e'}];
  assert.doesNotThrow(()=>logic.buildReviewModel([nested]));
});

test('safe formatters preserve structured content and existing corrections for every review decision',()=>{
  assert.deepEqual(logic.array({}),[]);assert.deepEqual(logic.object([]),{});assert.deepEqual(logic.object(null),{});
  assert.equal(logic.text({nested:true}),'{"nested":true}');assert.equal(logic.text(null),'');
  assert.deepEqual(logic.summary({material:'Polymer',name:'Ignored',value:0,unit:'phr'}),{name:'Polymer',value:'0 phr'});
  for(const decision of ['uncertain','confirmed','corrected']){
    const reviewed={item:{value:3},review:{decision,correction:{value:4}}};
    assert.equal(logic.displayFact(reviewed).value,4);assert.equal(logic.accepted(reviewed),decision!=='uncertain');
  }
});

test('canonical scopes assign every fact once using server membership and keep distinct methods',()=>{
 const group=fixture();group.raw.statements=[fact('cross',1,{about:['s0','s1']})];
 group.review_scopes=[{kind:'sample',source_ref:sample0,label:'Same',candidate_refs:[`${sample0}/composition/0`]},{kind:'sample',source_ref:sample1,label:'Same',candidate_refs:[`${sample0}/composition/1`]},{kind:'shared_method',source_ref:'/shared_methods/0',label:'Shared protocol',candidate_refs:['/shared_methods/0/details/0']},{kind:'cross_sample_statements',source_ref:'/statements',label:'跨样品研究陈述',candidate_refs:['/statements/0']}];
 const model=logic.buildReviewModel([group]);assert.equal(model.scopes[1].blocks.flatMap(b=>b.rows)[0].pointer,`${sample0}/composition/1`);
 assert.deepEqual(model.scopes.filter(s=>s.reviewScope).map(s=>s.reviewScope),group.review_scopes.map(({kind,source_ref})=>({kind,source_ref})));
 assert.equal(model.scopes.flatMap(s=>s.blocks.flatMap(b=>b.rows)).filter(f=>f.pointer==='/statements/0').length,1);
});
test('default pages show at most 25 facts and preserve test boundaries',()=>{
 const group=fixture();group.raw.studies[0].samples[0].composition=Array.from({length:51},(_,i)=>fact(`m${i}`,i));
 assert.deepEqual(logic.pagesFor(logic.buildReviewModel([group]).scopes[0],'composition').map(p=>p.rows.length),[25,25,1]);
});
test('cross-sample statements link to related sample scopes without duplicating facts',()=>{
 const group=fixture();group.raw.statements=[fact('cross',1,{about:['s0','s1']})];group.review_scopes=[{kind:'sample',source_ref:sample0,label:'A',candidate_refs:[]},{kind:'sample',source_ref:sample1,label:'B',candidate_refs:[]},{kind:'cross_sample_statements',source_ref:'/statements',label:'跨样品研究陈述',candidate_refs:['/statements/0']}];const model=logic.buildReviewModel([group]),scope=model.scopes[2];assert.deepEqual(scope.relatedScopes.map(link=>link.scopeKey),model.scopes.slice(0,2).map(s=>s.key));assert.equal(scope.blocks.flatMap(b=>b.rows).length,1);
});
test('malformed scope contracts lack writable scope and orphan cross statements are not duplicated',()=>{
 const group=fixture();group.raw.statements=[fact('cross',1,{about:['s0','s1']})];group.review_scopes=[{source_ref:sample0,candidate_refs:[`${sample0}/composition/0`]}];const model=logic.buildReviewModel([group]);assert.ok(model.scopes.every(scope=>!scope.reviewScope));assert.equal(model.scopes.flatMap(scope=>scope.blocks.flatMap(block=>block.rows)).filter(fact=>fact.pointer==='/statements/0').length,1);
});

test('nonobject members across all categories and statements preserve raw values and canonical identity',()=>{
 const group=fixture(),sample=group.raw.studies[0].samples[0];sample.composition=[null];sample.preparation=[17];sample.tests[0].conditions=[false];sample.tests[0].results=[['bad']];group.raw.shared_methods[0].details=['bad detail'];group.raw.statements=[null];
 group.review_scopes=[{kind:'sample',source_ref:sample0,label:'A',candidate_refs:[`${sample0}/composition/0`,`${sample0}/preparation/0`,`${sample0}/tests/0/conditions/0`,`${sample0}/tests/0/results/0`]},{kind:'shared_method',source_ref:'/shared_methods/0',label:'Method',candidate_refs:['/shared_methods/0/details/0']},{kind:'unclassified',source_ref:'/',label:'Unknown',candidate_refs:['/statements/0']}];
 const model=logic.buildReviewModel([group]);const bad=model.facts.filter(fact=>fact.malformed);assert.equal(bad.length,6);assert.deepEqual(bad.map(fact=>fact.rawValue),[null,17,false,['bad'],'bad detail',null]);assert.ok(bad.every(fact=>fact.warnings.some(w=>w.includes('结构异常'))));assert.equal(model.scopes.filter(scope=>scope.reviewScope).flatMap(scope=>scope.blocks.flatMap(block=>block.rows)).length,6);
});
test('display previews local corrections including an empty draft that undoes a historical correction',()=>{
 const fact={key:'fact',item:{value:10},review:{decision:'corrected',correction:{value:12}}};assert.equal(logic.displayFact(fact,{fact:{value:13}}).value,13);assert.equal(logic.displayFact(fact,{fact:{}}).value,10);assert.equal(logic.displayFact(fact).value,12);
});

const annotate=(group,items,hashes={})=>{group.issue_annotations={version:'document-issue-scope-v1',items:items.map(([index,scope,target_pointers])=>({issue_pointer:`/issues/${index}`,scope,target_pointers,origin:'manual'})),field_sha256:hashes};return group;};

test('document and sample explanations do not propagate through shared evidence or sample ancestry',()=>{
 const group=fixture(),pointer=sample0+'/composition/0';group.raw.issues=[{description:'document issue',refs:['study']},{description:'sample issue',refs:['s0']},{description:'field issue',refs:['e']}];
 annotate(group,[[0,'document',[]],[1,'sample',[sample0]],[2,'field',[pointer]]],{[pointer]:'a'.repeat(64)});
 const model=logic.buildReviewModel([group]);assert.deepEqual(model.documentIssues.map(x=>x.description),['document issue']);
 assert.deepEqual(model.scopes[0].sampleIssues.map(x=>x.description),['sample issue']);assert.deepEqual(model.scopes[1].sampleIssues,[]);
 assert.deepEqual(model.facts[0].warnings,['field issue']);assert.ok(model.facts.slice(1).every(f=>f.warnings.length===0));
 assert.equal(model.facts[0].issueScopeSha256,'a'.repeat(64));
});

test('issue identities are candidate-local and identical descriptions remain distinct source issues',()=>{
 const first=fixture(),second=fixture();second.candidate_id='other';
 first.raw.issues=[{description:'same'},{description:'same'}];second.raw.issues=[{description:'same'}];
 annotate(first,[[0,'document',[]],[1,'document',[]]]);annotate(second,[[0,'field',[sample0+'/composition/0']]]);
 const model=logic.buildReviewModel([first,second]);assert.equal(model.documentIssues.length,2);
 assert.equal(new Set(model.documentIssues.map(x=>x.key)).size,2);
 assert.deepEqual(model.facts.filter(f=>f.group===first).flatMap(f=>f.warnings),[]);
 assert.deepEqual(model.facts.find(f=>f.group===second).warnings,['same']);
});

test('missing malformed partial and ambiguous annotations preserve each issue at document level',()=>{
 for(const annotations of [undefined,{version:'future',items:[]},{version:'document-issue-scope-v1',items:[]}]){
  const group=fixture();group.raw.issues=[{description:'unclassified'}];group.issue_annotations=annotations;
  const model=logic.buildReviewModel([group]);assert.deepEqual(model.documentIssues.map(x=>x.description),['unclassified']);assert.ok(model.facts.every(f=>f.warnings.length===0));
 }
 const group=fixture();group.raw.issues=Array.from({length:5},(_,i)=>({description:`issue ${i}`}));
 annotate(group,[[0,'field',[sample0+'/composition/0']],[1,'sample',[sample0+'/tests/0']],[2,'field',[sample0+'/composition/1','/missing']],[3,'document',[sample0]],[4,'field',[sample0+'/composition/2']],[4,'field',[sample0+'/composition/3']]]);
 const model=logic.buildReviewModel([group]);assert.deepEqual(model.documentIssues.map(x=>x.description),['issue 1','issue 2','issue 3','issue 4']);assert.deepEqual(model.facts[0].warnings,['issue 0']);assert.ok(model.facts.slice(1).every(f=>f.warnings.length===0));
});

test('malformed raw issue members and malformed issue collections remain visible as safe explanations',()=>{
 for(const value of [[null,7,'literal issue',{description:{odd:true}},{}],{description:'wrong collection'},null]){
  const group=fixture();group.raw.issues=value;
  const model=logic.buildReviewModel([group]);assert.equal(model.documentIssues.length,Array.isArray(value)?value.length:1);
  assert.ok(model.documentIssues.every(x=>typeof x.description==='string'&&x.description.length>0));assert.ok(model.facts.every(f=>f.warnings.length===0));
 }
});

test('field explanation acknowledgement requires valid current scope hash and no local change',()=>{
 const group=fixture(),pointer=sample0+'/composition/0',sha='a'.repeat(64);group.raw.issues=[{description:'specific'}];annotate(group,[[0,'field',[pointer]]],{[pointer]:sha});
 for(const decision of ['confirmed','corrected']){
  group.reviews=[{candidate_ref:pointer,decision,correction:{},issue_scope_sha256:sha}];const fact=logic.buildReviewModel([group]).facts[0];
  assert.equal(logic.issuesAcknowledged(fact),true);assert.equal(logic.issuesAcknowledged(fact,{[fact.key]:{}}),false);assert.equal(logic.issuesAcknowledged(fact,{}, {[fact.key]:true}),false);
  for(const value of [undefined,'invalid','b'.repeat(64)])assert.equal(logic.issuesAcknowledged({...fact,review:{...fact.review,issue_scope_sha256:value}}),false);
 }
 group.reviews=[{candidate_ref:pointer,decision:'confirmed',correction:{},issue_scope_sha256:sha,ownership_target_ref:'/missing'}];assert.equal(logic.issuesAcknowledged(logic.buildReviewModel([group]).facts[0]),false);
});
