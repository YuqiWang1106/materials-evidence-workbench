// @ts-check
/** Pure helpers for the entity graph: type labels, a deterministic layered layout, and grouping for the detail panel. */

export const TYPE_ORDER = ['formulation', 'material', 'property', 'method', 'instrument', 'organization'];
export const TYPE_META = {
  formulation: {label: '配方', color: '#155DFC'},
  material: {label: '材料', color: '#4A91F8'},
  property: {label: '性能', color: '#334155'},
  method: {label: '方法', color: '#64748B'},
  instrument: {label: '仪器', color: '#94A3B8'},
  organization: {label: '机构', color: '#525252'},
};
export const SECTION_ORDER = ['文中名称', '目的', '区别', '原料', '制备过程', '性能', '测试方法', '结论', '解释', '说明'];

export const typeLabel = type => TYPE_META[type]?.label || type;
export const typeColor = type => TYPE_META[type]?.color || '#525252';

export function nodeRadius(node) {
  const base = node.type === 'formulation' ? 9 : 6;
  return base + Math.min(10, Math.sqrt(node.degree || 0) * 1.8);
}

/** Column of each type, left to right: what recipes are made of, the recipes, what was measured, how. */
export const GRAPH_COLUMNS = [['material'], ['formulation'], ['property'], ['method', 'instrument']];
export const GRAPH_TYPES = GRAPH_COLUMNS.flat();

/** @typedef {Map<string, {x: number, y: number}> & {width: number, height: number}} GraphPositions */

/** A layered layout: one column per type (long columns split into side-by-side columns), recipes in list order
 * (siblings together), every other node at the average height of the recipes it links to, so links run mostly
 * level and nodes never overlap. Deterministic. The returned Map also carries the drawing's width and height. */
export function layoutGraph(nodes, edges, {width = 860, height = 600, rowGap = 26, maxRows = 26, columnWidth = 210} = {}) {
  const positions = /** @type {GraphPositions} */ (new Map());
  positions.width = width; positions.height = height;
  if (!nodes.length) return positions;
  const columnOf = type => { const at = GRAPH_COLUMNS.findIndex(types => types.includes(type)); return at === -1 ? GRAPH_COLUMNS.length : at; };
  const groups = Array.from({length: GRAPH_COLUMNS.length + 1}, () => []);
  for (const node of nodes) groups[columnOf(node.type)].push(node);
  const linked = new Map(nodes.map(node => [node.entity_id, []]));
  for (const edge of edges) {
    if (linked.has(edge.a) && linked.has(edge.b)) { linked.get(edge.a).push(edge.b); linked.get(edge.b).push(edge.a); }
  }
  const name = (a, b) => a.name.localeCompare(b.name, 'zh-Hans-CN');
  const rank = new Map();
  const recipes = orderFormulations(groups[1]);
  recipes.forEach((node, index) => rank.set(node.entity_id, (index + 0.5) / recipes.length));
  // Two passes: other columns follow the recipes; recipes linked to nothing else keep their list order.
  for (let pass = 0; pass < 2; pass++) {
    groups.forEach((group, column) => {
      if (column === 1) return;
      const place = node => {
        const known = linked.get(node.entity_id).map(id => rank.get(id)).filter(value => value !== undefined);
        return known.length ? known.reduce((sum, value) => sum + value, 0) / known.length : 2;
      };
      group.sort((a, b) => place(a) - place(b) || (b.degree || 0) - (a.degree || 0) || name(a, b));
      group.forEach((node, index) => rank.set(node.entity_id, place(node) < 2 ? place(node) : 1 + index / 1000));
    });
  }
  groups[1] = recipes;
  const columns = [];
  for (const group of groups) {
    for (let start = 0; start < group.length; start += maxRows) columns.push(group.slice(start, start + maxRows));
  }
  const used = columns.filter(column => column.length);
  // Rows are at least one node apart: a recipe with many links is drawn larger than a material.
  const gaps = used.map(column => Math.max(rowGap, 6 + 2 * Math.max(...column.map(nodeRadius))));
  const tallest = Math.max(...used.map((column, index) => column.length * gaps[index]));
  positions.width = Math.max(width, 48 + used.length * columnWidth);
  positions.height = Math.max(height, 48 + tallest);
  const left = (positions.width - used.length * columnWidth) / 2;
  used.forEach((column, index) => {
    const top = (positions.height - (column.length - 1) * gaps[index]) / 2;
    column.forEach((node, row) => positions.set(node.entity_id, {x: left + 24 + index * columnWidth, y: top + row * gaps[index]}));
  });
  return positions;
}

