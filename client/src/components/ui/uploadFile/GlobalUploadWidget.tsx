import { useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { formatBytes } from '@/utils/formatBytes';
import { useUploadQueue } from '@/context/UploadQueueContext';
import ProgressBar from '@/components/ui/ProgressBar';
import { AlertIcon, CheckCircleIcon, CloudIcon, RefreshIcon } from '@/components/ui/icons';
import type { UploadQueueItem } from '@/types/contentLibrary';

const CONTENT_LIBRARY_PATH = '/dashboard/content-library';

const PAGE_LABEL: Record<UploadQueueItem['source'], string> = {
  'content-library': 'Content Library',
  poc5: 'POC 5',
};

/**
 * Floating panel for every upload in the shared queue — uploading, failed,
 * and completed — shown on every page except Content Library itself, which
 * already renders the same items in its own inline queue. Clicking a
 * finished upload's "Success" button takes you there.
 *
 * Once ContentLibraryPage has actually rendered a completed upload in its
 * own list (see its acknowledgedInWidget effect), that item stops showing
 * here — otherwise a finished upload the user already went and looked at
 * would keep following them around the app forever.
 */
const GlobalUploadWidget = () => {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const { items, retryCloudUpload, removeItem } = useUploadQueue();
  const [collapsed, setCollapsed] = useState(false);

  const uploading = items.filter((item) => item.cloudUpload?.status === 'uploading');
  const failed = items.filter((item) => item.cloudUpload?.status === 'failed');
  const success = items.filter((item) => item.cloudUpload?.status === 'success' && !item.acknowledgedInWidget);
  const visible = [...uploading, ...failed, ...success];

  if (visible.length === 0 || pathname === CONTENT_LIBRARY_PATH) return null;

  const goToContentLibrary = () => navigate(CONTENT_LIBRARY_PATH);

  const summary =
    uploading.length > 0
      ? `Uploading ${uploading.length} file${uploading.length === 1 ? '' : 's'}…`
      : failed.length > 0
        ? `${failed.length} upload${failed.length === 1 ? '' : 's'} failed`
        : `${success.length} upload${success.length === 1 ? '' : 's'} complete`;

  const overallPercent = uploading.length
    ? Math.round(uploading.reduce((sum, item) => sum + (item.cloudUpload?.percent ?? 0), 0) / uploading.length)
    : 100;

  return (
    <div className="fixed bottom-4 right-4 z-50 w-80 rounded-lg border border-border bg-background shadow-lg">
      <button
        type="button"
        onClick={() => setCollapsed((prev) => !prev)}
        className="flex w-full items-center gap-sm rounded-lg px-md py-sm text-left"
        aria-expanded={!collapsed}
      >
        <CloudIcon className="h-4 w-4 shrink-0 text-slate-600" />
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-slate-900">{summary}</span>
        <span className="shrink-0 text-xs text-muted">{collapsed ? 'Show' : 'Hide'}</span>
      </button>

      {!collapsed && (
        <div className="max-h-80 overflow-y-auto border-t border-border">
          {uploading.length > 0 && (
            <div className="px-md pt-sm">
              <ProgressBar value={overallPercent} variant="primary" label="Overall upload progress" />
            </div>
          )}
          <ul className="divide-y divide-border">
            {visible.map((item) => {
              const cloud = item.cloudUpload!;
              const isUploading = cloud.status === 'uploading';
              const isFailed = cloud.status === 'failed';
              const isSuccess = cloud.status === 'success';

              return (
                <li key={item.id}>
                  <div className="flex items-start gap-sm px-md py-sm">
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-xs font-medium text-slate-900" title={item.name}>
                        {item.name}
                      </p>
                      <p className="text-xs text-muted">
                        {PAGE_LABEL[item.source]} · {formatBytes(item.size)}
                      </p>
                      {isUploading && (
                        <div className="mt-xs flex items-center gap-sm">
                          <ProgressBar
                            value={cloud.percent}
                            variant="primary"
                            label={`${item.name} upload progress`}
                            className="flex-1"
                          />
                          <span className="shrink-0 text-xs text-muted">{cloud.percent}%</span>
                        </div>
                      )}
                      {isFailed && (
                        <p className="mt-xs truncate text-xs text-danger" title={cloud.error}>
                          {cloud.error ?? 'Upload failed.'}
                        </p>
                      )}
                      {isSuccess && (
                        <button
                          type="button"
                          onClick={goToContentLibrary}
                          className="mt-xs inline-flex items-center gap-xs rounded-md bg-success/10 px-sm py-xs text-xs font-medium text-success hover:bg-success/20"
                        >
                          <CheckCircleIcon className="h-3.5 w-3.5" />
                          Success
                        </button>
                      )}
                    </div>

                    <div className="flex shrink-0 items-center gap-xs">
                      {isFailed && (
                        <>
                          <AlertIcon className="h-4 w-4 text-danger" />
                          <button
                            type="button"
                            onClick={() => retryCloudUpload(item.id)}
                            aria-label={`Retry ${item.name}`}
                            className="rounded-md p-xs text-muted hover:bg-surface hover:text-slate-900"
                          >
                            <RefreshIcon className="h-3.5 w-3.5" />
                          </button>
                        </>
                      )}
                      {!isUploading && (
                        <button
                          type="button"
                          onClick={() => removeItem(item.id)}
                          aria-label={`Dismiss ${item.name}`}
                          className="rounded-md px-xs text-sm text-muted hover:bg-surface hover:text-slate-900"
                        >
                          ×
                        </button>
                      )}
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
};

export default GlobalUploadWidget;
