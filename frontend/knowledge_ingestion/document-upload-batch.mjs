// @ts-check
/**
 * One immutable source/task per PDF; retries reuse submitted tasks and source IDs.
 * @param {{
 *   batch: readonly File[],
 *   ids: string[],
 *   type: string,
 *   api: typeof import('./app.js').api,
 *   submitted: Map<File, {type: string, sourceId: string, task?: Record<string, any>}>,
 *   active?: () => boolean,
 *   onProgress?: (completed: number, total: number) => void,
 * }} options
 */
export async function submitDocumentBatch({batch, ids, type, api, submitted, active = () => true, onProgress = () => {}}) {
  const pdfs = batch.map((file, i) => ({file, id: ids[i]})).filter(({file}) => /[.]pdf$/i.test(file.name || ''));
  if (!pdfs.length) throw new Error('请选择 PDF 原件，说明文件请随对应 PDF 一起上传。');
  if (pdfs.length > 1 && pdfs.length !== batch.length) throw new Error('多篇 PDF 的说明文件归属不明确，请将每篇 PDF 与其说明文件分别上传。');
  const tasks = [];
  for (let i = 0; i < pdfs.length; i++) {
    if (!active()) break;
    const {file, id} = pdfs[i];
    let entry = submitted.get(file);
    if (!entry) { entry = {type, sourceId: 'document-' + crypto.randomUUID().replaceAll('-', '')}; submitted.set(file, entry); }
    if (entry.type !== type) throw new Error('部分资料已提交，请保持原资料类型继续，或到资料库查看已提交任务。');
    if (!entry.task) entry.task = await api('/api/tasks', {method: 'POST', json: {
      source_id: entry.sourceId, source_type: type, asset_ids: pdfs.length === 1 ? ids : [id], options: {}, expected_revision: 0,
    }});
    tasks.push(entry.task); onProgress(tasks.length, pdfs.length);
  }
  return tasks;
}
