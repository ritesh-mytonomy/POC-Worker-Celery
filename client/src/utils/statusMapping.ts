/**
 * Server file statuses (LLD M2.5) → what a queue row shows — upload-ingest-merge design.md §3.
 * staged / uploading are not mapped: while bytes are moving the row keeps its own upload progress.
 */
export interface ServerFileStatus {
  status: string;
  status_message: string | null;
  entries_total: number | null;
  entries_done: number;
}

/** Counts from the review data for one file: candidates staged (processed) out of all its candidates. */
export interface FileCandidateCounts {
  processed: number;
  total: number;
}

export type StatusTone = 'progress' | 'success' | 'warning' | 'danger' | 'muted';

export interface StatusLabel {
  label: string;
  tone: StatusTone;
}

export const UPLOAD_CANCELLED = 'Upload cancelled';

export function mapServerStatus(file: ServerFileStatus, counts?: FileCandidateCounts): StatusLabel | null {
  switch (file.status) {
    case 'staged':
    case 'uploading':
      return null;
    case 'uploaded':
    case 'processing':
      return {
        label: file.entries_total
          ? `Checking… ${file.entries_done} of ${file.entries_total}`
          : 'Checking…',
        tone: 'progress',
      };
    case 'processed':
      return { label: 'Ready', tone: 'success' };
    case 'partial':
      return {
        label: counts ? `Partly ready — ${counts.processed} of ${counts.total}` : 'Partly ready',
        tone: 'warning',
      };
    case 'rejected':
      return { label: `Rejected — ${file.status_message ?? 'not accepted'}`, tone: 'danger' };
    case 'error':
      if (file.status_message === UPLOAD_CANCELLED) return { label: 'Cancelled', tone: 'muted' };
      if (counts && counts.processed > 0) {
        return { label: `Failed part-way — ${counts.processed} staged`, tone: 'danger' };
      }
      return { label: `Failed — ${file.status_message ?? 'could not be processed'}`, tone: 'danger' };
    default:
      return { label: file.status, tone: 'muted' };
  }
}
