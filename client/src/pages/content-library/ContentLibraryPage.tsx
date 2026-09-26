import { useEffect, useMemo, useState } from 'react';
import PageHeader from '@/components/layout/PageHeader';
import Button from '@/components/ui/Button';
import UploadDropzone from '@/components/ui/uploadFile/UploadDropzone';
import UploadQueue from '@/components/ui/uploadFile/UploadQueue';
import { formatBytes } from '@/utils/formatBytes';
import {
  duplicateQueueMessage,
  findQueueDuplicate,
  isSameFile,
} from '@/utils/duplicateCheck';
import {
  acceptedFileInput,
  getUploadableExtension,
  unsupportedFormatMessage,
  validateUploadFile,
} from '@/utils/contentValidation';
import { useUploadConfig } from '@/hooks/useUploadConfig';
import { useBatchStatus } from '@/hooks/useBatchStatus';
import ReviewPanel from '@/components/ui/uploadFile/ReviewPanel';
import { commitBatch } from '@/utils/reviewApi';
import { mapServerStatus, type StatusLabel } from '@/utils/statusMapping';
import type { UploadConfig } from '@/utils/uploadConfig';
import { canStartCloudUpload, isBlockedFromUpload, useUploadQueue } from '@/context/UploadQueueContext';
import { checkDuplicateUpload, fetchLibraryDocuments, getDownloadUrl, type LibraryDocument } from '@/utils/uploadsApi';
import StoredUploadsTable from '@/components/ui/uploadFile/StoredUploadsTable';
import type { UploadQueueItem } from '@/types/contentLibrary';
import { UploadIcon } from '@/components/ui/icons';

// Allowed types and the size limit come from the server (GET /api/v1/uploads/config), not a client setting.
const formatsLabel = (config: UploadConfig) => {
  const types = config.allowedTopLevelExt.map((ext) =>
    ext === 'zip' ? `ZIP (${config.allowedZipEntryExt.map((e) => e.toUpperCase()).join(', ')} only)` : ext.toUpperCase(),
  );
  return `Supported formats: ${types.join(', ')} up to ${formatBytes(config.maxUploadBytes)}`;
};

let idCounter = 0;
const createItemId = () => {
  idCounter += 1;
  return `upload-item-${Date.now()}-${idCounter}-${Math.random().toString(36).slice(2, 8)}`;
};

