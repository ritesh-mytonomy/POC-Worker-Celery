import { describe, expect, it } from 'vitest';
import { mapServerStatus, type ServerFileStatus } from '@/utils/statusMapping';

const file = (status: string, extra: Partial<ServerFileStatus> = {}): ServerFileStatus => ({
  status,
  status_message: null,
  entries_total: null,
  entries_done: 0,
  ...extra,
});

describe('mapServerStatus (design.md §3)', () => {
  it('leaves staged and uploading to the row’s own upload progress', () => {
    expect(mapServerStatus(file('staged'))).toBeNull();
    expect(mapServerStatus(file('uploading'))).toBeNull();
  });

  it('shows Checking…, with entry progress for an archive', () => {
    expect(mapServerStatus(file('uploaded'))).toEqual({ label: 'Checking…', tone: 'progress' });
    expect(mapServerStatus(file('processing', { entries_total: 30, entries_done: 12 }))).toEqual({
      label: 'Checking… 12 of 30',
      tone: 'progress',
    });
  });

  it('shows Ready, and Partly ready with the staged count', () => {
    expect(mapServerStatus(file('processed'))).toEqual({ label: 'Ready', tone: 'success' });
    expect(mapServerStatus(file('partial'), { processed: 3, total: 5 })).toEqual({
      label: 'Partly ready — 3 of 5',
      tone: 'warning',
    });
    expect(mapServerStatus(file('partial'))?.label).toBe('Partly ready');
  });

  it('shows Rejected with the reason', () => {
    expect(mapServerStatus(file('rejected', { status_message: 'File is damaged and cannot be read' }))).toEqual({
      label: 'Rejected — File is damaged and cannot be read',
      tone: 'danger',
    });
  });

  it('tells a cancelled upload, an archive that failed part-way and a plain failure apart', () => {
    expect(mapServerStatus(file('error', { status_message: 'Upload cancelled' }))).toEqual({
      label: 'Cancelled',
      tone: 'muted',
    });
    expect(mapServerStatus(file('error', { status_message: 'x' }), { processed: 3, total: 3 })).toEqual({
      label: 'Failed part-way — 3 staged',
      tone: 'danger',
    });
    expect(mapServerStatus(file('error', { status_message: 'Could not be processed after 3 attempts' }))).toEqual({
      label: 'Failed — Could not be processed after 3 attempts',
      tone: 'danger',
    });
  });
});
