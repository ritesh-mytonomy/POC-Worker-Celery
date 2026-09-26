import { formatBytes } from '@/utils/formatBytes';
import type { LibraryDocument } from '@/utils/uploadsApi';

interface StoredUploadsTableProps {
  documents: LibraryDocument[];
  isLoading?: boolean;
  error?: string;
  onDownload: (documentId: string) => void;
}

/**
 * The Library table on the Content Library page — Anugrah's stored-uploads table, adapted to library documents
 * (GET /api/v1/library/documents, newest first) with a download per row (upload-ingest-merge D18, U9.3).
 */
const StoredUploadsTable = ({ documents, isLoading, error, onDownload }: StoredUploadsTableProps) => {
  return (
    <div>
      <h3 className="text-sm font-bold text-slate-900">Library ({documents.length})</h3>
      {isLoading && <p className="mt-sm text-xs text-muted">Loading…</p>}
      {error && <p className="mt-sm text-xs text-danger">{error}</p>}
      {!isLoading && !error && documents.length === 0 && (
        <p className="mt-sm text-xs text-muted">No documents in the library yet. Add some from a review above.</p>
      )}
      {documents.length > 0 && (
        <div className="mt-sm overflow-x-auto">
          <table className="w-full min-w-[32rem] text-left text-sm">
            <thead>
              <tr className="border-b border-border text-xs uppercase tracking-wide text-muted">
                <th className="py-sm pr-md font-medium">Title</th>
                <th className="py-sm pr-md font-medium">File name</th>
                <th className="py-sm pr-md font-medium">Type</th>
                <th className="py-sm pr-md font-medium">File size</th>
                <th className="py-sm pr-md font-medium">Added</th>
                <th className="py-sm font-medium">
                  <span className="sr-only">Download</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {documents.map((doc) => (
                <tr key={doc.document_id}>
                  <td className="max-w-[12rem] truncate py-sm pr-md font-medium text-slate-900" title={doc.title}>
                    {doc.title}
                  </td>
                  <td className="max-w-[12rem] truncate py-sm pr-md text-xs text-muted" title={doc.file_name}>
                    {doc.file_name}
                  </td>
                  <td className="py-sm pr-md text-xs uppercase text-muted">{doc.file_ext}</td>
                  <td className="whitespace-nowrap py-sm pr-md text-slate-900">{formatBytes(doc.size_bytes)}</td>
                  <td className="whitespace-nowrap py-sm pr-md text-muted">
                    {new Date(doc.created_at).toLocaleString()}
                  </td>
                  <td className="py-sm">
                    <button
                      type="button"
                      onClick={() => onDownload(doc.document_id)}
                      className="text-xs font-medium text-primary underline hover:no-underline"
                      aria-label={`Download ${doc.title}`}
                    >
                      Download
                    </button>
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
