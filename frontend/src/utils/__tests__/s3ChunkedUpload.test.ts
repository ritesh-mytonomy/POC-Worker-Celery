import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { server } from '@/mocks/server';

// API_BASE is read when the module loads, so set it first and import afresh in each test.
const API = 'http://api.test';

const loadUploader = async () => {
  vi.stubEnv('VITE_SCAN_API_URL', API);
  vi.resetModules();
  return (await import('@/utils/s3ChunkedUpload')).S3MultipartUploader;
};

describe('S3MultipartUploader and batchId (upload-ingest-merge D4)', () => {
  let initiateBodies: Record<string, unknown>[];

  beforeEach(() => {
    initiateBodies = [];
    server.use(
      http.post(`${API}/api/uploads/initiate`, async ({ request }) => {
        initiateBodies.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json({ id: 'f1', fileId: 'f1', uploadId: 'u1', key: 'k', partSize: 8, totalParts: 1 });
      }),
      // Stop right after initiate: the batchId is all these tests look at.
      http.post(`${API}/api/uploads/parts/presign`, () => HttpResponse.json({ detail: 'stop here' }, { status: 500 })),
    );
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('sends the batchId with initiate', async () => {
    const Uploader = await loadUploader();
    const uploader = new Uploader(new File(['x'], 'a.docx'), { batchId: 'batch-1' });
    await expect(uploader.start()).rejects.toThrow('stop here');
    expect(initiateBodies).toEqual([
      { filename: 'a.docx', fileSize: 1, contentType: 'application/octet-stream', batchId: 'batch-1' },
    ]);
  });

  it('sends exactly Anugrah’s body when there is no batchId', async () => {
    const Uploader = await loadUploader();
    await expect(new Uploader(new File(['x'], 'a.docx')).start()).rejects.toThrow();
    expect(initiateBodies).toEqual([{ filename: 'a.docx', fileSize: 1, contentType: 'application/octet-stream' }]);
  });
});
