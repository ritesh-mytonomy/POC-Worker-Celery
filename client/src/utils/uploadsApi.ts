const API_BASE = import.meta.env.VITE_SCAN_API_URL;

export interface StoredUpload {
  id: string;
  filename: string;
  size_bytes: number;
  content_type: string | null;
  s3_key: string;
  s3_location: string | null;
  status: string;
  parent_id: string | null;
  source_path: string | null;
  created_at: string;
}

export async function fetchStoredUploads(): Promise<StoredUpload[]> {
  if (!API_BASE) return [];

  const response = await fetch(`${API_BASE}/api/uploads`);
  if (!response.ok) {
    throw new Error(`Failed to load uploads (HTTP ${response.status}).`);
  }
  return response.json() as Promise<StoredUpload[]>;
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
