import { describe, expect, it } from 'vitest';
import { reviewAction } from '@/utils/reviewAction';

describe('reviewAction — the review panel button', () => {
  it('offers "Add N to library" while anything is ready, even with files still busy', () => {
    expect(reviewAction({ ready_to_add: 4, pending_review: 6, in_progress: 0 })).toEqual({
      label: 'Add 4 to library', enabled: true, visible: true,
    });
    expect(reviewAction({ ready_to_add: 1, pending_review: 1, in_progress: 2 }).label).toBe('Add 1 to library');
  });

  it('offers "Done" when only duplicates or rejections remain', () => {
    expect(reviewAction({ ready_to_add: 0, pending_review: 2, in_progress: 0 })).toEqual({
      label: 'Done', enabled: true, visible: true,
    });
  });

  it('waits, disabled, while files are still processing and nothing is pending', () => {
    expect(reviewAction({ ready_to_add: 0, pending_review: 0, in_progress: 1 })).toEqual({
      label: 'Waiting for 1 file…', enabled: false, visible: true,
    });
    expect(reviewAction({ ready_to_add: 0, pending_review: 0, in_progress: 3 }).label).toBe('Waiting for 3 files…');
  });

  it('shows no button once everything is committed', () => {
    expect(reviewAction({ ready_to_add: 0, pending_review: 0, in_progress: 0 }).visible).toBe(false);
  });
});
