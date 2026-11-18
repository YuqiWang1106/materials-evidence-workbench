// @ts-check
import {questionSuggestions} from './browse-ui.js';
import { el, button } from "./dom.js";
import { mountResearchSummary } from "./research-summary.js";
import { renderChatMarkdown } from "./table-chat-markdown.js";
import { renderWikiSupport, researchSourceRole } from "./research-chat-wiki.js";

const ACTIVE = new Set(["queued", "running"]);
const STATUS = {
  queued: "等待研究",
  running: "正在研究",
  completed: "已完成",
  failed: "本轮未完成",
  cancelled: "已取消",
  interrupted: "执行已中断，未自动重发",
};
const ERROR = {
  not_ready: "研究服务尚未就绪，请分别检查模型和资料状态。",
  model_not_configured: "尚未配置模型服务，请填写本实例的私有模型配置；保存后正常重启网站，再刷新状态。",
  api_key_missing: "模型 API key 尚未填写，请在本实例私有配置中补齐；保存后正常重启网站，再刷新状态。",
  model_config_invalid: "模型配置格式无效，请检查私有配置中的字段、服务地址和凭据格式；保存后正常重启网站，再刷新状态。",
  unsupported_protocol: "模型服务协议不受支持，请检查 Responses 或 Chat Completions 配置；保存后正常重启网站，再刷新状态。",
  unsupported_model_protocol: "模型服务协议不受支持，请修改私有配置；保存后正常重启网站，再刷新状态。",
  unsupported_capability: "模型接口不支持本次所需能力，请核对输入、工具和结构化输出能力；修改配置后正常重启，再刷新状态。",
  unsupported_model_capability: "模型接口不支持本次所需能力，请核对输入、工具和结构化输出能力；修改配置后正常重启，再刷新状态。",
  provider_authentication: "模型服务鉴权失败，请核对私有配置中的 API key；修改后正常重启网站，再刷新状态。",
  provider_permission: "模型服务拒绝访问，请核对账户和模型权限；本轮未自动重发。",
  provider_model_unavailable: "模型不存在或当前账户无法访问，请核对服务地址及模型 ID；修改配置后正常重启，再刷新状态。",
  provider_rate_limit: "模型服务限流，本轮未自动重发；请稍后再试。",
  provider_rate_limited: "模型服务限流，本轮未自动重发；请稍后再试。",
  provider_server_error: "模型服务返回服务端错误，本轮未自动重发。",
  provider_timeout: "模型请求超时，结果可能未知；请查询本轮状态，不会自动重发。",
  provider_request_rejected: "模型服务拒绝本次参数或能力，请核对配置；本轮未自动重发。",
  provider_refusal: "模型拒绝生成本次结果，尚未保存研究答案。",
  request_unknown: "请求已发送但结果未知，已保留请求记录；刷新或重启不会自动重发。",
  request_outcome_unknown: "请求已发送但结果未知，已保留请求记录；刷新或重启不会自动重发。",
  parser_not_ready: "本地 Standard 解析环境未就绪；已有可读取资料仍可用于研究。",
  no_sources: "暂无可用研究资料，请先上传并解析资料，或查询已有 Wiki。",
  sources_not_ready: "所选文献尚未解析完成或暂无可读取内容，请完成解析后刷新并重新选择资料。",
  index_not_ready: "研究索引尚未就绪，请完成资料解析后刷新来源。",
  legacy_identity_mismatch: "旧研究配置与当前工作区身份不一致，请保留原身份并核对配置。",
  stale_context:
    "资料或会话版本已变化。请刷新查看；来源失效时，请重新选择资料新建研究对话。",
  budget_denied: "这是旧版本保存的调用拒绝记录，历史状态保持不变；本轮未自动重发。",
  answer_validation_failed:
    "回答的数值或引用未通过校验；已保留读取记录，未自动修补。",
  context_limit: "本轮内容超过上限，请缩小问题或新建研究对话。",
  provider_failure: "模型请求未完成，执行结果可能未知；未自动重发。",
  provider_protocol: "模型响应格式不符合所需协议；本轮未自动重发。",
  timeout: "本轮研究查询超时；已保存读取结果，本轮未自动重发模型请求。",
  invalid_request: "研究请求格式无效，请检查选择与输入。",
  query_limit: "检索问题超过输入限制，请缩小问题；本次未截断问题。",
  cancelled: "本轮已取消；已保存的读取结果仍保留。",
  interrupted: "服务中断，已保留本轮记录；不会自动重发。",
  execution_failed: "研究执行失败，已保存读取结果；本轮未自动重发。",
  tool_limit: "本轮读取次数已达上限，请缩小问题后继续。",
  unsupported: "当前研究工具不支持此操作。",
  table_selection_required: "本轮未选择该保存表版本，不能进行新计算；请重新选表后提交新消息。",
  wiki_page_limit: "本轮已读取 8 个 Wiki 版本，请在下一轮继续。",
  wiki_relation_limit: "Wiki 关联导航已到两跳上限，不能继续沿此路径展开。",
  query_selection_required: "尚未选择已读取候选或完整查询记录，无法生成筛选结果。",
  query_receipt_invalid: "查询记录无法在当前范围内验证，请重新查询并读取候选。",
  query_receipt_mixed: "查询记录来自不同查询或资料范围，不能合并。",
  query_receipt_incomplete: "查询记录尚未覆盖完整分页，本次未生成筛选结果。",
  query_receipt_duplicate: "查询页或上下文重复，本次未生成筛选结果。",
  query_receipt_empty: "本次精确查询没有匹配候选，不代表所有配方均不满足目标。",
  query_receipt_limit: "查询候选超过本次处理上限，请缩小范围或分批查询。",
  query_receipt_unread: "查询中的部分依据尚未完整读取，请重新读取对应查询页。",
  finalization_protocol: "模型在最终答复阶段仍请求工具；本次未执行该工具，本轮未自动续跑。",
  invalid_query_selection: "模型选择的查询范围无效，本次未执行查询或扩大范围。",
  model_turn_limit: "本轮已达到 8 次模型请求上限；读取结果已保存，未自动继续。",
  target_unknown_profile: "模型选择的目标字段不受支持；尚未登记目标或推断结论。",
  target_quote_not_found: "目标引用未匹配本轮用户原文，尚未登记目标。",
  target_quote_ambiguous: "目标原文出现多次，暂时无法唯一确定对应条件。",
  target_quote_overlap: "目标引用片段互相重叠，尚未登记目标。",
  target_clause_unsupported: "当前目标解析方式不支持该原文条件；尚未改写目标或补充数值。",
  task_record_unavailable: "保存的程序记录无法读取或校验，请重新读取相关依据。",
  task_record_limit: "程序记录超过本次读取上限，请减少候选或约束后重试。",
  target_clarification: "无法从原文直接确认目标，请明确字段、比较条件、数值、单位和配方基准。",
};
const id = () => globalThis.crypto.randomUUID().replaceAll("-", "");
const nameOf = (s) =>
  s?.label ||
  ((s?.assets || []).filter(a => /[.]pdf$/i.test(a.name || '')).length > 1 ? `资料组（${s.assets.filter(a => /[.]pdf$/i.test(a.name || '')).length} 份文献）` : null) ||
  s?.assets
    ?.map((a) => a.name)
    .filter(Boolean)
    .join("、") ||
  "未命名资料";
