import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import ReviewPanel from '@/components/ui/uploadFile/ReviewPanel';
import type { BatchView } from '@/hooks/useBatchStatus';
import type { BatchFile, StagedCandidate } from '@/utils/reviewApi';

const file = (id: string, name: string, status: string, message: string | null = null): BatchFile => ({
  file_id: id, file_name: name, status, status_message: message, entries_total: 3, entries_done: 3,
  attempt_count: 1, detected_type: 'zip',
});

const candidate = (id: string, fileId: string, name: string, extra: Partial<StagedCandidate> = {}): StagedCandidate => ({
  staged_id: id, source_file_id: fileId, source_entry_name: name, entry_index: 0, file_name: name.split('/').pop()!,
  status: 'processed', reject_reason: null, content_hash: 'ab'.repeat(32), duplicate_of: null, ...extra,
});

const view = (counts: { ready_to_add: number; pending_review: number; in_progress: number },
              files: BatchFile[], staged: StagedCandidate[]): BatchView => ({
  batch: { batch_id: 'b1', files, ...counts }, staged, counts: {},
});

describe('ReviewPanel', () => {
  it('lists ready, duplicate with the matching title, rejected with its reason, and offers "Add N to library"', () => {
    render(
      <ReviewPanel
        onCommit={vi.fn()}
        view={view({ ready_to_add: 1, pending_review: 3, in_progress: 0 }, [file('f1', 'mixed.zip', 'partial')], [
          candidate('s1', 'f1', 'doc1.docx'),
          candidate('s2', 'f1', 'guide.docx', { duplicate_of: { document_id: 'd', title: 'Pre-op Guide', kind: 'same_content' } }),
          candidate('s3', 'f1', 'notes.pdf', { status: 'rejected', reject_reason: '.pdf is not supported' }),
        ])}
      />,
    );
    expect(screen.getByText('doc1.docx')).toBeInTheDocument();
    expect(screen.getByText(/Already in the library as “Pre-op Guide”/)).toBeInTheDocument();
    expect(screen.getByText(/\.pdf is not supported/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add 1 to library' })).toBeEnabled();
  });

  it('says so when an archive failed part-way, and still offers its staged entries', () => {
    render(
      <ReviewPanel
        onCommit={vi.fn()}
        view={view({ ready_to_add: 2, pending_review: 2, in_progress: 0 },
          [file('f1', 'big.zip', 'error', 'Could not be processed after 3 attempts')],
          [candidate('s1', 'f1', 'a.docx'), candidate('s2', 'f1', 'b.docx')])}
      />,
    );
    expect(screen.getByRole('alert')).toHaveTextContent('big.zip stopped part-way: 2 entries were staged');
    expect(screen.getByRole('button', { name: 'Add 2 to library' })).toBeEnabled();
  });

  it('offers "Done" when only a duplicate and a rejection remain', () => {
    render(
      <ReviewPanel
        onCommit={vi.fn()}
        view={view({ ready_to_add: 0, pending_review: 2, in_progress: 0 }, [file('f1', 'x.zip', 'partial')], [
          candidate('s1', 'f1', 'a.docx', { duplicate_of: { document_id: 'd', title: 'A', kind: 'same_title' } }),
          candidate('s2', 'f1', 'n.pdf', { status: 'rejected', reject_reason: '.pdf is not supported' }),
        ])}
      />,
    );
    expect(screen.getByRole('button', { name: 'Done' })).toBeEnabled();
    expect(screen.getByText(/A document titled “A” is already in the library/)).toBeInTheDocument();
  });

  it('waits, disabled, while a file is still processing', () => {
    render(<ReviewPanel onCommit={vi.fn()} view={view({ ready_to_add: 0, pending_review: 0, in_progress: 1 },
      [file('f1', 'x.docx', 'processing')], [])} />);
    expect(screen.getByRole('button', { name: 'Waiting for 1 file…' })).toBeDisabled();
  });

  it('commits on click and shows what was added and skipped', async () => {
    const onCommit = vi.fn().mockResolvedValue({
      added: 1, still_in_progress: 0, documents: ['d1'],
      skipped: [{ staged_id: 's2', file_name: 'guide.docx', reason: 'Already in the library' }],
    });
    render(
      <ReviewPanel
        onCommit={onCommit}
        view={view({ ready_to_add: 1, pending_review: 2, in_progress: 0 }, [file('f1', 'x.zip', 'processed')], [
          candidate('s1', 'f1', 'doc1.docx'),
          candidate('s2', 'f1', 'guide.docx', { duplicate_of: { document_id: 'd', title: 'G', kind: 'same_content' } }),
        ])}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: 'Add 1 to library' }));
    expect(onCommit).toHaveBeenCalledOnce();
    expect(await screen.findByRole('status')).toHaveTextContent('Added 1 · skipped 1: guide.docx (Already in the library)');
  });
});
