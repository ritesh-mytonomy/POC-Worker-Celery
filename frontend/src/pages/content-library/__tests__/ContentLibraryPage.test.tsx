import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { server } from '@/mocks/server';
import { renderWithProviders, screen, waitFor } from '@/testing/testUtils';

const API = 'http://api.test';
const uploads: { name: string; batchId?: string }[] = [];

// The uploader is mocked: this test is about which batchId each Upload click hands it.
vi.mock('@/utils/s3ChunkedUpload', () => ({
  S3UploadAborted: class extends Error {},
  S3MultipartUploader: class {
    private readonly name: string;
    private readonly batchId?: string;
    constructor(file: File, handlers: { batchId?: string } = {}) {
      this.name = file.name;
      this.batchId = handlers.batchId;
      uploads.push({ name: file.name, batchId: handlers.batchId });
    }
    start() {
      return Promise.resolve({ id: `id-${this.name}`, key: 'k', location: 'l', batchId: this.batchId });
    }
    retry() {
      return this.start();
    }
    cancel() {}
  },
}));

const renderPage = async () => {
  vi.stubEnv('VITE_SCAN_API_URL', API);
  vi.resetModules();
  const { default: ContentLibraryPage } = await import('@/pages/content-library/ContentLibraryPage');
  const { UploadQueueProvider } = await import('@/context/UploadQueueContext');
  server.use(
    http.get(`${API}/api/v1/uploads/config`, () =>
      HttpResponse.json({
        allowed_top_level_ext: ['docx', 'zip'],
        allowed_zip_entry_ext: ['docx'],
        max_upload_bytes: 5 * 1024 ** 3,
        max_zip_folder_depth: 1,
      }),
    ),
    http.post(`${API}/api/uploads/check-duplicate`, () => HttpResponse.json({ duplicate: false, message: null })),
    http.get(`${API}/api/v1/uploads/batches/:id`, () =>
      HttpResponse.json({ batch_id: 'b', files: [], ready_to_add: 0, pending_review: 0, in_progress: 0 }),
    ),
    http.get(`${API}/api/v1/uploads/batches/:id/staged`, () => HttpResponse.json({ batch_id: 'b', staged: [] })),
    http.get(`${API}/api/v1/library/documents`, () => HttpResponse.json({ documents: [], total: 0 })),
  );
  const view = renderWithProviders(
    <UploadQueueProvider>
      <ContentLibraryPage />
    </UploadQueueProvider>,
  );
  await screen.findByText(/Supported formats: DOCX, ZIP \(DOCX only\)/);
  return view;
};

// applyAccept: false — a dragged-in file is not filtered by the input's accept list the way the file picker is.
const drop = async (container: HTMLElement, names: string[]) => {
  const input = container.querySelector('input[type="file"]') as HTMLInputElement;
  await userEvent.setup({ applyAccept: false }).upload(
    input,
    names.map((name) => new File(['content'], name)),
  );
};

describe('ContentLibraryPage — one batchId per Upload click (D4)', () => {
  afterEach(() => {
    uploads.length = 0;
    vi.unstubAllEnvs();
  });

  it('starts every file of one click with the same batchId, and a new click with a new one', async () => {
    const { container } = await renderPage();
    const upload = () => screen.getByRole('button', { name: /upload/i });

    await drop(container, ['a.docx', 'b.docx']);
    await waitFor(() => expect(upload()).toBeEnabled());
    await userEvent.click(upload());
    await waitFor(() => expect(uploads).toHaveLength(2));

    await drop(container, ['c.docx']);
    await waitFor(() => expect(upload()).toBeEnabled());
    await userEvent.click(upload());
    await waitFor(() => expect(uploads).toHaveLength(3));

    const [a, b, c] = uploads;
    expect(a.batchId).toMatch(/^[0-9a-f-]{36}$/);
    expect(b.batchId).toBe(a.batchId);
    expect(c.batchId).not.toBe(a.batchId);
  });

  it('shows a type the server does not allow as not supported, without uploading it', async () => {
    const { container } = await renderPage();
    await drop(container, ['notes.pdf']);
    expect(await screen.findByText('.pdf is not a supported format')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /upload/i })).toBeDisabled();
  });
});