/** A level S-curve between two nodes; a link inside one column bows out to the right. */
export function edgePath(a, b) {
  if (Math.abs(a.x - b.x) < 1) {
    const bow = 30 + Math.min(80, Math.abs(a.y - b.y) / 4);
    return `M ${a.x} ${a.y} C ${a.x + bow} ${a.y}, ${b.x + bow} ${b.y}, ${b.x} ${b.y}`;
  }
  const middle = (a.x + b.x) / 2;
  return `M ${a.x} ${a.y} C ${middle} ${a.y}, ${middle} ${b.y}, ${b.x} ${b.y}`;
}

export function neighbours(edges, entityId) {
  const result = new Set();
  for (const edge of edges) {
    if (edge.a === entityId) result.add(edge.b);
    if (edge.b === entityId) result.add(edge.a);
  }
  return result;
}

export function groupAttributes(attributes = []) {
  const groups = new Map();
  for (const item of attributes) {
    if (!groups.has(item.section)) groups.set(item.section, []);
    groups.get(item.section).push(item);
  }
  const rank = section => { const at = SECTION_ORDER.indexOf(section); return at === -1 ? SECTION_ORDER.length : at; };
  return [...groups.entries()].sort((a, b) => rank(a[0]) - rank(b[0]) || a[0].localeCompare(b[0]))
    .map(([section, items]) => ({section, items}));
}

export function groupRelated(related = []) {
  const groups = new Map();
  for (const item of related) {
    if (!groups.has(item.type)) groups.set(item.type, []);
    groups.get(item.type).push(item);
  }
  const rank = type => { const at = TYPE_ORDER.indexOf(type); return at === -1 ? TYPE_ORDER.length : at; };
  return [...groups.entries()].sort((a, b) => rank(a[0]) - rank(b[0])).map(([type, items]) => ({type, items}));
}

export function countByType(nodes = []) {
  const counts = {};
  for (const node of nodes) counts[node.type] = (counts[node.type] || 0) + 1;
  return TYPE_ORDER.filter(type => counts[type]).map(type => ({type, count: counts[type]}));
}

export function filterGraph(graph, {types = null, query = ''} = {}) {
  const needle = query.trim().toLowerCase();
  const nodes = (graph?.nodes || []).filter(node => (!types || types.has(node.type)));
  const kept = new Set(nodes.map(node => node.entity_id));
  const edges = (graph?.edges || []).filter(edge => kept.has(edge.a) && kept.has(edge.b));
  const matches = new Set(needle ? nodes.filter(node => [node.name, ...(node.aliases || [])]
    .some(name => String(name).toLowerCase().includes(needle))).map(node => node.entity_id) : []);
  return {nodes, edges, matches};
}

/**
 * A readable page of the selected entity's direct neighbours. Type filters apply to neighbours only.
 * @param {{nodes?: any[], edges?: any[]}} graph
 * @param {{entityId?: string, types?: Set<string>, page?: number, pageSize?: number}} [options]
 */
