import { act, renderHook, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { server } from '@/mocks/server';

const API = 'http://api.test';

describe('useBatchStatus', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('polls while a file is in progress, then stops', async () => {
    vi.stubEnv('VITE_SCAN_API_URL', API);
    vi.resetModules();
    const { useBatchStatus } = await import('@/hooks/useBatchStatus');
    let polls = 0;
    server.use(
      http.get(`${API}/api/v1/uploads/batches/b1`, () => {
        polls += 1;
        return HttpResponse.json({ batch_id: 'b1', files: [], ready_to_add: 0, pending_review: 0,
                                   in_progress: polls < 3 ? 1 : 0 });
      }),
      http.get(`${API}/api/v1/uploads/batches/b1/staged`, () => HttpResponse.json({ batch_id: 'b1', staged: [] })),
    );
    const { result } = renderHook(() => useBatchStatus(['b1'], new Set<string>(), 20));
    await waitFor(() => expect(result.current.batches.b1?.batch.in_progress).toBe(0));
    const settled = polls;
    await new Promise((resolve) => setTimeout(resolve, 150));             // several more intervals
    expect(settled).toBe(3);
    expect(polls).toBe(settled);                                           // stopped: no more requests
  });

  it('keeps polling a finished batch while one of its uploads is still running', async () => {
    vi.stubEnv('VITE_SCAN_API_URL', API);
    vi.resetModules();
    const { useBatchStatus } = await import('@/hooks/useBatchStatus');
    let polls = 0;
    server.use(
      http.get(`${API}/api/v1/uploads/batches/b1`, () => {
        polls += 1;
        return HttpResponse.json({ batch_id: 'b1', files: [], ready_to_add: 0, pending_review: 0, in_progress: 0 });
      }),
      http.get(`${API}/api/v1/uploads/batches/b1/staged`, () => HttpResponse.json({ batch_id: 'b1', staged: [] })),
    );
    renderHook(() => useBatchStatus(['b1'], new Set(['b1']), 20));
    await waitFor(() => expect(polls).toBeGreaterThanOrEqual(4));
  });

  it('keeps a file\'s last known counts after a commit deletes its candidates', async () => {
    vi.stubEnv('VITE_SCAN_API_URL', API);
    vi.resetModules();
    const { useBatchStatus } = await import('@/hooks/useBatchStatus');
    const { mapServerStatus } = await import('@/utils/statusMapping');
    const candidate = (i: number, status: 'processed' | 'rejected') => ({
      staged_id: `s${i}`, source_file_id: 'zip', source_entry_name: `e${i}`, entry_index: i, file_name: `e${i}`,
      status, reject_reason: status === 'rejected' ? 'bad' : null, content_hash: null, duplicate_of: null,
    });
    let committed = false;
    server.use(
      http.get(`${API}/api/v1/uploads/batches/b1`, () =>
        HttpResponse.json({ batch_id: 'b1', files: [], ready_to_add: committed ? 0 : 3,
                            pending_review: committed ? 0 : 5, in_progress: 0 })),
      http.get(`${API}/api/v1/uploads/batches/b1/staged`, () => HttpResponse.json({ batch_id: 'b1', staged: committed
        ? [] : [0, 1, 2].map((i) => candidate(i, 'processed')).concat([3, 4].map((i) => candidate(i, 'rejected'))) })),
    );
    const { result } = renderHook(() => useBatchStatus(['b1'], new Set<string>(), 20));
    await waitFor(() => expect(result.current.batches.b1?.counts.zip).toEqual({ processed: 3, total: 5 }));

    committed = true;
    await act(() => result.current.refresh('b1'));
    expect(result.current.batches.b1.staged).toEqual([]);
    expect(result.current.batches.b1.counts.zip).toEqual({ processed: 3, total: 5 });
    const file = { status: 'partial', status_message: null, entries_total: 5, entries_done: 5 };
    expect(mapServerStatus(file, result.current.batches.b1.counts.zip)?.label).toBe('Partly ready — 3 of 5');
  });
});
