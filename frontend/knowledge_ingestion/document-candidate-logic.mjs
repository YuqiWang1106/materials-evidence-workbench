/** Read-only presentation of original candidate pointers and separate human reviews. */
export const array = value => Array.isArray(value) ? value : [];
export const object = value => value !== null && typeof value === 'object' && !Array.isArray(value)
  && [Object.prototype, null].includes(Object.getPrototypeOf(value)) ? value : {};
export function text(value) {
  if (value == null) return '';
  if (typeof value !== 'object') return String(value);
  try { return JSON.stringify(value) ?? ''; } catch { return '无法显示此内容'; }
}
export const accepted = fact => ['confirmed', 'corrected'].includes(fact?.review?.decision);
export const excluded = fact => fact?.review?.decision === 'excluded';
export const displayFact = (fact,drafts={}) => ({...object(fact?.item),
  ...object(Object.hasOwn(drafts,fact?.key)?drafts[fact.key]:fact?.review?.correction)});
export function summary(value) {
  const item=object(value);
  return {name:text(item.material || item.name || item.label || item.description || '未命名内容'),
    value:`${item.value == null ? '未提供数值' : text(item.value)}${item.unit ? ` ${text(item.unit)}` : ''}`};
}

function distinguishLabels(entries) {
  const counts=new Map(),ordinals=new Map();
  for (const entry of entries) counts.set(entry.label,(counts.get(entry.label)||0)+1);
  for (const entry of entries) {
    const label=entry.label;
    if (counts.get(label)<2) continue;
    const ordinal=(ordinals.get(label)||0)+1;ordinals.set(label,ordinal);
    entry.label=`${label}（记录 ${ordinal}）`;
  }
}

const validHash=value=>typeof value==='string'&&/^[a-f0-9]{64}$/.test(value);
export function issuesAcknowledged(fact,drafts={},exclusions={}) {
  return accepted(fact)&&!excluded(fact)&&!fact.malformed&&!array(fact.localWarnings).length
    &&!Object.hasOwn(drafts,fact.key)&&exclusions[fact.key]!==true
    &&validHash(fact.issueScopeSha256)&&validHash(fact.review?.issue_scope_sha256)
    &&fact.issueScopeSha256===fact.review.issue_scope_sha256;
}

function assignIssues(group,scopes,facts,documentIssues) {
  const raw=object(group.raw),annotations=object(group.issue_annotations);
  const validContract=annotations.version==='document-issue-scope-v1'&&Array.isArray(annotations.items);
  const items=validContract?annotations.items:[],hashes=validContract?object(annotations.field_sha256):{};
  const rowsByPointer=new Map(facts.map(fact=>[fact.pointer,fact]));
  const samplesByPointer=new Map();
  for(const scope of scopes){
    scope.sampleIssues=[];
    const pointer=scope.reviewScope?.kind==='sample'?scope.reviewScope.source_ref:scope.samplePointer;
    if(typeof pointer==='string'&&/^\/studies\/(?:0|[1-9][0-9]*)\/samples\/(?:0|[1-9][0-9]*)$/.test(pointer)&&scope.samplePointer===pointer)samplesByPointer.set(pointer,scope);
  }
  for(const fact of facts){fact.issues=[];fact.issueScopeSha256=validHash(hashes[fact.pointer])?hashes[fact.pointer]:null;}
  const entries=Array.isArray(raw.issues)?raw.issues.map((issue,index)=>({issue,pointer:`/issues/${index}`}))
    :Object.hasOwn(raw,'issues')?[{issue:raw.issues,pointer:'/issues',malformed:true}]:[];
  for(const {issue,pointer,malformed} of entries){
    const readable=!malformed&&object(issue)===issue&&typeof issue.description==='string'&&issue.description.trim();
    const description=readable?issue.description:`说明内容格式异常：${(JSON.stringify(issue)??text(issue))||'未提供内容'}`;
    const matches=items.filter(item=>object(item)===item&&item.issue_pointer===pointer),annotation=matches.length===1?matches[0]:null;
    const targets=annotation?.target_pointers;
    let scope='document';
    if(readable&&annotation&&Array.isArray(targets)&&targets.every(target=>typeof target==='string')&&new Set(targets).size===targets.length){
      if(annotation.scope==='document'&&!targets.length)scope='document';
      if(annotation.scope==='sample'&&targets.length&&targets.every(target=>samplesByPointer.has(target)))scope='sample';
      if(annotation.scope==='field'&&targets.length&&targets.every(target=>rowsByPointer.has(target)))scope='field';
    }
    const entry={key:`${text(group.candidate_id)}:${pointer}`,issuePointer:pointer,description,scope,origin:text(annotation?.origin)};
    if(scope==='document')documentIssues.push(entry);
    if(scope==='sample')for(const target of targets)samplesByPointer.get(target).sampleIssues.push(entry);
    if(scope==='field')for(const target of targets)rowsByPointer.get(target).issues.push(entry);
  }
  for(const fact of facts)fact.warnings=[...fact.localWarnings,...fact.issues.map(issue=>issue.description)];
}