export function focusGraph(graph, {entityId, types = null, page = 0, pageSize = 12} = {}) {
  const byId = new Map((graph?.nodes || []).map(item => [item.entity_id, item]));
  const focus = byId.get(entityId);
  if (!focus) return {nodes: [], edges: [], totalNeighbors: 0, page: 0, pageCount: 0};
  const direct = neighbours(graph?.edges || [], entityId);
  direct.delete(entityId);
  const rank = type => { const index = TYPE_ORDER.indexOf(type); return index < 0 ? TYPE_ORDER.length : index; };
  const adjacent = [...direct].map(id => byId.get(id)).filter(item => item && (!types || types.has(item.type)))
    .sort((a, b) => rank(a.type) - rank(b.type)
      || String(a.name || '').localeCompare(String(b.name || ''), 'zh-Hans-CN')
      || String(a.entity_id).localeCompare(String(b.entity_id), 'en'));
  // The focus layout has six slots on each side; a larger page would make the labels overlap again.
  const size = Number.isFinite(pageSize) ? Math.min(12, Math.max(1, Math.floor(pageSize))) : 12;
  const pageCount = Math.ceil(adjacent.length / size);
  const currentPage = Number.isFinite(page) ? Math.max(0, Math.min(Math.max(0, pageCount - 1), Math.floor(page))) : 0;
  const nodes = [focus, ...adjacent.slice(currentPage * size, (currentPage + 1) * size)];
  const shown = new Set(nodes.slice(1).map(item => item.entity_id));
  const edges = (graph?.edges || []).filter(edge =>
    (edge.a === entityId && shown.has(edge.b)) || (edge.b === entityId && shown.has(edge.a)));
  return {nodes, edges, totalNeighbors: adjacent.length, page: currentPage, pageCount};
}

/** Place a focus and its page of at most twelve neighbours in a stable, spacious two-sided layout. */
export function layoutFocusGraph(nodes, entityId, {width = 860, height = 600} = {}) {
  const positions = /** @type {GraphPositions} */ (new Map());
  positions.width = width;
  positions.height = height;
  if (!nodes.some(item => item.entity_id === entityId)) return positions;
  positions.set(entityId, {x: width / 2, y: height / 2});
  const adjacent = nodes.filter(item => item.entity_id !== entityId);
  const split = Math.ceil(adjacent.length / 2);
  [adjacent.slice(0, split), adjacent.slice(split)].forEach((side, index) => {
    side.forEach((item, row) => positions.set(item.entity_id, {
      x: index === 0 ? 36 : width - 36,
      y: side.length === 1 ? height / 2 : 60 + row * (height - 120) / (side.length - 1),
    }));
  });
  return positions;
}

export function shortLabel(name, limit = 14) {
  const text = String(name || '');
  return text.length > limit ? text.slice(0, limit - 1) + '…' : text;
}


/** The shared name of a formulation without its postfix: 「La@PN-GO阻燃ABS（1.0 wt%）」 -> 「La@PN-GO阻燃ABS」. */
export function baseName(name = '') {
  return String(name).replace(/[（(][^（）()]*[）)]\s*$/, '').trim();
}

function postfixNumber(name = '') {
  const match = String(name).match(/[（(]([^（）()]*)[）)]\s*$/);
  const number = match && match[1].match(/\d+(?:\.\d+)?/);
  return number ? Number(number[0]) : Number.POSITIVE_INFINITY;
}

function formulationRank(name = '') {
  if (/组合物范围/.test(name)) return 0;
  if (/对照|空白/.test(name)) return 2;
  return 1;
}

/**
 * Formulations in one list: claimed ranges, then samples by shared name with their postfix values in numeric order,
 * then controls. Siblings that differ only in an amount stay next to each other, also across papers.
 */
export function orderFormulations(nodes = []) {
  const compare = (a, b) => formulationRank(a.name) - formulationRank(b.name)
    || baseName(a.name).localeCompare(baseName(b.name), 'zh-Hans-CN')
    || postfixNumber(a.name) - postfixNumber(b.name)
    || a.name.localeCompare(b.name, 'zh-Hans-CN');
  return nodes.filter(item => item.type === 'formulation').sort(compare);
}

/** An entity as one paper shows it: only the links and attributes that this paper supports (each location and
 * attribute says which paper it comes from). A material shared by several papers lists only this paper's recipes. */
export function onlySource(entity, sourceId) {
  if (!entity || !sourceId) return entity;
  // Something that names no paper is kept; only what clearly comes from another paper is left out.
  const here = item => !item.source_id || item.source_id === sourceId || (item.citations || []).some(other => other.source_id === sourceId);
  const related = [];
  for (const item of entity.related || []) {
    const locations = (item.locations || []).filter(place => !place.source_id || place.source_id === sourceId);
    if (locations.length) related.push({...item, locations});
  }
  return {...entity, attributes: (entity.attributes || []).filter(here), related};
}
