import {pageTitle} from './page-title.js';
import {coverageWarning} from './document-processing.mjs';
import {evidenceRegions} from './document-result-logic.mjs';
import {titleForResult} from './result-logic.js';
import {TYPE_ORDER, groupAttributes, orderFormulations, typeColor, typeLabel} from './entity-graph-logic.mjs';
import {heldGroups, heldMissing, heldNotice, heldOf, heldReason, heldSubject} from './entity-held-logic.mjs';

const idPath = value => encodeURIComponent(value);
const BATCH = 200;
const SECTIONS = ['文中名称', '目的', '区别', '原料', '制备过程', '性能', '测试方法', '结论', '解释', '说明'];
// The entry of the entity list that opens every set-aside line; never the key of an entity.
const HELD = ' held';

/** Pure: the decision items that local drafts become, in the order they must be sent. */
export function draftItems(drafts) {
  return Object.entries(drafts || {}).map(([key, draft]) => ({key, decision: draft.decision,
    ...(draft.decision === 'corrected' ? {correction: draft.correction} : {})}));
}

/** Pure: the state shown for one item — a local draft first, then the saved decision. */
export function reviewState(item, drafts) {
  const draft = drafts?.[item.key];
  if (draft) return draft.decision === 'excluded' ? 'excluded-draft' : 'corrected-draft';
  return item.review?.decision || 'pending';
}

const STATE_LABEL = {pending: '待核对', confirmed: '已确认', corrected: '已修改', excluded: '暂不确认',
  'corrected-draft': '修改未提交', 'excluded-draft': '暂不确认（未提交）'};

/**
 * Check the extracted entities of one paper before they are resolved against the knowledge base.
 * Edits stay as local drafts until 「确认并写入知识库」 sends them with the current versions and confirms.
 */
