/**
 * What the server allows — GET /api/v1/uploads/config, the ONE source of truth for the client's file checks
 * (upload-ingest-merge 6.2). When Product decides D2 only the server's settings change; nothing here.
 */
const API_BASE = import.meta.env.VITE_SCAN_API_URL;

export interface UploadConfig {
  allowedTopLevelExt: string[];
  allowedZipEntryExt: string[];
  maxUploadBytes: number;
  maxZipFolderDepth: number;
}

interface UploadConfigResponse {
  allowed_top_level_ext: string[];
  allowed_zip_entry_ext: string[];
  max_upload_bytes: number;
  max_zip_folder_depth: number;
}

export async function fetchUploadConfig(): Promise<UploadConfig> {
  const response = await fetch(`${API_BASE}/api/v1/uploads/config`);
  if (!response.ok) {
    throw new Error(`Could not load the upload settings (HTTP ${response.status}).`);
  }
  const body = (await response.json()) as UploadConfigResponse;
  return {
    allowedTopLevelExt: body.allowed_top_level_ext,
    allowedZipEntryExt: body.allowed_zip_entry_ext,
    maxUploadBytes: body.max_upload_bytes,
    maxZipFolderDepth: body.max_zip_folder_depth,
  };
}
