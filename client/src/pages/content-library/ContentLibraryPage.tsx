import { useEffect } from 'react';
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
  ACCEPTED_FILE_INPUT,
  getUploadableExtension,
  unsupportedFormatMessage,
  validateUploadFile,
} from '@/utils/contentValidation';
import { canStartCloudUpload, isBlockedFromUpload, useUploadQueue } from '@/context/UploadQueueContext';
import { checkDuplicateUpload } from '@/utils/uploadsApi';
import type { UploadQueueItem } from '@/types/contentLibrary';
import { UploadIcon } from '@/components/ui/icons';

const MAX_FILE_SIZE_MB = import.meta.env.VITE_MAX_UPLOAD_FILE_SIZE_MB;
const MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB ? Number(MAX_FILE_SIZE_MB) * 1024 * 1024 : undefined;

const FORMATS_LABEL = `Supported formats: DOCX, PDF, HTML, ZIP (PDF only)${
  MAX_FILE_SIZE_BYTES ? ` up to ${formatBytes(MAX_FILE_SIZE_BYTES)}` : ''
}`;

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

  const items = allItems.filter((item) => item.source === 'content-library');
  const libraryQueue = items.map((item) => ({ id: item.id, name: item.name, size: item.size }));

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
    const result = await validateUploadFile(file);
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
    const fileList = Array.from(files);
    const newItems: UploadQueueItem[] = fileList.map((file, index) => {
      const extension = getUploadableExtension(file.name);
      const errors: string[] = [];

      if (!extension) {
        errors.push(unsupportedFormatMessage(file.name));
      } else if (file.size === 0) {
        errors.push('File is empty.');
      } else if (
        MAX_FILE_SIZE_BYTES &&
        file.size > MAX_FILE_SIZE_BYTES &&
        extension !== 'bin' &&
        extension !== 'dat'
      ) {
        errors.push(`File exceeds the maximum size of ${formatBytes(MAX_FILE_SIZE_BYTES)}.`);
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
    for (const item of pending) startCloudUpload(item);
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
            <UploadDropzone
              onFilesSelected={addFiles}
              accept={ACCEPTED_FILE_INPUT}
              formatsLabel={FORMATS_LABEL}
            />
          </div>
        </div>

        {items.length > 0 && (
          <div className="rounded-lg border border-border bg-background p-lg">
            <UploadQueue
              title="Uploaded Video & Documents"
              items={items}
              onRemove={handleRemove}
              onRetryCloudUpload={retryCloudUpload}
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
      </div>
    </div>
  );
};

export default ContentLibraryPage;