export function renderEntityReview({root, task, api, onTaskChange, storage}) {
  const doc = root.ownerDocument;
  if (storage === undefined) { try { storage = globalThis.localStorage; } catch { storage = null; } }
  const path = `/api/tasks/${idPath(task.task_id)}`;
  let view = null, drafts = {}, selected = null, focus = null, busy = false, disposed = false, conflict = false;
  const node = (tag, className, text) => {
    const element = doc.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  };
  const button = (label, action, className = 'entity-button') => {
    const element = node('button', className, label);
    element.type = 'button';
    element.addEventListener('click', event => { event.stopPropagation?.(); if (!busy) action(); });
    return element;
  };
  const shell = node('section', 'entity-page entity-review');
  const heading = node('header', 'entity-heading');
  const headingText = node('div', 'entity-heading-text');
  const title = node('h2', '', titleForResult(task));
  const note = node('p', 'entity-heading-note', '核对提取结果 · 确认后才写入知识库并与已有实体消歧');
  headingText.append(pageTitle(title,'资料整理'), note);
  const operatorLabel = node('label', 'entity-operator');
  const operator = node('input', 'entity-search');
  operator.placeholder = '核对人';
  operator.setAttribute('aria-label', '核对人');
  try { operator.value = storage?.getItem('kb-operator') || ''; } catch { operator.value = ''; }
  operatorLabel.append(node('span', '', '核对人'), operator);
  const confirmButton = button('确认并写入知识库', confirmAll, 'entity-button primary');
  heading.append(headingText, operatorLabel, confirmButton);
  const stats = node('div', 'entity-stats');
  const message = node('p', 'entity-message');
  message.setAttribute('role', 'status');
  message.hidden = true;
  const body = node('div', 'entity-review-body');
  const nav = node('nav', 'entity-review-nav');
  nav.setAttribute('aria-label', '提取的实体');
  const content = node('section', 'entity-review-content');
  const source = node('aside', 'entity-review-source');
  source.setAttribute('aria-label', '原文对照');
  body.append(nav, content, source);
  const overlay = node('div', 'entity-source-overlay');
  overlay.hidden = true;
  // An interrupted or partly failed extraction looks like any other draft; say what is missing before it is saved.
  const incomplete = node('p', 'entity-message error', coverageWarning(task.processing?.extraction_coverage));
  incomplete.setAttribute('role', 'note');
  incomplete.hidden = !incomplete.textContent;
  // Lines the program set aside are not in the draft; say so here, where the reviewer decides to save.
  const heldNote = node('p', 'entity-message entity-held-note');
  heldNote.setAttribute('role', 'note');
  heldNote.hidden = true;
  shell.append(heading, incomplete, heldNote, stats, message, body, overlay);
  root.replaceChildren(shell);

  const tell = (text, tone = '') => { message.textContent = text || ''; message.hidden = !text; message.className = 'entity-message ' + tone; };
  const storageKey = () => `kb-entity-review:${task.task_id}:${view?.draft_sha256}`;
  const persist = () => { try { storage?.setItem(storageKey(), JSON.stringify(drafts)); } catch { tell('浏览器无法保存本地修改，请尽快确认写入。'); } };
  operator.addEventListener('input', () => { try { storage?.setItem('kb-operator', operator.value); } catch { /* optional */ } });
  const entities = () => view?.entities || [];
  const entity = key => entities().find(item => item.key === key);
  const editable = () => !busy && !conflict && task.state === 'needs_review';

  function setDraft(key, draft) {
    if (draft) drafts[key] = draft; else delete drafts[key];
    persist();
    draw();
  }

  function drawHeldNote() {
    const text = heldNotice(view?.held);
    heldNote.replaceChildren();
    heldNote.hidden = !text;
    if (text) heldNote.append(node('span', '', text), button('查看', () => { selected = HELD; focus = null; draw(); }, 'entity-link-button'));
  }

  function drawStats() {
    stats.replaceChildren();
    const counts = {};
    for (const item of entities()) counts[item.type] = (counts[item.type] || 0) + 1;
    for (const type of TYPE_ORDER.filter(type => counts[type])) {
      const chip = node('span', 'entity-stat');
      const dot = node('span', 'entity-dot');
      dot.style.background = typeColor(type);
      chip.append(dot, node('span', '', `${typeLabel(type)} ${counts[type]}`));
      stats.append(chip);
    }
    const pending = Object.keys(drafts).length;
    stats.append(node('span', 'entity-stat muted', `已提交决定 ${view?.decided || 0} 项`));
    if (pending) stats.append(node('span', 'entity-stat warn', `未提交修改 ${pending} 项`));
  }

  function drawNav() {
    nav.replaceChildren();
    for (const type of TYPE_ORDER) {
      const group = type === 'formulation' ? orderFormulations(entities())
        : entities().filter(item => item.type === type);
      if (!group.length) continue;
      nav.append(node('p', 'entity-list-label', `${typeLabel(type)}（${group.length}）`));
      for (const item of group) {
        const state = reviewState(item, drafts);
        const pick = button('', () => { selected = item.key; focus = item.attributes[0] || null; draw(); }, 'entity-review-pick');
        pick.append(node('span', 'entity-review-name', drafts[item.key]?.correction?.name || item.name),
          node('span', 'entity-review-state ' + state, STATE_LABEL[state]));
        if (item.key === selected) pick.setAttribute('aria-current', 'true');
        nav.append(pick);
      }
    }
    if (!view?.held?.total) return;
    nav.append(node('p', 'entity-list-label', '未通过核对'));
    const all = button('', () => { selected = HELD; focus = null; draw(); }, 'entity-review-pick');
    all.append(node('span', 'entity-review-name', '全部'), node('span', 'entity-review-state excluded', `${view.held.total} 条`));
    if (selected === HELD) all.setAttribute('aria-current', 'true');
    nav.append(all);
  }

  /** One set-aside line. It can be read and located in the source; it cannot be confirmed here. */
  function heldCard(item, withSubject) {
    const reason = heldReason(item);
    const card = node('article', 'entity-attribute entity-held' + (item.page ? ' entity-review-clickable' : '') + (item === focus ? ' focused' : ''));
    const head = node('div', 'entity-review-actions');
    const badge = node('span', 'entity-review-state excluded', reason.label);
    if (reason.hint) badge.title = reason.hint;
    head.append(badge);
    if (withSubject && heldSubject(item)) head.append(node('span', 'entity-aliases', heldSubject(item)));
    card.append(head, node('p', 'entity-attribute-text', item.text || ''));
    if (heldMissing(item)) card.append(node('p', 'entity-aliases', heldMissing(item)));
    if (item.quote) card.append(node('blockquote', 'entity-review-quote', item.quote));
    if (item.page) {
      const where = node('div', 'entity-quote-meta');
      where.append(node('span', '', `第 ${item.page} 页`));
      card.append(where);
      card.addEventListener('click', () => { if (focus !== item) { focus = item; drawSource(); drawContent(); } });
    }
    return card;
  }

  function drawHeld() {
    content.append(node('h3', '', '未通过核对的提取内容'),
      node('p', 'entity-aliases', '这些内容是模型提取出来、但没有通过程序核对的。它们不会写入知识库，现在只供查看。'));
    for (const group of heldGroups(view.held.items)) {
      const block = node('section', 'entity-attribute-section');
      block.append(node('h5', '', `${group.label}（${group.items.length}）`));
      if (group.hint) block.append(node('p', 'entity-aliases', group.hint));
      for (const item of group.items) block.append(heldCard(item, true));
      content.append(block);
    }
  }

  function editEntity(item) {
    const current = drafts[item.key]?.correction || {};
    const form = node('form', 'entity-edit');
    const name = node('input', 'entity-search'); name.value = current.name ?? item.name;
    const type = node('select', 'entity-search');
    for (const kind of TYPE_ORDER) { const option = node('option', '', typeLabel(kind)); option.value = kind; type.append(option); }
    type.value = current.type ?? item.type;
    const aliases = node('input', 'entity-search'); aliases.value = (current.aliases ?? item.aliases ?? []).join('、');
    const merge = node('select', 'entity-search');
    const none = node('option', '', '不合并'); none.value = ''; merge.append(none);
    for (const other of entities().filter(other => other.key !== item.key && other.type === item.type)) {
      const option = node('option', '', other.name); option.value = other.key; merge.append(option);
    }
    merge.value = current.merge_into || '';
    const field = (label, input) => { const row = node('label', 'entity-edit-row'); row.append(node('span', '', label), input); return row; };
    const actions = node('div', 'entity-edit-actions');
    actions.append(button('取消', () => draw()), button('保存修改', () => {
      const correction = {};
      if (name.value.trim() && name.value.trim() !== item.name) correction.name = name.value.trim();
      if (type.value !== item.type) correction.type = type.value;
      const list = aliases.value.split(/[、,，;；]/).map(value => value.trim()).filter(Boolean);
      if (list.join('|') !== (item.aliases || []).join('|')) correction.aliases = list;
      if (merge.value) correction.merge_into = merge.value;
      setDraft(item.key, Object.keys(correction).length ? {decision: 'corrected', correction} : null);
    }, 'entity-button primary'));
    form.append(field('名称', name), field('类型', type), field('别名（用顿号分隔）', aliases), field('并入同类实体', merge), actions);
    return form;
  }

  function editAttribute(item, owner) {
    const current = drafts[item.key]?.correction || {};
    const form = node('form', 'entity-edit');
    const section = node('input', 'entity-search'); section.value = current.section ?? item.section;
    section.setAttribute('list', 'entity-sections');
    const list = node('datalist'); list.id = 'entity-sections';
    for (const name of SECTIONS) { const option = node('option'); option.value = name; list.append(option); }
    const text = node('textarea', 'entity-search entity-edit-text'); text.value = current.text ?? item.text;
    const move = node('select', 'entity-search');
    for (const other of entities()) { const option = node('option', '', `${typeLabel(other.type)}：${other.name}`); option.value = other.key; move.append(option); }
    move.value = current.owner ?? owner.key;
    const field = (label, input) => { const row = node('label', 'entity-edit-row'); row.append(node('span', '', label), input); return row; };
    const actions = node('div', 'entity-edit-actions');
    actions.append(button('取消', () => draw()), button('保存修改', () => {
      const correction = {};
      if (section.value.trim() && section.value.trim() !== item.section) correction.section = section.value.trim();
      if (text.value.trim() && text.value.trim() !== item.text) correction.text = text.value.trim();
      if (move.value !== owner.key) correction.owner = move.value;
      setDraft(item.key, Object.keys(correction).length ? {decision: 'corrected', correction} : null);
    }, 'entity-button primary'));
    form.append(list, field('段名', section), field('内容', text), field('属于实体', move), actions);
    return form;
  }

  function itemActions(item, onEdit) {
    const row = node('div', 'entity-review-actions');
    const state = reviewState(item, drafts);
    row.append(node('span', 'entity-review-state ' + state, STATE_LABEL[state]));
    if (!editable()) return row;
    if (onEdit) row.append(button('修改', onEdit, 'entity-link-button'));
    if (state === 'excluded-draft' || state === 'corrected-draft') row.append(button('撤销', () => setDraft(item.key, null), 'entity-link-button'));
    else if (state !== 'excluded') row.append(button('暂不确认', () => setDraft(item.key, {decision: 'excluded'}), 'entity-link-button'));
    return row;
  }

  function drawContent() {
    content.replaceChildren();
    if (selected === HELD) { drawHeld(); return; }
    const item = entity(selected);
    if (!item) { content.append(node('p', 'entity-empty', '从左侧选择一个实体核对。')); return; }
    const top = node('div', 'entity-detail-top');
    const badge = node('span', 'entity-badge', typeLabel(item.type));
    badge.style.background = typeColor(item.type);
    top.append(badge, node('h3', '', item.name));
    content.append(top);
    if (item.aliases?.length) content.append(node('p', 'entity-aliases', '别名：' + item.aliases.join('、')));
    const correction = drafts[item.key]?.correction || item.review?.correction || {};
    if (correction.merge_into) content.append(node('p', 'entity-aliases', '将并入：' + (entity(correction.merge_into)?.name || correction.merge_into)));
    const holder = node('div');
    content.append(itemActions(item, () => { holder.replaceChildren(editEntity(item)); }), holder);
    content.append(node('h4', 'entity-section-title', `属性（${item.attributes.length}）`));
    if (!item.attributes.length) content.append(node('p', 'entity-empty', '没有属性。'));
    for (const {section, items} of groupAttributes(item.attributes)) {
      const block = node('section', 'entity-attribute-section');
      block.append(node('h5', '', `${section}（${items.length}）`));
      for (const attribute of items) {
        const card = node('article', 'entity-attribute entity-review-clickable' + (attribute === focus ? ' focused' : ''));
        const fix = drafts[attribute.key]?.correction || {};
        card.append(node('p', 'entity-attribute-text', fix.text || attribute.text));
        if (fix.section) card.append(node('p', 'entity-aliases', '改为段名：' + fix.section));
        if (fix.owner) card.append(node('p', 'entity-aliases', '移到：' + (entity(fix.owner)?.name || fix.owner)));
        card.append(node('blockquote', 'entity-review-quote', attribute.quote));
        const where = node('div', 'entity-quote-meta');
        if (attribute.page) where.append(node('span', '', `第 ${attribute.page} 页`));
        card.append(where);
        if (attribute.citations?.length) {
          // Restatements of the same reading were folded into this one; their places stay openable.
          const also = node('div', 'entity-quote-meta');
          also.append(node('span', '', '另见'));
          for (const other of attribute.citations) {
            also.append(button(other.page ? `第 ${other.page} 页` : '原文', () => {
              focus = {...other, text: attribute.text}; drawSource(); drawContent();
            }, 'entity-link-button'));
          }
          card.append(also);
        }
        // Clicking anywhere on the card shows its passage on the original page; buttons inside keep their own action.
        card.addEventListener('click', () => { if (focus !== attribute) { focus = attribute; drawSource(); drawContent(); } });
        const edit = node('div');
        card.append(itemActions(attribute, () => { edit.replaceChildren(editAttribute(attribute, item)); }), edit);
        block.append(card);
      }
      content.append(block);
    }
    const held = heldOf(view.held, item.key);
    if (held.length) {
      content.append(node('h4', 'entity-section-title', `未通过核对（${held.length}）· 不会写入知识库`));
      for (const line of held) content.append(heldCard(line, false));
    }
    content.append(node('h4', 'entity-section-title', `相关（${item.related.length}）`));
    for (const link of item.related) {
      const row = node('div', 'entity-related entity-review-clickable');
      if (link.locations?.[0]) row.addEventListener('click', () => { focus = {...link.locations[0], text: link.name}; drawSource(); });
      const head = node('div', 'entity-review-link');
      const dot = node('span', 'entity-dot');
      dot.style.background = typeColor(link.type);
      head.append(dot, button(link.name, () => { selected = link.local_id; focus = null; draw(); }, 'entity-link-button'));
      row.append(head, itemActions(link, null));
      content.append(row);
    }
  }

  function drawSource() {
    source.replaceChildren();
    if (!focus || !focus.page) { source.append(node('p', 'entity-empty', '点击一条属性查看原件页和高亮位置。')); return; }
    const asset = focus.asset_ref || task.asset_ids?.[0];
    source.append(node('div', 'entity-list-label', `第 ${focus.page} 页`), node('blockquote', 'entity-source-quote', focus.quote || ''));
    const wrap = node('div', 'entity-page-wrap');
    const image = node('img', 'entity-page-image');
    image.alt = `原件第 ${focus.page} 页`;
    image.src = `${path}/source-page?asset_id=${idPath(asset)}&page=${focus.page}`;
    wrap.append(image);
    for (const region of evidenceRegions(focus, focus.page)) {
      const box = node('div', 'entity-source-region');
      box.setAttribute('aria-hidden', 'true');
      for (const [key, value] of Object.entries(region)) box.style[key] = `${value}%`;
      wrap.append(box);
    }
    source.append(wrap);
  }

  function draw() {
    confirmButton.disabled = !editable();
    drawHeldNote();
    drawStats();
    drawNav();
    drawContent();
    drawSource();
  }

  async function confirmAll() {
    const who = operator.value.trim();
    if (!who) { tell('请先填写核对人。', 'error'); return; }
    busy = true; confirmButton.disabled = true;
    try {
      let reviewHash = view.review_sha256;
      const items = draftItems(drafts);
      for (let index = 0; index < items.length; index += BATCH) {
        tell(`正在提交修改 ${Math.min(index + BATCH, items.length)}/${items.length}…`);
        const saved = await api(`${path}/entity-draft/reviews`, {method: 'POST', json: {
          expected_draft_sha256: view.draft_sha256, expected_review_sha256: reviewHash, operator: who, items: items.slice(index, index + BATCH)}});
        reviewHash = saved.review_sha256;
      }
      drafts = {}; persist();
      tell('正在与知识库消歧并写入…');
      const updated = await api(`${path}/entity-draft/confirm`, {method: 'POST', timeout: 600000, json: {
        expected_draft_sha256: view.draft_sha256, expected_review_sha256: reviewHash, operator: who}});
      if (disposed) return;
      tell(updated.progress || '已写入知识库。', 'success');
      onTaskChange?.(updated);
    } catch (error) {
      if (disposed) return;
      if (error.status === 409) { conflict = true; tell('提取结果或核对版本已变化，未提交的修改已保留，请刷新后再确认。', 'error'); }
      else tell(error.message || '写入未完成，修改已保留，可重试。', 'error');
      await load();
    } finally {
      busy = false;
      if (!disposed) draw();
    }
  }

  async function load() {
    try {
      view = await api(`${path}/entity-draft`);
      if (disposed) return;
      if (view.status !== 'available') { tell('还没有可核对的提取结果。'); content.replaceChildren(); return; }
      if (view.title) title.textContent = view.title;
      try { drafts = JSON.parse(storage?.getItem(storageKey()) || '{}') || {}; } catch { drafts = {}; }
      const keys = new Set(entities().flatMap(item => [item.key, ...item.attributes.map(a => a.key), ...item.related.map(r => r.key)]));
      for (const key of Object.keys(drafts)) if (!keys.has(key)) delete drafts[key];
      if (selected !== HELD && !entity(selected)) selected = entities().find(item => item.type === 'formulation')?.key || entities()[0]?.key || null;
      focus = focus || entity(selected)?.attributes[0] || null;
      draw();
    } catch (error) {
      if (!disposed) tell(error.message || '提取结果暂时无法读取。', 'error');
    }
  }

  content.append(node('p', 'entity-empty', '正在读取提取结果…'));
  const ready = load();
  const dispose = () => { disposed = true; };
  dispose.ready = ready;
  return dispose;
}
