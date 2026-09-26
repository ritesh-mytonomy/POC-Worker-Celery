import PageHeader from '@/components/layout/PageHeader';
import Button from '@/components/ui/Button';
import UploadDropzone from '@/components/ui/uploadFile/UploadDropzone';
import UploadQueue from '@/components/ui/uploadFile/UploadQueue';
import { useUploadQueue } from '@/context/UploadQueueContext';
import { createSampleFile } from '@/utils/generateSampleFile';
import type { UploadQueueItem } from '@/types/contentLibrary';

let idCounter = 0;
const createItemId = () => {
  idCounter += 1;
  return `poc5-item-${idCounter}`;
};

const SAMPLE_SIZES_MB = [50, 250, 1024];

/**
 * POC 5: Large File Upload with ReactJS.
 *
 * Standalone from the Content Library's clinical scan flow — this page is
 * only about the upload mechanic itself: any file type, streamed directly to
 * S3 in chunks (Client/src/utils/s3ChunkedUpload.ts) with live progress and
 * retry, never fully loaded into memory. See POC5_Large_File_Upload.md at
 * the repo root for the full write-up.
 *
 * Uploads are added to the app-wide UploadQueueContext (mounted in
 * DashboardLayout) rather than page-local state, so a large upload started
 * here keeps running — visible in the GlobalUploadWidget — after navigating
 * to another page.
 */
const Poc5Page = () => {
  const { items: allItems, enqueueItems, startCloudUpload, retryCloudUpload, removeItem, clearItems } =
    useUploadQueue();

  const items = allItems.filter((item) => item.source === 'poc5');

  const addFiles = (files: FileList | File[]) => {
    const newItems: UploadQueueItem[] = Array.from(files).map((file) => ({
      id: createItemId(),
      file,
      name: file.name,
      extension: null,
      size: file.size,
      status: 'queued',
      source: 'poc5',
      validated: true,
      validation: { valid: true, errors: [] },
    }));

    enqueueItems(newItems);
    for (const item of newItems) startCloudUpload(item);
  };

  const addSampleFile = (sizeMB: number) => {
    addFiles([createSampleFile(sizeMB)]);
  };

  const handleRemove = (id: string) => removeItem(id);

  const handleClearQueue = () => clearItems((item) => item.source === 'poc5');

  return (
    <div className="flex min-h-full flex-col">
      <PageHeader
        title="POC 5: Large File Upload"
        subtitle="Chunked, direct-to-S3 uploads with progress, retry, and no full-file memory buffering."
      />

      <div className="flex flex-col gap-lg p-lg">
        <div className="rounded-lg border border-border bg-background p-lg">
          <p className="text-xs font-medium uppercase tracking-wide text-muted">Upload Files</p>
          <div className="mt-sm">
            <UploadDropzone onFilesSelected={addFiles} accept="*/*" formatsLabel="Any file type or size" />
          </div>
        </div>

        <div className="rounded-lg border border-border bg-background p-lg">
          <p className="text-xs font-medium uppercase tracking-wide text-muted">
            Performance validation — generate a sample file in-browser
          </p>
          <p className="mt-xs text-xs text-muted">
            Builds a synthetic file client-side (never fully materialized in memory) so upload
            performance can be checked without sourcing a real large file.
          </p>
          <div className="mt-sm flex flex-wrap gap-sm">
            {SAMPLE_SIZES_MB.map((sizeMB) => (
              <Button key={sizeMB} variant="secondary" size="sm" onClick={() => addSampleFile(sizeMB)}>
                Upload {sizeMB >= 1024 ? `${sizeMB / 1024} GB` : `${sizeMB} MB`} sample
              </Button>
            ))}
          </div>
        </div>

        {items.length > 0 && (
          <div className="rounded-lg border border-border bg-background p-lg">
            <UploadQueue
              title="Uploads"
              items={items}
              onRemove={handleRemove}
              onRetryCloudUpload={retryCloudUpload}
            />
          </div>
        )}

        {items.length > 0 && (
          <div className="flex items-center justify-between pt-md">
            <button
              type="button"
              onClick={handleClearQueue}
              className="text-sm font-medium text-muted hover:text-slate-900"
            >
              Clear Queue
            </button>
          </div>
        )}
      </div>
    </div>
  );
};

export default Poc5Page;
