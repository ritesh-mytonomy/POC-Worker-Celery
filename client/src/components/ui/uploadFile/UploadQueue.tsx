import UploadQueueRow from '@/components/ui/uploadFile/UploadQueueRow';
import type { UploadQueueItem } from '@/types/contentLibrary';
import type { StatusLabel } from '@/utils/statusMapping';

interface UploadQueueProps {
  title: string;
  items: UploadQueueItem[];
  onRemove: (id: string) => void;
  onRetryCloudUpload?: (id: string) => void;
  /** Server processing status per queue item id (upload-ingest-merge 6.2). */
  serverStatuses?: Record<string, StatusLabel | null>;
}

const UploadQueue = ({ title, items, onRemove, onRetryCloudUpload, serverStatuses }: UploadQueueProps) => {
  return (
    <div>
      <h3 className="text-sm font-bold text-slate-900">
        {title} ({items.length})
      </h3>
      <ul className="mt-sm divide-y divide-border">
        {items.map((item) => (
          <UploadQueueRow
            key={item.id}
            item={item}
            onRemove={onRemove}
            onRetryCloudUpload={onRetryCloudUpload}
            serverStatus={serverStatuses?.[item.id]}
          />
        ))}
      </ul>
    </div>
  );
};

export default UploadQueue;
