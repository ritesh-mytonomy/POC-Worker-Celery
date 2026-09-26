import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchBatch, fetchStaged, type BatchStatus, type StagedCandidate } from '@/utils/reviewApi';

export interface BatchView {
  batch: BatchStatus;
  staged: StagedCandidate[];
}

export const POLL_MS = 2000;

/**
 * Polls each batch (its files' statuses and its review list) every ~2 s after its first complete (U10.3).
 * A batch stops being polled once nothing in it is in progress and none of its uploads is still running
 * (`activeBatchIds`); it is fetched again on demand — `refresh(id)`, e.g. after a commit.
 */
export const useBatchStatus = (
  batchIds: string[],
  activeBatchIds: ReadonlySet<string>,
  intervalMs: number = POLL_MS,
): { batches: Record<string, BatchView>; refresh: (batchId: string) => Promise<void> } => {
  const [batches, setBatches] = useState<Record<string, BatchView>>({});
  const latest = useRef(batches); // the poll's view of the last results, updated after each render
  useEffect(() => {
    latest.current = batches;
  }, [batches]);

  const refresh = useCallback(async (batchId: string) => {
    try {
      const [batch, staged] = await Promise.all([fetchBatch(batchId), fetchStaged(batchId)]);
      setBatches((prev) => ({ ...prev, [batchId]: { batch, staged } }));
    } catch {
      // a missed poll is retried on the next tick
    }
  }, []);

  const key = batchIds.join(',');
  const activeKey = [...activeBatchIds].sort().join(',');

  useEffect(() => {
    const needsPoll = (id: string) => {
      const view = latest.current[id];
      return !view || view.batch.in_progress > 0 || activeBatchIds.has(id);
    };
    const tick = () => {
      for (const id of batchIds) if (needsPoll(id)) void refresh(id);
    };
    tick();
    const timer = setInterval(tick, intervalMs);
    return () => clearInterval(timer);
    // batchIds / activeBatchIds are compared by content (key, activeKey), not identity
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, activeKey, intervalMs, refresh]);

  return { batches, refresh };
};