const storageDefault = () => { try { return globalThis.sessionStorage; } catch { return null; } };
const emptyFocus = () => ({document_source_ids:[],table_version_ids:[]});
const validFocus = f => f && ['document_source_ids','table_version_ids'].every((k,i) => Array.isArray(f[k]) && f[k].length <= (i ? 10 : 100) && f[k].every(x => typeof x === 'string' && x.length > 0) && new Set(f[k]).size === f[k].length);
const sameFocus = (a,b) => validFocus(a) && validFocus(b) && ['document_source_ids','table_version_ids'].every(k => a[k].length === b[k].length && a[k].every((v,i) => v === b[k][i]));
const tableName = t => [t.original_name || t.name || '表格', t.sheet, t.table_range].filter(Boolean).join(' · ');
const sourceReadable = source => source.formal_status==='saved_receipt'||source.reading_status==='available'||source.current_available===true;
const pathOf = (cid) => "/api/research-chats/" + encodeURIComponent(cid);

/**
 * @param {{
 *   root: HTMLElement,
 *   api: typeof import('./app.js').api,
 *   chatId?: string,
 *   go?: (hash: string) => void,
 *   replaceRoute?: (hash: string) => void,
 *   onStateChange?: (state: {chatId: string}) => void,
 *   isCurrent?: () => boolean,
 *   pollInterval?: number,
 *   summaryStorage?: Storage,
 *   storage?: Storage,
 *   tableVersionId?: string,
 *   sourceId?: string,
 * }} options
 */
