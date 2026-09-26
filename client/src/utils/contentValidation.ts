import * as JSZip from 'jszip';
import * as mammoth from 'mammoth';
import type {
  FileValidationResult,
  SupportedExtension,
  VideoSection,
  ZipEntryResult,
} from '@/types/contentLibrary';
import type { UploadConfig } from '@/utils/uploadConfig';

const VIDEO_SECTION_REGEX = /Video\s+(\d+)\s*-\s*([^\n:]+):/gi;

export function getFileExtension(fileName: string): string | null {
  const match = /\.([a-z0-9]+)$/i.exec(fileName.trim());
  return match?.[1]?.toLowerCase() ?? null;
}

export function getSupportedExtension(fileName: string, config: UploadConfig): SupportedExtension | null {
  const extension = getFileExtension(fileName);

  if (!extension) return null;
  const normalized = extension === 'htm' ? 'html' : extension;
  if (config.allowedTopLevelExt.includes(normalized)) return normalized as SupportedExtension;

  return null;
}

export function getUploadableExtension(fileName: string, config: UploadConfig): SupportedExtension | null {
  return getSupportedExtension(fileName, config);
}

/** The file picker's accept list, from the server's allowed types (e.g. ".docx,.zip"). */
export function acceptedFileInput(config: UploadConfig): string {
  return config.allowedTopLevelExt.flatMap((ext) => (ext === 'html' ? ['.html', '.htm'] : [`.${ext}`])).join(',');
}

export function unsupportedFormatMessage(fileName: string, config: UploadConfig): string {
  const extension = getFileExtension(fileName);
  if (extension) return `.${extension} is not a supported format`;
  return `Unsupported file type. Supported formats: ${config.allowedTopLevelExt.map((ext) => ext.toUpperCase()).join(', ')}.`;
}

export function extractVideoSections(text: string): VideoSection[] {
  const sections: VideoSection[] = [];
  const regex = new RegExp(VIDEO_SECTION_REGEX);
  let match: RegExpExecArray | null;

  while ((match = regex.exec(text)) !== null) {
    sections.push({
      id: `video-${match[1]}-${sections.length}`,
      index: Number(match[1]),
      title: match[2].trim(),
    });
  }

  return sections;
}

export function getAnalysisItemCount(result: FileValidationResult | undefined): number {
  if (!result) return 0;

  if (result.zipEntries) {
    return result.zipEntries
      .filter((entry) => entry.valid)
      .reduce((total, entry) => total + (entry.videoSections?.length || 1), 0);
  }

  if (result.videoSections) {
    return result.videoSections.length || 1;
  }

  return result.valid ? 1 : 0;
}

async function validateDocxBuffer(arrayBuffer: ArrayBuffer): Promise<FileValidationResult> {
  try {
    const result = await mammoth.extractRawText({ arrayBuffer });

    if (!result.value.trim()) {
      return { valid: false, errors: ['The document appears to be empty.'] };
    }

    return { valid: true, errors: [], videoSections: extractVideoSections(result.value) };
  } catch {
    return { valid: false, errors: ['The DOCX file is corrupted or cannot be read.'] };
  }
}

async function validatePdfBuffer(arrayBuffer: ArrayBuffer): Promise<FileValidationResult> {
  const header = new Uint8Array(arrayBuffer.slice(0, 5));
  const signature = String.fromCharCode(...header);

  if (signature !== '%PDF-') {
    return { valid: false, errors: ['The PDF file is corrupted or invalid.'] };
  }

  return { valid: true, errors: [] };
}

function validateHtmlText(text: string): FileValidationResult {
  if (!text.trim()) {
    return { valid: false, errors: ['The HTML file is empty.'] };
  }

  const parserError = new DOMParser().parseFromString(text, 'text/html').querySelector('parsererror');
  if (parserError) {
    return { valid: false, errors: ['The HTML file could not be parsed.'] };
  }

  return { valid: true, errors: [] };
}

