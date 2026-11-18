import {createObservationView} from './document-observation-view.mjs';
import {subscribeObservation} from './document-observation-stream.mjs';

/** Isolated read-only monitor, independent of the editable entity result view. */
export function mountDocumentObservation({taskId,api}) {
  const view=createObservationView({taskId});
  const stream=subscribeObservation({taskId,api,onSnapshot:value=>view.update(value),onClock:value=>view.updateClock(value),
    onConnection:connection=>view.setConnection(connection.state,connection.reason)});
  const timer=setInterval(()=>view.tick(),1000);
  Reflect.get(Object(timer), 'unref')?.call(timer);  // a Node timer must not keep a test process alive
  return {root:view.root,refresh:stream.refresh,dispose(){clearInterval(timer);stream.dispose();view.dispose();}};
}
