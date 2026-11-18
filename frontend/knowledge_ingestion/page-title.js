/** Keep the live title node so asynchronous title updates remain bound. */
export function pageTitle(title, kicker = '资料整理', description = '') {
 const doc=title.ownerDocument, block=doc.createElement('div'), label=doc.createElement('p');
 block.className='workspace-page-title-stack';label.className='workspace-page-kicker';label.textContent=kicker;
 block.append(label,title);
 if(description){const note=doc.createElement('p');note.className='workspace-page-description';note.textContent=description;block.append(note);}
 return block;
}