export function mountResearchChat({
  root,
  api,
  chatId = null,
  go = () => {},
  replaceRoute = () => {},
  onStateChange = () => {},
  isCurrent = () => true,
  pollInterval = 1200,
  summaryStorage,
  storage = storageDefault(),
  tableVersionId = null,
  sourceId = null,
}) {
  let disposed = false,
    submitting = false,
    chat = null,
    cid = chatId,
    caps = {},
    scopeMode = "unified",
    tables = [],
    pending = null,
    querySucceeded = false,
    restoredFocus = false,
    selectedTables = [],
    sources = [],
    timer = null,
    loadVersion = 0,
    catalogueVersion = 0,
    catalogueLoading = false,
    previewVersion = 0,
    previewController = null,
    summaryController = null,
    summaryChatId = null;
  const selected = new Set(),
    sourceInputs = [],
    controller = new AbortController();
  const alive = () => !disposed && isCurrent();
  const request = (path, opts = {}) =>
    api(path, { ...opts, signal: controller.signal });
  const layout = el("section", "research-chat"),
    header = el("header", "research-chat-heading"),
    scopePicker = el("select", "research-scope-picker"),
    headingActions = el("div", "research-heading-actions"),
    sourcePanel = el("details", "research-sources"),
    sourceSummary = el("summary", null, "选择研究资料"),
    sourceList = el("div", "research-source-list"),
    sourceSearch = el("input", "research-source-search"),
    sourceTags = el("div", "research-source-tags"),
    clearFocus = button("清空重点", () => { if (pending || submitting) return; selected.clear(); selectedTables=[]; restoredFocus=true; persist(); renderSources(); }, "quiet"),
    pendingActions = el("div", "research-pending-actions"),
    notice = el("p", "research-notice"),
    dataGuide = el("div", "research-data-guidance"),
    messages = el("div", "research-messages"),
    summaryHost = el("div", "research-summary-host"),
    form = el("form", "research-composer"),
    input = el("textarea"),
    actions = el("div", "research-composer-actions"),
    send = button("发送", submit, "primary"),
    cancel = button("停止本轮", cancelTurn, "secondary"),
    reload = button("刷新状态", () => refreshStatus().catch(showError), "quiet"),
    newChat = button(
      "新建对话",
      () => go("#research-chat/new"),
      "quiet",
    );
  let draft = "";
  const localKey = key => 'sog.research-chat.v1:' + key;
  const initialKey = localKey(chatId || 'new:' + (tableVersionId || '') + ':' + (sourceId || ''));
  try {
    const saved = JSON.parse(storage?.getItem(initialKey) || 'null');
    if (saved?.version === 1) {
      if (validFocus(saved.focus)) { for (const value of saved.focus.document_source_ids) selected.add(value); selectedTables=[...saved.focus.table_version_ids]; restoredFocus=true; }
      if (typeof saved.draft === 'string') draft=saved.draft;
      const intent=saved.pending;
      if (intent && typeof intent.create_request_id === 'string' && typeof intent.payload?.request_id === 'string' && typeof intent.payload.message === 'string' && Number.isInteger(intent.payload.expected_revision) && validFocus(intent.payload.source_focus) && (!chatId || intent.chat_id === chatId)) {
        pending=intent; cid=intent.chat_id || cid; draft=intent.payload.message;
        selected.clear(); for(const value of intent.payload.source_focus.document_source_ids) selected.add(value);
        selectedTables=[...intent.payload.source_focus.table_version_ids]; restoredFocus=true;
      }
    }
  } catch { /* Invalid local cache never establishes server state. */ }
  if (!restoredFocus && tableVersionId) { selectedTables=[tableVersionId]; restoredFocus=true; }
  input.value=draft;
  sourceSearch.type='search'; sourceSearch.placeholder='搜索文件或表格'; sourceSearch.setAttribute('aria-label','搜索重点资料');
  sourceSearch.addEventListener('input', renderSources);
  const focusValue = () => ({document_source_ids:[...selected],table_version_ids:[...selectedTables]});
  function persist(required=false) {
    if (scopeMode !== 'unified') return true;
    try {
      if (!storage) throw Error('storage unavailable');
      const value=JSON.stringify({version:1,draft,focus:focusValue(),pending});
      if (cid) storage.setItem(localKey(cid),value);
      storage.setItem(initialKey, !cid || initialKey===localKey(cid) || pending ? value : 'null');
      return true;
    } catch { if (required) status('无法保存发送状态，请恢复浏览器存储后重试。'); return false; }
  }
  function acceptPending() {
    if (!pending) return;
    const value=chat?.turns?.find(t => t.request_id===pending.payload.request_id);
    if (!value) return;
    if (value.message!==pending.payload.message || !sameFocus(value.source_focus,pending.payload.source_focus)) throw Error('发送回执与原请求不匹配');
    if (draft.trim()===pending.payload.message) { draft=''; input.value=''; }
    pending=null; querySucceeded=false; persist();
  }
  input.rows = 2;
  scopePicker.hidden=true;
  scopePicker.setAttribute("aria-label", "对话范围");
  scopePicker.addEventListener("change", () => {
    if (cid || submitting) return;
    scopeMode = scopePicker.value === "all_wiki" && caps.scope_modes?.includes("all_wiki") ? "all_wiki" : "selected_sources";
    renderSources();
    renderMessages();
  });
  input.maxLength = 8000;
  input.placeholder = "输入你的研究问题…";
  input.setAttribute("aria-label", "研究问题");
  input.addEventListener("input", () => {
    draft = input.value;
    persist();
    controls();
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      submit();
    }
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submit();
  });
  notice.setAttribute("role", "status");
  notice.hidden = true;
  messages.setAttribute("aria-label", "研究对话内容");
  sourcePanel.open = !cid;
  headingActions.append(scopePicker, newChat);
  header.append(headingActions);
  sourcePanel.append(sourceSummary, sourceSearch, clearFocus, sourceTags, sourceList);
  actions.append(reload, cancel, send);
  form.append(sourcePanel, input, pendingActions, actions);
  layout.append(header, summaryHost, notice, dataGuide, messages, form);
  root.replaceChildren(layout);
  const preview = el("div", "research-evidence-overlay"),
    previewDialog = el("section", "research-evidence-dialog"),
    previewTop = el("header", "research-evidence-heading"),
    previewTitle = el("h2", null, "原页依据"),
    previewClose = button("关闭依据", closePreview, "quiet"),
    previewBody = el("div", "research-evidence-body");
  preview.hidden = true;
  previewDialog.setAttribute("role", "dialog");
  previewDialog.setAttribute("aria-modal", "true");
  previewDialog.setAttribute("aria-label", "引用依据");
  previewTop.append(previewTitle, previewClose);
  previewDialog.append(previewTop, previewBody);
  preview.append(previewDialog);
  root.append(preview);
  let previewFocus = null;
  let previewChoicesKey = null,
    previewChoicesCount = 0;
  let stateNoticeKey = null;
  const escape = (event) => {
    if (event.key === "Escape" && !preview.hidden) {
      event.preventDefault();
      closePreview();
    }
  };
  previewDialog.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const items = [
      ...(previewDialog.querySelectorAll?.(
        "button:not([disabled]),a[href],select",
      ) || [previewClose]),
    ];
    const position = items.indexOf(root.ownerDocument.activeElement);
    if (event.shiftKey && position <= 0) {
      event.preventDefault();
      items.at(-1)?.focus();
    } else if (
      !event.shiftKey &&
      (position < 0 || position === items.length - 1)
    ) {
      event.preventDefault();
      items[0]?.focus();
    }
  });
  const eventRoot = root.ownerDocument.addEventListener
    ? root.ownerDocument
    : root;
  eventRoot.addEventListener("keydown", escape);
  function status(text) {
    notice.textContent = text;
    notice.hidden = !text;
  }
  function showError(error) {
    if (alive() && error?.name !== "AbortError")
      status(
        ERROR[error?.code] ||
          "请求未完成。请刷新状态查看后台结果，未自动重发。",
      );
  }
  function active() {
    return (
      chat?.active_turn_id ||
      chat?.turns?.findLast((t) => ACTIVE.has(t.status))?.turn_id
    );
  }
  function controls() {
    const unified=scopeMode==='unified';
    const unavailable=unified && (selectedTables.some(key=>!tables.some(t=>t.table_version_id===key)) || [...selected].some(key=>!sources.some(s=>s.source_id===key&&sourceReadable(s))));
    send.disabled = caps.model_ready !== true || catalogueLoading || Boolean(cid && !chat) || submitting || Boolean(pending) || Boolean(active()) || !draft.trim() || unavailable || (unified ? !caps.scope_modes?.includes('unified') : scopeMode==='selected_sources' && !cid && !selected.size);
    reload.disabled = catalogueLoading;
    input.disabled = submitting;
    input.readOnly = Boolean(pending);
    scopePicker.hidden = unified;
    scopePicker.disabled = true;
    sourcePanel.hidden = scopeMode==='all_wiki';
    sourceSearch.hidden = !unified; clearFocus.hidden = !unified;
    clearFocus.disabled = submitting || Boolean(pending) || !(selected.size+selectedTables.length);
    newChat.hidden = !cid;
    input.placeholder = unified ? '向全库 Wiki 提问，或选择本轮重点资料…' : scopeMode==='all_wiki' ? '向全库 Wiki 提问…' : '向所选论文与专利提问…';
    for (const item of sourceInputs) item.disabled = submitting || Boolean(pending) || item.dataset.unavailable==='true';
    cancel.hidden = !active(); cancel.disabled = submitting;
    sourceSummary.textContent = unified ? selected.size+selectedTables.length ? `本轮重点 · ${selected.size} 份文献 · ${selectedTables.length} 张表格` : '全库 Wiki · 选择重点资料' : '研究资料（本会话固定）';
    pendingActions.replaceChildren();
    if (pending) {
      pendingActions.append(el('p', 'research-turn-status', submitting ? '正在发送…' : '发送结果待确认，原问题和重点资料已保留。'));
      if (!submitting) {
        pendingActions.append(button('查询发送状态', async()=>{ if(cid) await refresh().catch(showError); else {querySucceeded=true;controls();} }, 'quiet'));
        if(querySucceeded) pendingActions.append(button('重试原发送', sendIntent, 'secondary'));
      }
    }
  }
  function renderSources() {
    if (chat) scopeMode = chat.selection?.scope_mode || "selected_sources";
    scopePicker.replaceChildren();
    for (const [value, label] of [["all_wiki", "全库 Wiki"], ["selected_sources", "论文与专利"]]) {
      if (value === "all_wiki" && !caps.scope_modes?.includes(value) && !(cid && scopeMode === value)) continue;
      const option = el("option", null, label);
      option.value = value;
      scopePicker.append(option);
    }
    scopePicker.value = scopeMode;
    sourceList.replaceChildren();
    for(const warning of chat?.source_warnings||[])sourceList.append(el('p','research-source-unavailable',warning.message+'。历史问答保留，该来源不能继续作为有效依据。'));
    sourceInputs.length = 0;
    dataGuide.replaceChildren();
    const documents=sources.filter(source=>['paper','patent'].includes(source.source_type));
    if(!tables.length && !documents.some(sourceReadable)) {
      const guidance=el('p','muted',documents.length ?
        '所列文献尚未解析完成或暂无可读取内容；请完成解析后刷新并选择资料。已有 Wiki 仍可查询。' :
        '暂无可选择的文献或保存表格。请上传资料并完成解析后刷新；若已有 Wiki，仍可查询。');
      const link=el('a',null,'上传与解析资料');link.href='#documents';
      dataGuide.append(guidance,link);
    }
    if (scopeMode==='unified') {
      sourceTags.replaceChildren(el('small','muted',selected.size+selectedTables.length ? '全库 Wiki 仍可补充；新表格计算只使用本轮所选版本。' : '当前从全库 Wiki 读取；选择表格后可进行新计算。'));
      for (const key of selected) sourceTags.append(el('span','research-focus-tag',nameOf(sources.find(s=>s.source_id===key))));
      for (const key of selectedTables) sourceTags.append(el('span','research-focus-tag',tables.find(t=>t.table_version_id===key) ? tableName(tables.find(t=>t.table_version_id===key)) : '已选表格当前不可用'));
      const query=sourceSearch.value.trim().toLocaleLowerCase();
      for(const source of sources.filter(s=>['paper','patent'].includes(s.source_type))) {
        if(query && ![nameOf(source),...(source.assets||[]).map(a=>a.name)].join(' ').toLocaleLowerCase().includes(query))continue;
        const label=el('label','research-source-option'),check=el('input'),copy=el('span');
        check.type='checkbox';check.value=source.source_id;check.checked=selected.has(source.source_id);
        check.dataset.unavailable=String(!sourceReadable(source));
        check.addEventListener('change',()=>{
          if(pending||submitting)return;
          if(check.checked){if(selected.size>=100){check.checked=false;status('最多选择 100 份文献。');return;}selected.add(source.source_id);}else selected.delete(source.source_id);
          restoredFocus=true;persist();renderSources();
        });
        copy.append(el('strong',null,nameOf(source)),el('small','muted',(source.source_type==='patent'?'专利':'论文')+' · '+(check.dataset.unavailable==='true'?'尚未解析完成或暂无可读取内容':source.formal_status==='saved_receipt'?'已有保存记录':'当前可读 · 尚未人工审核')));
        if(source.assets?.length>1)for(const asset of source.assets)copy.append(el('small','muted',asset.name));
        label.append(check,copy);sourceList.append(label);sourceInputs.push(check);
      }
      for(const table of tables) {
        if(query&&!tableName(table).toLocaleLowerCase().includes(query))continue;
        const label=el('label','research-source-option'),check=el('input'),copy=el('span');
        check.type='checkbox';check.value=table.table_version_id;check.checked=selectedTables.includes(check.value);
        check.addEventListener('change',()=>{
          if(pending||submitting)return;
          if(check.checked){const next=selectedTables.filter(key=>tables.find(t=>t.table_version_id===key)?.table_id!==table.table_id);if(next.length>=10){check.checked=false;status('最多选择 10 张表格。');return;}selectedTables=[...next,table.table_version_id];}
          else selectedTables=selectedTables.filter(key=>key!==table.table_version_id);
          restoredFocus=true;persist();renderSources();
        });
        copy.append(el('strong',null,tableName(table)),el('small','muted','已保存表格 · 版本 '+table.table_version_id.slice(-8)));

        label.append(check,copy);sourceList.append(label);sourceInputs.push(check);
      }
      if(!sourceList.children.length)sourceList.append(el('p','muted',query?'没有匹配的资料。':'暂无可选择资料，可直接向全库 Wiki 提问。'));
    } else {
      sourceTags.replaceChildren();
      for(const key of chat?.selection?.source_ids||[])sourceList.append(el('p','research-source-name',nameOf(sources.find(s=>s.source_id===key))));
    }
    controls();
  }
  function renderMessages() {
    const scrollTop = messages.scrollTop || 0;
    const follow = !messages.children.length || (messages.scrollHeight || 0) - scrollTop - (messages.clientHeight || 0) < 80;
    messages.replaceChildren();
    if (!chat?.turns?.length) {
      messages.append(
        el(
          "p",
          "research-empty",
          ["all_wiki","unified"].includes(scopeMode) ? "想从知识库中了解什么？" : cid ? "可以开始提问。" : "选择论文或专利，开始提问。",
        ),
      );
      messages.append(el('p','research-empty-description','围绕已有资料梳理结论、比较条件，并回到引用依据核对。问题建议只填入输入框。'),questionSuggestions(['概括当前资料的主要结论与适用条件','比较不同配方的性能和测试条件','列出已有依据与尚待验证的缺口'],prompt=>{if(pending||submitting)return;input.value=prompt;input.dispatchEvent(new Event('input'));input.focus();}));
      return;
    }
    for (const turn of chat.turns) {
      const group = el("article", "research-turn"),
        question = el("p", "research-question", turn.message);
      group.append(question);
      if(validFocus(turn.source_focus)) {
        const names=[...turn.source_focus.document_source_ids.map(key=>nameOf(sources.find(s=>s.source_id===key))),...turn.source_focus.table_version_ids.map(key=>tables.find(t=>t.table_version_id===key)?tableName(tables.find(t=>t.table_version_id===key)):'历史表版本 '+key)];
        group.append(el('p','research-turn-focus',names.length?'本轮重点：'+names.join('；'):'本轮：全库 Wiki'));
      }
      if (turn.status === "completed" && turn.answer) {
        const answer = el("div", "research-answer");
        for (const block of turn.answer.blocks || [
          { text: turn.answer.text },
        ]) {
          const row = el("div", "research-answer-block");
          const entry = turn.answer.registry?.[block.support_id];
          if (["wiki_content", "wiki_evidence"].includes(block.kind)) {
            if (entry?.kind === block.kind && entry.id === block.support_id) row.append(renderWikiSupport(entry,{focus:turn.source_focus}));
            else if (block.kind === "wiki_content") row.append(renderChatMarkdown(block.text || ""));
            else row.append(el("p", null, "打开依据查看保存的结果。"));
          } else {
            const role=entry && researchSourceRole(entry,turn.source_focus);
            if(role)row.append(el('p','research-source-role',role));
            row.append(renderChatMarkdown(block.text || ""));
          }
          if (block.support_id && turn.unavailable_supports?.[block.support_id]) {
            row.append(el('p','research-source-unavailable',turn.unavailable_supports[block.support_id]+' · 历史引用已失效'));
          } else if (block.support_id) {
            const open = button(
              block.kind === "wiki_content" ? "查看 Wiki 章节" : "查看依据",
              () => openPreview(turn.turn_id, block.support_id, 0, open),
              "research-evidence-link",
            );
            row.append(open);
          }
          answer.append(row);
        }
        group.append(answer);
      } else {
        group.append(
          el(
            "p",
            "research-turn-status",
            ERROR[turn.error?.code] ||
              STATUS[turn.status] ||
              "状态待确认",
          ),
        );
      }
      messages.append(group);
    }
    messages.scrollTop = follow ? messages.scrollHeight || 0 : scrollTop;
    controls();
  }
  function schedule() {
    clearTimeout(timer);
    if (alive() && active())
      timer = setTimeout(
        () =>
          refresh().catch((error) => {
            showError(error);
            schedule();
          }),
        pollInterval,
      );
  }
  async function refresh() {
    if (!cid || !alive()) return;
    const version = ++loadVersion;
    let result = await request(pathOf(cid)),
      turns = [...(result.turns || [])],
      offset = 0;
    const revision = result.revision;
    while (result.has_more) {
      if (
        !Number.isInteger(result.next_offset) ||
        result.next_offset <= offset ||
        turns.length >= 200
      )
        throw Error("invalid pagination");
      offset = result.next_offset;
      result = {
        ...result,
        ...(await request(pathOf(cid) + "?offset=" + offset + "&limit=50")),
      };
      if (result.revision !== revision)
        throw Object.assign(Error("history changed"), {
          code: "stale_context",
        });
      turns.push(...(result.turns || []));
    }
    if (!alive() || version !== loadVersion) return;
    chat = { ...result, turns };
    if (chat.selection?.scope_mode==='unified') {
      scopeMode='unified';
      if (!restoredFocus && validFocus(chat.selection.source_focus)) { selected.clear();for(const value of chat.selection.source_focus.document_source_ids)selected.add(value);selectedTables=[...chat.selection.source_focus.table_version_ids];restoredFocus=true; }
      querySucceeded=Boolean(pending); acceptPending(); persist();
    }
    updateSummary();
    renderSources();
    renderMessages();
    const key = JSON.stringify([
      cid,
      chat.revision,
      chat.active_turn_id,
      chat.turns.at(-1)?.status,
    ]);
    if (key !== stateNoticeKey) {
      stateNoticeKey = key;
      onStateChange({ chatId: cid });
    }
    schedule();
  }
  function updateSummary() {
    if (!cid || !chat) return;
    if (chat.selection?.scope_mode === "all_wiki") {
      summaryController?.dispose();
      summaryController = null;
      summaryChatId = null;
      summaryHost.replaceChildren();
      return;
    }
    if (summaryChatId !== cid) {
      summaryController?.dispose();
      summaryChatId = cid;
      summaryController = mountResearchSummary({root: summaryHost, api, chatId: cid, go, isCurrent: alive, pollInterval, storage: summaryStorage});
    }
    summaryController.updateChat(chat, caps.wiki_model_ready === true);
  }
  async function sendIntent() {
    if (!pending || submitting || !alive()) return;
    const intent=pending;
    submitting=true;querySucceeded=false;controls();status('');
    try {
      if (!cid) {
        const created=await request('/api/research-chats',{method:'POST',json:{request_id:intent.create_request_id,scope_mode:'unified'}});
        if(!alive())return;
        if(typeof created?.chat_id!=='string'||!Number.isInteger(created.revision)||created.selection?.scope_mode!=='unified')throw Error('创建回执不匹配');
        cid=created.chat_id;chat=created;intent.chat_id=cid;
        if(!persist(true))return;
        replaceRoute('#research-chat/'+encodeURIComponent(cid));onStateChange({chatId:cid});
      }
      const result=await request(pathOf(cid)+'/turns',{method:'POST',json:structuredClone(intent.payload)});
      if(!alive())return;
      if(result.request_id!==intent.payload.request_id||result.message!==intent.payload.message||!sameFocus(result.source_focus,intent.payload.source_focus)||typeof result.turn_id!=='string')throw Error('发送回执与原请求不匹配');
      chat={...chat,revision:Math.max(chat.revision,intent.payload.expected_revision+1),turns:[...(chat.turns||[]).filter(t=>t.turn_id!==result.turn_id),result]};
      acceptPending();await refresh();
    } catch(error) {
      showError(error);
      if(Number.isInteger(error.status)&&error.status>=400&&error.status<500&&![408,429].includes(error.status)){pending=null;persist();}
      if(cid&&alive()) await refresh().catch(showError);else querySucceeded=true;
    } finally {if(alive()){submitting=false;persist();renderSources();renderMessages();controls();}}
  }
  async function submit() {
    if (send.disabled || !alive() || submitting) return;
    const question = draft.trim();
    if (!question) return;
    if(scopeMode==='unified') {
      pending={chat_id:cid,create_request_id:id(),payload:{request_id:id(),expected_revision:chat?.revision||0,message:question,source_focus:focusValue()}};
      if(!persist(true)){pending=null;controls();return;}
      await sendIntent();return;
    }
    submitting = true;
    controls();
    status("");
    try {
      if (!cid) {
        const result = await request("/api/research-chats", {
          method: "POST",
          json: { request_id: id(), scope_mode: scopeMode, source_ids: scopeMode === "all_wiki" ? [] : [...selected] },
        });
        if (!alive()) return;
        chat = result;
        cid = result.chat_id;
        replaceRoute("#research-chat/" + encodeURIComponent(cid));
        renderSources();
        onStateChange({ chatId: cid });
      }
      await request(pathOf(cid) + "/turns", {
        method: "POST",
        json: {
          request_id: id(),
          expected_revision: chat.revision,
          message: question,
        },
      });
      if (!alive()) return;
      draft = "";
      input.value = "";
      await refresh();
    } catch (error) {
      showError(error);
      if (cid && alive()) await refresh().catch(() => {});
    } finally {
      if (alive()) {
        submitting = false;
        controls();
      }
    }
  }
  async function cancelTurn() {
    const tid = active();
    if (!tid || submitting || !alive()) return;
    submitting = true;
    controls();
    try {
      await request(
        pathOf(cid) + "/turns/" + encodeURIComponent(tid) + "/cancel",
        { method: "POST", json: {} },
      );
      if (alive()) await refresh();
    } catch (error) {
      showError(error);
    } finally {
      if (alive()) {
        submitting = false;
        controls();
      }
    }
  }
  function closePreview() {
    previewVersion++;
    previewController?.abort();
    previewController = null;
    preview.hidden = true;
    previewChoicesKey = null;
    previewChoicesCount = 0;
    previewBody.replaceChildren();
    previewFocus?.focus?.();
  }
  async function openPreview(tid, supportId, evidenceIndex, trigger, explicitEvidence=false) {
    previewController?.abort();
    previewController = new AbortController();
    const signal = previewController.signal,
      version = ++previewVersion;
    previewFocus = trigger || previewFocus;
    preview.hidden = false;
    previewTitle.textContent = "引用依据";
    const choiceKey = tid + ":" + supportId;
    const studySupport=chat?.turns?.find(t=>t.turn_id===tid)?.answer?.registry?.[supportId]?.ref?.kind==='study_unit_snapshot';
    if (previewChoicesKey !== choiceKey) {
      previewChoicesKey = choiceKey;
      previewChoicesCount = 0;
    }
    const choices = el("div", "research-evidence-choices"),
      previewDetail = el("div", "research-evidence-detail");
    function renderChoices(count) {
      choices.replaceChildren();
      if (count > 1 || studySupport && count > 0)
        for (let i = 0; i < Math.min(count, 20); i++) {
          const b = button(
            "依据 " + (i + 1),
            () => openPreview(tid, supportId, i, trigger, true),
            "quiet",
          );
          b.disabled = i === evidenceIndex && (!studySupport || explicitEvidence);
          choices.append(b);
        }
    }
    renderChoices(previewChoicesCount);
    previewDetail.append(el("p", "muted", "正在读取来源…"));
    previewBody.replaceChildren(choices, previewDetail);
    previewClose.focus();
    const current = () =>
      alive() && version === previewVersion && !preview.hidden;
    try {
      const base =
        pathOf(cid) +
        "/turns/" +
        encodeURIComponent(tid) +
        "/supports/" +
        encodeURIComponent(supportId);
      const support = await api(
        base + (explicitEvidence || evidenceIndex ? "?evidence_index=" + evidenceIndex : ""),
        { signal },
      );
      if (!current()) return;
      const location = support.location,
        handle =
          location?.resource_ref || location?.evidence_data?.resource_ref;
      previewChoicesCount = support.evidence_refs?.length || support.payload?.evidence_refs?.length || Math.min(support.payload?.citations?.length||0,20) || Math.min(support.payload?.fact?.evidence_refs?.length||0,20);
      renderChoices(previewChoicesCount);
      previewDetail.replaceChildren();
      const wikiSupport = ["wiki_content", "wiki_evidence"].includes(support.kind);
      if (wikiSupport) {
        previewTitle.textContent = support.kind === "wiki_content" ? "Wiki 章节" : "引用依据";
        previewDetail.append(renderWikiSupport(support, { preview: true, focus:chat?.turns?.find(t=>t.turn_id===tid)?.source_focus }));
        if (!handle) return;
      }
      if (!handle) {
        previewDetail.append(
          el(
            "p",
            "muted",
            "这一结果由已读取记录的查询或比较得到，没有独立的原页定位。",
          ),
        );
        return;
      }
      const result = await api(
        "/api/knowledge/v1/resources/" +
          encodeURIComponent(handle) +
          "/preview",
        { signal },
      );
      if (!current()) return;
      const kind =
        location?.content_kind ||
        location?.evidence_data?.effective_content_kind ||
        support.content_kind ||
        "unknown";
      previewDetail.append(
        el(
          "p",
          "research-preview-caption",
          `第 ${result.physical_page} 页 · ` +
            ({
              parsed: "解析文本，请结合原页核对",
              model_transcription: "模型转写，请结合原页核对",
              verbatim: "来源逐字内容",
              unknown: "文字来源类型尚未确认",
            }[kind] || "文字来源类型尚未确认"),
        ),
      );
      if (
        !/^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(
          result.image?.data_uri || "",
        )
      )
        throw Error("invalid preview");
      const scroller = el("div", "research-evidence-scroll"),
        plane = el("div", "research-evidence-plane"),
        image = el("img");
      image.src = result.image.data_uri;
      image.alt = "依据原页，第 " + result.physical_page + " 页";
      image.width = result.image.width;
      image.height = result.image.height;
      plane.append(image);
      if (result.location_status === "located")
        for (const box of result.boxes || []) {
          if (
            ![box.x0, box.x1, box.y0, box.y1].every(
              (v) => Number.isFinite(v) && v >= 0 && v <= 1,
            ) ||
            box.x1 <= box.x0 ||
            box.y1 <= box.y0
          )
            continue;
          const mark = el("div", "research-evidence-box");
          mark.style.left = box.x0 * 100 + "%";
          mark.style.top = box.y0 * 100 + "%";
          mark.style.width = (box.x1 - box.x0) * 100 + "%";
          mark.style.height = (box.y1 - box.y0) * 100 + "%";
          mark.setAttribute("aria-hidden", "true");
          plane.append(mark);
        }
      else
        previewDetail.append(
          el("p", "muted", "当前仅能定位到页面，未绘制未经确认的高亮。"),
        );
      scroller.append(plane);
      previewDetail.append(scroller);
    } catch (error) {
      if (current() && error?.name !== "AbortError")
        previewDetail.replaceChildren(
          el(
            "p",
            "research-notice",
            ERROR[error?.code] ||
              "依据暂时不可用，可能已变更；请关闭后刷新来源状态。",
          ),
        );
    }
  }
  async function refreshCatalogue() {
    if(!alive())return false;
    const version=++catalogueVersion;
    catalogueLoading=true;controls();
    const results = await Promise.allSettled([
      request("/api/research-chats/capabilities"),
      request("/api/knowledge/v1/document-sources"),
      request("/api/raw-tables"),
    ]);
    if (!alive() || version!==catalogueVersion) return false;
    catalogueLoading=false;
    caps = results[0].status === 'fulfilled' ? results[0].value : {};
    tables = results[2].status === 'fulfilled' ? results[2].value.tables||[] : [];
    sources = results[1].status === 'fulfilled' ? results[1].value.sources||[] : [];
    renderSources();
    renderMessages();
    updateSummary();
    if(results[0].status==='rejected')showError(results[0].reason);
    else if(caps.model_ready!==true) status(ERROR[caps.model_status?.reason] || ERROR.not_ready);
    else if (!cid && !caps.scope_modes?.includes("unified")) status("统一问答能力尚未就绪，请正常重启服务后刷新状态再试。");
    else if(results[1].status==='rejected')showError(results[1].reason);
    else if(results[2].status==='rejected')showError(results[2].reason);
    else status('');
    return true;
  }
  async function refreshStatus() {
    if(!await refreshCatalogue())return;
    if (cid) await refresh();
    else if (pending) {querySucceeded=true;controls();}
  }
  const ready = refreshStatus().catch(showError);
  return {
    ready,
    refresh,
    dispose() {
      disposed = true;
      clearTimeout(timer);
      controller.abort();
      previewVersion++;
      previewController?.abort();
      summaryController?.dispose();
      eventRoot.removeEventListener("keydown", escape);
    },
  };
}
