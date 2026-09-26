export type SupportedExtension = 'docx' | 'pdf' | 'html' | 'zip';

export type UploadableExtension = SupportedExtension;

export type UploadStatus = 'queued' | 'success' | 'validation-failed' | 'upload-failed';

export interface VideoSection {
  id: string;
  index: number;
  title: string;
}

export interface ZipEntryResult {
  id: string;
  path: string;
  fileName: string;
  valid: boolean;
  error?: string;
  videoSections?: VideoSection[];
}

export interface FileValidationResult {
  valid: boolean;
  errors: string[];
  videoSections?: VideoSection[];
  zipEntries?: ZipEntryResult[];
}

export type CloudUploadStatus = 'idle' | 'uploading' | 'success' | 'failed' | 'canceled';

/**
 * Progress/result of streaming this file directly to S3 in chunks (see
 * `Client/src/utils/s3ChunkedUpload.ts`).
 */
export interface CloudUploadState {
  status: CloudUploadStatus;
  uploadedBytes: number;
  totalBytes: number;
  percent: number;
  error?: string;
  key?: string;
  location?: string;
}

/** Which page added this item — lets a page show only its own uploads while the queue itself is shared app-wide. */
export type UploadSource = 'content-library';

export interface UploadQueueItem {
  id: string;
  file: File;
  name: string;
  extension: UploadableExtension | null;
  size: number;
  status: UploadStatus;
  source: UploadSource;
  /** Whether the deep "Validate Files" pass has run for this item. */
  validated: boolean;
  validation?: FileValidationResult;
  /** Direct-to-S3 chunked upload progress/result, set for every valid file. */
  cloudUpload?: CloudUploadState;
  /** Set once the user has visited Content Library and seen this upload listed there — GlobalUploadWidget stops showing it once true. */
  acknowledgedInWidget?: boolean;
}
