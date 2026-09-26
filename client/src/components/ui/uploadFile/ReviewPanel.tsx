import { useState } from 'react';
import Button from '@/components/ui/Button';
import type { BatchView } from '@/hooks/useBatchStatus';
import { reviewAction } from '@/utils/reviewAction';
import type { CommitResult, StagedCandidate } from '@/utils/reviewApi';

interface ReviewPanelProps {
  view: BatchView;
  onCommit: () => Promise<CommitResult>;
}

const duplicateText = (candidate: StagedCandidate) =>
  candidate.duplicate_of?.kind === 'same_title'
    ? `A document titled “${candidate.duplicate_of.title}” is already in the library`
    : `Already in the library as “${candidate.duplicate_of?.title}”`;

/**
 * One batch's review (upload-ingest-merge U10.4): what is ready, what was rejected and why, what duplicates the
 * library, and which archives stopped part-way — with "Add N to library", or "Done" when only duplicates or
 * rejections remain. After a commit it says what was added and skipped.
 */
const ReviewPanel = ({ view, onCommit }: ReviewPanelProps) => {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<CommitResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { batch, staged } = view;

  const ready = staged.filter((c) => c.status === 'processed' && !c.duplicate_of);
  const rejected = staged.filter((c) => c.status === 'rejected');
  const duplicates = staged.filter((c) => c.status === 'processed' && c.duplicate_of);
  // Decision 1: an archive that errored part-way still offers the entries it staged — say that it stopped.
  const stoppedArchives = batch.files
    .filter((f) => f.status === 'error' && f.status_message !== 'Upload cancelled')
    .map((f) => ({ file: f, staged: staged.filter((c) => c.source_file_id === f.file_id && c.status === 'processed') }))
    .filter(({ staged: entries }) => entries.length > 0);
  const action = reviewAction(batch);

  if (staged.length === 0 && !result && !action.visible) return null;

  const commit = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await onCommit());
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Adding to the library failed.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-label="Review" className="rounded-lg border border-border bg-background p-lg">
      <h3 className="text-sm font-bold text-slate-900">Review</h3>

      {stoppedArchives.map(({ file, staged: entries }) => (
        <p key={file.file_id} role="alert" className="mt-sm text-xs font-medium text-danger">
          {file.file_name} stopped part-way: {entries.length} {entries.length === 1 ? 'entry was' : 'entries were'}{' '}
          staged, the rest were not checked.
        </p>
      ))}

      {ready.length > 0 && (
        <div className="mt-sm">
          <p className="text-xs font-medium uppercase tracking-wide text-muted">Ready ({ready.length})</p>
          <ul className="mt-xs text-sm text-slate-900">
            {ready.map((c) => (
              <li key={c.staged_id}>{c.source_entry_name ?? c.file_name}</li>
            ))}
          </ul>
        </div>
      )}

      {duplicates.length > 0 && (
        <div className="mt-sm">
          <p className="text-xs font-medium uppercase tracking-wide text-muted">Duplicate ({duplicates.length})</p>
          <ul className="mt-xs text-sm">
            {duplicates.map((c) => (
              <li key={c.staged_id}>
                <span className="text-slate-900">{c.source_entry_name ?? c.file_name}</span>{' '}
                <span className="text-xs text-muted">— {duplicateText(c)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {rejected.length > 0 && (
        <div className="mt-sm">
          <p className="text-xs font-medium uppercase tracking-wide text-muted">Rejected ({rejected.length})</p>
          <ul className="mt-xs text-sm">
            {rejected.map((c) => (
              <li key={c.staged_id}>
                <span className="text-slate-900">{c.source_entry_name ?? c.file_name}</span>{' '}
                <span className="text-xs text-danger">— {c.reject_reason}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {result && (
        <p className="mt-sm text-xs text-success" role="status">
          Added {result.added}
          {result.skipped.length > 0 &&
            ` · skipped ${result.skipped.length}: ${result.skipped.map((s) => `${s.file_name} (${s.reason})`).join('; ')}`}
        </p>
      )}
      {error && (
        <p className="mt-sm text-xs text-danger" role="alert">
          {error}
        </p>
      )}

      {action.visible && (
        <div className="mt-md flex justify-end">
          <Button variant="primary" onClick={commit} disabled={!action.enabled || busy} isLoading={busy}>
            {action.label}
          </Button>
        </div>
      )}
    </section>
  );
};

export default ReviewPanel;
