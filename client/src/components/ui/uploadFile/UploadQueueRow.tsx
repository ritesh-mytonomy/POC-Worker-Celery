import { cn } from '@/utils/cn';
import { formatBytes } from '@/utils/formatBytes';
import ProgressBar from '@/components/ui/ProgressBar';
import ScanResults from '@/components/ui/uploadFile/ScanResults';
import {
  AlertIcon,
  CheckCircleIcon,
  DocumentIcon,
  RefreshIcon,
  TrashIcon,
  VideoIcon,
} from '@/components/ui/icons';
import { isDuplicateCloudError, isDuplicateError } from '@/utils/duplicateCheck';
import type { UploadQueueItem } from '@/types/contentLibrary';

interface UploadQueueRowProps {
  item: UploadQueueItem;
  onRemove: (id: string) => void;
  onRetryCloudUpload?: (id: string) => void;
}

const CloudUploadStatusLine = ({
  item,
  onRetryCloudUpload,
}: {
  item: UploadQueueItem;
  onRetryCloudUpload?: (id: string) => void;
}) => {
  const cloudUpload = item.cloudUpload;
  if (!cloudUpload || item.status === 'validation-failed') return null;

  if (cloudUpload.status === 'uploading') {
    return (
      <div className="mt-sm flex flex-col gap-xs">
        <div className="flex items-center gap-sm">
          <ProgressBar
            value={cloudUpload.percent}
            variant="primary"
            label={`${item.name} upload progress`}
            className="flex-1"
          />
          <span className="shrink-0 text-xs font-medium text-slate-900">{cloudUpload.percent}%</span>
          <span className="shrink-0 text-xs text-muted">
            {formatBytes(cloudUpload.uploadedBytes)} / {formatBytes(cloudUpload.totalBytes)}
          </span>
        </div>
        {cloudUpload.percent >= 100 && (
          <p className="text-xs text-muted">Finalizing file on the server…</p>
        )}
      </div>
    );
  }

  if (cloudUpload.status === 'success') {
    return null;
  }

  if (cloudUpload.status === 'failed') {
    return (
      <div className="mt-xs flex items-center gap-sm">
        <p className="truncate text-xs text-danger" title={cloudUpload.error}>
          Cloud upload failed{cloudUpload.error ? `: ${cloudUpload.error}` : '.'}
        </p>
        {onRetryCloudUpload && (
          <button
            type="button"
            onClick={() => onRetryCloudUpload(item.id)}
            className="flex shrink-0 items-center gap-xs text-xs font-medium text-danger underline hover:no-underline"
          >
            <RefreshIcon className="h-3.5 w-3.5" />
            Retry
          </button>
        )}
      </div>
    );
  }

  return null;
};

const UploadQueueRow = ({ item, onRemove, onRetryCloudUpload }: UploadQueueRowProps) => {
  const hasVideoSections = Boolean(item.validation?.videoSections?.length);
  const isChecking = !item.validated && item.status !== 'validation-failed';
  const isCloudUploading = item.status !== 'validation-failed' && item.cloudUpload?.status === 'uploading';
  const isQueued = item.status === 'queued' && !isCloudUploading && !isChecking;
  const isScanning = item.status === 'scanning';
  const isSuccess = item.status === 'success';
  const isFailed = item.status === 'validation-failed' || item.status === 'upload-failed';
  const isDuplicate =
    isDuplicateError(item.validation?.errors) ||
    isDuplicateCloudError(item.cloudUpload?.error);
  const scanProgress =
    item.scan && item.scan.totalChecks
      ? Math.round((item.scan.sections.length / item.scan.totalChecks) * 100)
      : 0;

  return (
    <li className="flex items-start gap-md py-md">
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-surface text-muted">
        {hasVideoSections ? <VideoIcon className="h-4 w-4" /> : <DocumentIcon className="h-4 w-4" />}
      </span>

      <div className="min-w-0 flex-1">
        <div className="flex items-start justify-between gap-md">
          <div className="min-w-0 flex-1">
            <p
              className={cn(
                'truncate text-sm font-medium',
                isQueued ? 'text-muted' : 'text-slate-900',
                isSuccess && 'text-slate-900',
                isDuplicate && 'text-danger',
              )}
            >
              {item.name}
            </p>
            {!isScanning && (
              <p className="text-xs text-muted">
                {formatBytes(item.size)}
                {hasVideoSections &&
                  ` · ${item.validation!.videoSections!.length} video section${item.validation!.videoSections!.length === 1 ? '' : 's'}`}
                {item.validation?.zipEntries &&
                  ` · ${item.validation.zipEntries.filter((entry) => entry.valid).length}/${item.validation.zipEntries.length} documents valid`}
              </p>
            )}
            {isChecking && (
              <p className="text-xs text-muted">
                {item.extension === 'zip' ? 'Checking ZIP contents…' : 'Checking file…'}
              </p>
            )}
            {item.status === 'validation-failed' && item.validation?.errors && (
              <p className="truncate text-xs text-danger">{item.validation.errors.join(' ')}</p>
            )}
            {item.status === 'upload-failed' && item.scan?.error && (
              <p className="truncate text-xs text-danger" title={item.scan.error}>
                {item.scan.error}
              </p>
            )}
            {item.scan && <ScanResults scan={item.scan} />}
            <CloudUploadStatusLine item={item} onRetryCloudUpload={onRetryCloudUpload} />
          </div>

          <div className="shrink-0 text-right">
            {isSuccess && (
              <span className="flex items-center justify-end gap-xs text-xs font-medium text-success">
                <CheckCircleIcon className="h-4 w-4" />
                {item.scan && item.scan.status !== 'unsupported' ? 'Scan complete' : 'Uploaded'}
              </span>
            )}

            {isCloudUploading && (
              <span className="text-xs text-muted">
                {item.cloudUpload && item.cloudUpload.percent >= 100 ? 'Finalizing…' : 'Uploading…'}
              </span>
            )}
            {isChecking && <span className="text-xs text-muted">Checking…</span>}
            {isQueued && <span className="text-xs text-muted">Queued for upload</span>}

            {isFailed && (
              <span
                className="flex items-center justify-end gap-xs text-xs font-medium text-danger"
                title={item.validation?.errors.join(' ')}
              >
                <AlertIcon className="h-4 w-4 shrink-0" />
                {isDuplicate
                  ? 'Duplicate'
                  : item.status === 'validation-failed'
                    ? item.validation?.errors.some((error) => error.includes('not a supported format'))
                      ? 'Not supported'
                      : 'Validation failed'
                    : 'Upload failed'}
              </span>
            )}
          </div>
        </div>

        {isScanning && (
          <div className="mt-sm flex items-center gap-sm">
            <ProgressBar
              value={scanProgress}
              variant="accent"
              label={`${item.name} scan progress`}
              className="flex-1"
            />
            <span className="shrink-0 text-xs font-medium text-slate-900">{scanProgress}%</span>
            <span className="shrink-0 text-xs text-muted">{formatBytes(item.size)}</span>
          </div>
        )}
      </div>

      <button
        type="button"
        onClick={() => onRemove(item.id)}
        disabled={isScanning || isCloudUploading}
        aria-label={`Remove ${item.name}`}
        className="shrink-0 rounded-md p-xs text-muted transition-colors hover:bg-surface hover:text-danger disabled:cursor-not-allowed disabled:opacity-50"
      >
        <TrashIcon className="h-4 w-4" />
      </button>
    </li>
  );
};

export default UploadQueueRow;
