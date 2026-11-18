import {pageTitle} from './page-title.js';
import {KIND_LABELS, FIELD_LABELS, valueText, rowLabel, titleForResult, saveDecision,
  saveReviewedResult, curveSegments, previewRequest, readerOptionsForLocation, pagination,
  groupNotices, coverageText, errorText, headerGroups, headerLocation, typedValueInfo, bindingChoices, bindingLocation, relationText, objectLabel, structuralContext, structuralLinkText, structuralReasonText} from './result-logic.js';

const STAGES = {queued:'等待整理', reading:'读取原件', extracting:'整理所选内容', validating:'核对来源与归属',
  needs_review:'等待你核对', publishing:'正在保存', write_unknown:'保存状态待核对', saved:'已归档',
  failed:'整理未完成', cancelled:'已取消', superseded:'已有更新版本'};
const NS = 'http://www.w3.org/2000/svg';
const idPath = value => encodeURIComponent(value);
const number = value => Number.isFinite(value) ? value.toLocaleString('zh-CN') : '—';
const columnName = column => {
  let value = column, text = '';
  while (value > 0) {value--; text = String.fromCharCode(65 + value % 26) + text; value = Math.floor(value / 26);}
  return text;
};

/** Mount a bounded result projection. The returned cleanup invalidates in-flight requests. */
export function renderResult({root, task, api, onEdit, onRefresh, capabilities}) {
  const doc = root.ownerDocument;
  const controller = new AbortController();
  let disposed = false, loadVersion = 0, evidenceVersion = 0, busy = false;
  let currentTask = task, view = null, currentOffset = 0, catalogue = {...FIELD_LABELS};
  let returnFocus = null, documentEntries = null, documentOffset = 0;
  const node = (tag, className, text) => {
    const element = doc.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  };
  const button = (text, handler, className = 'button secondary') => {
    const element = node('button', className, text); element.type = 'button';
    element.addEventListener('click', handler); return element;
  };
  const get = async (path, options = {}) => {
    if (disposed) throw new DOMException('View closed', 'AbortError');
    const result = await api(path, {...options, signal: controller.signal});
    if (disposed) throw new DOMException('View closed', 'AbortError');
    return result;
  };
  const taskPath = () => `/api/tasks/${idPath(currentTask.task_id)}`;
  const wrapper = node('section', 'result-workspace');
  const heading = node('div', 'section-heading');
  const title = node('h2', '', titleForResult(task));
  const status = node('span', 'status-pill', STAGES[task.state] || '状态待确认');
  heading.append(pageTitle(title,'资料整理'), status);
  const message = node('p', 'message'); message.hidden = true; message.setAttribute('role', 'status');
  const toolbar = node('div', 'result-toolbar');
  const stats = node('div', 'result-stats');
  const body = node('div', 'result-layout');
  const main = node('div', 'result-main');
  const sourcePanel = node('aside', 'result-source-panel panel');
  sourcePanel.hidden = true; sourcePanel.tabIndex = -1;
  sourcePanel.setAttribute('aria-label', '原件与来源');
  sourcePanel.append(node('h3', '', '原件与来源'), node('p', 'muted', '点击结果中的数值或曲线，查看对应原格。'));
  body.append(main, sourcePanel); wrapper.append(heading, message, toolbar, stats, body); root.replaceChildren(wrapper);

  function closeSource() {
    evidenceVersion++; sourcePanel.hidden = true;
    if (returnFocus?.isConnected !== false) returnFocus?.focus?.();
  }
  sourcePanel.addEventListener('keydown', event => {if (event.key === 'Escape') {event.preventDefault(); closeSource();}});
  function sourceHeading(label) {
    const head = node('div', 'section-heading');
    head.append(node('h3', '', label), button('关闭来源', closeSource, 'button quiet'));
    return head;
  }

  function tell(text, tone = '') {
    if (disposed) return;
    message.textContent = text; message.className = `message ${tone}`.trim(); message.hidden = !text;
  }

  function drawToolbar() {
    if (disposed) return;
    toolbar.replaceChildren();
    const decision = saveDecision(currentTask, view, capabilities);
    if (decision.action) {
      const save = button(busy ? '正在核对…' : decision.label, saveCurrent, 'button primary');
      save.disabled = busy; toolbar.append(save);
    }
    if (onEdit && !currentTask.read_only && currentTask.source_type === 'experimental') {
      const edit = button('修改读法', () => onEdit(currentTask)); edit.disabled = busy; toolbar.append(edit);
    }
    const refresh = button('刷新状态', () => reload()); refresh.disabled = busy; toolbar.append(refresh);
    if (view) {
      const exportButton = button('导出完整记录', exportPackage, 'button quiet');
      exportButton.disabled = busy; toolbar.append(exportButton);
      if (['paper','patent'].includes(currentTask.source_type)) {
        const textButton = button('查看原文片段', loadDocumentIndex, 'button quiet');
        textButton.disabled = busy; toolbar.append(textButton);
      }
      const available = (view.assets || []).filter(a => currentTask.asset_ids?.includes(a.asset_id));
      if (available.length) {
        const select = node('select'); select.setAttribute('aria-label', '选择要下载的原件');
        for (const asset of available) {
          const option = node('option', '', asset.original_name || '原始文件'); option.value = asset.asset_id; select.append(option);
        }
        const download = node('a', 'button quiet', '下载原件'); download.download = '';
        const choose = () => {download.href = `/api/assets/${idPath(select.value)}/download`;};
        select.addEventListener('change', choose); choose(); toolbar.append(select, download);
      }
    }
    toolbar.append(node('p', 'help', decision.reason));
  }

  function drawStats() {
    stats.replaceChildren();
    if (!view) return;
    const summary = view.summary || {};
    const outcome = summary.outcome === 'complete' ? '所选内容已整理' : summary.outcome === 'partial' ? '部分内容已整理' : '完整性未确定';
    status.textContent = `${STAGES[currentTask.state] || '状态待确认'} · ${outcome}`;
    const values = [['可用数据项', summary.usable_values], ...Object.entries(summary.object_counts || {})
      .filter(([kind]) => kind !== 'source').map(([kind, count]) => [KIND_LABELS[kind] || '其他资料', count]),
      ['待确定事项', summary.unresolved_items], ['待明确内容', summary.coverage_gaps]];
    for (const [label, count] of values) {
      if (!Number.isFinite(count)) continue;
      const card = node('div', 'result-stat'); card.append(node('strong', '', number(count)), node('span', '', label)); stats.append(card);
    }
  }

  function curveFigure(row) {
    const preview = row.series_preview;
    const figure = node('div', 'result-curve');
    const segments = curveSegments(preview);
    const points = segments.flat();
    if (points.length) {
      const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
      const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
      const x = value => maxX === minX ? 160 : 28 + (value - minX) / (maxX - minX) * 264;
      const y = value => maxY === minY ? 62 : 98 - (value - minY) / (maxY - minY) * 76;
      const svg = doc.createElementNS(NS, 'svg'); svg.setAttribute('viewBox', '0 0 320 132');
      svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', `${rowLabel(row, catalogue)}曲线预览，缺失处断开`);
      const description = doc.createElementNS(NS, 'title'); description.textContent = '按来源顺序连接已知数值，缺失处不连线'; svg.append(description);
      for (const segment of segments) {
        const line = doc.createElementNS(NS, segment.length === 1 ? 'circle' : 'polyline');
        if (segment.length === 1) {
          line.setAttribute('cx', x(segment[0][0])); line.setAttribute('cy', y(segment[0][1]));
          line.setAttribute('r', '2'); line.setAttribute('fill', 'currentColor');
        } else {
          line.setAttribute('points', segment.map(p => `${x(p[0])},${y(p[1])}`).join(' '));
          line.setAttribute('fill', 'none'); line.setAttribute('stroke', 'currentColor'); line.setAttribute('stroke-width', '1.5');
        }
        svg.append(line);
      }
      for (const [text, left, top, anchor] of [[number(minX),28,118,'start'], [number(maxX),292,118,'end'],
        [number(minY),25,100,'end'], [number(maxY),25,24,'end']]) {
        const label = doc.createElementNS(NS, 'text'); label.textContent = text;
        label.setAttribute('x', left); label.setAttribute('y', top); label.setAttribute('font-size', '9'); label.setAttribute('text-anchor', anchor); svg.append(label);
      }
      figure.append(svg);
    } else figure.append(node('p', 'muted', '暂无可绘制的数值点，原件仍保留。'));
    const shown = preview.points?.length || 0, total = preview.total_points ?? row.point_count;
    figure.append(node('p', 'help', `${preview.x_label || '横轴'}${preview.x_unit ? ` (${preview.x_unit})` : '（单位未确定）'} · ${rowLabel(row, catalogue)}${row.unit ? ` (${row.unit})` : '（单位未确定）'}`),
      node('p', 'help', preview.sampled ? `预览 ${number(shown)} / ${number(total)} 点；保存保留完整数据，缺失处断开。` : `共 ${number(total)} 点；缺失处断开。`));
    if (preview.x_is_point_index) figure.append(node('p','help','横轴为原始点序号，不表示时间或另一项测量。'));
    if (preview.unplotted_points) figure.append(node('p','help',`${number(preview.unplotted_points)} 个点未绘制（缺失、范围、误差、公式缓存或其他不可直接绘制的值）；原值仍保留。`));
    return figure;
  }

  function drawRows() {
    const section = node('section', 'panel'); section.append(node('h3', '', '整理结果'));
    section.append(node('p', 'help', '原值只读。点击数值查看来源；读法确认与原格一致不代表科学认证。'));
    if (!view.rows?.length) section.append(node('p', 'muted', ['paper','patent'].includes(currentTask.source_type)
      ? '当前没有可展示的结果值。可查看原文片段、原件状态或下载原件。' : '当前没有可展示的结果值。请查看未确定事项或返回修改读法。'));
    else {
      const wrap = node('div', 'table-wrap');
      const table = node('table'); const head = node('thead'), header = node('tr');
      for (const label of [view.relationship_view ? '记录 / 归属' : '样品 / 测试归属','字段','原值 / 曲线','单位','状态']) {const th = node('th', '', label); th.scope = 'col'; header.append(th);}
      head.append(header); table.append(head); const rows = node('tbody');
      for (const row of view.rows) {
        const tr = node('tr'); const owner = node('td');
        const contexts = Array.isArray(row.contexts) ? row.contexts.slice(0, 8) : null;
        const object = view.relationship_view?.objects?.find(item => item.object_id === row.subject_ref);
        const structural = structuralContext(view.relationship_view, row.subject_ref);
        if (structural.links.length) {
          owner.append(node('p','',structural.label),node('p','help','结构归属'));
          for (const [endpoint,role] of [[structural.owner,'所属记录'],[structural.material,'所用材料']]) {
            if (!endpoint) continue;
            const identity = button(`${role}：${objectLabel(endpoint)}`, () => openIdentity(endpoint), 'result-value');
            identity.setAttribute('aria-label', `查看${objectLabel(endpoint)}的身份来源`);
            owner.append(identity);
            if (endpoint.identity_source_available === false) owner.append(node('p','help',`${role}未提供可定位的身份来源。`));
          }
          if (object && identityBindings(object).length) {
            const identity = button('组分身份来源', () => openIdentity(object), 'button quiet');
            identity.setAttribute('aria-label', `查看${objectLabel(object)}的身份来源`); owner.append(identity);
          }
          if (!structural.usable) owner.append(node('p','help',`归属待核对：${structural.reasons.join('；')}`));
          if (structural.links.some(link=>link.source_evidence_available === false)) owner.append(node('p','help','结构引用没有可展示的来源证据。'));
        } else if (object) {
          const identity = button(objectLabel(object), () => openIdentity(object), 'result-value');
          identity.setAttribute('aria-label', `查看${objectLabel(object)}的身份来源`);
          owner.append(identity,node('p','help',KIND_LABELS[object.kind] || '资料记录'));
          if (object.identity_status === 'local_only') owner.append(node('p','help','仅在本条记录内区分，未声明跨记录身份。'));
          if (object.usable === false) owner.append(node('p','help','身份或归属待核对。'));
        } else if (contexts?.length) {
          for (const context of contexts) {
            const group = node('div', 'result-context');
            for (const [key, label] of [['sample','样品'], ['test','测试'], ['run','仪器记录'], ['series','曲线']]) {
              if (context[`${key}_label`] != null) group.append(node('p', key === 'sample' ? '' : 'help',
                `${label}：${valueText(context[`${key}_label`], 160)}`));
            }
            if (context.sample_label == null) group.append(node('p', 'help', context.test_label != null || context.run_label != null
              ? '样品归属未确认' : '样品与测试归属未确认'));
            owner.append(group);
          }
          if (row.contexts_truncated || row.context_total > contexts.length) owner.append(node('p', 'help',
            `展示 ${number(contexts.length)} / ${number(row.context_total)} 个关联；其余可在完整记录中核对。`));
          else if (contexts.length > 1) owner.append(node('p', 'help', '来源列有多个关联，未选定单一归属。'));
        } else if (contexts && ['observation','series','reported_test','instrument_run','sample'].includes(row.kind)) {
          owner.append(node('p', '', '样品与测试归属未确认'));
        } else {
          const subject = row.subject === currentTask.source_id || row.subject === currentTask.task_id ? titleForResult(currentTask, view) : row.subject;
          owner.append(node('span', '', valueText(subject, 100)), node('p', 'help', KIND_LABELS[row.kind] || '资料记录'));
        }
        const valueCell = node('td');
        const inspect = button(row.series_preview ? '查看曲线原格' : valueText(row.value),
          () => row.source_bindings?.value
            ? openBindings(row.evidence_refs, row.source_bindings.value, rowLabel(row,catalogue), row.source_binding_counts?.value)
            : openEvidence(row.evidence_refs, rowLabel(row, catalogue), {series: Boolean(row.series_preview)}), 'result-value');
        inspect.disabled = !row.evidence_refs?.length;
        inspect.setAttribute('aria-label', `查看${rowLabel(row, catalogue)}的来源`);
        valueCell.append(inspect);
        const typed = typedValueInfo(row.typed_value);
        if (typed) {
          valueCell.append(node('span','result-type-badge',typed.label));
          if (typed.note) valueCell.append(node('p','help',typed.note));
        }
        if (row.series_preview) {
          valueCell.append(curveFigure(row));
          if (row.source_bindings?.x_value) valueCell.append(button('横轴来源',()=>openBindings(row.evidence_refs,row.source_bindings.x_value,'横轴',row.source_binding_counts?.x_value),'button quiet'));
          if (row.source_bindings?.x_unit) valueCell.append(button('横轴单位来源',()=>openBindings(row.evidence_refs,row.source_bindings.x_unit,'横轴单位'),'button quiet'));
        }
        if (row.value_count > 20) valueCell.append(node('p', 'help', `仅展示前 20 项，共 ${number(row.value_count)} 项。完整内容可显式导出。`));
        const unit = node('td'), unitText = row.unit === null || row.unit === undefined ? '未确定' : valueText(row.unit, 80);
        if (row.source_bindings) {
          const inspectUnit = button(unitText, () => openBindings(row.evidence_refs,row.source_bindings.unit,'单位',row.source_binding_counts?.unit), 'result-value');
          inspectUnit.setAttribute('aria-label', `查看${rowLabel(row,catalogue)}的单位来源`); unit.append(inspectUnit);
        } else if (headerGroups(row).length) {
          const inspectUnit = button(unitText, () => openHeaderSources(row, 'unit'), 'result-value');
          inspectUnit.setAttribute('aria-label', `查看${rowLabel(row, catalogue)}的单位来源`);
          unit.append(inspectUnit);
        } else unit.textContent = unitText;
        if (row.basis) {
          if (row.source_bindings?.basis) unit.append(button(`基准：${valueText(row.basis,80)}`,
            () => openBindings(row.evidence_refs,row.source_bindings.basis,'用量基准'), 'result-value'));
          else unit.append(node('p', 'help', `基准：${valueText(row.basis, 80)}`));
        }
        const field = node('td'); field.append(node('strong', '', rowLabel(row, catalogue)));
        drawHeaders(row, field);
        const conditions = Object.entries(row.source_bindings || {}).filter(([key]) => key.startsWith('qualifier:'));
        if (conditions.length) {
          const inspect = button('条件与含义来源', () => openBindings(row.evidence_refs,conditions.flatMap(([,value]) => Array.isArray(value) ? value : [value]),'条件与含义'), 'button quiet');
          inspect.setAttribute('aria-label', `查看${rowLabel(row,catalogue)}的条件来源`); field.append(inspect);
        }
        const headers = Object.entries(row.source_bindings || {}).filter(([key]) => key.startsWith('header:'));
        if (headers.length && !headerGroups(row).length) field.append(button('标题来源',
          () => openBindings(row.evidence_refs,headers.flatMap(([,value]) => Array.isArray(value) ? value : [value]),'标题'), 'button quiet'));
        tr.append(owner, field, valueCell, unit,
          node('td', '', structural.links.length && !structural.usable ? '归属待核对' : row.usable ? '可用' : '暂不用于筛选'));
        rows.append(tr);
      }
      table.append(rows); wrap.append(table); section.append(wrap);
    }
    const page = pagination(view), paginationBar = node('div', 'result-pagination');
    const previous = button('上一页', () => loadPage(page.previous)); previous.disabled = page.previous === null;
    const next = button('下一页', () => loadPage(page.next)); next.disabled = page.next === null;
    paginationBar.append(previous, node('span', '', `${number(page.start)}–${number(page.end)} / ${number(page.total)} 条`), next);
    section.append(paginationBar); main.append(section);
  }

  function drawHeaders(row, field) {
    const groups = headerGroups(row);
    if (!groups.length) return;
    const labels = node('div', 'result-context');
    const hasLabels = groups.some(group => group.labels.some(label => label.role !== 'note'));
    if (hasLabels) labels.append(node('p', 'help', '原表标签'));
    const sourceButton = (text, source, label) => {
      const inspect = button(text, () => openEvidence(row.evidence_refs, label, {source}), 'result-value');
      inspect.disabled = !source || !row.evidence_refs?.length;
      return inspect;
    };
    for (const group of groups) {
      for (const label of group.labels.filter(label => label.role !== 'note')) {
        const line = node('p', 'help');
        const title = `${label.name}：${valueText(label.text, 120)}`;
        const inspect = sourceButton(title, label.source, `${label.name}${label.source ? ` ${label.source.address}` : ''}`);
        inspect.title = `${valueText(label.text, 512)}${label.source ? ` · 原格 ${label.source.address}` : ''}`;
        line.append(inspect);
        if (label.unresolved) line.append(node('span', '', `（${label.statusLabel}）`));
        labels.append(line);
      }
    }
    if (hasLabels) field.append(labels);
    field.append(button('标题来源', () => openHeaderSources(row, 'header'), 'button quiet'));
  }

  function openHeaderSources(row, kind) {
    const groups = headerGroups(row), choices = [], notes = [], seen = new Set();
    for (const group of groups) {
      const entries = kind === 'unit' ? group.unitSources.map(source => ({source, text:'单位'}))
        : [...group.labels, ...group.headers];
      if (kind === 'unit') notes.push(`${group.axisLabel}：${group.unitText}${group.unitCandidates.length
        ? `；来源候选 ${group.unitCandidates.slice(0, 8).join('、')}${group.unitCandidates.length > 8 ? '等' : ''}` : ''}`);
      for (const entry of entries) {
        if (!entry.source) continue;
        const key = JSON.stringify(entry.source);
        if (seen.has(key)) continue;
        seen.add(key);
        choices.push({source:entry.source, label:`${group.axisLabel} ${entry.source.address} · ${valueText(entry.text, 100)}${entry.unresolved ? '（待核对）' : ''}`});
      }
    }
    if (groups.some(group => group.blocked)) notes.push('来源含义或单位仍待核对，未选定其中一种。');
    if (row.header_context_truncated || groups.some(group => group.truncated) || choices.length > 64) notes.push('这里展示部分来源，其余保留在完整记录中。');
    return openEvidence(row.evidence_refs, kind === 'unit' ? '单位' : '标题',
      {sourceChoices:choices.slice(0, 64), sourceNotes:notes});
  }

  function openBindings(refs, bindings, label, total = 0) {
    const choices = bindingChoices(bindings), notes = [...new Set(choices.map(choice => choice.note).filter(Boolean))];
    if (total > choices.length) notes.push(`展示 ${choices.length} / ${total} 个来源，其余保留在完整记录中。`);
    return openEvidence(refs,label,{sourceChoices:choices,sourceNotes:notes});
  }

  function identityBindings(object) {
    return (object?.identity_occurrences || []).flatMap(item => item.bindings || []);
  }

  function openIdentity(object) {
    const bindings = identityBindings(object);
    const count = (object.identity_occurrences || []).reduce((sum,item) => sum + (item.binding_total ?? item.bindings?.length ?? 0),0);
    const notes = object.identity_occurrences_truncated ? ['身份来源展示有界；其余在完整记录中。'] : [];
    if (object.identity_source_available === false || !bindings.length) notes.push('未提供可定位的身份来源；已保存名称和引用不等于原格证明。');
    if (object.identity_status === 'local_only') notes.push('记录仅在当前范围内区分，没有声明可跨范围复用的来源身份。');
    return openEvidence(object.evidence_refs,`${objectLabel(object)}身份`,{
      sourceChoices:bindingChoices(bindings),sourceNotes:[...notes,...bindingChoices(bindings).map(item=>item.note).filter(Boolean),
        ...(count > bindings.length ? [`当前展示 ${bindings.length} / ${count} 个身份依据。`] : [])]});
  }

  function drawRelationships() {
    const graph = view.relationship_view;
    if (!graph) return;
    const section = node('section','panel'); section.append(node('h3','','归属与记录关系'),
      node('p','help','这里只展示当前页的已保存引用与明确关系。可用状态不代表独立科学认证。'));
    if (graph.structural_links?.length) {
      section.append(node('h4','','结构归属'),node('p','help','来自已保存的对象引用；身份来源用于分别核对端点名称。'));
      for (const link of graph.structural_links) {
        const line=node('div','result-relation');
        line.append(node('p','',structuralLinkText(link)),node('span',link.usable ? 'result-type-badge' : 'result-type-badge warn',link.usable ? '归属可用' : '归属待核对'));
        if (!link.usable) line.append(node('p','help',(link.unusable_reasons || []).map(structuralReasonText).join('；') || '归属尚不可用，请核对已保存记录。'));
        if (link.source_evidence_available === false) line.append(node('p','help','这条结构引用没有可展示的来源证据。'));
        const endpoint=(graph.objects || []).find(object=>object.object_id===link.to_ref);
        if (endpoint) {
          const inspect=button(`核对${objectLabel(endpoint)}身份来源`,()=>openIdentity(endpoint),'button quiet');
          inspect.setAttribute('aria-label',`查看${objectLabel(endpoint)}的身份来源`); line.append(inspect);
          if (endpoint.identity_source_available === false) line.append(node('p','help','未提供可定位的身份来源。'));
        } else line.append(node('p','help','当前投影未提供端点身份来源，请在完整记录中核对。'));
        section.append(line);
      }
      if (graph.structural_links_truncated) section.append(node('p','help',`当前展示 ${number(graph.structural_links.length)} / ${number(graph.structural_link_total)} 条结构归属，其余保留在完整记录中。`));
    }
    section.append(node('h4','','来源声明的关系'));
    if (!graph.relations?.length) section.append(node('p','muted','当前页没有已声明的记录关系。'));
    for (const relation of graph.relations || []) {
      const line = node('div','result-relation');
      line.append(node('p','',relationText(relation)),node('span',relation.usable ? 'result-type-badge' : 'result-type-badge warn',relation.usable ? '关系可用' : '关系待核对'));
      line.append(button('查看关系依据', () => {
        const endpoints = (graph.objects || []).filter(object => [relation.from_ref,relation.to_ref].includes(object.object_id));
        const proof = relation.proof || {}, own = [...(proof.left_bindings || []),...(proof.right_bindings || [])];
        const sources = own.length ? own : endpoints.flatMap(identityBindings);
        const notes = [proof.kind === 'exact_source_join' ? '两个来源按明确指定的身份键对应；请逐一核对原格。'
          : proof.kind === 'common_record_scope' ? '关系来自同一已确认记录范围；身份原格如下。' : '关系来自已保存的来源声明。'];
        if (!relation.usable) notes.push('当前关系尚不能用于组合筛选。');
        if (proof.left_bindings_truncated || proof.right_bindings_truncated) notes.push('关系依据为有界预览，其余保留在完整记录中。');
        const refs = [...new Set([...(relation.evidence_refs || []),...endpoints.flatMap(object=>object.evidence_refs || [])])];
        const choices = bindingChoices(sources);
        return openEvidence(refs,'关系依据',{sourceChoices:choices,sourceNotes:[...notes,...choices.map(item=>item.note).filter(Boolean)]});
      },'button quiet')); section.append(line);
    }
    if (graph.relations_truncated || graph.objects_truncated) section.append(node('p','help',
      `当前展示 ${number(graph.relations?.length || 0)} / ${number(graph.relation_total)} 条关系，完整记录保留其余内容。`));
    main.append(section);
  }

  function drawNotices() {
    const notices = groupNotices(view);
    const section = node('section', 'panel'); section.dataset.resultNotices = 'true'; section.tabIndex = -1;
    section.append(node('h3', '', '需要核对与原件状态'));
    if (!notices.groups.length) section.append(node('p', 'muted', '当前没有报告需要核对的问题。'));
    for (const group of notices.groups) {
      const line = node('div', `result-issue-group ${group.tone}`);
      line.append(node('strong', '', `${group.label} · ${number(group.items.length)} 处`));
      if (group.tone === 'info') line.append(node('p', 'help', '这是原件报告状态，不代表处理失败。'));
      const select = node('select'); select.setAttribute('aria-label', `选择${group.label}的位置`);
      group.items.forEach((item, index) => {
        const option = node('option', '', `第 ${index + 1} 处${item.raw_value !== undefined ? ` · ${valueText(item.raw_value, 48)}` : ''}`);
        option.value = String(index); select.append(option);
      });
      const inspect = button('查看来源', () => openEvidence(group.items[Number(select.value)]?.evidence_refs, group.label));
      const update = () => {inspect.disabled = !group.items[Number(select.value)]?.evidence_refs?.length;};
      select.addEventListener('change', update); update();
      line.append(select, inspect); section.append(line);
    }
    if (notices.unshownIssues || notices.unshownAssessments) section.append(node('p', 'message warn',
      `以上为有界预览；另有 ${number(notices.unshownIssues)} 条问题、${number(notices.unshownAssessments)} 条原件状态未展示。完整列表需主动导出完整结果。`));
    if (view.summary?.coverage_gaps) {
      const details = view.coverage_gaps_detail || [];
      section.append(node('p', 'help', `${number(view.summary.coverage_gaps)} 项内容仍待明确；包括含义或关系未确认的情况，不等于原格数值丢失。`));
      const grouped = new Map();
      for (const detail of details) {
        const text = coverageText(detail); grouped.set(text, (grouped.get(text) || 0) + 1);
      }
      for (const [text, count] of grouped) section.append(node('p', 'message warn', `${text}${count > 1 ? `（${number(count)} 处）` : ''}`));
      if (!details.length) section.append(node('p', 'help', '详细原因暂未提供，可显式导出完整记录核对。'));
      else if (details.length < view.summary.coverage_gaps) section.append(node('p', 'help', '以上仅列出部分原因，其余保留在完整记录中。'));
    }
    for (const supplement of view.supplements || []) {
      const note = node('div', 'result-issue-group'); note.append(node('strong', '', '独立补充说明'),
        node('p', '', supplement.text || ''), node('p', 'help', `提供者：${supplement.author || '未提供'}；不替代原格测量值。`));
      section.append(note);
    }
    main.append(section);
  }

  function drawHighlights() {
    const groups = groupNotices(view).groups.filter(group => group.tone !== 'info');
    const texts = [...new Set([...groups.map(group => group.label), ...(view.coverage_gaps_detail || []).map(coverageText)])];
    if (!texts.length) return;
    const highlights = node('div', 'message warn');
    for (const text of texts.slice(0, 3)) highlights.append(node('p', '', text));
    if (texts.length > 3) highlights.append(node('p', 'help', `另有 ${number(texts.length - 3)} 类事项，均保留在下方。`));
    highlights.append(button('查看具体待明确内容', () => {
      const target = main.querySelector?.('[data-result-notices]'); target?.scrollIntoView({block:'start'}); target?.focus();
    }, 'button quiet'));
    main.append(highlights);
  }

  function draw() {
    if (disposed) return;
    title.textContent = titleForResult(currentTask, view || {}); status.textContent = STAGES[currentTask.state] || '状态待确认';
    drawToolbar(); drawStats(); main.replaceChildren();
    if (view) {drawHighlights(); drawRows(); drawRelationships(); drawDocumentIndex(); drawNotices(); return;}
    const section = node('section', 'panel');
    section.append(node('h3', '', STAGES[currentTask.state] || '正在载入'));
    if (['queued','reading','extracting','validating'].includes(currentTask.state)) {
      section.append(node('p', 'help', '后台正在处理已确认的内容，完成后再核对并保存。'));
      const stages = node('ol', 'result-stages');
      for (const state of ['queued','reading','extracting','validating','needs_review']) {
        const item = node('li', '', STAGES[state]);
        if (state === currentTask.state) item.setAttribute('aria-current', 'step'); stages.append(item);
      }
      section.append(stages);
    } else section.append(node('p', 'help', '尚无可展示的结果。刷新状态，或返回修改读法后重新整理。'));
    main.append(section);
  }

  async function loadPage(offset) {
    if (disposed || offset === null || busy) return;
    const version = ++loadVersion;
    main.setAttribute('aria-busy', 'true');
    try {
      const next = await get(`${taskPath()}/result-view?offset=${offset}&limit=50`);
      if (disposed || version !== loadVersion) return;
      view = next; currentOffset = next.offset; draw();
    } catch (error) {if (!disposed && version === loadVersion) tell(errorText(error), 'error');}
    finally {if (!disposed && version === loadVersion) main.removeAttribute('aria-busy');}
  }

  async function reload() {
    if (disposed || busy) return;
    const version = ++loadVersion;
    main.setAttribute('aria-busy', 'true');
    try {
      const latest = await get(taskPath());
      if (disposed || version !== loadVersion) return;
      currentTask = latest;
      if (latest.package_sha256 || ['needs_review','saved','publishing','write_unknown'].includes(latest.state)) {
        const next = await get(`${taskPath()}/result-view?offset=${currentOffset}&limit=50`);
        if (disposed || version !== loadVersion) return;
        view = next;
      } else view = null;
      draw();
    } catch (error) {if (!disposed && version === loadVersion) {draw(); tell(errorText(error), 'error');}}
    finally {if (!disposed && version === loadVersion) main.removeAttribute('aria-busy');}
  }

  async function saveCurrent() {
    if (busy || disposed) return;
    const decision = saveDecision(currentTask, view, capabilities);
    if (!decision.action) return;
    let submitted = false;
    busy = true; drawToolbar(); tell(decision.action === 'reconcile' ? '正在核对已有保存状态…' : '正在核对当前版本并保存…');
    try {
      const result = await saveReviewedResult({task:currentTask, view, capabilities, api:get});
      if (disposed) return;
      currentTask = result.task;
      if (result.kind === 'state_changed') {tell('保存状态已变化，请核对更新后的状态。', 'warn'); return;}
      if (result.kind === 'version_changed') {view = result.view; tell('结果已更新，请重新核对后保存。', 'warn'); return;}
      submitted = result.kind === 'submitted';
      if (!disposed) tell('已完成保存状态核对。');
    } catch (error) {if (!disposed) tell(errorText(error, '保存未完成，结果仍保留；请刷新并核对保存状态。'), 'warn');}
    finally {
      busy = false;
      if (!disposed) {
        await reload();
        if (!disposed && submitted && onRefresh) onRefresh(currentTask);
      }
    }
  }

  async function exportPackage() {
    if (busy || disposed) return;
    busy = true; drawToolbar(); tell('正在准备完整结果；大文件可能需要一些时间。');
    try {
      const full = await get(`${taskPath()}/records`, {timeout:600000}); if (disposed) return;
      const blob = new Blob([JSON.stringify(full, null, 2)], {type:'application/json;charset=utf-8'});
      const url = URL.createObjectURL(blob), link = node('a'); link.href = url;
      link.download = `${titleForResult(currentTask, view).replace(/[\\/:*?"<>|]/g, '_').slice(0, 100)}-完整结果.json`;
      doc.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000);
      tell('已导出完整结果，包含完整数据及未确定事项。');
    } catch (error) {if (!disposed) tell(errorText(error, '完整结果导出失败，请重试。'), 'error');}
    finally {busy = false; if (!disposed) drawToolbar();}
  }

  async function loadDocumentIndex() {
    if (busy || disposed) return;
    if (documentEntries !== null) {draw(); return;}
    busy = true; drawToolbar(); tell('正在按需载入原文片段目录…');
    try {
      const full = await get(`${taskPath()}/records`, {timeout:600000}); if (disposed) return;
      const assets = new Map((full.assets || []).map(asset => [asset.asset_id, asset.original_name]));
      documentEntries = (full.evidence || []).filter(item => !['table_transcription','table_cells','table_row'].includes(item.content_kind))
        .map(item => ({evidence_id:item.evidence_id, anchor:item.anchor, asset:{original_name:assets.get(item.asset_ref)}}));
      documentOffset = 0; draw(); tell('原文目录已载入，请选择片段查看。');
    } catch (error) {if (!disposed) tell(errorText(error, '原文目录暂时无法载入，请重试或下载原件。'), 'error');}
    finally {busy = false; if (!disposed) drawToolbar();}
  }

  function drawDocumentIndex() {
    if (documentEntries === null) return;
    const section = node('section', 'panel'); section.append(node('h3', '', '原文片段'));
    const items = documentEntries.slice(documentOffset, documentOffset + 50);
    if (!items.length) section.append(node('p', 'muted', '未找到可展示的文字片段。原件仍可下载查看。'));
    else {
      const select = node('select'); select.setAttribute('aria-label', '选择原文片段');
      items.forEach((item, index) => {
        const option = node('option', '', `片段 ${documentOffset + index + 1} · ${locationLabel(item)}`);
        option.value = String(index); select.append(option);
      });
      select.value = '0';
      section.append(select, button('打开所选片段', () => openEvidence([items[Number(select.value)].evidence_id], '原文片段')));
    }
    const controls = node('div', 'result-pagination');
    const previous = button('前 50 个片段', () => {documentOffset = Math.max(0, documentOffset - 50); draw();});
    const next = button('后 50 个片段', () => {documentOffset += 50; draw();});
    previous.disabled = documentOffset === 0; next.disabled = documentOffset + 50 >= documentEntries.length;
    controls.append(previous, node('span', '', `共 ${number(documentEntries.length)} 个片段`), next); section.append(controls); main.append(section);
  }

  function locationLabel(location) {
    const anchor = location.anchor || {}, pieces = [];
    if (location.asset?.original_name) pieces.push(location.asset.original_name);
    if (anchor.sheet) pieces.push(anchor.sheet);
    if (anchor.row_range) pieces.push(`第 ${anchor.row_range.join('–')} 行`);
    if (anchor.columns?.length) pieces.push(`${anchor.columns.slice(0, 6).map(columnName).join('、')} 列${anchor.columns.length > 6 ? '等' : ''}`);
    if (anchor.page || anchor.page_numbers) pieces.push(`第 ${anchor.page || anchor.page_numbers.join('、')} 页`);
    if (anchor.metadata_key === 'sheet_name') pieces.push('工作表名称');
    return pieces.join(' · ') || '原文位置';
  }

  async function openEvidence(refs = [], label = '来源', {series = false, source = null, sourceChoices = null, sourceNotes = []} = {}) {
    const version = ++evidenceVersion;
    returnFocus = doc.activeElement; sourcePanel.hidden = false;
    sourcePanel.replaceChildren(sourceHeading(`${label}的来源`), node('p', 'muted', '正在读取原件位置…'));
    sourcePanel.focus?.();
    if (sourceChoices && !sourceChoices.some(choice => choice.source)) {
      sourcePanel.replaceChildren(sourceHeading(`${label}的来源`), ...sourceNotes.map(text => node('p', 'help', text)),
        node('p', 'help', '未提供可定位的原格。')); return;
    }
    if (!refs?.length) {sourcePanel.append(node('p', 'help', '来源位置未提供。')); return;}
    try {
      const results = await Promise.allSettled(refs.slice(0, 12).map(ref => get(`${taskPath()}/evidence/${idPath(ref)}/location`)));
      if (disposed || version !== evidenceVersion) return;
      const locations = results.filter(result => result.status === 'fulfilled').map(result => result.value);
      if (!locations.length) throw results.find(result => result.status === 'rejected')?.reason || new Error('Source unavailable');
      if (sourceChoices) {
        sourcePanel.replaceChildren(sourceHeading(`${label}的来源`), ...sourceNotes.map(text => node('p', 'help', text)));
        const choices = sourceChoices.map(choice => ({...choice, location:bindingLocation(choice.source, locations)}));
        const content = node('div');
        const showChoice = choice => {
          if (choice.location) return showLocation(choice.location, content, version);
          content._locationToken = Symbol();
          content.replaceChildren(node('p', choice.source ? 'message warn' : 'help', choice.source
            ? '无法核对该原格的文件与读取设置，请下载原件查看。' : choice.note || '此项没有可定位的原格。'));
        };
        if (choices.length > 1) {
          const select = node('select'); select.setAttribute('aria-label', `选择${label}来源`);
          choices.forEach((choice, index) => {const option = node('option', '', choice.label); option.value = String(index); select.append(option);});
          select.value = '0';
          select.addEventListener('change', () => showChoice(choices[Number(select.value)]));
          sourcePanel.append(select);
        } else sourcePanel.append(node('p', 'help', choices[0].label));
        sourcePanel.append(content); await showChoice(choices[0]); return;
      }
      if (source) {
        const selected = headerLocation(source, locations);
        sourcePanel.replaceChildren(sourceHeading(`${label}的来源`));
        if (!selected) {
          sourcePanel.append(node('p', 'message warn', '无法核对该原格的文件与读取设置，请下载原件查看。')); return;
        }
        const content = node('div'); sourcePanel.append(content);
        await showLocation(selected, content, version); return;
      }
      const tableLocations = locations.filter(item => item.content_kind === 'table_cells' && previewRequest(item));
      const chosen = series ? tableLocations.find(item => item.anchor.row_range[1] > item.anchor.row_range[0])
        : tableLocations.find(item => item.anchor.row_range[0] === item.anchor.row_range[1] && item.anchor.columns.length === 1);
      let selected = chosen || locations.find(item => item.content_kind !== 'table_transcription') || locations[0];
      sourcePanel.replaceChildren(sourceHeading(`${label}的来源`));
      const select = node('select'); select.setAttribute('aria-label', '选择来源位置');
      locations.forEach((item, index) => {const option = node('option', '', locationLabel(item)); option.value = String(index); select.append(option);});
      select.value = String(locations.indexOf(selected)); sourcePanel.append(select);
      if (refs.length > 12) sourcePanel.append(node('p', 'help', '当前展示前 12 个来源位置，其余见完整导出。'));
      if (results.some(result => result.status === 'rejected')) sourcePanel.append(node('p', 'message warn', '部分来源位置暂时无法读取，可稍后重试。'));
      const content = node('div'); sourcePanel.append(content);
      select.addEventListener('change', () => {selected = locations[Number(select.value)]; showLocation(selected, content, version, series);});
      await showLocation(selected, content, version, series);
    } catch (error) {if (!disposed && version === evidenceVersion) sourcePanel.replaceChildren(sourceHeading(`${label}的来源`), node('p', 'message error', errorText(error)));}
  }

  async function showLocation(location, content, evidenceToken, series = false) {
    const locationToken = Symbol(); content._locationToken = locationToken;
    const active = () => !disposed && evidenceToken === evidenceVersion && content._locationToken === locationToken;
    content.replaceChildren(node('p', 'help', locationLabel(location)));
    const registered = location.registered_asset_id;
    if (registered) {
      const download = node('a', 'button quiet', '下载此原件'); download.href = `/api/assets/${idPath(registered)}/download`; download.download = ''; content.append(download);
    }
    const options = readerOptionsForLocation(location);
    if (location.anchor?.format === 'csv' && options === null) {
      content.append(node('p', 'message warn', '该历史结果没有可用的已确认读取设置，无法可靠预览原格。请下载原件查看。')); return;
    }
    // Curve table_cells anchors declare [x, y, ...extra] in source order. A
    // distant y column must be visible initially instead of an unrelated A:L.
    const focusColumn = series && location.content_kind === 'table_cells' && location.anchor?.row_range?.[1] > location.anchor?.row_range?.[0]
      ? location.anchor.columns?.[1] : null;
    const request = previewRequest(location, options || {}, focusColumn);
    if (request && registered) {
      const grid = node('div'); content.append(grid);
      await loadGrid(request); return;
      async function loadGrid(bounds) {
        const gridToken = Symbol(); grid._requestToken = gridToken;
        grid.replaceChildren(node('p', 'muted', '正在读取有界原格…'));
        try {
          const preview = await get(`/api/assets/${idPath(registered)}/table-preview`, {method:'POST', json:bounds});
          if (!active() || grid._requestToken !== gridToken) return;
          grid.replaceChildren();
          const wrap = node('div', 'table-wrap'), table = node('table'), head = node('thead'), header = node('tr');
          header.append(node('th', '', '原行'));
          for (let column = bounds.column_start; column <= (preview.bounds?.column_end ?? bounds.column_end); column++) header.append(node('th', '', columnName(column)));
          head.append(header); table.append(head); const tbody = node('tbody');
          for (const row of preview.rows || []) {
            const tr = node('tr'), rowHead = node('th', '', row.row); rowHead.scope = 'row'; tr.append(rowHead);
            for (const cell of row.cells || []) {
              const td = node('td', '', cell.raw_value === null ? '（空白）' : cell.raw_value === '' ? '（空字符串）' : valueText(cell.raw_value, 1000));
              td.title = `${cell.address || `${columnName(cell.column)}${cell.row}`} · ${valueText(cell.raw_value, 2000)}`;
              if (row.row >= location.anchor.row_range[0] && row.row <= location.anchor.row_range[1] && location.anchor.columns.includes(cell.column)) td.className = 'result-cell-focus';
              tr.append(td);
            }
            tbody.append(tr);
          }
          table.append(tbody); wrap.append(table); grid.append(wrap);
          const actual = preview.bounds || bounds;
          grid.append(node('p', 'help', `原件第 ${actual.row_start}–${actual.row_end} 行，${columnName(actual.column_start)}–${columnName(actual.column_end)} 列。原值只读，合并空白不自动填充。`));
          const controls = node('div', 'result-pagination');
          for (const [label, axis, delta, max] of [['上 20 行','row',-20,preview.nrows],['下 20 行','row',20,preview.nrows],
            ['左 12 列','column',-12,preview.ncols],['右 12 列','column',12,preview.ncols]]) {
            const startKey = `${axis}_start`, endKey = `${axis}_end`, size = axis === 'row' ? 20 : 12;
            const start = delta < 0 ? Math.max(1, actual[startKey] + delta) : actual[endKey] + 1;
            const move = button(label, () => loadGrid({...bounds, [startKey]:start, [endKey]:Math.min(max, start + size - 1)}), 'button quiet');
            move.disabled = !Number.isFinite(max) || start > max || (delta < 0 && actual[startKey] === 1); controls.append(move);
          }
          grid.append(controls);
        } catch (error) {if (active() && grid._requestToken === gridToken) grid.replaceChildren(node('p', 'message error', errorText(error)));}
      }
    }
    if (['table_transcription','table_cells','table_row'].includes(location.content_kind)) {
      content.append(node('p', 'message warn', '此记录缺少可定位的原格范围。请下载原件查看。')); return;
    }
    if (location.content_kind === 'table_sheet_metadata') {
      content.append(node('p', '', `原始工作表名称：${location.anchor?.sheet || '未提供'}`)); return;
    }
    try {
      const evidence = await get(`${taskPath()}/evidence/${idPath(location.evidence_id)}`);
      if (!active()) return;
      const source = evidence.content;
      const text = typeof source === 'string' ? source : source && typeof source === 'object'
        ? ['text','quote','raw_text','caption','description','statement'].map(key => source[key]).filter(value => typeof value === 'string').join('\n\n') : '';
      content.append(node('p', 'result-source-text', text || '此片段没有可展示的文字，请下载原件查看。'));
    } catch (error) {if (active()) content.append(node('p', 'message error', errorText(error)));}
  }

  draw(); reload();
  get('/api/table-catalogue').then(data => {
    if (disposed) return;
    for (const field of data.fields || []) if (field.field_id && field.label) catalogue[field.field_id] = field.label;
  }).catch(() => {}); // Built-in common labels remain available if the optional catalogue fails.
  return () => {disposed = true; loadVersion++; evidenceVersion++; controller.abort();};
}
