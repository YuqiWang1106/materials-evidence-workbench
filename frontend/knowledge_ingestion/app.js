// @ts-check
import {submitDocumentBatch} from './document-upload-batch.mjs';
import {processingSummary, processingView} from './document-processing.mjs';
import {modelOutcomeUnknown} from './document-processing-logic.mjs';
import {taskObservation} from './document-observation-task.mjs';
import { el, button, input, select, field, commitPendingInputs } from "./dom.js";
import { uploadBatch, uploadPolicy } from "./reading-logic.mjs";
import { renderResult } from "./result-view.js";
import { renderDocumentResult } from "./document-result-view.js";
import { renderDocumentCandidates } from "./document-candidate-view.js";
import { renderEntityResult, mountKnowledgeGraph, mountEvidenceSource } from "./entity-result-view.js";
import { renderEntityReview } from "./entity-review-view.js";
import { mountRowQuery } from './row-query-view.js';
import { mountDocumentQuery } from './document-query-view.js';
import { mountRegionReview } from './region-review.js';
import { mountStandardReview } from './standard-review.js';
import { renderStandardResult } from './standard-result-view.js';
import { mountTableChat } from './table-chat.js';
import { mountResearchChat } from './research-chat.js';
import { mountWorkspaceShell } from './workspace-shell.js';
import { mountWorkspaceLibrary } from './workspace-library.js';
const shell = mountWorkspaceShell({root:document.getElementById('app-shell'),go:hash=>go(hash)});
const main = shell.pageRoot, chatRoot=shell.chatRoot;
const state = {
  capabilities: {},
  assets: new Map(),
  routeVersion: 0,
  cleanup: null,
  busy: false,
  task: null,
  taskSignature: "",
  pollTimer: null,
  chatController: null,
  chatKey: null,
  chatEpoch: 0,
};
const STATUSES = {
  queued: "等待整理",
  reading: "读取原件",
  extracting: "整理记录",
  validating: "核对结构",
  needs_review: "提取完成",
  publishing: "正在保存",
  saved: "已保存",
  write_unknown: "保存待核对",
  failed: "整理未完成",
  cancelled: "已取消",
  superseded: "历史版本",
};
function stored(name) {
  try {
    return JSON.parse(localStorage.getItem(name) || "null");
  } catch {
    return null;
  }
}
function remember(name, value) {
  try {
    localStorage.setItem(name, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}
function message(text, tone = "") {
  const n = document.getElementById("global-message");
  n.textContent = text;
  n.className = "message " + tone;
  n.hidden = !text;
}
function nativeDocumentsNotReadyMessage() {
  const reason = state.capabilities?.native_document_readiness?.reason;
  const messages = {
    model_not_configured: "尚未配置文档提取模型服务，请补齐私有模型配置。",
    api_key_missing: "文档提取模型缺少 API key，请在私有模型配置中填写。",
    model_config_invalid: "文档模型配置格式无效，请核对私有配置文件。",
    unsupported_protocol: "当前文档模型协议暂不受支持，请检查配置的接口协议。",
    unsupported_model_capability: "当前模型接口不支持文档提取所需能力，请检查该模型支持的输入与输出。",
    parser_not_ready: "本地 Standard 解析环境或模型文件尚未就绪，请检查解析环境与模型文件校验结果。",
    standard_runtime_or_model_files_unavailable: "本地 Standard 解析环境或模型文件尚未就绪，请检查解析环境与模型文件校验结果。",
    website_dependencies_unavailable: "网站运行依赖尚未就绪，请先补齐网站运行环境。",
  };
  const detail = Object.hasOwn(messages, reason) ? messages[reason] : "论文 / 专利提取尚未就绪，请检查文档模型服务与本地 Standard 解析环境。";
  return detail + " 保存配置或补齐环境后，请正常重启网站，再刷新页面。";
}
function friendly(error) {
  const s = error.message || String(error);
  if (error.code === "NATIVE_DOCUMENTS_NOT_READY" ||
      error.code === "native_documents_not_ready" || s === "native_documents_not_ready")
    return nativeDocumentsNotReadyMessage();
  if (error.status === 409)
    return "草稿或结果已变化，请载入最新版本后重新核对。";
  if (error.status === 422) {
    if (/owner|target/.test(s))
      return "字段含义与所属对象不相符，请检查当前内容块的对象与字段。";
    if (/header|range|row|column|coordinate|bounds/.test(s))
      return "所选表头、行列或范围还不完整，请对照原表检查后再继续。";
    if (/key|identity/.test(s))
      return "身份键或原字段名称尚未匹配，请对照原表检查。";
    if (/fields|profiles|reading/.test(s))
      return "请完善内容块的字段含义，并至少保留一组内容。";
    return "读法尚未通过检查，请核对当前内容块的范围、字段及对象关系。";
  }
  if (/archive|publication/i.test(s))
    return "暂时无法确认保存状态。请稍后刷新，原件与预览已保留。";
  return s;
}
/**
 * @param {string} path
 * @param {{method?: string, json?: unknown, body?: BodyInit, signal?: AbortSignal, timeout?: number}} [options]
 */
export async function api(
  path,
  { method = "GET", json, body, signal, timeout = 120000 } = {},
) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  if (signal?.aborted) controller.abort();
  const timer = setTimeout(abort, timeout);
  try {
    const headers = { Accept: "application/json" };
    if (json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(json);
    }
    const r = await fetch(path, {
      method,
      body,
      headers,
      credentials: "same-origin",
      signal: controller.signal,
    });
    const d = await r.json().catch(() => null);
    if (!r.ok)
      throw Object.assign(
        new Error(
          typeof d?.detail === "string"
            ? d.detail
            : r.status === 401
              ? "连接会话已过期，请刷新页面。"
              : "请求未完成，请检查输入后重试。",
        ),
        { status: r.status, code: d?.error?.code ?? d?.code ?? d?.detail?.code, gaps: d?.error?.gaps ?? d?.gaps ?? d?.detail?.gaps, rangeConflict: d?.range_conflict },
      );
    return d;
  } catch (e) {
    if (e.name === "AbortError" && !signal?.aborted)
      throw new Error("请求仍未返回。请刷新查看后台状态，避免重复提交。");
    if (e instanceof TypeError)
      throw new Error("暂时无法连接本机服务，请恢复连接后重试。");
    throw e;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}
function heading(title, subtitle, kicker = "资料整理") {
  const n = el("div", "page-heading");
  const copy = el("div");
  copy.append(
    el("p", "eyebrow", kicker),
    el("h1", null, title),
    el("p", "muted", subtitle),
  );
  n.append(copy);
  return n;
}
function steps(current, rowMode = false) {
  const list = el("ol", "steps");
  (rowMode ? ["上传并选择", "确认含义", "核对并保存"] : ["上传资料", "选择内容", "核对并保存"]).forEach((text, i) => {
    const li = el(
      "li",
      i + 1 === current ? "current" : i + 1 < current ? "done" : "",
    );
    li.append(
      el("span", "step-number", i + 1 < current ? "✓" : i + 1),
      el("span", null, text),
    );
    if (i + 1 === current) li.setAttribute("aria-current", "step");
    list.append(li);
  });
  return list;
}
function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}
function lock(action) {
  return async () => {
    if (state.busy) return;
    state.busy = true;
    const actionRoute = state.routeVersion;
    try {
      await action();
    } catch (e) {
      if (actionRoute === state.routeVersion) message(friendly(e), "error");
    } finally {
      state.busy = false;
    }
  };
}
function assetName(id, index = 0) {
  return (
    state.assets.get(id)?.original_name ||
    stored("kb-asset:" + id)?.original_name ||
    `原件 ${index + 1}`
  );
}
function renderDocumentUpload() {
  main.replaceChildren(
    heading(
      "整理论文与专利",
      "上传论文或专利，提取后按样品核对并保存。",
    ),
  );
  const panel = el("section", "panel upload-panel");
  const form = el("form", "upload-form");
  const files = [];
  const policy = uploadPolicy(state.capabilities, true);
  const unavailable = () => state.capabilities?.native_document_readiness?.status === "not_ready";
  let uploading = false;
  const uploaded = new Map();
  let titleTouched = false;
  const title = input(
    "",
    () => {
      titleTouched = true;
    },
    { placeholder: "选择文件后自动填写" },
  );
  title.maxLength = 200;
  title.required = true;
  let type = "paper";
  form.append(
    field(
      "资料类型",
      select(
        [
          ["paper", "论文"],
          ["patent", "专利"],
        ],
        type,
        (v) => (type = v),
      ),
    ),
  );
  form.append(field("资料名称", title));
  const drop = el("label", "drop-zone");
  const picker = el("input");
  picker.type = "file";
  picker.multiple = true;
  picker.accept = ".pdf,.txt,.md,.xml,.json,.cff";
  picker.setAttribute("aria-label", "选择资料文件");
  drop.append(
    el("span", "upload-icon", "↥"),
    el("strong", null, "选择文件，或拖到这里"),
    el(
      "span",
      "help",
      "每篇 PDF 独立解析。附带说明文件时，请逐篇上传对应 PDF 与说明文件。",
    ),
    picker,
  );
  form.append(drop);
  const list = el("ul", "file-list");
  const feedback = el("p", "help");
  feedback.setAttribute("aria-live", "polite");
  const readinessNotice = el("p", "message warn", nativeDocumentsNotReadyMessage());
  readinessNotice.setAttribute("role", "status");
  readinessNotice.hidden = !unavailable();
  const submit = button(
    "开始提取",
    () => {},
    "primary",
  );
  submit.type = "submit";
  submit.disabled = unavailable();
  form.append(
    readinessNotice,
    list,
    feedback,
    el("p", "help", `每个原件最多 ${policy.label}，最多 50 个文件。`),
    submit,
  );
  panel.append(form);
  main.append(panel);
  const draw = () => {
    list.replaceChildren();
    for (const [i, f] of files.entries()) {
      const li = el("li");
      li.append(
        el("span", null, f.name),
        el("small", "muted", `${(f.size / 1024).toFixed(0)} KB`),
        button(
          "移除",
          () => {
            if (uploading) return;
            files.splice(i, 1);
            draw();
          },
          "quiet",
        ),
      );
      li.lastChild.disabled = uploading;
      list.append(li);
    }
  };
  const submittedDocuments = new Map();
  const add = (items) => {
    if (state.busy) return;
    for (const f of items) {
      if (!f.size || f.size > policy.maxBytes) {
        message(`空文件或超过 ${policy.label} 的文件未加入。`, "warn");
        continue;
      }
      if (files.length < 50 && !files.includes(f)) files.push(f);
    }
    if (!titleTouched && files.length) title.value = files[0].name;
    draw();
  };
  picker.addEventListener("change", () => add(picker.files));
  drop.addEventListener("dragover", (e) => {
    e.preventDefault();
    drop.classList.add("dragging");
  });
  drop.addEventListener("dragleave", () => drop.classList.remove("dragging"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("dragging");
    add(e.dataTransfer.files);
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    lock(async () => {
      if (unavailable()) {
        submit.disabled = true;
        readinessNotice.textContent = nativeDocumentsNotReadyMessage();
        readinessNotice.hidden = false;
        message(nativeDocumentsNotReadyMessage(), "warn");
        return;
      }
      if (!files.length) {
        message("请先选择原件。", "warn");
        return;
      }
      const actionRoute = state.routeVersion;
      const active = () => actionRoute === state.routeVersion;
      const batch = uploadBatch(files);
      const pdfCount = batch.filter(file => /[.]pdf$/i.test(file.name)).length;
      if (!pdfCount || (pdfCount > 1 && pdfCount !== batch.length)) {
        message(!pdfCount ? '请选择 PDF 原件，说明文件请随对应 PDF 一起上传。' : '多篇 PDF 的说明文件归属不明确，请逐篇上传对应 PDF 与说明文件。', 'warn');
        return;
      }
      const submittedType = type;
      uploading = true;
      submit.disabled = true;
      picker.disabled = true;
      draw();
      try {
        const ids = [];
        for (const [i, f] of batch.entries()) {
          if (!active()) return;
          feedback.textContent = `正在保留原件 ${i + 1} / ${batch.length}…`;
          let a = uploaded.get(f);
          if (!a) {
            const data = new FormData();
            data.append("file", f);
            a = await api("/api/assets", { method: "POST", body: data });
            uploaded.set(f, a);
            state.assets.set(a.asset_id, a);
            remember("kb-asset:" + a.asset_id, {
              original_name: a.original_name,
            });
          }
          ids.push(a.asset_id);
        }
        if (!active()) return;
        const tasks = await submitDocumentBatch({batch, ids, type: submittedType, api, submitted: submittedDocuments, active,
          onProgress: (done, total) => { feedback.textContent = `已提交 ${done} / ${total} 份文献，每份独立解析；可在资料库分别查看。`; }});
        if (tasks.length === 1) remember("kb-title:" + tasks[0].source_id, title.value.trim());
        if (active()) go(tasks.length === 1 ? "#task/" + tasks[0].task_id : "#library");
      } finally {
        uploading = false;
        submit.disabled = unavailable();
        readinessNotice.textContent = nativeDocumentsNotReadyMessage();
        readinessNotice.hidden = !unavailable();
        picker.disabled = false;
        draw();
        feedback.textContent = "";
      }
    })();
  });
}
async function openDraft(id) {
  const version = state.routeVersion;
  main.replaceChildren(el("p", "loading", "正在读取草稿…"));
  let remote;
  try {
    remote = await api("/api/table-drafts/" + encodeURIComponent(id));
  } catch (error) {
    if (version !== state.routeVersion) return;
    if (![404, 410].includes(error.status)) throw error;
    main.replaceChildren(
      heading("草稿已移除", "这份草稿已不存在，请从当前表格入口开始。"),
      button("上传新表格", () => go("#regions"), "primary"),
    );
    return;
  }
  if (version !== state.routeVersion) return;
  if (remote.recipe?.entry_profile === 'table_regions_v1') {
    go('#regions/' + encodeURIComponent(id));
  } else if (remote.recipe?.standard_review?.parent_source_id) {
    go('#standard/' + encodeURIComponent(remote.recipe.standard_review.parent_source_id));
  } else {
    main.replaceChildren(
      heading("旧版表格草稿不再继续编辑", "请从当前入口重新上传原件，确认范围后核对含义。"),
      button("上传新表格", () => go("#regions"), "primary"),
    );
  }
}
async function library() {
  main.replaceChildren(
    heading("我的资料", "继续未完成的草稿，或回看已整理的记录。"),
  );
  let render = () => {};
  const search = input("", () => {});
  search.addEventListener("input", () => render());
  search.type = "search";
  search.placeholder = "按资料名称查找";
  main.append(field("查找资料", search));
  const list = el("div", "library-list");
  main.append(list);
  list.append(el("p", "loading", "正在读取资料…"));
  const [drafts, tasks] = await Promise.all([
    api("/api/table-drafts"),
    api("/api/tasks"),
  ]);
  if (!list.isConnected) return;
  const draftMap = new Map(drafts.map((d) => [d.source_id, d]));
  const grouped = new Map();
  for (const t of tasks) {
    if (!grouped.has(t.source_id)) grouped.set(t.source_id, []);
    grouped.get(t.source_id).push(t);
  }
  let titles = new Map();
  for (const d of drafts) titles.set(d.source_id, d.title);
  for (const t of tasks)
    if (!titles.has(t.source_id))
      titles.set(
        t.source_id,
        stored("kb-title:" + t.source_id) ||
          (t.title && t.title !== t.source_id ? t.title : null) ||
          assetName(t.asset_ids?.[0]) ||
          "资料",
      );
  render = () => {
    const q = search.value.trim().toLowerCase();
    list.replaceChildren();
    const sources = [
      ...new Set([
        ...drafts.map((d) => d.source_id),
        ...tasks.map((t) => t.source_id),
      ]),
    ];
    for (const id of sources) {
      const title = titles.get(id) || "资料";
      if (q && !title.toLowerCase().includes(q)) continue;
      const d = draftMap.get(id);
      const ts = (grouped.get(id) || []).sort(
        (a, b) => b.generation - a.generation,
      );
      const latest = ts[0];
      const card = el("article", "panel library-card");
      const text = el("div");
      text.append(
        el("h2", null, title),
        el(
          "p",
          "muted",
          latest ? STATUSES[latest.state] || "处理中" : "未完成的草稿",
        ),
      );
      const actions = el("div", "library-actions");
      if (d)
        actions.append(
          button(
            d.recipe?.standard_review?.parent_source_id ? '继续核对含义' : d.recipe?.entry_profile === 'table_regions_v1' ? "继续范围核对" : "上传新表格",
            () => go(d.recipe?.standard_review?.parent_source_id ? '#standard/' + encodeURIComponent(d.recipe.standard_review.parent_source_id) : d.recipe?.entry_profile === 'table_regions_v1' ? '#regions/' + encodeURIComponent(id) : "#regions"),
            "secondary",
          ),
        );
      if (d?.recipe?.entry_profile === 'table_regions_v1' && state.capabilities?.table_analysis_workflow === 'basic-table-analysis-v1')
        actions.append(button(state.capabilities?.table_chat_workflow==='table-chat-v1'?'对话分析':'原表与分析',()=>go(state.capabilities?.table_chat_workflow==='table-chat-v1'?'#table-chat/new?source='+encodeURIComponent(id):'#table-analysis/'+encodeURIComponent(id)),'primary'));
      if (latest)
        actions.append(
          button("查看记录", () => go("#task/" + latest.task_id), "primary"),
        );
      if (ts.length > 1) {
        const chooser = select(
          [
            ["", "历史版本"],
            ...ts
              .slice(1)
              .map((t) => [
                t.task_id,
                `版本 ${t.generation} · ${STATUSES[t.state] || "历史记录"}`,
              ]),
          ],
          "",
          (v) => {
            if (v) go("#task/" + v);
          },
        );
        chooser.setAttribute("aria-label", title + " 的历史版本");
        actions.append(chooser);
      }
      card.append(text, actions);
      list.append(card);
    }
    if (!list.childNodes.length)
      list.append(
        el(
          "div",
          "empty",
          q ? "没有匹配的资料。" : "还没有资料。上传第一份原件后，从这里继续。",
        ),
      );
  };
  render();
}
async function openTask(id) {
  const version = state.routeVersion;
  main.replaceChildren(el("p", "loading", "正在读取整理进度…"));
  state.taskSignature = "";
  let retired = false;
  let taskId = id, candidateView = null, candidateBusy = false, candidateEpoch = 0, observation = null;
  const timelined = task => ['paper','patent'].includes(task.source_type) && task.document_extraction_mode === 'entity-graph-v1';
  const observing = task => timelined(task) ? (observation ||= taskObservation({api, state})) : null;
  const taskSignature = task => JSON.stringify(task.processing || {}) + task.state + ":" + (task.package_sha256 || "") + ":" + (task.updated_at || "") + ":" + modelOutcomeUnknown(task.error);
  const showRetired = () => {
    retired = true;
    main.replaceChildren(
      heading("旧版表格流程已停用", "请从当前入口重新上传原件，确认范围后核对含义。"),
      button("上传新表格", () => go("#regions"), "primary"),
    );
  };
  const update = async (supplied) => {
    if (candidateView && candidateBusy && !supplied) return;
    const requestEpoch = candidateEpoch, requestTaskId = taskId;
    const task = supplied?.task_id
      ? supplied
      : await api("/api/tasks/" + encodeURIComponent(taskId));
    if (version !== state.routeVersion || requestEpoch !== candidateEpoch || requestTaskId !== taskId) return;
    state.task = task;
    if (!task.title || task.title === task.source_id)
      task.title = stored("kb-title:" + task.source_id) || task.title;
    const signature = taskSignature(task);
    if (signature === state.taskSignature) return;
    if (observing(task)?.unchanged(task)) { state.taskSignature = signature; return; }
    if (candidateView && task.task_id === taskId && task.candidate_review_protocol === 'human_reviewed_sol_candidate_v1') {
      state.taskSignature = signature;
      candidateView.updateTask?.(task);
      return;
    }
    state.cleanup?.();
    state.cleanup = null;
    if (task.package_sha256) {
      const initialView = task.source_type === 'experimental'
        ? await api('/api/tasks/' + encodeURIComponent(task.task_id) + '/result-view?offset=0&limit=50') : null;
      if (version !== state.routeVersion) return;
      state.taskSignature = signature;
      const standardMode = initialView?.entry_profile === 'standard_tables_v1';
      if (task.source_type === 'experimental' && !standardMode) {
        showRetired();
        return;
      }
      const standardParent = initialView?.standard_review?.parent_source_id;
      main.replaceChildren();
      if (standardMode) main.append(el("p", "region-step-title", "第 5 步 · 核对并保存"));
      else if (task.state !== "saved" && task.source_type === 'experimental') main.append(steps(3));
      const content = el("div", standardMode ? "standard-task-workspace" : null);
      main.append(content);
      const candidateMode = task.candidate_review_protocol === 'human_reviewed_sol_candidate_v1';
      // Papers extracted as entities open the relation graph; earlier extraction modes keep their pages.
      const entityMode = timelined(task);
      // An extracted paper is checked first; only a confirmed one is in the knowledge base and opens as a graph.
      const resultRenderer = standardMode ? renderStandardResult
        : entityMode ? (task.state === 'saved' ? renderEntityResult : renderEntityReview) : candidateMode ? renderDocumentCandidates
        : ['paper','patent'].includes(task.source_type) ? renderDocumentResult : renderResult;
      let mountedView;
      mountedView = resultRenderer({
        root: content,
        go,
        task,
        api,
        capabilities: state.capabilities,
        initialView,
        onEdit: () => go(standardParent ? '#standard/' + encodeURIComponent(standardParent) : "#regions"),
        onRefresh: async (updated) => {
          state.taskSignature = "";
          await update(updated);
        },
        onContinue: (revision) => go("#task/" + encodeURIComponent(revision.task_id)),
        onBusyChange: (busy) => {
          if (!candidateMode || version !== state.routeVersion || state.cleanup !== mountedView) return;
          candidateBusy = busy;
          candidateEpoch++;
        },
        onTaskChange: (updated) => {
          if (entityMode && version === state.routeVersion && updated.task_id === state.task?.task_id) {
            state.task = updated;
            observation?.refresh();
            if (updated.state === 'saved') { state.taskSignature = ''; route(); }
            return;
          }
          if (!candidateMode || version !== state.routeVersion || state.cleanup !== mountedView || updated.source_id !== state.task?.source_id) return;
          candidateEpoch++;
          state.task = updated;
          state.taskSignature = taskSignature(updated);
          if (taskId !== updated.task_id) {
            taskId = updated.task_id;
            history.replaceState(null, '', '#task/' + encodeURIComponent(taskId));
          }
        },
      });
      state.cleanup = mountedView;
      observing(task)?.attach(task, main);
      if (candidateMode) candidateView = mountedView;
      return;
    }
    state.taskSignature = signature;
    main.replaceChildren(
      heading(
        task.title || stored("kb-title:" + task.source_id) || "正在整理资料",
        "处理在后台继续，离开页面后也可从“我的资料”返回。",
      ),
    );
    const panel = el("section", "panel processing-panel"), unknownOutcome = modelOutcomeUnknown(task.error);
    // A task with a timeline shows it instead of the summary and the stage bar; the timeline keeps itself current.
    if (!observing(task)?.attach(task, panel)) panel.append(...processingSummary(task, STATUSES[task.state] || "处理中"));
    else if (unknownOutcome) panel.append(...processingSummary(task, STATUSES[task.state] || "处理中").slice(-1));
    if (!timelined(task) && task.processing) {
      panel.append(processingView(task.processing));
      panel.append(el("p", "muted", "进度表示当前处理阶段；阶段完成不等于整篇完成，解析结果仍需确认。"));
      for (const document of task.processing.documents || []) panel.append(el("p", "muted", document.name));
    }
    let standardParent = null;
    if (task.source_type === 'experimental') {
      try { const draft = await api('/api/table-drafts/' + encodeURIComponent(task.source_id));
        if (version !== state.routeVersion) return;
        standardParent = draft.recipe?.standard_review?.parent_source_id || null;
      } catch (error) {
        if (version !== state.routeVersion) return;
        if (![404, 410].includes(error.status)) throw error;
      }
    }
    if (task.source_type === "experimental" && !standardParent) {
      showRetired();
      return;
    }
    if (task.source_type === "experimental")
      panel.append(
        button(
          "返回修改读法",
          () => go(standardParent ? '#standard/' + encodeURIComponent(standardParent) : "#regions"),
          "secondary",
        ),
      );
    else if (!unknownOutcome && ["failed", "cancelled"].includes(task.state))
      panel.append(
        button(
          "重新整理",
          lock(async () => {
            await api("/api/tasks/" + id + "/retry", { method: "POST" });
            state.taskSignature = "";
            await update();
          }),
          "primary",
        ),
      );
    if (["queued", "reading", "extracting", "validating"].includes(task.state))
      panel.append(
        button(
          "取消本次整理",
          lock(async () => {
            await api("/api/tasks/" + id + "/cancel", { method: "POST" });
            state.taskSignature = "";
            await update();
          }),
          "quiet",
        ),
      );
    main.append(panel);
  };
  await update();
  if (retired || version !== state.routeVersion) return;
  const poll = async () => {
    if (version !== state.routeVersion) return;
    try {
      if (document.visibilityState !== "hidden") await update();
    } catch (e) {
      message(friendly(e), "error");
    }
    if (version === state.routeVersion && !retired)
      state.pollTimer = setTimeout(poll, 4000);
  };
  state.pollTimer = setTimeout(poll, 4000);
}
async function route() {
  state.routeVersion++;
  state.observationCleanup?.();
  state.observationCleanup = null;
  state.cleanup?.();
  state.cleanup = null;
  clearTimeout(state.pollTimer);
  message("");
  const parts = location.hash.slice(1).split("?")[0].split("/");
  const routeQuery = ['research-chat', 'table-chat'].includes(parts[0])
    ? new URLSearchParams(location.hash.split('?')[1] || '') : null;
  if(parts[0]==='table-chat'&&(!parts[1]||parts[1]==='new')) {
    if(parts[2])routeQuery.set('table',decodeURIComponent(parts[2]));
    parts.splice(0,parts.length,'research-chat','new');
    history.replaceState(null,'','#research-chat/new'+(routeQuery.toString()?'?'+routeQuery.toString():''));
  }
  const knownPages = ['research-chat', 'table-chat', 'table-analysis', 'draft', 'regions', 'standard', 'meaning', 'standard-preview', 'sol-preview', 'library', 'wiki', 'query', 'task', 'documents', 'graph', 'source'];
  const defaultPage = state.capabilities?.document_extraction_mode === 'program-only-v1' ? 'documents' : 'regions';
  const page = parts[0] === 'query-legacy' ? 'query' : knownPages.includes(parts[0]) ? parts[0] : defaultPage;
  if (parts[0] === 'query-legacy') history.replaceState(null, '', '#query');
  else if (!knownPages.includes(parts[0])) {
    history.replaceState(null, '', '#' + defaultPage);
    parts.splice(1);
  }
  state.activePage = page;
  const isNewChatRoute=['table-chat','research-chat'].includes(page)&&(!parts[1]||parts[1]==='new');
  shell.setCurrentPage(page,{newChat:isNewChatRoute});
  document
    .querySelectorAll("[data-page]")
    .forEach(/** @param {HTMLElement} a */ (a) =>
      a.setAttribute(
        "aria-current",
        a.dataset.page ===
          (page === "draft" ? "regions" : page === "task" ? "library" : ['table-analysis','standard','meaning','standard-preview','sol-preview'].includes(page) ? 'regions' : page)
          && (!['table-chat','research-chat'].includes(a.dataset.page)||isNewChatRoute)
          ? "page"
          : "false",
      ),
    );
  try {
    if (page === 'research-chat') {
      main.hidden=false;chatRoot.hidden=true;state.chatController?.setVisible(false);
      const version=state.routeVersion,root=el('section','research-chat-host');main.replaceChildren(root);
      const chatId=parts[1]&&parts[1]!=='new'?decodeURIComponent(parts[1]):null;
      shell.setCurrentChat(chatId,'research');
      const mounted=mountResearchChat({root,api,go,chatId,tableVersionId:routeQuery.get('table'),sourceId:routeQuery.get('source'),isCurrent:()=>state.routeVersion===version,
        replaceRoute:hash=>{if(state.routeVersion===version)history.replaceState(null,'',hash);},
        onStateChange:value=>{if(state.routeVersion!==version)return;shell.setCurrentChat(value.chatId,'research');shell.setCurrentPage('research-chat',{newChat:!value.chatId});shell.refreshHistory(api);}});
      state.cleanup=mounted.dispose;await mounted.ready;
    } else if (page === 'table-chat') {
      if(state.capabilities?.table_chat_workflow !== 'table-chat-v1')throw Error('当前服务尚未提供对话分析。');
      const query=new URLSearchParams(location.hash.split('?')[1]||''),chatId=parts[1]&&parts[1]!=='new'?decodeURIComponent(parts[1]):null,
        tableVersionId=parts[1]==='new'&&parts[2]?decodeURIComponent(parts[2]):null,
        key=chatId||'new:'+(tableVersionId||'')+':'+(query.get('source')||'');
      if(state.chatController&&state.chatKey!==key){state.chatController.dispose();state.chatController=null;state.chatKey=null;state.chatEpoch++;}
      main.hidden=true;chatRoot.hidden=false;
      if(!state.chatController){
        const epoch=++state.chatEpoch,root=chatRoot;
        let controller;
        controller=mountTableChat({root,api,go,replaceRoute:hash=>{if(state.chatController===controller&&state.chatEpoch===epoch&&state.activePage==='table-chat')history.replaceState(null,'',hash);},chatId,tableVersionId,sourceId:query.get('source'),
          isCurrent:()=>state.chatController===controller&&state.chatEpoch===epoch&&state.activePage==='table-chat',
          onStateChange:value=>{if(state.chatController!==controller||state.chatEpoch!==epoch)return;if(value.chatId&&state.chatKey?.startsWith('new:'))state.chatKey=value.chatId;if(state.activePage==='table-chat'){shell.setCurrentChat(value.chatId,'table');shell.setCurrentPage('table-chat',{newChat:!value.chatId});}shell.refreshHistory(api);}});
        state.chatController=controller;state.chatKey=key;shell.setCurrentChat(chatId);shell.refreshHistory(api);
      }else {state.chatController.setVisible(true);shell.setCurrentChat(chatId,'table');shell.refreshHistory(api);}
    } else if(page==='wiki') {
      main.hidden=false;main.replaceChildren();chatRoot.hidden=true;state.chatController?.setVisible(false);
      const version=state.routeVersion,query=new URLSearchParams(location.hash.split('?')[1]||''),root=el('section','wiki-workspace');main.append(root);
      const {mountWikiWorkspace}=await import('./wiki-workspace.js');if(state.routeVersion!==version)return;
      const mounted=mountWikiWorkspace({root,api,go,pageId:parts[1]&&parts[1]!=='new'?decodeURIComponent(parts[1]):null,chatId:query.get('chat'),turnId:query.get('turn'),isCurrent:()=>state.routeVersion===version});
      state.cleanup=mounted.dispose;await mounted.ready;
    } else if (page === "draft") {
      main.hidden=false;chatRoot.hidden=true;state.chatController?.setVisible(false);
      await openDraft(decodeURIComponent(parts[1] || ''));
    } else if (page === 'regions') {
      main.hidden=false;chatRoot.hidden=true;state.chatController?.setVisible(false);
      const version = state.routeVersion;
      const root = el('section'); main.replaceChildren(root);
      state.cleanup = mountRegionReview({root, api, sourceId: parts[1] ? decodeURIComponent(parts[1]) : undefined,
        go, rawTableAnalysis: state.capabilities?.table_analysis_workflow === 'basic-table-analysis-v1', tableChat: state.capabilities?.table_chat_workflow === 'table-chat-v1', isCurrent: () => state.routeVersion === version}).dispose;
    } else if (page === 'table-analysis') {
      main.hidden=false;chatRoot.hidden=true;state.chatController?.setVisible(false);
      const version = state.routeVersion;
      const {mountRawTableAnalysis} = await import('./raw-table-analysis.js');
      if(state.routeVersion !== version)return;
      const root = el('section'); main.replaceChildren(root);
      state.cleanup = mountRawTableAnalysis({root,api,sourceId:decodeURIComponent(parts[1]||''),
        tableVersionId:parts[2]?decodeURIComponent(parts[2]):undefined,go,
        isCurrent:()=>state.routeVersion===version}).dispose;
    } else if (page === 'standard' || page === 'meaning' || page === 'standard-preview' || page === 'sol-preview') {
      main.hidden=false;chatRoot.hidden=true;state.chatController?.setVisible(false);
      const version = state.routeVersion;
      const root = el('section'); main.replaceChildren(root);
      state.cleanup = mountStandardReview({root,api,parentId:decodeURIComponent(parts[1]||''),go,stage:page==='sol-preview'?'payload':page==='standard-preview'?'reading':'meaning',
        isCurrent:()=>state.routeVersion===version}).dispose;
    } else if (page === 'source') {
      main.hidden=false;chatRoot.hidden=true;state.chatController?.setVisible(false);
      const root=el('section','entity-source-host');main.replaceChildren(root);
      state.cleanup=mountEvidenceSource({root,api,evidenceRef:parts[1]?decodeURIComponent(parts[1]):'',
        onClose:()=>go('#library')});
      await state.cleanup.ready;
    } else if (page === 'graph') {
      main.hidden=false;chatRoot.hidden=true;state.chatController?.setVisible(false);
      const root=el('section','entity-graph-host');main.replaceChildren(root);
      state.cleanup=mountKnowledgeGraph({root,api,entityId:parts[1]?decodeURIComponent(parts[1]):null});
    } else if (page === "library") {
      main.hidden=false;chatRoot.hidden=true;state.chatController?.setVisible(false);const version=state.routeVersion,root=el('section','workspace-library-host');main.replaceChildren(root);
      const mounted=mountWorkspaceLibrary({root,api,go,isCurrent:()=>state.routeVersion===version});
      state.cleanup=mounted.dispose;
    }
    else if (page === 'query') {
      main.hidden=false;chatRoot.hidden=true;state.chatController?.setVisible(false);
      const documents = state.capabilities?.document_extraction_mode === 'program-only-v1';
      main.replaceChildren(heading('查找已有数据', documents ? '查找已确认并保存的结果，查看对应原文。' : '按样品、指标与结果查找，并回到原格。'));
      const root = el('section'); main.append(root);
      state.cleanup = (documents ? mountDocumentQuery : mountRowQuery)({root,api,onOpenTask:id => go('#task/' + encodeURIComponent(id))}).dispose;
    }
    else if (page === "task") {main.hidden=false;chatRoot.hidden=true;state.chatController?.setVisible(false);await openTask(parts[1]);}
    else if (page === "documents") {main.hidden=false;chatRoot.hidden=true;state.chatController?.setVisible(false);renderDocumentUpload();}
  } catch (e) {
    message(friendly(e), "error");
    main.append(button("重试", () => route(), "secondary"));
  }
  if(!main.hidden)main.focus({ preventScroll: true });
}
window.addEventListener("hashchange", route);
window.addEventListener("beforeunload", () => {
  commitPendingInputs();
});
function configureTableNavigation(capabilities) {
  const nav=/** @type {HTMLElement} */ (document.querySelector('[data-page="table-chat"]'));
  if(nav)nav.hidden=capabilities.table_chat_workflow!=='table-chat-v1';
}
function configureDocumentNavigation(capabilities) {
  if (capabilities.document_extraction_mode !== 'program-only-v1') return;
  const upload = /** @type {HTMLAnchorElement} */ (document.querySelector('[data-page="regions"]'));
  const brand = /** @type {HTMLAnchorElement} */ (document.querySelector('.workspace-brand'));
  if (upload) {
    upload.href = '#documents';
    upload.dataset.page = 'documents';
    upload.textContent = '上传论文 / 专利';
  }
  if (brand) brand.href = '#documents';
}
async function boot() {
  try {
    state.capabilities = await api("/api/capabilities");
    configureDocumentNavigation(state.capabilities);
    configureTableNavigation(state.capabilities);
    document.getElementById("connection-status").textContent = "本机已连接";
    await shell.refreshHistory(api);
    await route();
  } catch (e) {
    document.getElementById("connection-status").textContent = "连接未就绪";
    document.getElementById("connection-status").classList.add('is-error');
    main.replaceChildren(
      heading("暂时无法连接", "请确认本机服务正在运行。"),
      button("重新连接", boot, "primary"),
    );
    message(friendly(e), "error");
  }
}
boot();
