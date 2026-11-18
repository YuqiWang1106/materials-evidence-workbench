// @ts-check
import {mountEntitySupplement} from './entity-supplement-view.js';
import {pageTitle} from './page-title.js';
import {evidenceRegions} from './document-result-logic.mjs';
import {titleForResult} from './result-logic.js';
import {GRAPH_TYPES, countByType, edgePath, filterGraph, focusGraph, layoutFocusGraph, groupAttributes, groupRelated, layoutGraph, neighbours,
  nodeRadius, onlySource, shortLabel, typeColor, typeLabel} from './entity-graph-logic.mjs';

const SVG = 'http://www.w3.org/2000/svg';
const idPath = value => encodeURIComponent(value);
const GRAPH_WIDTH = 860, GRAPH_HEIGHT = 600;

/** A paper extracted as entities: its relation graph, and the attributes and links of the entity you click. */
export function renderEntityResult({root, task, api}) {
  const path = `/api/tasks/${idPath(task.task_id)}`;
  const documentCount = task.processing?.documents?.length ?? task.asset_ids?.length ?? 1;
  const repairRoot=root.ownerDocument.createElement('section'), graphRoot=root.ownerDocument.createElement('section');
  root.replaceChildren(repairRoot,graphRoot);
  const mount=()=>mountEntityExplorer({root:graphRoot, api, task, title: titleForResult(task),
    scopes: [
      {key: 'paper', label: (documentCount > 1 ? '本资料组' : '本篇'), load: () => api(`${path}/entity-graph`)},
      ...(task.processing?.documents || []).filter(() => documentCount > 1).map((document, index) => ({
        key: 'asset:' + document.asset_id, label: '原件 ' + (index + 1), description: document.name,
        load: () => api(`${path}/entity-graph?asset_id=${idPath(document.asset_id)}`),
      })),
      {key: 'all', label: '全库', load: () => api('/api/entity-graph')},
    ],
    readEntity: (entityId, scope) => scope !== 'all'
      ? api(`${path}/entity-graph?entity_id=${idPath(entityId)}${scope.startsWith('asset:') ? '&asset_id=' + idPath(scope.slice(6)) : ''}`).then(payload => onlySource(payload.entity, task.source_id))
      : api(`/api/entities/${idPath(entityId)}`)});
  let cleanupGraph=mount();
  const cleanupRepair=mountEntitySupplement({root:repairRoot,path,api,onApplied:()=>{cleanupGraph?.();cleanupGraph=mount();}});
  const dispose=()=>{cleanupRepair();cleanupGraph?.();};
  dispose.ready=cleanupGraph?.ready;
  return dispose;
}

/** How this paper's mentions became entities: found across windows, merged within the paper, merged into the
 * knowledge base. Recipes belong to their paper and are never merged into another paper's. */
export function mergeLine(summary) {
  const found = summary?.check?.entities;
  const entities = summary?.resolved?.entities || summary?.merged?.entities;
  if (!found || !entities) return '';
  const total = typeof entities === 'number' ? entities : Object.values(entities).reduce((sum, value) => sum + value, 0);
  const existing = summary.decisions?.existing || 0, fresh = summary.decisions?.new || 0, uncertain = summary.decisions?.uncertain || 0;
  return `发现 ${found} 处实体，本篇合并为 ${total} 个；并入知识库已有实体 ${existing} 个，新建 ${fresh} 个`
    + (uncertain ? `，待定 ${uncertain} 个` : '');
}

/** The whole workspace knowledge graph, across every extracted paper. */
export function mountKnowledgeGraph({root, api, entityId = null}) {
  return mountEntityExplorer({root, api, task: null, title: '知识图谱', initialEntity: entityId,
    scopes: [{key: 'all', label: '全库', load: () => api('/api/entity-graph')}],
    readEntity: id => api(`/api/entities/${idPath(id)}`)});
}

