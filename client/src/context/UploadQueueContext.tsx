import { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { isDuplicateCloudError } from '@/utils/duplicateCheck';
import { S3MultipartUploader, S3UploadAborted, type S3UploadResult } from '@/utils/s3ChunkedUpload';
import type { CloudUploadState, UploadQueueItem } from '@/types/contentLibrary';

/**
 * Whole-app upload queue: mounted once in DashboardLayout (which stays alive
 * across every /dashboard/* route since the router only swaps its <Outlet>
 * children) rather than inside a page component. That's what lets an S3
 * upload keep running — and stay visible in <GlobalUploadWidget /> — while
 * the user navigates to a different page; a page-local useState/useRef pair
 * would be torn down on unmount and abandon the upload's UI state (the
 * in-flight XHRs themselves have no relation to React's lifecycle either
 * way, but nothing would be left to show their progress or complete them).
 */

export const isBlockedFromUpload = (item: UploadQueueItem): boolean =>
  item.status === 'validation-failed' ||
  item.validation?.valid === false ||
  !item.extension ||
  !item.validated;

export const canStartCloudUpload = (item: UploadQueueItem): boolean => {
  if (isBlockedFromUpload(item)) return false;
  const cloud = item.cloudUpload;
  return !cloud || cloud.status === 'idle' || cloud.status === 'failed';
};

interface UploadQueueContextValue {
  items: UploadQueueItem[];
  enqueueItems: (items: UploadQueueItem[]) => void;
  updateItem: (id: string, patch: Partial<UploadQueueItem>) => void;
  startCloudUpload: (item: UploadQueueItem, batchId?: string) => void;
  retryCloudUpload: (id: string) => void;
  removeItem: (id: string) => void;
  clearItems: (predicate: (item: UploadQueueItem) => boolean) => void;
}

const UploadQueueContext = createContext<UploadQueueContextValue | null>(null);

export const UploadQueueProvider = ({ children }: { children: ReactNode }) => {
  const [items, setItems] = useState<UploadQueueItem[]>([]);
  const uploadersRef = useRef(new Map<string, S3MultipartUploader>());

  const patchCloudUpload = useCallback(
    (id: string, updater: (cloudUpload: CloudUploadState) => CloudUploadState) => {
      setItems((prev) =>
        prev.map((item) => {
          if (item.id !== id) return item;
          if (isBlockedFromUpload(item)) return item;
          const current: CloudUploadState =
            item.cloudUpload ?? { status: 'idle', uploadedBytes: 0, totalBytes: item.size, percent: 0 };
          return { ...item, cloudUpload: updater(current) };
        }),
      );
    },
    [],
  );

  const handleCloudUploadSettled = useCallback(
    (id: string, run: Promise<S3UploadResult>) => {
      run
        .then((result) => {
          uploadersRef.current.delete(id);
          patchCloudUpload(id, (cloudUpload) => ({
            ...cloudUpload,
            status: 'success',
            percent: 100,
            uploadedBytes: cloudUpload.totalBytes,
            key: result.key,
            location: result.location,
            fileId: result.id, // joins the server's file statuses to this row
            batchId: result.batchId ?? cloudUpload.batchId,
          }));
          setItems((prev) =>
            prev.map((item) =>
              item.id === id && !isBlockedFromUpload(item) ? { ...item, status: 'success' } : item,
            ),
          );
        })
        .catch((err: unknown) => {
          if (err instanceof S3UploadAborted) return;
          const message = err instanceof Error ? err.message : 'Cloud upload failed.';
          const isDuplicate = isDuplicateCloudError(message);

          patchCloudUpload(id, (cloudUpload) => ({ ...cloudUpload, status: 'failed', error: message }));
          setItems((prev) =>
            prev.map((item) => {
              if (item.id !== id || isBlockedFromUpload(item)) return item;
              if (isDuplicate) {
                return {
                  ...item,
                  status: 'validation-failed',
                  validation: { valid: false, errors: [message] },
                };
              }
              return { ...item, status: 'upload-failed' };
            }),
          );
        });
    },
    [patchCloudUpload],
  );

  const startCloudUpload = useCallback(
    (item: UploadQueueItem, batchId?: string) => {
      if (isBlockedFromUpload(item)) return;
      // One batch per Upload click (D4); a retry of this row keeps the batch it started in.
      const batch = batchId ?? item.cloudUpload?.batchId;

      const existing = uploadersRef.current.get(item.id);
      if (existing && item.cloudUpload?.status === 'failed') {
        patchCloudUpload(item.id, (cloudUpload) => ({ ...cloudUpload, status: 'uploading', error: undefined }));
        setItems((prev) => prev.map((row) => (row.id === item.id ? { ...row, status: 'queued' } : row)));
        handleCloudUploadSettled(item.id, existing.retry());
        return;
      }

      const uploader = new S3MultipartUploader(item.file, {
        onProgress: (progress) =>
          patchCloudUpload(item.id, (cloudUpload) => ({
            ...cloudUpload,
            status: 'uploading',
            uploadedBytes: progress.uploadedBytes,
            totalBytes: progress.totalBytes,
            percent: progress.percent,
          })),
        batchId: batch,
      });
      uploadersRef.current.set(item.id, uploader);
      patchCloudUpload(item.id, (cloudUpload) => ({ ...cloudUpload, status: 'uploading', error: undefined, batchId: batch }));
      handleCloudUploadSettled(item.id, uploader.start());
    },
    [handleCloudUploadSettled, patchCloudUpload],
  );

  const cancelCloudUpload = useCallback((id: string) => {
    uploadersRef.current.get(id)?.cancel();
    uploadersRef.current.delete(id);
  }, []);

  const enqueueItems = useCallback((newItems: UploadQueueItem[]) => {
    setItems((prev) => [...prev, ...newItems]);
  }, []);

  const updateItem = useCallback((id: string, patch: Partial<UploadQueueItem>) => {
    setItems((prev) => prev.map((item) => (item.id === id ? { ...item, ...patch } : item)));
  }, []);

  const retryCloudUpload = useCallback(
    (id: string) => {
      setItems((prev) => {
        const item = prev.find((row) => row.id === id);
        if (item && !isBlockedFromUpload(item)) startCloudUpload(item);
        return prev;
      });
    },
    [startCloudUpload],
  );

  const removeItem = useCallback(
    (id: string) => {
      cancelCloudUpload(id);
      setItems((prev) => prev.filter((item) => item.id !== id));
    },
    [cancelCloudUpload],
  );

  const clearItems = useCallback(
    (predicate: (item: UploadQueueItem) => boolean) => {
      setItems((prev) => {
        for (const item of prev) if (predicate(item)) cancelCloudUpload(item.id);
        return prev.filter((item) => !predicate(item));
      });
    },
    [cancelCloudUpload],
  );

  const value = useMemo<UploadQueueContextValue>(
    () => ({ items, enqueueItems, updateItem, startCloudUpload, retryCloudUpload, removeItem, clearItems }),
    [items, enqueueItems, updateItem, startCloudUpload, retryCloudUpload, removeItem, clearItems],
  );

  return <UploadQueueContext.Provider value={value}>{children}</UploadQueueContext.Provider>;
};

export const useUploadQueue = (): UploadQueueContextValue => {
  const ctx = useContext(UploadQueueContext);
  if (!ctx) throw new Error('useUploadQueue must be used within a UploadQueueProvider.');
  return ctx;
};