function isIgnoredZipEntry(path: string): boolean {
  if (path.includes('__MACOSX/')) return true;

  const baseName = path.split('/').pop() ?? '';
  return baseName.startsWith('.');
}

async function validateBufferByExtension(
  extension: Exclude<SupportedExtension, 'zip'>,
  arrayBuffer: ArrayBuffer,
): Promise<FileValidationResult> {
  if (extension === 'docx') return validateDocxBuffer(arrayBuffer);
  if (extension === 'pdf') return validatePdfBuffer(arrayBuffer);
  if (extension === 'html') return validateHtmlText(new TextDecoder().decode(arrayBuffer));
  return { valid: true, errors: [] }; // allowed by the server, no browser-side check: the worker decides
}

/**
 * The ZIP check (upload-ingest-merge D16), following the server's allowed-entry list and folder depth.
 * __MACOSX/ entries and hidden files are skipped SILENTLY, as the server skips them — every Mac zip has them.
 * An entry of a disallowed type, or nested too deeply, is a WARNING: the zip still uploads and the worker rejects
 * that entry with its reason. The zip is blocked only when no entry at all is usable.
 */
async function validateZipBuffer(file: File, config: UploadConfig): Promise<FileValidationResult> {
  let zip: Awaited<ReturnType<typeof JSZip.loadAsync>>;

  try {
    zip = await JSZip.loadAsync(file);
  } catch {
    return { valid: false, errors: ['The ZIP file is corrupted or cannot be read.'] };
  }

  const entries = Object.values(zip.files).filter(
    (entry) => !entry.dir && !isIgnoredZipEntry(entry.name),
  );

  const zipEntries: ZipEntryResult[] = [];

  for (const entry of entries) {
    const segments = entry.name.split('/').filter(Boolean);
    const fileName = segments[segments.length - 1];
    const depth = segments.length - 1;

    if (depth > config.maxZipFolderDepth) {
      zipEntries.push({
        id: entry.name,
        path: entry.name,
        fileName,
        valid: false,
        error: 'File is nested more than one subfolder deep.',
      });
      continue;
    }

    const extension = getFileExtension(fileName);
    if (!extension || !config.allowedZipEntryExt.includes(extension)) {
      zipEntries.push({
        id: entry.name,
        path: entry.name,
        fileName,
        valid: false,
        error: extension ? `.${extension} is not supported inside a ZIP.` : 'Files without an extension are not supported.',
      });
      continue;
    }

    if (extension === 'pdf') {
      const buffer = await entry.async('arraybuffer');
      const innerResult = buffer.byteLength === 0
        ? { valid: false, errors: ['File is empty.'] }
        : await validatePdfBuffer(buffer);
      zipEntries.push({
        id: entry.name,
        path: entry.name,
        fileName,
        valid: innerResult.valid,
        error: innerResult.errors[0],
      });
      continue;
    }

    zipEntries.push({ id: entry.name, path: entry.name, fileName, valid: true });
  }

  const warnings = zipEntries.filter((entry) => !entry.valid).map((entry) => `${entry.path}: ${entry.error}`);

  if (!zipEntries.some((entry) => entry.valid)) {
    const allowed = config.allowedZipEntryExt.map((ext) => ext.toUpperCase()).join(', ');
    return { valid: false, errors: [`The ZIP file does not contain any ${allowed} files that can be added.`], warnings, zipEntries };
  }

  return { valid: true, errors: [], warnings, zipEntries };
}

export async function validateUploadFile(file: File, config: UploadConfig): Promise<FileValidationResult> {
  const extension = getUploadableExtension(file.name, config);

  if (!extension) {
    return {
      valid: false,
      errors: [unsupportedFormatMessage(file.name, config)],
    };
  }

  if (file.size === 0) {
    return { valid: false, errors: ['File is empty.'] };
  }

  if (extension === 'zip') {
    return validateZipBuffer(file, config);
  }

  const arrayBuffer = await file.arrayBuffer();
  return validateBufferByExtension(extension, arrayBuffer);
}