/** Shared original-page panel for graph locations and Agent citation links. */
export function renderSourcePanel({root, location, task = null, onClose = () => {}}) {
  const doc = root.ownerDocument;
  let disposed = false;
  const node = (tag, className, text) => {
    const element = doc.createElement(tag);
    if (className) element.className = className;
    if (text != null) element.textContent = String(text);
    return element;
  };
  const button = (label, action, className) => {
    const element = node('button', className, label);
    element.type = 'button';
    element.addEventListener('click', action);
    return element;
  };
  const closeSource = onClose;
  const taskId = location.task_id || task?.task_id;
  const asset = location.asset_ref || task?.asset_ids?.[0];
  const panel = node('div', 'entity-source-panel');
  const head = node('div', 'entity-source-head');
  head.append(node('strong', '', [location.source_title, location.page ? `第 ${location.page} 页` : ''].filter(Boolean).join(' · ') || '原文位置'),
    button('关闭', closeSource, 'entity-button'));
  const columns = node('div', 'entity-source-columns');
  const pageWrap = node('div', 'entity-page-wrap');
  const side = node('div', 'entity-source-side');
  side.append(node('div', 'entity-list-label', location.section || '出处'));
  if (location.text) side.append(node('p', '', location.text));
  side.append(node('div', 'entity-list-label', '原文摘录'), node('blockquote', 'entity-source-quote', location.quote || ''));
  if (asset) {
    const download = node('a', 'entity-button', '下载完整原件');
    download.href = `/api/assets/${idPath(asset)}/download`;
    download.download = '';
    side.append(download);
  }
  if (taskId && asset && location.page) {
    const image = node('img', 'entity-page-image');
    image.alt = `原件第 ${location.page} 页`;
    image.src = `/api/tasks/${idPath(taskId)}/source-page?asset_id=${idPath(asset)}&page=${location.page}`;
    image.addEventListener('error', () => {
      if (disposed) return;
      pageWrap.replaceChildren(node('p', 'entity-empty', '原件页暂时无法预览，请下载原件核对。'));
    });
    pageWrap.append(image);
    for (const region of evidenceRegions(location, location.page)) {
      const box = node('div', 'entity-source-region');
      box.setAttribute('aria-hidden', 'true');
      for (const [key, value] of Object.entries(region)) box.style[key] = `${value}%`;
      pageWrap.append(box);
    }
  } else {
    pageWrap.append(node('p', 'entity-empty', '这条记录没有可定位的原件页。'));
  }
  columns.append(pageWrap, side);
  panel.append(head, columns);
  root.replaceChildren(panel);
  return () => { disposed = true; };
}

export function mountEvidenceSource({root, api, evidenceRef, onClose = () => {}}) {
  const doc = root.ownerDocument, controller = new AbortController();
  let disposed = false, closePanel = null;
  const loading = doc.createElement('p');
  loading.className = 'entity-empty';
  loading.textContent = '正在读取原文出处…';
  root.replaceChildren(loading);
  const ready = (async () => {
    try {
      if (!evidenceRef) throw Object.assign(new Error('缺少证据引用。'), {code: 'INVALID_ARGUMENT'});
      const payload = await api('/api/agent-knowledge/v1/evidence/read', {
        method: 'POST', json: {evidence_ref: evidenceRef}, signal: controller.signal});
      if (disposed) return;
      const evidence = payload?.data?.evidence;
      if (!evidence?.task_id || !evidence.asset_id || !evidence.quote)
        throw new Error('证据内容不完整，无法打开原文。');
      const overlay = doc.createElement('div');
      overlay.className = 'entity-source-overlay';
      overlay.setAttribute('role', 'dialog');
      overlay.setAttribute('aria-label', '原文出处');
      root.replaceChildren(overlay);
      closePanel = renderSourcePanel({root: overlay, location: {
        ...evidence, asset_ref: evidence.asset_id, source_title: evidence.title}, onClose});
    } catch (error) {
      if (disposed) return;
      const messages = {
        SOURCE_CHANGED: '来源已更新，此引用无法自动切换到新版。请在 Omni 重新检索并读取证据。',
        SOURCE_NOT_READY: '来源产物暂不可读取，请检查知识库中的资料状态。',
        REFERENCE_NOT_FOUND: '这条引用对应的来源已不可用。',
        INTEGRITY_FAILURE: '来源与保存的引用不一致，无法打开原文。',
        UNAUTHORIZED: '读取会话不可用，请刷新知识库页面。',
      };
      loading.textContent = messages[error.code] || error.message || '暂时无法读取原文出处。';
      const back = doc.createElement('button');
      back.type = 'button'; back.className = 'entity-button'; back.textContent = '返回资料库';
      back.addEventListener('click', onClose);
      root.replaceChildren(loading, back);
    }
  })();
  const dispose = () => { disposed = true; controller.abort(); closePanel?.(); };
  dispose.ready = ready;
  return dispose;
}

