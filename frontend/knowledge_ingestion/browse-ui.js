import {el,button} from './dom.js';

export function browseSelect(label,items,onChange){
 const select=el('select','browse-select');select.setAttribute('aria-label',label);
 for(const [value,text]of items){const option=el('option',null,text);option.value=value;select.append(option);}
 select.value=items[0][0];select.addEventListener('change',()=>onChange(select.value));return select;
}
export function sectionDirectory(sections){
 const nav=el('nav','section-directory');nav.setAttribute('aria-label','章节目录');nav.append(el('strong',null,'本页目录'));
 for(const section of sections)nav.append(button(section.title,()=>section.node.scrollIntoView?.({behavior:globalThis.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches?'auto':'smooth',block:'start'}),'quiet'));
 return nav;
}
export function questionSuggestions(prompts,fill){
 const group=el('div','question-suggestions');group.setAttribute('aria-label','问题建议');
 for(const prompt of prompts)group.append(button(prompt,()=>fill(prompt),'secondary'));
 return group;
}
export function nameSort(items,getName,descending=false){return [...items].sort((a,b)=>(descending?-1:1)*String(getName(a)||'').localeCompare(String(getName(b)||''),'zh-CN',{numeric:true}));}
