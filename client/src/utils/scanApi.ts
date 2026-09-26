/**
 * Client for the ClinSync server's `POST /api/scan` endpoint (see
 * `Server/app.py` and `Server/ARCHITECTURE.md`). Sends the raw file bytes
 * (not multipart — the server reads `request.stream()` directly) and reads
 * the Server-Sent Events response as it streams in, dispatching one handler
 * call per event so the UI can update section-by-section, live.
 */
import type { ScanSectionResult, ScanSummary } from '@/types/contentLibrary';

const SCAN_API_URL = import.meta.env.VITE_SCAN_API_URL;

export class ScanRequestError extends Error {}

export interface ScanEventHandlers {
  onMeta?: (meta: {
    candidate_sections: number;
    total_checks: number;
    search_budget: number;
    doc_title: string;
  }) => void;
  onUploadWarning?: (message: string) => void;
  onScanning?: (index: number, title: string) => void;
  onResult?: (result: ScanSectionResult) => void;
  onSummary?: (summary: ScanSummary) => void;
  onError?: (message: string) => void;
}

interface RawScanEvent {
  type: string;
  [key: string]: unknown;
}

function dispatchEvent(event: RawScanEvent, handlers: ScanEventHandlers): void {
  switch (event.type) {
    case 'upload_warning':
      handlers.onUploadWarning?.(String(event.message ?? ''));
      break;
    case 'meta':
      handlers.onMeta?.({
        candidate_sections: Number(event.candidate_sections ?? 0),
        total_checks: Number(event.total_checks ?? 0),
        search_budget: Number(event.search_budget ?? 0),
        doc_title: String(event.doc_title ?? ''),
      });
      break;
    case 'scanning':
      handlers.onScanning?.(Number(event.index ?? 0), String(event.title ?? ''));
      break;
    case 'result':
      handlers.onResult?.(event as unknown as ScanSectionResult);
      break;
    case 'summary':
      handlers.onSummary?.(event as unknown as ScanSummary);
      break;
    case 'error':
      handlers.onError?.(String(event.message ?? 'Unknown server error.'));
      break;
    default:
      break;
  }
}

async function readErrorDetail(response: Response): Promise<string> {
  try {
    const parsed = await response.json();
    if (parsed && typeof parsed.detail === 'string') return parsed.detail;
  } catch {
    // response body wasn't JSON — fall through to the generic message
  }
  return `Scan request failed (HTTP ${response.status}).`;
}

/**
 * POSTs one .docx file to the scan endpoint and streams back its verdicts.
 * Resolves once the SSE stream ends (success, server-reported error, or a
 * fatal frame) — callers drive per-section UI updates entirely from `handlers`.
 */
export async function scanDocument(
  file: File,
  handlers: ScanEventHandlers,
  signal?: AbortSignal,
): Promise<void> {
  if (!SCAN_API_URL) {
    throw new ScanRequestError(
      'VITE_SCAN_API_URL is not configured — set it in .env to point at the ClinSync server.',
    );
  }

  let response: Response;
  try {
    response = await fetch(`${SCAN_API_URL}/api/scan`, {
      method: 'POST',
      headers: { 'X-Filename': file.name },
      body: file,
      signal,
    });
  } catch {
    throw new ScanRequestError('Could not reach the ClinSync server. Is it running?');
  }

  if (!response.ok || !response.body) {
    throw new ScanRequestError(await readErrorDetail(response));
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let separatorIndex = buffer.indexOf('\n\n');
    while (separatorIndex !== -1) {
      const frame = buffer.slice(0, separatorIndex);
      buffer = buffer.slice(separatorIndex + 2);

      const dataLine = frame.split('\n').find((line) => line.startsWith('data: '));
      if (dataLine) {
        try {
          dispatchEvent(JSON.parse(dataLine.slice(6)) as RawScanEvent, handlers);
        } catch {
          // ignore a malformed/partial frame
        }
      }

      separatorIndex = buffer.indexOf('\n\n');
    }
  }
}