export function buildReviewModel(groups) {
  const scopes=[], facts=[], targets=[],documentIssues=[];
  for (const groupValue of array(groups)) {
    if (object(groupValue)!==groupValue) continue;
    const group=groupValue, raw=object(group.raw), id=text(group.candidate_id);
    const groupScopes=[], owners=new Map(), objectsByRef=new Map(), methods=[];
    const reviews=new Map(array(group.reviews).filter(r=>object(r)===r).map(r=>[r.candidate_ref,r]));
    const evidenceByRef=new Map(array(raw.evidence).filter(e=>object(e)===e).map(e=>[e.ref,e]));
    const locations=object(group.source_locations);
    const addScope=(key,label,extra={})=>{
      const scope={key:`${id}:${key}`,group,label:text(label),studyLabel:'',studyPointer:null,blocks:[],...extra};
      groupScopes.push(scope);return scope;
    };
    const register=(item,entry)=>{
      if (typeof item.ref==='string' && item.ref) {
        const matches=objectsByRef.get(item.ref)||[];matches.push(entry);objectsByRef.set(item.ref,matches);
      }
    };
    const blockFor=(scope,key,category,title,extra={})=>{
      let block=scope.blocks.find(b=>b.key===`${id}:${key}`);
      if (!block) {block={key:`${id}:${key}`,category,title:text(title),rows:[],methods:[],...extra};scope.blocks.push(block);}
      return block;
    };
    let sharedScope=null, unknownScope=null;
    const shared=()=>sharedScope || (sharedScope=addScope('shared','共用方法'));
    const unknown=()=>unknownScope || (unknownScope=addScope('unclassified-statements','未归类陈述'));
    const studies=array(raw.studies).flatMap((study,si)=>object(study)===study
      ? [{kind:'study',item:study,pointer:`/studies/${si}`,scope:null,label:text(study.label)||`研究 ${si+1}`}]:[]);
    distinguishLabels(studies);
    studies.forEach(studyEntry=>{
      const study=studyEntry.item,studyPointer=studyEntry.pointer;
      register(study,studyEntry);
      array(study.samples).forEach((sample,mi)=>{
        if (object(sample)!==sample) return;
        const pointer=`${studyPointer}/samples/${mi}`;
        const scope=addScope(pointer,sample.label||`样品 ${mi+1}`,{samplePointer:pointer,sampleRef:sample.ref,studyRef:study.ref,studyPointer,studyLabel:studyEntry.label,sample,study});
        const entry={kind:'sample',item:sample,pointer,scope,study,studyPointer};
        owners.set(pointer,entry);register(sample,entry);
        array(sample.tests).forEach((test,ti)=>{
          if (object(test)!==test) return;
          const testPointer=`${pointer}/tests/${ti}`;
          const block=blockFor(scope,testPointer,'tests',test.label||`测试 ${ti+1}`,{test});
          const testEntry={kind:'test',item:test,pointer:testPointer,scope,block,study,studyPointer,sample,samplePointer:pointer};
          owners.set(testPointer,testEntry);register(test,testEntry);
        });
      });
      distinguishLabels(groupScopes.filter(scope=>scope.study===study));
    });
    const groupTargets=[];
    for (const entry of owners.values()) {
      if (typeof entry.item.ref!=='string' || !entry.item.ref) continue;
      const label=[entry.scope.studyLabel,entry.scope.label,entry.kind==='test'?entry.block.title:null].filter(Boolean).join(' · ');
      groupTargets.push({groupId:id,pointer:entry.pointer,label,kind:entry.kind});
    }
    distinguishLabels(groupTargets);targets.push(...groupTargets);
    array(raw.shared_methods).forEach((method,mi)=>{
      if (object(method)!==method) return;
      const pointer=`/shared_methods/${mi}`,scope=shared();
      const block=blockFor(scope,pointer,'tests',method.label||`方法 ${mi+1}`,{test:method});
      const entry={kind:'method',item:method,pointer,scope,block};
      methods.push(entry);register(method,entry);
    });
    for (const entry of owners.values()) {
      if (entry.kind!=='test') continue;
      for (const method of methods) {
        const explicitRef=typeof entry.item.method_ref==='string' && entry.item.method_ref===method.item.ref;
        const explicitTarget=typeof entry.item.ref==='string' && array(method.item.applies_to).includes(entry.item.ref);
        if (explicitRef || explicitTarget) entry.block.methods.push({label:method.block.title,scopeKey:method.scope.key,blockKey:method.block.key});
      }
    }
    for (const method of methods) {
      const linked=[...owners.values()].filter(entry=>entry.kind==='test' && entry.block.methods.some(link=>link.blockKey===method.block.key));
      method.block.context=linked.length?'适用于：'+linked.map(entry=>groupTargets.find(target=>target.pointer===entry.pointer)?.label || [entry.scope.studyLabel,entry.scope.label,entry.block.title].filter(Boolean).join(' · ')).join('；'):'适用对象待确认';
    }
    const addFact=(rawValue,pointer,role,originalOwner,scopeOverride=null)=>{
      const malformed=object(rawValue)!==rawValue,item=object(rawValue);
      const review=reviews.get(pointer),originalOwnerPointer=originalOwner?.pointer||null;
      let owner=originalOwner;
      const requested=review?.ownership_target_ref, candidate=requested ? owners.get(requested) : null;
      const desiredKind=['composition','preparation'].includes(role)?'sample':['conditions','results'].includes(role)?'test':null;
      const validMove=candidate && candidate.kind===desiredKind && typeof candidate.item.ref==='string' && candidate.item.ref
        && (!(role in candidate.item) || Array.isArray(candidate.item[role]));
      if (validMove) owner=candidate;
      const evidence=array(item.evidence).filter(ref=>typeof ref==='string').map(ref=>({ref,evidence:object(evidenceByRef.get(ref)),location:object(locations[ref])}));
      const localWarnings=[];
      if (requested && !validMove) localWarnings.push('保存的归属无法对应当前候选对象，请重新核对');
      if (malformed) localWarnings.push('结构异常：此条内容只能暂不确认，原始值保留供核对');
      const fact={key:`${id}:${pointer}`,group,pointer,item,rawValue,malformed,review,role,ownerPointer:owner?.pointer||null,originalOwnerPointer,
        localWarnings,warnings:[...localWarnings],evidence,study:owner?.study,sample:owner?.sample|| (owner?.kind==='sample'?owner.item:null),test:owner?.kind==='test'?owner.item:null};
      facts.push(fact);
      const scope=scopeOverride||owner?.scope||unknown();
      const category=role==='details'?'tests':['conditions','results'].includes(role)?'tests':role;
      const title={composition:'组成',preparation:'制备',statements:'研究陈述'}[category]||'测试与结果';
      const block=category==='tests' && owner?.block ? owner.block : blockFor(scope,`${scope.key}/${category}`,category,title);
      block.rows.push(fact);return fact;
    };
    for (const entry of owners.values()) {
      const roles=entry.kind==='sample'?['composition','preparation']:['conditions','results'];
      for (const role of roles) array(entry.item[role]).forEach((item,i)=>addFact(item,`${entry.pointer}/${role}/${i}`,role,entry));
    }
    for (const method of methods) array(method.item.details).forEach((item,i)=>addFact(item,`${method.pointer}/details/${i}`,'details',method));
    array(raw.statements).forEach((rawValue,i)=>{
      const item=object(rawValue);
      const matches=array(item.about).flatMap(ref=>objectsByRef.get(ref)||[]);
      const matchingScopes=[];
      for (const entry of matches) {
        if (entry.kind==='study' && !entry.scope) entry.scope=addScope(`${entry.pointer}/statements`,'研究陈述',{studyLabel:entry.label,studyRef:entry.item.ref,studyPointer:entry.pointer});
        if (entry.scope && !matchingScopes.includes(entry.scope)) matchingScopes.push(entry.scope);
      }
      const scope=matchingScopes[0]||unknown(), owner=matches.find(m=>m.scope===scope);
      const fact=addFact(rawValue,`/statements/${i}`,'statements',owner,scope);
      for (const extra of matchingScopes.slice(1)) blockFor(extra,`${extra.key}/statements`,'statements','研究陈述').rows.push(fact);
    });
    if (!groupScopes.length) addScope('raw','其他候选内容',{rawOnly:true});
    const order=['composition','preparation','tests','statements'];
    for (const scope of groupScopes) scope.blocks.sort((a,b)=>order.indexOf(a.category)-order.indexOf(b.category));
    if (Array.isArray(group.review_scopes)) {
      const originalBlocks=groupScopes.flatMap(scope=>scope.blocks);
      const assigned=new Set();
      const canonical=group.review_scopes.filter(value=>object(value)===value && ['sample','shared_method','cross_sample_statements','unclassified'].includes(value.kind) && typeof value.source_ref==='string' && value.source_ref.startsWith('/') && Array.isArray(value.candidate_refs) && value.candidate_refs.every(ref=>typeof ref==='string'&&ref.startsWith('/'))).map(contract=>{
        const prior=groupScopes.find(scope=>scope.samplePointer===contract.source_ref);
        const scope={...prior,key:`${id}:${contract.kind}:${contract.source_ref}`,group,label:text(contract.label)||'待归属内容',blocks:[],reviewScope:{kind:contract.kind,source_ref:contract.source_ref}};
        const members=new Set(contract.candidate_refs);
        for (const block of originalBlocks) {
          const rows=block.rows.filter(fact=>members.has(fact.pointer)&&!assigned.has(fact.key));
          if (rows.length || prior?.blocks.includes(block) && !block.rows.length) {
            rows.forEach(fact=>assigned.add(fact.key));scope.blocks.push({...block,rows});
          }
        }
        return scope;
      });
      for(const scope of canonical) {
        const about=new Set(scope.blocks.flatMap(block=>block.rows).filter(fact=>fact.role==='statements').flatMap(fact=>array(fact.item.about)));
        scope.relatedScopes=canonical.filter(other=>other!==scope&&other.reviewScope?.kind==='sample'&&(about.has(other.sampleRef)||about.has(other.studyRef))).map(other=>({scopeKey:other.key,label:[other.studyLabel,other.label].filter(Boolean).join(' / ')}));
      }
      for(const scope of canonical) for(const block of scope.blocks) block.methods=block.methods.map(link=>{
        const target=canonical.find(other=>other.blocks.some(b=>b.key===link.blockKey));
        return target?{...link,scopeKey:target.key}:null;
      }).filter(Boolean);
      const orphan=facts.filter(fact=>fact.group===group&&!assigned.has(fact.key));
      const orphanSeen=new Set();
      if(orphan.length) canonical.push({key:`${id}:contract-missing`,group,label:'范围未提供的内容',blocks:originalBlocks.map(block=>({...block,rows:block.rows.filter(fact=>{if(!orphan.includes(fact)||orphanSeen.has(fact.key))return false;orphanSeen.add(fact.key);return true;})})).filter(block=>block.rows.length)});
      if(!canonical.length) canonical.push({key:`${id}:raw`,group,label:'其他候选内容',blocks:[],rawOnly:true});
      distinguishLabels(canonical);scopes.push(...canonical);
    } else scopes.push(...groupScopes);
    assignIssues(group,scopes.filter(scope=>scope.group===group),facts.filter(fact=>fact.group===group),documentIssues);
  }
  return {scopes,facts,targets,documentIssues};
}

export function pagesFor(scope,category,size=25) {
  const pageSize=Number.isFinite(size) && size>0 ? Math.max(1,Math.floor(size)) : 25;
  return array(scope?.blocks).filter(block=>block.category===category).flatMap(block=>{
    const rows=array(block.rows),parts=Math.max(1,Math.ceil(rows.length/pageSize));
    return Array.from({length:parts},(_,index)=>({block,rows:rows.slice(index*pageSize,(index+1)*pageSize),part:index+1,parts}));
  });
}

