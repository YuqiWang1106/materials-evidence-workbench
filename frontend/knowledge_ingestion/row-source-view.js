import {bindingLocation,previewRequest,readerOptionsForLocation,errorText,valueText} from './result-logic.js';

const columnName=n=>{let s='';while(n>0){n--;s=String.fromCharCode(65+n%26)+s;n=Math.floor(n/26);}return s;};

/** Resolve only server-backed sources, using the decoder saved with the original. */
export function mountRowSource({root,api,taskId}) {
  const doc=root.ownerDocument;
  const node=(tag,cls,text)=>{const el=doc.createElement(tag);if(cls)el.className=cls;if(text!==undefined)el.textContent=String(text);return el;};
  let version=0,disposed=false,request=null;
  function close(){version++;request?.abort();root.hidden=true;root.replaceChildren();}
  async function show(row,{source=row.source_bindings?.value,label='结果原格'}={}) {
    const token=++version;request?.abort();request=new AbortController();root.hidden=false;
    const panel=node('section','panel row-source-panel'),heading=node('div','row-source-heading');
    const button=node('button','button quiet','关闭');button.type='button';button.addEventListener('click',close);
    heading.append(node('h3','',label),button);panel.append(heading);root.replaceChildren(panel);
    const current=()=>!disposed&&token===version;
    if(!source||!row.evidence_refs?.length){panel.append(node('p','message','没有可定位的原格，请查看原件。'));return;}
    const status=node('p','muted','正在读取原格…');panel.append(status);
    try {
      const locations=await Promise.all(row.evidence_refs.slice(0,12).map(ref=>api(`/api/tasks/${encodeURIComponent(taskId)}/evidence/${encodeURIComponent(ref)}/location`,{signal:request.signal})));
      if(!current())return;
      const location=bindingLocation(source,locations),options=readerOptionsForLocation(location);
      const bounds=options&&previewRequest(location,options);
      if(!location||!bounds){status.textContent='没有可定位的原格，或原件的读取方式尚未确认。';return;}
      const address=`${columnName(source.column)}${source.row}`;
      status.textContent=`${source.sheet||'CSV'} · ${address}`;
      const link=node('a','button quiet','下载原件');link.href=`/api/assets/${encodeURIComponent(location.registered_asset_id)}/download`;link.download='';panel.append(link);
      const preview=await api(`/api/assets/${encodeURIComponent(location.registered_asset_id)}/table-preview`,{method:'POST',json:bounds,signal:request.signal});
      if(!current())return;
      const scroll=node('div','table-scroll'),table=node('table','row-source-table');table.setAttribute('aria-label',`原格 ${address}`);
      const columns=[...new Set((preview.rows||[]).flatMap(r=>r.cells.map(c=>c.column)))].sort((a,b)=>a-b);
      const head=node('tr');head.append(node('th','','原行'));for(const c of columns)head.append(node('th','',columnName(c)));
      const thead=node('thead');thead.append(head);table.append(thead);const body=node('tbody');
      for(const r of preview.rows||[]){
        const tr=node('tr');tr.append(node('th','',r.row));
        for(const c of r.cells){
          const focus=c.row===source.row&&c.column===source.column;
          const td=node('td',focus?'result-cell-focus':'');
          const raw=c.raw_value==null?'（空白）':String(c.raw_value),span=source.selector?.span;
          if(focus&&Array.isArray(span)&&span.length===2){const chars=Array.from(raw);td.append(node('span','',chars.slice(0,span[0]).join('')),node('mark','',chars.slice(...span).join('')),node('span','',chars.slice(span[1]).join('')));}
          else td.textContent=raw;
          if(focus)td.setAttribute('aria-label',`${address} ${raw}`);tr.append(td);
        }body.append(tr);
      }table.append(body);scroll.append(table);panel.append(scroll);
      if(source.selector?.span)panel.append(node('p','muted',`使用原格中的文字：${valueText(source.raw,240)}`));
    }catch(error){if(current())status.textContent=errorText(error,'原格读取失败，请重新点击结果重试。');}
  }
  return {show,close,dispose(){disposed=true;version++;request?.abort();}};
}
