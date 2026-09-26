const API_BASE = import.meta.env.VITE_SCAN_API_URL;

/** A document in the library — GET /api/v1/library/documents (upload-ingest-merge U9). */
export interface LibraryDocument {
  document_id: string;
  title: string;
  file_name: string;
  file_ext: string;
  size_bytes: number;
  created_at: string;
}

export async function fetchLibraryDocuments(): Promise<LibraryDocument[]> {
  if (!API_BASE) return [];

  const response = await fetch(`${API_BASE}/api/v1/library/documents`);
  if (!response.ok) {
    throw new Error(`Failed to load the library (HTTP ${response.status}).`);
  }
  return ((await response.json()) as { documents: LibraryDocument[] }).documents;
}

/** A short-lived presigned download URL that saves the document under its file name. */
export async function getDownloadUrl(documentId: string): Promise<string> {
  const response = await fetch(`${API_BASE}/api/v1/library/documents/${documentId}/download`);
  if (!response.ok) {
    throw new Error(`Failed to prepare the download (HTTP ${response.status}).`);
  }
  return ((await response.json()) as { url: string }).url;
}

export interface DuplicateCheckResult {
  duplicate: boolean;
  message?: string;
}

export async function checkDuplicateUpload(
  filename: string,
  fileSize: number,
): Promise<DuplicateCheckResult> {
  if (!API_BASE) return { duplicate: false };

  const response = await fetch(`${API_BASE}/api/uploads/check-duplicate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename, fileSize }),
  });

  if (!response.ok) {
    throw new Error(`Duplicate check failed (HTTP ${response.status}).`);
  }

  return response.json() as Promise<DuplicateCheckResult>;
}
