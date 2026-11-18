import {mountDocumentObservation} from './document-observation.mjs';
import {modelOutcomeUnknown} from './document-processing-logic.mjs';

const ACTIVE = ['queued', 'reading', 'extracting', 'validating'];

/** Whether the task is a paper or patent extracted as entities: the only kind that keeps a timeline. */
export const hasTimeline = task => ['paper', 'patent'].includes(task.source_type) &&
  task.document_extraction_mode === 'entity-graph-v1';

/** What the task page currently shows for such a task: the running attempt, the draft under review, or the saved result. */
function viewKey(task) {
  if (task.package_sha256) return `${task.task_id}:${task.package_sha256}:${task.state === 'saved' ? 'saved' : 'review'}`;
  return `${task.task_id}:${ACTIVE.includes(task.state) ? 'active' : task.state}:${modelOutcomeUnknown(task.error)}`;
}

/**
 * The timeline of one open task page. It is mounted once and moved between renders, so progress polling never
 * remounts it (or the editable review beside it). `state.observationCleanup` disposes it when the route changes.
 */
export function taskObservation({api, state, mount = mountDocumentObservation}) {
  let observation = null, shown = null;
  return {
    /** True when the page already shows this task in the same phase: only the timeline needs to update. */
    unchanged: task => shown !== null && shown === viewKey(task),
    attach(task, target) {
      if (!observation) {
        observation = mount({taskId: task.task_id, api});
        state.observationCleanup = () => { observation?.dispose(); observation = null; shown = null; };
      }
      target.append(observation.root);
      shown = viewKey(task);
      return true;
    },
    refresh: () => observation?.refresh(),
  };
}
