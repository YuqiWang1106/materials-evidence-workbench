/** Limited Markdown, built from controlled DOM nodes. HTML is always source text. */
export function renderChatMarkdown(value,{document=globalThis.document}={}){
 const root=document.createElement('div');root.className='chat-markdown';
 const make=tag=>document.createElement(tag);
 const literal=(parent,text)=>{if(!text)return;if(document.createTextNode)parent.append(document.createTextNode(text));else{const node=make('span');node.textContent=text;parent.append(node);}};
 const escaped=(text,index)=>{let count=0;while(index>0&&text[--index]==='\\')count++;return count%2===1;};
 const word=character=>Boolean(character&&/[\p{L}\p{N}]/u.test(character));
 const runAt=(text,index,character)=>{let end=index;while(text[end]===character)end++;return end-index;};
 function codeEnd(text,start,size){for(let i=start;i<text.length;i++){if(text[i]!=='`')continue;const run=runAt(text,i,'`');if(run===size)return i;i+=run-1;}return -1;}
 function paired(text,start,open,close){let depth=1;for(let i=start;i<text.length;i++){if(escaped(text,i))continue;if(text[i]===open)depth++;if(text[i]===close&&--depth===0)return i;}return -1;}
 function linkAt(text,start,image){const labelStart=start+(image?2:1),labelEnd=paired(text,labelStart,'[',']');if(labelEnd<0||text[labelEnd+1]!=='(')return null;const end=paired(text,labelEnd+2,'(',')');if(end<0)return null;return {end:end+1,label:text.slice(labelStart,labelEnd),destination:text.slice(labelEnd+2,end),source:text.slice(start,end+1)};}
 function safeDestination(destination){
  // Schemes are checked before URL normalization. No entity decoding or relative URLs.
  const match=destination.match(/^\s*(?:<([^<>]*)>|([^\s]+?))(?:[ \t]+["']([^"']*)["'])?\s*$/);
  if(!match)return null;const href=(match[1]??match[2]).replace(/\\([()])/g,'$1');
  if(!/^https?:\/\//i.test(href)||/[\s\u0000-\u001f\u007f-\u009f\\]/u.test(href))return null;
  try{const url=new URL(href);return /^https?:$/.test(url.protocol)?{href:url.href,title:match[3]}:null;}catch{return null;}
 }
 function emphasisEnd(text,start,marker,width){
  for(let i=start;i<text.length;i++){
   if(text[i]==='`'&&!escaped(text,i)){const size=runAt(text,i,'`'),end=codeEnd(text,i+size,size);if(end>=0){i=end+size-1;continue;}}
   if(text[i]!==marker||escaped(text,i))continue;const run=runAt(text,i,marker);
   if(width===1&&run===2){const nested=emphasisEnd(text,i+2,marker,2);if(nested>=0){i=nested+1;continue;}}
   const end=i+Math.max(0,run-width);
   if(run>=width&&!(width===1&&run===2)&&!/[\s]/u.test(text[end-1]||' ')&&!(marker==='_'&&word(text[end-1])&&word(text[end+width])))return end;
   i+=run-1;
  }
  return -1;
 }
 function inline(parent,text,depth=0,links=true){
  if(depth>=32){literal(parent,text);return;}
  let pending='';const flush=()=>{literal(parent,pending);pending='';};
  for(let i=0;i<text.length;){
   const character=text[i];
   if(character==='\\'&&/[!"#$%&'()*+,\-./:;<=>?@[\]\\^_`{|}~]/.test(text[i+1]||'')){pending+=text[i+1];i+=2;continue;}
   if(character==='`'){const size=runAt(text,i,'`'),end=codeEnd(text,i+size,size);if(end>=0){flush();const code=make('code');code.textContent=text.slice(i+size,end);parent.append(code);i=end+size;continue;}pending+=text.slice(i,i+size);i+=size;continue;}
   const image=character==='!'&&text[i+1]==='[';
   if(image||character==='['){const link=linkAt(text,i,image);if(link){flush();const destination=!image&&links?safeDestination(link.destination):null;if(destination){const node=make('a');node.setAttribute('href',destination.href);node.setAttribute('target','_blank');node.setAttribute('rel','noopener noreferrer');if(destination.title!==undefined)node.setAttribute('title',destination.title);inline(node,link.label,depth+1,false);parent.append(node);}else literal(parent,link.source);i=link.end;continue;}}
   if((character==='*'||character==='_')&&!(character==='_'&&word(text[i-1])&&word(text[i+1]))){const width=Math.min(3,runAt(text,i,character));if(text[i+width]&&!/\s/u.test(text[i+width])){const end=emphasisEnd(text,i+width,character,width);if(end>i+width){flush();const node=make(width===1?'em':'strong');if(width===3){const em=make('em');inline(em,text.slice(i+width,end),depth+1,links);node.append(em);}else inline(node,text.slice(i+width,end),depth+1,links);parent.append(node);i=end+width;continue;}}pending+=text.slice(i,i+width);i+=width;continue;}
   if(character==='\n'&&(pending.endsWith('  ')||pending.endsWith('\\'))){pending=pending.endsWith('\\')?pending.slice(0,-1):pending.replace(/ +$/,'');flush();parent.append(make('br'));i++;continue;}
   pending+=character;i++;
  }
  flush();
 }
 function cells(line){
  const result=[];let value='',fence=0,pipes=0;
  for(let i=0;i<line.length;i++){
   if(line[i]==='\\'&&i+1<line.length){value+=line[i]+line[++i];continue;}
   if(line[i]==='`'){const count=runAt(line,i,'`');if(!fence)fence=count;else if(fence===count)fence=0;value+=line.slice(i,i+count);i+=count-1;continue;}
   if(line[i]==='|'&&!fence){result.push(value.trim());value='';pipes++;}else value+=line[i];
  }
  if(!pipes)return null;result.push(value.trim());if(result[0]===''&&line.trimStart().startsWith('|'))result.shift();if(result.at(-1)===''&&line.trimEnd().endsWith('|')&&!escaped(line.trimEnd(),line.trimEnd().length-1))result.pop();return result;
 }
 function tableAt(lines,index){const headings=cells(lines[index]||''),delimiters=cells(lines[index+1]||'');if(!headings||!delimiters||headings.length!==delimiters.length||!delimiters.every(cell=>/^:?-{3,}:?$/.test(cell)))return null;return {headings,align:delimiters.map(cell=>cell.startsWith(':')&&cell.endsWith(':')?'center':cell.endsWith(':')?'right':'left')};}
 const fenceAt=line=>line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
 const headingAt=line=>line.match(/^ {0,3}(#{1,6})(?:[ \t]+|$)(.*)$/);
 const quoteAt=line=>line.match(/^ {0,3}>[ \t]?(.*)$/);
 function listAt(line){const match=line.match(/^( *)([-+*]|\d{1,9}[.)])([ \t]+)(.*)$/);return match?{indent:match[1].length,ordered:/^\d/.test(match[2]),start:Number.parseInt(match[2],10),content:match[4],width:match[1].length+match[2].length+match[3].length}:null;}
 const special=(lines,index)=>Boolean(fenceAt(lines[index])||headingAt(lines[index])||quoteAt(lines[index])||listAt(lines[index])||tableAt(lines,index));
 function blocks(parent,lines,depth=0){
  if(depth>=32){const paragraph=make('p');paragraph.textContent=lines.join('\n');parent.append(paragraph);return;}
  let i=0;
  while(i<lines.length){
   if(!lines[i].trim()){i++;continue;}
   const fence=fenceAt(lines[i]);
   if(fence){const marker=fence[1][0],width=fence[1].length,start=++i;while(i<lines.length&&!new RegExp('^ {0,3}'+(marker==='`'?'`':'~')+'{'+width+',}[ \\t]*$').test(lines[i]))i++;const pre=make('pre'),code=make('code');pre.className='chat-markdown-code';code.textContent=lines.slice(start,i).join('\n')+(i<lines.length&&i>start?'\n':'');pre.append(code);parent.append(pre);if(i<lines.length)i++;continue;}
   const heading=headingAt(lines[i]);
   if(heading){const node=make('h'+Math.min(6,heading[1].length+2));inline(node,heading[2].replace(/[ \t]+#+[ \t]*$/,''));parent.append(node);i++;continue;}
   if(quoteAt(lines[i])){const inner=[];while(i<lines.length&&quoteAt(lines[i]))inner.push(quoteAt(lines[i++])[1]);const quote=make('blockquote');blocks(quote,inner,depth+1);parent.append(quote);continue;}
   const table=tableAt(lines,i);
   if(table){const wrapper=make('div'),element=make('table'),head=make('thead'),body=make('tbody');wrapper.className='chat-markdown-table';
    const row=(values,tag)=>{const node=make('tr');for(let column=0;column<Math.max(values.length,table.headings.length);column++){const cell=make(tag);cell.style.textAlign=table.align[column]||'left';inline(cell,values[column]||'');node.append(cell);}return node;};head.append(row(table.headings,'th'));i+=2;while(i<lines.length&&lines[i].trim()&&cells(lines[i]))body.append(row(cells(lines[i++]),'td'));element.append(head,body);wrapper.append(element);parent.append(wrapper);continue;}
   const first=listAt(lines[i]);
   if(first){const list=make(first.ordered?'ol':'ul');if(first.ordered&&first.start!==1)list.setAttribute('start',String(first.start));
    while(i<lines.length){const item=listAt(lines[i]);if(!item||item.indent!==first.indent||item.ordered!==first.ordered)break;const inner=[item.content];i++;
     while(i<lines.length){const next=listAt(lines[i]),indent=(lines[i].match(/^ */)||[''])[0].length;if(next&&next.indent<=first.indent)break;
      if(!lines[i].trim()){let after=i+1;while(after<lines.length&&!lines[after].trim())after++;const following=listAt(lines[after]||'');if(following&&following.indent===first.indent&&following.ordered===first.ordered){i=after;break;}if(after<lines.length&&(lines[after].match(/^ */)||[''])[0].length>first.indent){inner.push('');i++;continue;}break;}
      if(indent>first.indent){inner.push(lines[i].slice(Math.min(indent,item.width)));i++;continue;}
      if(special(lines,i))break;inner.push(lines[i++]);
     }
     const child=make('li');blocks(child,inner,depth+1);list.append(child);
    }
    parent.append(list);continue;
   }
   const paragraph=make('p'),content=[lines[i++]];while(i<lines.length&&lines[i].trim()&&!special(lines,i))content.push(lines[i++]);inline(paragraph,content.join('\n'));parent.append(paragraph);
  }
 }
 blocks(root,String(value??'').replace(/\r\n?/g,'\n').split('\n'));return root;
}