function mountEntityExplorer({root, api, task, title, scopes, readEntity, initialEntity = null}) {
  const doc = root.ownerDocument, controller = new AbortController();
  let disposed = false, scope = scopes[0].key, graph = {nodes: [], edges: []}, positions = /** @type {Map<string, {x: number, y: number}> & {width?: number, height?: number}} */ (new Map());
  let mode = 'focus', relationPage = 0, searchPage = 0, layoutKey = '', detailOpen = false, expanded = false;
  let selected = null, detail = null, hidden = new Set(), query = '', view = {x: 0, y: 0, k: 1};
  let graphToken = 0;
  let detailToken = 0, closeSourceView = null, summary = null, graphWidth = GRAPH_WIDTH, graphHeight = GRAPH_HEIGHT;
  const node = (tag, className, text) => {
    const element = doc.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  };
  const svg = (tag, attributes = {}) => {
    const element = doc.createElementNS(SVG, tag);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
    return element;
  };
  const button = (label, action, className = 'entity-button') => {
    const element = node('button', className, label);
    element.type = 'button';
    element.addEventListener('click', action);
    return element;
  };
  const guard = promise => promise.then(value => { if (disposed) throw Error('视图已关闭'); return value; });

  const shell = node('section', 'entity-page entity-explorer');
  const heading = node('header', 'entity-heading');
  const headingText = node('div', 'entity-heading-text');
  const headingTitle = node('h2', '', title);
  const headingNote = node('p', 'entity-heading-note', '从一个实体出发，查看直接关系与原文证据');
  headingText.append(pageTitle(headingTitle,task?'资料整理':'知识浏览'), headingNote);
  const scopeTabs = node('div', 'entity-scope-tabs');
  scopeTabs.setAttribute('role', 'tablist');
  heading.append(headingText, scopeTabs);
  const stats = node('div', 'entity-stats');
  const message = node('p', 'entity-message');
  message.setAttribute('role', 'status');
  message.hidden = true;
  const body = node('div', 'entity-body detail-closed');
  const graphPanel = node('section', 'entity-graph-panel');
  graphPanel.setAttribute('aria-label', '关系图');
  const toolbar = node('div', 'entity-graph-toolbar');
  const search = node('input', 'entity-search');
  search.type = 'search';
  search.placeholder = '查找实体名称或别名';
  search.setAttribute('aria-label', '查找实体');
  const legend = node('div', 'entity-legend');
  const zoom = node('div', 'entity-zoom');
  const expandButton = button('全屏查看', () => setExpanded(!expanded));
  const detailButton = button('查看属性', () => setDetailOpen(!detailOpen));
  zoom.append(button('＋', () => zoomBy(1.25), 'entity-icon-button'), button('－', () => zoomBy(0.8), 'entity-icon-button'),
    button('适应画布', () => { fitView(); }, 'entity-icon-button'), detailButton, expandButton);
  toolbar.append(search, zoom);
  const canvas = svg('svg', {viewBox: `0 0 ${GRAPH_WIDTH} ${GRAPH_HEIGHT}`, class: 'entity-graph', role: 'group'});
  canvas.setAttribute('aria-label', '实体关系图，点击节点查看详情');
  const stage = svg('g');
  const edgeLayer = svg('g', {class: 'entity-edges'});
  const nodeLayer = svg('g', {class: 'entity-nodes'});
  stage.append(edgeLayer, nodeLayer);
  canvas.append(stage);
  const graphNote = node('p', 'entity-graph-note');
  const modes = node('div', 'entity-graph-modes');
  const focusButton = button('局部关系', () => changeMode('focus'));
  const overviewButton = button('全图概览', () => changeMode('overview'));
  modes.append(focusButton, overviewButton);
  const context = node('div', 'entity-graph-context');
  const contextTitle = node('strong', 'entity-focus-title');
  const pager = node('div', 'entity-relation-pager');
  context.append(contextTitle, pager);
  const searchResults = node('div', 'entity-graph-search-results');
  searchResults.hidden = true;
  searchResults.setAttribute('aria-label', '实体搜索结果');
  graphNote.setAttribute('role', 'status');
  const filters = node('div', 'entity-graph-filters');
  filters.append(modes, legend);
  graphPanel.append(toolbar, searchResults, filters, context, canvas, graphNote);
  const detailPanel = node('aside', 'entity-detail');
  detailPanel.setAttribute('aria-label', '实体详情');
  detailPanel.setAttribute('aria-live', 'polite');
  body.append(graphPanel, detailPanel);
  const overlay = node('div', 'entity-source-overlay');
  overlay.hidden = true;
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-label', '原文位置');
  shell.append(heading, stats, message, body, overlay);
  root.replaceChildren(shell);

  const tell = (text, tone = '') => { message.textContent = text || ''; message.hidden = !text; message.className = 'entity-message ' + tone; };

  search.addEventListener('input', () => { query = search.value || ''; searchPage = 0; drawSearch(); drawGraph(); });

  function setDetailOpen(value) {
    const wasOpen = detailOpen;
    detailOpen = value;
    detailPanel.hidden = !value;
    body.className = 'entity-body' + (value ? '' : ' detail-closed');
    detailButton.textContent = value ? '收起属性' : '查看属性';
    detailButton.setAttribute('aria-expanded', String(value));
    if (wasOpen && !value) detailButton.focus();
  }
  function setExpanded(value) {
    expanded = value;
    shell.className = 'entity-page entity-explorer' + (value ? ' entity-expanded' : '');
    expandButton.textContent = value ? '退出全屏' : '全屏查看';
    expandButton.setAttribute('aria-expanded', String(value));
    if (value) { shell.setAttribute('role', 'dialog'); shell.setAttribute('aria-modal', 'true'); shell.setAttribute('aria-label', '知识图谱全屏查看'); expandButton.focus(); }
    else { shell.removeAttribute('role'); shell.removeAttribute('aria-modal'); shell.removeAttribute('aria-label'); expandButton.focus(); }
  }
  const onEscape = event => {
    if (expanded && event.key === 'Tab') {
      const controls = [...shell.querySelectorAll('button, input, a[href], [tabindex="0"]')].filter(item => !item.disabled && item.getClientRects().length);
      const first = controls[0], last = controls.at(-1);
      if (!shell.contains(doc.activeElement)) { event.preventDefault(); (event.shiftKey ? last : first)?.focus(); }
      else if (event.shiftKey && doc.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && doc.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
    if (event.key !== 'Escape') return;
    if (!overlay.hidden) closeSource();
    else if (expanded) setExpanded(false);
  };
  doc.addEventListener?.('keydown', onEscape);
  setDetailOpen(false);
  function changeMode(value) { mode = value; layoutKey = ''; drawGraph(); }
  function drawSearch(focusControl = null) {
    searchResults.replaceChildren();
    searchResults.hidden = !query.trim();
    if (searchResults.hidden) return;
    const matches = filterGraph(graph, {query}).matches;
    const items = graph.nodes.filter(item => matches.has(item.entity_id));
    const pages = Math.max(1, Math.ceil(items.length / 8));
    searchPage = Math.min(searchPage, pages - 1);
    searchResults.append(node('p', 'entity-search-count', items.length ? `当前图中匹配 ${items.length} 个实体 · 点击定位` : '当前图中没有匹配实体，请尝试其他名称或别名。'));
    for (const item of items.slice(searchPage * 8, (searchPage + 1) * 8)) {
      const pick = button(`${typeLabel(item.type)} · ${item.name}`, () => {
        mode = 'focus'; query = ''; search.value = ''; drawSearch(); select(item.entity_id, {focusNode: true});
      }, 'entity-search-result');
      searchResults.append(pick);
    }
    if (pages > 1) {
      const prev = button('上一组结果', () => { searchPage--; drawSearch('上一组结果'); }); prev.disabled = searchPage === 0;
      const next = button('下一组结果', () => { searchPage++; drawSearch('下一组结果'); }); next.disabled = searchPage === pages - 1;
      searchResults.append(prev, node('span', '', ` ${searchPage + 1} / ${pages} `), next);
      if (focusControl) ([prev, next].find(item => item.textContent === focusControl && !item.disabled) || [prev, next].find(item => !item.disabled))?.focus();
    }
  }
  function fitView() {
    const k = mode === 'focus' ? 1 : Math.min(1, graphWidth / (positions.width || graphWidth), graphHeight / (positions.height || graphHeight));
    view = {x: (graphWidth - (positions.width || graphWidth) * k) / 2, y: (graphHeight - (positions.height || graphHeight) * k) / 2, k};
    applyView();
  }
  const Observer = doc.defaultView?.ResizeObserver;
  const resizeObserver = Observer ? new Observer(() => {
    const box = canvas.getBoundingClientRect();
    if (box.width > 0 && box.height > 0 && (Math.abs(graphWidth - box.width) > 1 || Math.abs(graphHeight - box.height) > 1)) {
      graphWidth = box.width; graphHeight = box.height;
      canvas.setAttribute('viewBox', `0 0 ${graphWidth} ${graphHeight}`);
      layoutKey = ''; drawGraph();
    }
  }) : null;
  resizeObserver?.observe(canvas);

  function drawScopes() {
    scopeTabs.replaceChildren();
    const current = scopes.find(item => item.key === scope);
    headingNote.textContent = current.description || '实体 · 属性 · 相关';
    if (scopes.length < 2) return;
    for (const item of scopes) {
      const tab = button(item.label, () => { if (scope !== item.key) { scope = item.key; loadGraph(); } }, 'entity-scope-tab');
      if(item.description)tab.setAttribute('title',item.description);
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', String(scope === item.key));
      scopeTabs.append(tab);
    }
  }

  function drawStats() {
    stats.replaceChildren();
    for (const {type, count} of countByType(graph.nodes)) {
      const chip = node('span', 'entity-stat');
      const dot = node('span', 'entity-dot');
      dot.style.background = typeColor(type);
      chip.append(dot, node('span', '', `${typeLabel(type)} ${count}`));
      stats.append(chip);
    }
    stats.append(node('span', 'entity-stat muted', `相关 ${graph.edges.length}`));
    const merged = mergeLine(summary);
    if (merged) stats.append(node('span', 'entity-stat muted entity-merge-stat', merged));
  }

  function drawLegend() {
    legend.replaceChildren();
    const present = new Set(graph.nodes.map(item => item.type));
    for (const type of GRAPH_TYPES.filter(item => present.has(item))) {
      const label = node('label', 'entity-legend-item');
      const box = node('input');
      box.type = 'checkbox';
      box.checked = !hidden.has(type);
      box.addEventListener('change', () => { if (box.checked) hidden.delete(type); else hidden.add(type); relationPage = 0; layoutKey = ''; drawGraph(); });
      const dot = node('span', 'entity-dot');
      dot.style.background = typeColor(type);
      label.append(box, dot, node('span', '', typeLabel(type)));
      legend.append(label);
    }
  }

  function applyView() {
    stage.setAttribute('transform', `translate(${view.x} ${view.y}) scale(${view.k})`);
  }

  function zoomBy(factor) {
    const k = Math.min(4, Math.max(0.05, view.k * factor));
    view = {x: graphWidth / 2 - (graphWidth / 2 - view.x) * (k / view.k), y: graphHeight / 2 - (graphHeight / 2 - view.y) * (k / view.k), k};
    applyView();
  }

  function svgPoint(event) {
    const matrix = canvas.getScreenCTM?.();
    if (matrix && canvas.createSVGPoint) {
      const point = canvas.createSVGPoint(); point.x = event.clientX; point.y = event.clientY;
      return point.matrixTransform(matrix.inverse());
    }
    const box = canvas.getBoundingClientRect?.();
    if (!box || !box.width) return {x: 0, y: 0};
    return {x: (event.clientX - box.left) * (graphWidth / box.width), y: (event.clientY - box.top) * (graphHeight / box.height)};
  }

  let drag = null;
  canvas.addEventListener('pointerdown', event => {
    if (event.target === canvas || event.target === stage) {
      drag = {kind: 'pan', start: svgPoint(event), origin: {...view}};
      canvas.setPointerCapture?.(event.pointerId);
    }
  });
  canvas.addEventListener('pointermove', event => {
    if (!drag) return;
    const point = svgPoint(event);
    if (drag.kind === 'pan') {
      view = {...drag.origin, x: drag.origin.x + point.x - drag.start.x, y: drag.origin.y + point.y - drag.start.y};
      applyView();
    } else if (drag.kind === 'node') {
      const moved = Math.hypot(point.x - drag.start.x, point.y - drag.start.y);
      if (moved > 3) drag.moved = true;
      positions.set(drag.id, {x: (point.x - view.x) / view.k, y: (point.y - view.y) / view.k});
      drawGraph();
    }
  });
  let suppressClick = false;
  const endDrag = () => {
    if (drag?.kind === 'node') {
      suppressClick = true;
      if (!drag.moved) select(drag.id);
    }
    drag = null;
  };
  canvas.addEventListener('pointercancel', () => { drag = null; suppressClick = false; });
  canvas.addEventListener('pointerup', endDrag);
  canvas.addEventListener('pointerleave', () => { if (drag?.kind === 'pan') drag = null; });
  canvas.addEventListener('wheel', event => {
    event.preventDefault?.();
    zoomBy(event.deltaY < 0 ? 1.12 : 0.89);
  }, {passive: false});

  function drawGraph(focusControl = null) {
    const focusedNode = doc.activeElement?.getAttribute?.('data-entity-id');
    const types = new Set(GRAPH_TYPES.filter(type => !hidden.has(type)));
    const filtered = filterGraph(graph, {types, query});
    const shown = mode === 'focus' ? focusGraph(graph, {entityId: selected, types, page: relationPage}) : filtered;
    shown.matches = filtered.matches;
    relationPage = shown.page || 0;
    const key = [mode, selected, relationPage, ...hidden].join('|');
    if (key !== layoutKey) {
      positions = mode === 'focus' ? layoutFocusGraph(shown.nodes, selected, {width: graphWidth, height: graphHeight})
        : layoutGraph(shown.nodes, shown.edges, {width: graphWidth, height: graphHeight});
      layoutKey = key; fitView();
    }
    focusButton.setAttribute('aria-pressed', String(mode === 'focus'));
    overviewButton.setAttribute('aria-pressed', String(mode === 'overview'));
    const current = graph.nodes.find(item => item.entity_id === selected);
    contextTitle.textContent = mode === 'focus' ? (current ? current.name : '选择一个实体开始查看') : '全图概览 · 搜索实体可进入局部关系';
    pager.replaceChildren();
    if (mode === 'focus') {
      pager.append(node('span', '', `直接相连 ${shown.totalNeighbors} 个`));
      if (shown.pageCount > 1) {
        const prev = button('上一组关系', () => { relationPage--; layoutKey = ''; drawGraph('上一组关系'); }); prev.disabled = relationPage === 0;
        const next = button('下一组关系', () => { relationPage++; layoutKey = ''; drawGraph('下一组关系'); }); next.disabled = relationPage === shown.pageCount - 1;
        pager.append(prev, node('span', '', `${relationPage + 1} / ${shown.pageCount}`), next);
      }
    }
    const focus = selected && shown.nodes.some(item => item.entity_id === selected) ? selected : null;
    const near = focus ? neighbours(shown.edges, focus) : new Set();
    edgeLayer.replaceChildren();
    nodeLayer.replaceChildren();
    for (const edge of shown.edges) {
      const a = positions.get(edge.a), b = positions.get(edge.b);
      if (!a || !b) continue;
      const active = focus && (edge.a === focus || edge.b === focus);
      const line = svg('path', {d: edgePath(a, b), fill: 'none',
        class: 'entity-edge' + (active ? ' active' : focus ? ' faded' : '') + (edge.kind === 'uncertain_identity' ? ' uncertain' : ''),
        'stroke-width': Math.min(4, 0.8 + Math.log2(1 + (edge.weight || 1)))});
      edgeLayer.append(line);
    }
    const labelAll = shown.nodes.length <= 60;
    for (const item of shown.nodes) {
      const at = positions.get(item.entity_id);
      if (!at) continue;
      const isFocus = item.entity_id === focus, isNear = near.has(item.entity_id), matched = shown.matches.has(item.entity_id);
      const group = svg('g', {class: 'entity-node' + (isFocus ? ' selected' : '') + (focus && !isFocus && !isNear ? ' faded' : '') + (matched ? ' matched' : ''),
        transform: `translate(${at.x} ${at.y})`, tabindex: 0, role: 'button'});
      group.setAttribute('data-entity-id', item.entity_id);
      group.setAttribute('aria-current', String(isFocus));
      group.setAttribute('aria-label', `${typeLabel(item.type)}：${item.name}`);
      const radius = mode === 'focus' ? (isFocus ? 18 : 9) : nodeRadius(item);
      const shape = item.type === 'formulation'
        ? svg('rect', {x: -radius, y: -radius, width: radius * 2, height: radius * 2, rx: 4, fill: typeColor(item.type)})
        : svg('circle', {r: radius, fill: typeColor(item.type)});
      const tip = svg('title');
      tip.textContent = `${typeLabel(item.type)}：${item.name}${item.aliases?.length ? '（' + item.aliases.join('、') + '）' : ''}`;
      group.append(shape, tip);
      if (labelAll || isFocus || isNear || matched || item.type === 'formulation' || (item.degree || 0) >= 4) {
        // Beside the node: columns leave room on the right, rows are too close for labels underneath.
        const local = mode === 'focus', right = at.x > graphWidth / 2;
        const label = svg('text', {x: local && isFocus ? 0 : local && right ? -radius - 9 : radius + 9,
          y: local && isFocus ? 40 : 4, 'text-anchor': local && isFocus ? 'middle' : local && right ? 'end' : 'start', class: 'entity-node-label'});
        const limit = local ? Math.max(3, Math.min(24, Math.floor((graphWidth / 2 - 85) / 14))) : 15;
        label.textContent = local && isFocus ? '当前' + typeLabel(item.type) : shortLabel(item.label || item.name, limit);
        group.append(label);
      }
      group.addEventListener('pointerdown', event => {
        event.stopPropagation?.();
        drag = {kind: 'node', id: item.entity_id, start: svgPoint(event), moved: false};
        canvas.setPointerCapture?.(event.pointerId);
      });
      group.addEventListener('click', () => { if (suppressClick) { suppressClick = false; return; } select(item.entity_id); });
      group.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault?.(); select(item.entity_id, {focusNode: true}); } });
      nodeLayer.append(group);
    }
    graphNote.textContent = !graph.nodes.length ? (scope.startsWith('asset:') ? '此原件没有可定位到它的已保存实体。资料组已保存不代表每篇都抽取成功，请核对原文与抽取覆盖提示。' : '还没有写入实体。提取完成后，配方、材料、性能等会显示在这里。')
      : mode === 'focus' ? (shown.nodes.length ? `当前展示 ${Math.max(0, shown.nodes.length - 1)} / ${shown.totalNeighbors} 个直接关联实体。点击节点切换中心；拖动平移，滚轮缩放。` : '该实体不在当前图中，可切换范围或搜索其他实体。')
      : `${shown.nodes.length} 个实体 · ${shown.edges.length} 条关系。概览用于查看分布，阅读名称请搜索或点击节点进入局部关系。`;
    if (focusedNode) [...nodeLayer.children].find(item => item.getAttribute?.('data-entity-id') === focusedNode)?.focus();
    if (focusControl) ([...pager.children].find(item => item.textContent === focusControl && !item.disabled) || [...pager.children].find(item => item.tagName?.toLowerCase() === 'button' && !item.disabled))?.focus();
    if (graph.truncated) graphNote.textContent += ' 当前图为连接较多的实体子集，并非全库全部关系。';
  }

  function quoteBlock(location) {
    const wrap = node('div', 'entity-quote');
    wrap.append(node('blockquote', '', location.quote || ''));
    const meta = node('div', 'entity-quote-meta');
    const where = [location.source_title && scope !== 'paper' ? location.source_title : null,
      location.page ? `第 ${location.page} 页` : null].filter(Boolean).join(' · ');
    if (where) meta.append(node('span', '', where));
    wrap.append(meta);
    if (location.page) {
      // Clicking the passage opens it on the original page.
      wrap.className += ' entity-review-clickable';
      wrap.addEventListener('click', event => { event.stopPropagation?.(); openSource(location); });
    }
    return wrap;
  }

  function drawDetail() {
    detailPanel.replaceChildren();
    if (!detail) {
      detailPanel.append(node('p', 'entity-empty', graph.nodes.length ? '点击图中的节点查看属性和相关实体。' : '暂无实体。'));
      return;
    }
    detailPanel.append(button('收起属性', () => setDetailOpen(false), 'entity-link-button'));
    const top = node('div', 'entity-detail-top');
    const badge = node('span', 'entity-badge', typeLabel(detail.type));
    badge.style.background = typeColor(detail.type);
    top.append(badge, node('h3', '', detail.name));
    detailPanel.append(top);
    if (detail.aliases?.length) detailPanel.append(node('p', 'entity-aliases', '别名：' + detail.aliases.join('、')));
    if (detail.type === 'formulation' && detail.source_title && scope !== 'paper') {
      detailPanel.append(node('p', 'entity-aliases', '来自：' + detail.source_title));
    }
    const sections = groupAttributes(detail.attributes);
    detailPanel.append(node('h4', 'entity-section-title', '属性'));
    if (!sections.length) detailPanel.append(node('p', 'entity-empty', '这个实体没有直接写在它上面的属性。用量、条件和测得值记录在相关配方上。'));
    for (const {section, items} of sections) {
      const block = node('section', 'entity-attribute-section');
      block.append(node('h5', '', `${section}（${items.length}）`));
      for (const item of items) {
        const card = node('article', 'entity-attribute');
        card.append(node('p', 'entity-attribute-text', item.text));
        card.append(quoteBlock(item));
        if (item.citations?.length) {
          // The same reading found at other places, in this paper or another.
          const also = node('div', 'entity-same-claims');
          also.append(node('span', 'entity-list-label', '另见'));
          for (const other of item.citations) {
            const label = [other.source_title && other.source_title !== item.source_title ? other.source_title : null,
              other.page ? `第 ${other.page} 页` : '原文'].filter(Boolean).join(' · ');
            also.append(button(label, () => openSource(other), 'entity-link-button'));
          }
          card.append(also);
        }
        if (item.same_claims?.length) {
          const same = node('div', 'entity-same-claims');
          same.append(node('span', 'entity-list-label', '同一结论也见于'));
          for (const other of item.same_claims) {
            const row = node('div', 'entity-same-claim');
            row.append(node('span', '', `${other.name || ''}：${other.text}`));
            if (other.page) row.append(button(`第 ${other.page} 页`, () => openSource(other), 'entity-link-button'));
            same.append(row);
          }
          card.append(same);
        }
        block.append(card);
      }
      detailPanel.append(block);
    }
    detailPanel.append(node('h4', 'entity-section-title', `相关（${detail.related?.length || 0}）`));
    if (!detail.related?.length) detailPanel.append(node('p', 'entity-empty', '还没有相关实体。'));
    for (const {type, items} of groupRelated(detail.related)) {
      const block = node('section', 'entity-related-group');
      block.append(node('h5', '', `${typeLabel(type)}（${items.length}）`));
      for (const item of items) {
        const row = node('details', 'entity-related');
        const summaryRow = node('summary');
        const dot = node('span', 'entity-dot');
        dot.style.background = typeColor(item.type);
        summaryRow.append(dot, node('span', 'entity-related-name', item.name),
          node('span', 'entity-related-count', item.kind === 'uncertain_identity' ? '可能是同一实体' : `${item.locations?.length || 0} 处出处`));
        const open = button('打开', event => { event.preventDefault?.(); select(item.entity_id, {focusNode: true}); }, 'entity-link-button');
        summaryRow.append(open);
        row.append(summaryRow);
        for (const location of item.locations || []) row.append(quoteBlock(location));
        block.append(row);
      }
      detailPanel.append(block);
    }
  }

  function closeSource() {
    closeSourceView?.();
    closeSourceView = null;
    overlay.hidden = true;
    overlay.replaceChildren();
  }

  function openSource(location) {
    closeSourceView?.();
    closeSourceView = renderSourcePanel({root: overlay, location, task, onClose: closeSource});
    overlay.hidden = false;
  }
  overlay.addEventListener('click', event => { if (event.target === overlay) closeSource(); });

  async function select(entityId, {reveal = true, focusNode = false} = {}) {
    if (!entityId) return;
    selected = entityId; relationPage = 0; mode = 'focus'; layoutKey = '';
    if (reveal) setDetailOpen(true);
    drawGraph();
    if (focusNode) nodeLayer.querySelector?.('[aria-current="true"]')?.focus();
    const token = ++detailToken;
    detailPanel.replaceChildren(node('p', 'entity-empty', '正在读取…'));
    try {
      const entity = await guard(readEntity(entityId, scope));
      if (token !== detailToken) return;
      detail = entity;
      if (!graph.nodes.some(item => item.entity_id === entityId) && entity) {
        // An entity outside the current graph (for example a related entity in another paper): show it in the full graph.
        tell('该实体不在当前视图中，可切换到「全库」查看它的位置。');
      }
      drawDetail();
      drawGraph();
    } catch (error) {
      if (disposed || token !== detailToken) return;
      detailPanel.replaceChildren(node('p', 'entity-empty', error.message || '实体暂时无法读取。'));
    }
  }

  async function loadGraph() {
    const token = ++graphToken;
    ++detailToken;
    drawScopes();
    tell('');
    graphNote.textContent = '正在读取关系图…';
    try {
      const payload = await guard(scopes.find(item => item.key === scope).load());
      if(disposed || token !== graphToken)return;
      graph = payload.graph || {nodes: payload.nodes || [], edges: payload.edges || [], truncated: payload.truncated};
      if (scope === 'paper') {
        if (payload.title) headingTitle.textContent = payload.title;
      }
      // Organizations are kept in the data (and in an entity's related list) but not drawn.
      const drawn = new Set(graph.nodes.filter(item => GRAPH_TYPES.includes(item.type)).map(item => item.entity_id));
      graph = {...graph, nodes: graph.nodes.filter(item => drawn.has(item.entity_id)),
        edges: graph.edges.filter(edge => drawn.has(edge.a) && drawn.has(edge.b))};
      summary = scope === 'paper' ? payload.summary || null : null;
      const coverage = scope !== 'all' ? task?.processing?.extraction_coverage : null;
      if (coverage?.failed_windows || coverage?.skipped_pages) tell(`资料组抽取存在缺口：${coverage.failed_windows} / ${coverage.windows} 个窗口失败${coverage.skipped_pages ? '，另有未抽取页码' : ''}。已保存不表示全部原件抽取完成，请逐篇核对。`, 'warn');
      layoutKey = ''; relationPage = 0; drawSearch();
      drawStats();
      drawLegend();
      const keep = selected && graph.nodes.some(item => item.entity_id === selected) ? selected : null;
      const first = initialEntity || keep || payload.entity?.entity_id
        || graph.nodes.find(item => item.type === 'formulation')?.entity_id || graph.nodes[0]?.entity_id;
      initialEntity = null;
      if (payload.entity && first === payload.entity.entity_id) {
        selected = first; detail = scope === 'paper' ? onlySource(payload.entity, task?.source_id) : payload.entity; drawGraph(); drawDetail();
      } else if (first) {
        drawGraph(); await select(first, {reveal: false});
      } else {
        selected = null; detail = null; drawGraph(); drawDetail();
      }
    } catch (error) {
      if (disposed || token !== graphToken) return;
      tell(error.message || '关系图暂时无法读取。', 'error');
      graphNote.textContent = '';
    }
  }

  detailPanel.append(node('p', 'entity-empty', '正在读取抽取结果…'));
  const ready = loadGraph();
  const dispose = () => { disposed = true; detailToken++; resizeObserver?.disconnect(); doc.removeEventListener?.('keydown', onEscape); closeSourceView?.(); controller.abort(); };
  dispose.ready = ready;
  dispose.select = select;
  return dispose;
}