const ContentLibraryPage = () => {
  const {
    items: allItems,
    enqueueItems,
    updateItem,
    startCloudUpload,
    retryCloudUpload,
    removeItem,
    clearItems,
  } = useUploadQueue();
  const { config, error: configError } = useUploadConfig();

  const items = allItems.filter((item) => item.source === 'content-library');
  const libraryQueue = items.map((item) => ({ id: item.id, name: item.name, size: item.size }));

  // Server statuses for the rows, and the review panels (upload-ingest-merge U10.3, U10.4): a batch is polled
  // after its first complete, until nothing in it is in progress and none of its uploads is still running.
  const batchIds = useMemo(
    () => [...new Set(items.filter((i) => i.cloudUpload?.status === 'success').map((i) => i.cloudUpload!.batchId!))],
    [items],
  );
  const activeBatchIds = useMemo(
    () => new Set(items.filter((i) => i.cloudUpload?.status === 'uploading').map((i) => i.cloudUpload?.batchId ?? '')),
    [items],
  );
  const { batches, refresh } = useBatchStatus(batchIds, activeBatchIds);
  const serverStatuses = useMemo(() => {
    const statuses: Record<string, StatusLabel | null> = {};
    for (const item of items) {
      const view = item.cloudUpload?.batchId ? batches[item.cloudUpload.batchId] : undefined;
      const file = view?.batch.files.find((f) => f.file_id === item.cloudUpload?.fileId);
      if (!view || !file) continue;
      statuses[item.id] = mapServerStatus(file, view.counts[file.file_id]);
    }
    return statuses;
  }, [items, batches]);

  // The Library table (upload-ingest-merge U9.3): loaded on arrival, and again after every commit (version + 1).
  const [library, setLibrary] = useState<LibraryDocument[] | null>(null);
  const [libraryError, setLibraryError] = useState<string | undefined>();
  const [libraryVersion, setLibraryVersion] = useState(0);
  useEffect(() => {
    let active = true;
    fetchLibraryDocuments()
      .then((documents) => {
        if (!active) return;
        setLibrary(documents);
        setLibraryError(undefined);
      })
      .catch((err: unknown) => {
        if (active) setLibraryError(err instanceof Error ? err.message : 'Failed to load the library.');
      });
    return () => {
      active = false;
    };
  }, [libraryVersion]);

  const download = async (documentId: string) => {
    try {
      window.location.assign(await getDownloadUrl(documentId)); // Content-Disposition: attachment — stays here
    } catch (err) {
      setLibraryError(err instanceof Error ? err.message : 'Failed to prepare the download.');
    }
  };

  const addToLibrary = async (batchId: string) => {
    const result = await commitBatch(batchId);
    await refresh(batchId);
    setLibraryVersion((version) => version + 1);
    return result;
  };

  // Once a completed upload has actually been rendered here, it's
  // "acknowledged" — GlobalUploadWidget stops surfacing it on other pages.
  useEffect(() => {
    for (const item of items) {
      if (item.cloudUpload?.status === 'success' && !item.acknowledgedInWidget) {
        updateItem(item.id, { acknowledgedInWidget: true });
      }
    }
  }, [items, updateItem]);

  const applyValidationFailure = (id: string, errors: string[]) => {
    updateItem(id, {
      validated: true,
      validation: { valid: false, errors },
      status: 'validation-failed',
    });
  };

  const validateZipItem = async (id: string, file: File) => {
    const result = await validateUploadFile(file, config!);
    if (!result.valid) {
      applyValidationFailure(id, result.errors);
      return;
    }
    updateItem(id, {
      validated: true,
      validation: result,
      status: 'queued',
    });
  };

  const checkServerDuplicate = async (id: string, file: File): Promise<boolean> => {
    try {
      const result = await checkDuplicateUpload(file.name, file.size);
      if (result.duplicate) {
        applyValidationFailure(id, [result.message ?? `Duplicate file — ${file.name} has already been uploaded.`]);
        return true;
      }
    } catch {
      // Backend will reject on complete if the API is unreachable.
    }
    return false;
  };

  const processNewItem = async (item: UploadQueueItem) => {
    if (await checkServerDuplicate(item.id, item.file)) return;

    if (item.extension === 'zip') {
      await validateZipItem(item.id, item.file);
      return;
    }

    updateItem(item.id, {
      validated: true,
      validation: { valid: true, errors: [] },
      status: 'queued',
    });
  };

  const addFiles = (files: FileList) => {
    if (!config) return;
    const fileList = Array.from(files);
    const newItems: UploadQueueItem[] = fileList.map((file, index) => {
      const extension = getUploadableExtension(file.name, config);
      const errors: string[] = [];

      if (!extension) {
        errors.push(unsupportedFormatMessage(file.name, config));
      } else if (file.size === 0) {
        errors.push('File is empty.');
      } else if (file.size > config.maxUploadBytes) {
        errors.push(`File exceeds the maximum size of ${formatBytes(config.maxUploadBytes)}.`);
      } else if (
        findQueueDuplicate(libraryQueue, file) ||
        fileList.slice(0, index).some((other) => isSameFile(other, file))
      ) {
        errors.push(duplicateQueueMessage(file.name));
      }

      const hasError = errors.length > 0;

      return {
        id: createItemId(),
        file,
        name: file.name,
        extension,
        size: file.size,
        status: hasError ? 'validation-failed' : 'queued',
        source: 'content-library',
        validated: hasError,
        validation: hasError ? { valid: false, errors } : undefined,
      };
    });

    enqueueItems(newItems);

    for (const item of newItems) {
      if (item.status !== 'validation-failed') {
        void processNewItem(item);
      }
    }
  };

  const handleRemove = (id: string) => removeItem(id);

  const handleClearQueue = () => clearItems((item) => item.source === 'content-library');

  const handleUpload = () => {
    const pending = items.filter(canStartCloudUpload);
    const batchId = crypto.randomUUID(); // one batch per Upload click, sent with every initiate (D4)
    for (const item of pending) startCloudUpload(item, batchId);
  };

  const isUploading = items.some(
    (item) => !isBlockedFromUpload(item) && item.cloudUpload?.status === 'uploading',
  );
  const isCheckingFiles = items.some((item) => !item.validated && item.status !== 'validation-failed');
  const hasPendingUploads = items.some(canStartCloudUpload);

  return (
    <div className="flex min-h-full flex-col">
      <PageHeader
        title="Upload Content"
        subtitle="Add new patient guidance, training videos, and regulatory materials to queue verification."
      />

      <div className="flex flex-col gap-lg p-lg">
        <div className="rounded-lg border border-border bg-background p-lg">
          <p className="text-xs font-medium uppercase tracking-wide text-muted">Upload Files</p>
          <div className="mt-sm">
            {config ? (
              <UploadDropzone
                onFilesSelected={addFiles}
                accept={acceptedFileInput(config)}
                formatsLabel={formatsLabel(config)}
              />
            ) : (
              <p className={configError ? 'text-sm text-danger' : 'text-sm text-muted'}>
                {configError ?? 'Loading upload settings…'}
              </p>
            )}
          </div>
        </div>

        {items.length > 0 && (
          <div className="rounded-lg border border-border bg-background p-lg">
            <UploadQueue
              title="Uploaded Video & Documents"
              items={items}
              onRemove={handleRemove}
              onRetryCloudUpload={retryCloudUpload}
              serverStatuses={serverStatuses}
            />
          </div>
        )}

        <div className="flex items-center justify-between pt-md">
          <button
            type="button"
            onClick={handleClearQueue}
            className="text-sm font-medium text-muted hover:text-slate-900"
          >
            Clear Queue
          </button>

          <Button
            variant="primary"
            onClick={handleUpload}
            disabled={!hasPendingUploads || isUploading || isCheckingFiles}
            isLoading={isUploading || isCheckingFiles}
          >
            <UploadIcon className="h-4 w-4" />
            {isCheckingFiles ? 'Checking…' : isUploading ? 'Uploading…' : 'Upload'}
          </Button>
        </div>

        {[...batchIds].reverse().map(
          (batchId) =>
            batches[batchId] && (
              <ReviewPanel key={batchId} view={batches[batchId]} onCommit={() => addToLibrary(batchId)} />
            ),
        )}

        <div className="rounded-lg border border-border bg-background p-lg">
          <StoredUploadsTable
            documents={library ?? []}
            isLoading={library === null && !libraryError}
            error={libraryError}
            onDownload={(id) => void download(id)}
          />
        </div>
      </div>
    </div>
  );
};

export default ContentLibraryPage;
