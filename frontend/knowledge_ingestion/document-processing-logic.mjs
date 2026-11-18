/** New and historical unknown outcomes both require checking the retained request. */
export function modelOutcomeUnknown(error) {
  return typeof error === 'string' && (error.startsWith('model_outcome_unknown:') ||
    error.startsWith('model_response_lost_after_retries:'));
}
