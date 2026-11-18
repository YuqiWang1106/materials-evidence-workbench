import {renderRowResult} from './row-result-view.js';

/** Standard results use the server's selected-result projection and existing save/source flow. */
export function renderStandardResult(options){
 return renderRowResult({...options,standardMode:true});
}
