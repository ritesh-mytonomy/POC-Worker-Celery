import { renderHook, waitFor } from '@testing-library/react';
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
});
