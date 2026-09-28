/**
 * The review panel's button (upload-ingest-merge U10.4, and the pending_review decision):
 *   something ready            → "Add N to library", enabled
 *   only duplicates/rejections → "Done", enabled — the commit discards them and the batch can close
 *   nothing pending, files busy → "Waiting for N file(s)…", disabled
 *   nothing pending, all done  → no button
 */
export interface ReviewCounts {
  ready_to_add: number;
  pending_review: number;
  in_progress: number;
}

export interface ReviewAction {
  label: string;
  enabled: boolean;
  visible: boolean;
}

export function reviewAction({ ready_to_add, pending_review, in_progress }: ReviewCounts): ReviewAction {
  if (ready_to_add > 0) return { label: `Add ${ready_to_add} to library`, enabled: true, visible: true };
  if (pending_review > 0) return { label: 'Done', enabled: true, visible: true };
  if (in_progress > 0) {
    return { label: `Waiting for ${in_progress} file${in_progress === 1 ? '' : 's'}…`, enabled: false, visible: true };
  }
  return { label: '', enabled: false, visible: false };
}
