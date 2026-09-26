import { formatBytes } from '@/utils/formatBytes';
import type { StoredUpload } from '@/utils/uploadsApi';

interface StoredUploadsTableProps {
  uploads: StoredUpload[];
  isLoading?: boolean;
  error?: string;
}

const StoredUploadsTable = ({ uploads, isLoading, error }: StoredUploadsTableProps) => {
  return (
    <div>
      <h3 className="text-sm font-bold text-slate-900">Stored documents ({uploads.length})</h3>
      {isLoading && <p className="mt-sm text-xs text-muted">Loading…</p>}
      {error && <p className="mt-sm text-xs text-danger">{error}</p>}
      {!isLoading && !error && uploads.length === 0 && (
        <p className="mt-sm text-xs text-muted">No documents stored yet. Upload a file to see it here.</p>
      )}
      {uploads.length > 0 && (
        <div className="mt-sm overflow-x-auto">
          <table className="w-full min-w-[32rem] text-left text-sm">
            <thead>
              <tr className="border-b border-border text-xs uppercase tracking-wide text-muted">
                <th className="py-sm pr-md font-medium">Document name</th>
                <th className="py-sm pr-md font-medium">ID</th>
                <th className="py-sm pr-md font-medium">File size</th>
                <th className="py-sm pr-md font-medium">Source</th>
                <th className="py-sm font-medium">Uploaded</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {uploads.map((upload) => (
                <tr key={upload.id}>
                  <td className="max-w-[12rem] truncate py-sm pr-md font-medium text-slate-900" title={upload.filename}>
                    {upload.filename}
                  </td>
                  <td className="max-w-[10rem] truncate py-sm pr-md font-mono text-xs text-muted" title={upload.id}>
                    {upload.id}
                  </td>
                  <td className="whitespace-nowrap py-sm pr-md text-slate-900">
                    {formatBytes(upload.size_bytes)}
                  </td>
                  <td className="max-w-[10rem] truncate py-sm pr-md text-xs text-muted" title={upload.source_path ?? undefined}>
                    {upload.parent_id
                      ? upload.source_path ?? 'From ZIP'
                      : upload.status === 'extracted'
                        ? 'ZIP (extracted)'
                        : upload.status === 'extracting'
                          ? 'ZIP (extracting…)'
                          : upload.status === 'extract_failed'
                            ? 'ZIP (extract failed)'
                            : 'Direct upload'}
                  </td>
                  <td className="whitespace-nowrap py-sm text-muted">
                    {new Date(upload.created_at).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};

export default StoredUploadsTable;
