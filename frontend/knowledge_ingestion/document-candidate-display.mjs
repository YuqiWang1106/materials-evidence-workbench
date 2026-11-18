/** Chinese display projections never become source values or human corrections. */
import {array,object,displayFact} from './document-candidate-logic.mjs';

const VERSION='document-display-zh-v1';
const FACT_FIELDS=['name','value','basis','statement_status'];
const numbers=value=>(value.match(/[+−-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+−-]?\d+)?/g)||[]).sort();
const numericText=value=>/\d/.test(value)&&/^[\s\d.+−\-eE%<>≤≥±:～~/(),×]+$/u.test(value);
const own=(value,key)=>object(value)===value&&Object.hasOwn(value,key);

// Enumerate the extraction schema, rather than following untrusted JSON pointers.
function targets(raw){
  const found=new Map();
  const add=(node,key,pointer)=>{if(own(node,key)&&typeof node[key]==='string')found.set(pointer,{node,key,source:node[key]});};
  const rows=(node,key)=>own(node,key)?array(node[key]):[];
  const facts=(node,key,path)=>rows(node,key).forEach((fact,index)=>{
    const pointer=`${path}/${key}/${index}`;
    for(const field of FACT_FIELDS)add(fact,field,`${pointer}/${field}`);
    rows(fact,'conditions').forEach((value,i)=>{if(typeof value==='string')found.set(`${pointer}/conditions/${i}`,{node:fact.conditions,key:i,source:value});});
  });
  rows(raw,'studies').forEach((study,si)=>{
    const sp=`/studies/${si}`;add(study,'label',`${sp}/label`);
    rows(study,'samples').forEach((sample,mi)=>{
      const mp=`${sp}/samples/${mi}`;add(sample,'label',`${mp}/label`);
      for(const key of ['composition','preparation'])facts(sample,key,mp);
      rows(sample,'tests').forEach((test,ti)=>{
        const tp=`${mp}/tests/${ti}`;add(test,'label',`${tp}/label`);
        for(const key of ['conditions','results'])facts(test,key,tp);
      });
    });
  });
  rows(raw,'shared_methods').forEach((method,mi)=>{const mp=`/shared_methods/${mi}`;add(method,'label',`${mp}/label`);facts(method,'details',mp);});
  facts(raw,'statements','');
  rows(raw,'issues').forEach((issue,i)=>add(issue,'description',`/issues/${i}/description`));
  return found;
}

function translation(group,pointer,source){
  const display=object(group?.display_zh),fields=object(display.fields);
  if(display.version!==VERSION||display.status!=='ready'||!Object.hasOwn(fields,pointer)||typeof source!=='string'||numericText(source))return source;
  const entry=object(fields[pointer]);
  if(!own(entry,'source')||!own(entry,'text')||entry.source!==source||typeof entry.text!=='string'||!entry.text.trim()||!/[\u3400-\u9fff]/u.test(entry.text))return source;
  if(JSON.stringify(numbers(source))!==JSON.stringify(numbers(entry.text)))return source;
  return entry.text;
}

function freeze(value){
  if(value!==null&&typeof value==='object'){for(const child of Object.values(value))freeze(child);Object.freeze(value);}
  return value;
}

export function localizedGroups(groups){
  return Object.freeze(array(groups).map(group=>{
    if(object(group)!==group)return group;
    const copy=structuredClone(group),originalFields=targets(group.raw);
    for(const [pointer,target] of targets(copy.raw))target.node[target.key]=translation(group,pointer,target.source);
    for(const scope of array(copy.review_scopes)){
      if(object(scope)!==scope||typeof scope.source_ref!=='string')continue;
      const pointer=`${scope.source_ref}/label`,source=originalFields.get(pointer)?.source;
      if(typeof source==='string'&&scope.label===source)scope.label=translation(group,pointer,source);
    }
    return freeze(copy);
  }));
}

export function presentFact(fact,drafts={}){
  const shown=structuredClone(displayFact(fact,drafts));
  const correction=object(Object.hasOwn(drafts,fact?.key)?drafts[fact.key]:fact?.review?.correction);
  const allowed=targets(fact?.group?.raw);
  const present=(key,value,suffix=key)=>{
    const pointer=`${fact?.pointer}/${suffix}`,source=allowed.get(pointer)?.source;
    return !Object.hasOwn(correction,key)&&typeof source==='string'&&source===value?translation(fact?.group,pointer,source):value;
  };
  for(const field of FACT_FIELDS)if(Object.hasOwn(shown,field))shown[field]=present(field,shown[field]);
  if(Array.isArray(shown.conditions))shown.conditions=shown.conditions.map((value,i)=>present('conditions',value,`conditions/${i}`));
  if(shown.unit==='times')shown.unit='次';
  return shown;
}
