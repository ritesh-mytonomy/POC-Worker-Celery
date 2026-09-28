# Upload POC review — task 0.2 (and 0.1)

| | |
|---|---|
| **Feature** | `specs/upload-ingest-merge/` — the files read say **revision 1.1** in their headers (the brief said 1.2) |
| **Source reviewed** | `File Upload source code/clynsync-poc`, commit `3d2bed9` (Anugrah M, 25 Sep 2026). Every file in `Client/` and `Server/` read |
| **Branch** | `feature/upload-merge`, created from `main` at `edc69e5` |
| **Fixtures** | `tests/fixtures/upload_api/` — 22 real exchanges, captured from Anugrah's server running unmodified (§3) |
| **Date** | 25 Sep 2026 |
| **Outcome** | Approved 26 Sep. D-1 to D-5 are settled by spec revision 1.2 (LLD statuses: an abort ends `error`, not `cancelled`; `uq_org_title` with `same_title`/`same_content` discards; candidates deleted on commit; task 1.1 adapts `/poc/seed`). D-6 to D-10 and the §7 adjustments are decided in revision 1.3 (D4, D14–D18). Where §8's proposals differ, the spec wins |

---

## 0. Summary — what needs a decision before Phase 1

The endpoint list in design §5.1 is right: the paths, the methods, and the two routes marked for removal all match the code. But ten things in the code or in `schema.sql` don't fit the spec as written. Each one needs a decision. D-1 to D-5 block Phase 1 or 3; D-6 to D-10 block Phase 6.

| # | Finding | Where it bites | Section |
|---|---|---|---|
| **D-1** | **`schema.sql` has no `cancelled` status** in the `upload_file` status list. U4.1, U4.3 and design §3/§6 need it | Phase 1, 3.3, 4.3 | §4.4 |
| **D-2** | **`schema.sql` has no unique `(organization_id, content_hash)` on `documents`**, only a plain index. U6.4 ("SHALL enforce") and commit's `ON CONFLICT (org, hash)` (§5.4) depend on it. It also has **no `source_staged_id`** | 5.2 | §4.4 |
| **D-3** | **`schema.sql` has a unique `(organization_id, title_norm)`** on `documents`, and titles default to the file-name stem. Two different files with the same stem (e.g. `intro.docx` from two zips) make commit fail. Nothing in the spec handles this | 5.2 | §4.4 |
| **D-4** | **`schema.sql` has no `committed_document_id`**, and says candidates are "deleted on commit". U7.2 (`added` marker), U8.3 (idempotency) and design §5.4 (`mark_committed`) assume the row stays | 1.1, 5.2, 5.3 | §4.4 |
| **D-5** | **`schema.sql` breaks the Ingest scenarios as they stand.** The `upload_file.status` default is `staged` (not `uploading`). `size_bytes` is `NOT NULL`, as is `upload_batch.created_by`. `/poc/seed` (`files.seed_batch`) sets none of these, so seeded files would start `staged`, `confirm` would never move them, and R1–R15 / UN-6 would fail | 1.1 | §4.4 |
| **D-6** | **"Pass the first file's `batchId` to the rest" can't work with this client.** Pressing **Upload** starts every file at once, so all `initiate` calls are sent in parallel, before any response comes back | U1.2, U10.2, 6.2 | §5 |
| **D-7** | **The API's error body must stay `{"detail": "…"}`.** The client reads only `body.detail`, and decides "Duplicate" by searching that text for `duplicate`. The Ingest app's global handlers rewrite every error to `{"error": {"code", "message"}}`, and 422 to 400 | 3.1–3.4 | §4.2 |
| **D-8** | **The client sends `abort` for uploads that already completed.** It does this whenever a finished row is removed or the queue is cleared. As U4.1 is written, that would set a processed file to `cancelled` | 3.3 | §4.2 |
| **D-9** | **The client's zip check rejects zips of `.docx`**, and blocks the whole zip if any entry is bad. So `mixed.zip` never uploads from the browser, and the §9.3 walkthrough (steps 2–4) can't happen. Design §7 only mentions the allowed top-level types | 6.2, M3 | §4.3 |
| **D-10** | **`s3ChunkedUpload.ts` can't stay unchanged.** The `initiate` body is built inside `start()`, so passing `batchId` means editing that file. Server statuses never reach the queue rows today, so "status labels" need a new data path, not a label change | 6.2, 6.3 | §4.3 |

**Your two adjustments** (the Library table on the content-library page; `StoredUploadsTable` adapted rather than removed) change requirement text in U9.3, U10.6 and design §7 / tasks 6.1 and 6.4. They are listed in §7 so the spec can carry them.

---

## 1. Task 0.1 — preconditions

- **Branch:** `main` = `origin/main` = `edc69e5` (the merge of `refactor/workers-tidy`). `feature/upload-merge` did not exist. It was created from `main` as task 0.1 says; not pushed.
- **Layout:** `engine/file_checks.py`, `workers/tasks.py`, `workers/clients.py`, `poc/main.py`, `poc/worker.py` are all present.
- **Full suite**, in the rebuilt image (`docker compose build api && docker compose run --rm --no-deps api pytest tests/`):

```
563 passed in 24.31s
Required test coverage of 80% reached. Total coverage: 97.06%
```

- **Untracked, not committed:** `specs/upload-ingest-merge/`, `File Upload source code/` (a separate git repo, 117 files), and this report with its fixtures.

---

## 2. The Upload API, exactly as the code has it (0.2a)

### 2.1 Conventions that apply to every endpoint

| Topic | What the code does |
|---|---|
| Base URL | `import.meta.env.VITE_SCAN_API_URL` (both `s3ChunkedUpload.ts` and `uploadsApi.ts`) |
| Request bodies | JSON, **camelCase**, header `Content-Type: application/json` |
| Responses | `initiate` and `complete` are **camelCase**; `GET /api/uploads` is **snake_case**; `check-duplicate` is `duplicate`/`message` |
| Error body | FastAPI default: **`{"detail": "<string>"}`**. For request-validation failures it is **422** with `{"detail": [<pydantic error objects>]}` |
| How the client reads errors | `readJsonOrThrow()`: if `body.detail` is a **string** it becomes the error message; otherwise the message is `"<context> failed (HTTP <status>)."`. A network failure becomes `"<context> failed (connection lost). Restart the API server and retry."` |
| Duplicate detection | `isDuplicateCloudError(message)` = `message.toLowerCase().includes('duplicate')`. So **the wording of the 409 detail is part of the contract** — it drives the red **Duplicate** label and moves the row to `validation-failed` |
| CORS | API: `CORSMiddleware` with `CORS_ORIGINS`, methods `GET POST PUT HEAD OPTIONS`, all headers, `expose_headers=["ETag"]`. Bucket: CORS set at API startup (`PUT GET HEAD`, `ExposeHeaders: ["ETag"]`) |
| Storage errors (any S3 call) | `ClientError` → **404** if code is `NoSuchUpload`/`NoSuchKey`/`NoSuchBucket`, else **502**; detail `"S3 error (<Code>): <Message>"`. Any other exception → **500** `"Upload storage failed: <exc>"` |

### 2.2 Endpoints the Client calls

#### `POST /api/uploads/check-duplicate`

| | |
|---|---|
| Caller | `uploadsApi.checkDuplicateUpload(filename, fileSize)`, from `ContentLibraryPage.processNewItem` for every newly added valid file |
| Request | `{"filename": string, "fileSize": number}` — `fileSize` must be `> 0` |
| Response 200 | `{"duplicate": boolean, "message": string \| null}` — `message` = `"Duplicate file — <filename> has already been uploaded."` when `duplicate` is true, else `null` |
| Errors | 422 (validation) |
| Client behaviour | On `duplicate: true` the row fails validation with `message`. **Any error is swallowed** (`catch {}`) and the file proceeds |
| Match rule today | Name `ILIKE` (case-insensitive) **and** equal size, among top-level rows in `stored`/`extracted`/`validated` |

#### `POST /api/uploads/initiate`

| | |
|---|---|
| Caller | `S3MultipartUploader.start()` — see §5 for when |
| Request | `{"filename": string, "fileSize": number, "contentType": string}` — client sends `file.type \|\| 'application/octet-stream'`; server model has `contentType: str \| None` |
| Response 200 | `{"id": string, "fileId": string, "uploadId": string, "key": string, "partSize": number, "totalParts": number}` — `id` and `fileId` are the same UUID |
| Errors | **413** `"File exceeds the 5120 MB limit for cloud uploads."` (the number is `MAX_CLOUD_UPLOAD_MB`) · **409** `"Duplicate file — <filename> has already been uploaded."` · 422 · S3 404/502/500 (§2.1) · DB failure → 500 after aborting the multipart upload |
| Client tolerance | Reads `uploadId ?? upload_id`, `fileId ?? id`, `partSize ?? part_size`, `totalParts ?? total_parts`. Throws `"Upload initiation did not return uploadId/key…"` if `uploadId` or `key` is missing |
| Server side | Row `status='initiated'`; key `incoming/<id>/<name>` for `.zip`, else `processed/<id>/<name>` (`name` = `PurePosixPath(filename).name`); `partSize` = `max(UPLOAD_PART_SIZE_MB, 5) MiB` (8 MiB default); `totalParts` = ceil(size / partSize) |

#### `POST /api/uploads/parts/presign`

| | |
|---|---|
| Caller | `runToCompletion()` — **once** for all parts without an ETag; again for **one** part (`[n]`) before that part's last retry |
| Request | `{"key": string, "uploadId": string, "partNumbers": number[]}` |
| Response 200 | `{"urls": {"<partNumber>": "<presigned PUT URL>"}}` — JSON object keys are strings (`"1"`, `"2"`); the client indexes with a number, which JavaScript coerces |
| Errors | **400** `"partNumbers must not be empty."` · 422 · S3 404/502/500. **No check** that `key`/`uploadId` belong to any row |

#### `PUT <presigned URL>` (to S3, not the API)

| | |
|---|---|
| Caller | `putChunk()` via `XMLHttpRequest`, up to `VITE_UPLOAD_CONCURRENCY` (default 4) at once |
| Request | Body = `file.slice(start, end)`; **no `Content-Type`** (a comment explains it would break the signature) |
| Response | 200 with an `ETag` header (quoted MD5, e.g. `"100b6168…"`). No `ETag` readable → `"Upload succeeded but no ETag header was readable — check the bucket's CORS ExposeHeaders includes "ETag"."` |
| Retries | 5 per part, backoff `500 ms × 2^(n-1)`, capped at 15 s |

#### `POST /api/uploads/complete`

| | |
|---|---|
| Caller | End of `runToCompletion()` |
| Request | `{"id": string, "fileId": string, "key": string, "uploadId": string, "filename": string, "fileSize": number, "contentType": string, "parts": [{"partNumber": number, "etag": string}]}` — every part, in part order, `etag` exactly as S3 returned it (with quotes) |
| Server model | `id`/`fileId` optional; `key`, `uploadId`, `filename`, `fileSize > 0`, `parts` required; `contentType` optional |
| Response 200 | `{"id": string, "location": string, "key": string, "status": string}` — `status` is `"stored"` (non-zip) or `"extracting"` (zip). `location` comes from S3 (LocalStack returns a virtual-host URL) |
| Errors, in order | **400** `"parts must not be empty."` · **404** `"Upload not found."` · **409** `"Upload is not awaiting completion."` (e.g. a second call) · **400** `"S3 key does not match this upload."` · **400** `"Multipart upload id does not match this upload."` · S3 404/502/500 · **409** duplicate (name+size, then SHA-256 — both **after** S3 has completed the object) · **400** content validation (`"The PDF file is corrupted or invalid."`, `"The DOCX file is empty."`, `"Unsupported file type: <name>"`, …) · **503** `"ZIP uploaded, but extraction could not be queued. Is Redis running?"` · 422 |
| Client behaviour | Uses only `id`, `key`, `location` (`S3UploadResult`). **`status` is never read** |

#### `POST /api/uploads/abort`

| | |
|---|---|
| Caller | `S3MultipartUploader.cancel()` — fire-and-forget: `fetch(…, {keepalive: true}).catch(() => {})`, response ignored. `cancel()` is called by `removeItem()` and `clearItems()` for **every** uploader that has an `uploadId`, **including completed ones** |
| Request | `{"key": string, "uploadId": string}` |
| Response 200 | `{"ok": true}` |
| Errors | S3 errors other than `NoSuchUpload`/`404` → 404/502/500 · 422. An unknown pair, or a completed upload, returns `{"ok": true}` |
| Server side | Aborts the multipart upload, then **deletes** rows with that `s3_key` in status `initiated` |

### 2.3 Kept by design §5.1 but not called by the Client

| Endpoint | Request | Response | Errors | Why unused |
|---|---|---|---|---|
| `GET /api/uploads` | — | array of `{"id", "filename", "size_bytes", "content_type", "s3_key", "s3_location", "status", "parent_id", "source_path", "created_at"}`, newest first, `initiated` rows hidden. `created_at` has no timezone (SQLite) | — | `uploadsApi.fetchStoredUploads()` exists, but nothing calls it; `StoredUploadsTable` is never rendered |
| `GET /api/uploads/{uploadId}/parts?key=` | — | `{"parts": [{"partNumber", "etag", "size"}]}` | S3 404 `NoSuchUpload` after abort/complete | `retry()` resumes from ETags held in memory; a page refresh loses them |

### 2.4 Server routes nothing in the Client calls (confirmed by search)

`POST /api/uploads/{id}/reextract`, `PUT /api/uploads/parts/{uploadId}/{partNumber}` (disk mode), `POST /api/scan` (`utils/scanApi.ts` is imported by nothing), `GET /health`, `GET /`. The Client's only other calls are RTK Query `POST /logout` to `VITE_AUTH_API_URL` (`localhost:4000`, called by `useAuth`) and an unused `/login` — auth, out of scope.

---

## 3. Fixtures (0.2b)

**How they were captured.** Anugrah's `Server/` was mounted **read-only** into a throwaway `python:3.11-slim` container and started with `uvicorn app:app` on `127.0.0.1:8001`. It was pointed at the compose LocalStack (`localhost:4566`, bucket `clinsync-uploads`), with a throwaway Redis on `127.0.0.1:6380` so a zip's `complete` could enqueue; no Celery worker ran. `tests/fixtures/upload_api/_capture.py` (standard library) sent each request exactly as the Client does, including `Origin: http://localhost:5173`. Afterwards the containers, the SQLite database and the bucket were removed. Ids and presigned URLs differ on every run.

Each file holds `endpoint`, `captured_from`, an optional `note`, `request` (method, path or URL, headers, body) and `response` (status, the content-type/ETag/CORS headers, body).

| Fixture | Status | What it is |
|---|---|---|
| `check_duplicate.json` | 200 | `duplicate: false` |
| `check_duplicate__true.json` | 200 | `duplicate: true`, name in different case |
| `initiate.json` | 200 | 12 MiB PDF → 2 parts of 8 MiB |
| `initiate__409_duplicate.json` | 409 | same name and size as a stored file |
| `initiate__413_too_large.json` | 413 | 5 GiB + 1 byte |
| `initiate__422_zero_size.json` | 422 | `fileSize: 0` |
| `parts_presign.json` | 200 | 2 URLs, host `localhost:4566` |
| `parts_presign__400_empty.json` | 400 | `partNumbers: []` |
| `s3_put_part.json` | 200 | the browser's PUT to S3; `ETag` exposed by CORS |
| `list_parts.json` | 200 | both parts, before complete |
| `list_parts__404_after_abort.json` | 404 | `NoSuchUpload` |
| `complete.json` | 200 | `status: "stored"` |
| `complete__zip_extracting.json` | 200 | `status: "extracting"`; one `extract_zip` message was left in Redis, args `["<file id>"]` |
| `complete__409_already_completed.json` | 409 | second call for the same file |
| `complete__409_duplicate_by_hash.json` | 409 | same bytes, new name — refused **after** S3 completed it |
| `complete__400_validation.json` | 400 | not a PDF — object and `initiated` row left behind |
| `complete__400_no_parts.json` | 400 | `parts: []` |
| `complete__404_unknown.json` | 404 | unknown id and key |
| `abort.json` | 200 | mid-upload abort |
| `abort__200_unknown_pair.json` | 200 | nonsense key/uploadId still returns `ok` |
| `abort__200_completed_file.json` | 200 | what the Client sends when a finished row is removed |
| `list_uploads.json` | 200 | the stored PDF and the extracting zip |

---

## 4. Code vs design §2, §5.1, §7 (0.2c)

### 4.1 Design §2 — layout

**Matches**
- The "From the Upload POC's `Server/`" table accounts for every Server file (bar `scripts/__init__.py` and `llm_client.md`), and each fate is right: the scan modules and `references/` are independent (they import only each other, `docx`, `httpx`).
- `s3_storage.py` holds all the multipart and presign logic worth porting; its SigV4 + path-style set-up for a custom endpoint is what design §4 describes.

**Differs or missed**
1. **`app/main.py` isn't in the layout, but must change.** It must mount `routes/upload_api.py` and add `CORSMiddleware` for `CORS_ORIGINS` (§8 mentions CORS; the Ingest app has none today). It must also make sure the upload routes keep the `{"detail": …}` error body (D-7).
2. **`scripts/check_large_upload.py` can't be a move.** `validate_large_upload.py` uses `requests` and `psutil` (not standard library, which the scenario scripts require). It also PUTs parts **with** `Content-Type: application/octet-stream`, which the browser deliberately doesn't send.
3. **The bucket-creation part of `ensure_bucket_ready`** (create bucket if missing) is covered by `init-s3.sh`; fine. The CORS rule it applied allowed `PUT GET HEAD`. The design mentions only `ExposeHeaders`, so the methods need stating too.

### 4.2 Design §5.1 — the kept Upload API

**Matches**
- All seven kept endpoints exist with exactly these paths and methods. `reextract` and the disk-mode `PUT` exist and **nothing in the Client calls them**, so removing them (U4.4, task 3.5) is safe.
- The request bodies in the fixtures are the ones the Client sends; making `batchId` optional keeps them valid.

**Differs or missed**
1. **Error body shape (D-7).** Every Upload API error the client can show is `detail: string`. If the ported routes raise through the Ingest envelope, the client shows `"Upload initiation failed (HTTP 409)."` instead of the reason. A duplicate is then labelled **Upload failed**, not **Duplicate**, because the word `duplicate` is gone. The 422→400 rewrite also changes a status code. Proposal: the upload routes return `{"detail": "<message>"}` for their own errors and keep FastAPI's 422, e.g. handlers scoped to that router or an `HTTPException` path the envelope passes through. The 409 texts keep the word "Duplicate".
2. **`abort` on a completed file (D-8).** U4.1 says abort "SHALL validate the pair … and set the file to `cancelled`". The client sends abort after success too (`abort__200_completed_file.json`). Proposal: abort changes state only when the file is `uploading`. Otherwise it returns `{"ok": true}` and does nothing. The client ignores the response, so 404/409 for a bad pair are harmless, but the no-op must be explicit.
3. **`complete` called again.** Today it's **409** `"Upload is not awaiting completion."`; U3.5 makes it 200 with the current status. That's a status-code change, but it only helps (the client re-sends `complete` only through `retry()` after a failure). Worth recording as an intended difference.
4. **`complete`'s request model must stay as strict as today** (`filename` and `fileSize > 0` required), or the "same request body" contract tests become one-sided. The new internals use only `key`, `uploadId` and `parts`; the rest is accepted and ignored.
5. **`location`.** Kept as a field. With the internal/public split it will be an internal-host URL. The client stores it and never shows it, so any string is fine, but the design should say what it is.
6. **Duplicate-by-hash moves from a 409 at `complete` to a marker in review (D6).** Same content under a new name used to be refused before the row appeared; now it uploads, is processed, and shows as "already in the library". Consistent with D6; noted because the user-visible behaviour changes.
7. **`check-duplicate` against the library only.** A file uploaded but not yet committed is not a duplicate, so the same file can go into two batches. Commit's hash rule stops it reaching the library twice (if D-2 is resolved). The design is silent on this; it looks acceptable.
8. **Presign for a non-`uploading` file → 409 (U2.2).** The client only re-presigns while uploading, so no client path hits it.

### 4.3 Design §7 — the Client

**Matches**
- `s3ChunkedUpload.ts` behaviour: 8 MiB parts from the server's `partSize`, 4 in parallel, 5 retries with backoff, re-presign before the last retry, `retry()` resends only parts without an ETag, `cancel()` aborts.
- `UploadQueueContext` sits in `DashboardLayout`, so uploads survive navigation.
- `duplicateCheck.ts` can stay as it is.
- Removing `scanApi.ts`, `ScanResults`, `pages/poc5/` and the hidden `.bin`/`.dat` types matches the code: none of the upload path depends on them.

**Differs or missed**
1. **`batchId` timing (D-6)** — see §5.
2. **`s3ChunkedUpload.ts` must change (D-10).** `start()` builds the `initiate` body itself, so `batchId` has to be passed into the uploader (constructor option) and added to that body. `S3UploadResult` (`{id, key, location}`) has no `batchId` or `status`, and `InitiateResponse` has no `batchId`.
3. **Status labels aren't a label change (D-10).** `UploadQueueRow` renders only client-side states: `UploadStatus` (`queued`, `scanning`, `success`, `validation-failed`, `upload-failed`) and `CloudUploadStatus` (`idle`, `uploading`, `success`, `failed`, `canceled`). No server status reaches an item. The §3 mapping (*Checking… 12 of 30*, *Ready*, *Partly ready*) needs `useBatchStatus` results joined to queue items by file id. `complete`'s `id` is the file id, so the join is possible, but it's new code in the context or row, not a label edit.
4. **Zip validation in the browser (D-9).** `validateZipBuffer` allows only PDF entries (`"Only PDF files are allowed inside a ZIP (.docx is not supported)."`). It marks the whole zip invalid if **any** entry is invalid, and an invalid item is never uploaded (`isBlockedFromUpload`). With D2's default (`zip` of `docx`) every zip is rejected client-side, and a `mixed.zip` can never reach the worker, so §9.3 steps 2–4 fail. Options:
   - allow the D2 entry types and stop blocking on bad entries, leaving entry rejection to the worker (my recommendation — the worker is the authority and reports reasons per entry); or
   - keep blocking, and change the walkthrough.
   Either way `contentValidation.ts` changes more than the allowed-types list.
5. **Hard-coded format text.** `ContentLibraryPage` shows `"Supported formats: DOCX, PDF, HTML, ZIP (PDF only)"`, and `ACCEPTED_FILE_INPUT = '.docx,.pdf,.html,.htm,.zip,.bin,.dat'` filters the OS file picker. For U10.5 ("show others as unavailable rather than hiding them"), note the picker's `accept` does hide them. Files dragged in are already shown as **Not supported**.
6. **Uploads start on the Upload button, not on drop.** Dropping only validates. The walkthrough's "Drop … both upload with progress bars" needs a click on **Upload**, and the natural batch boundary is that click.
7. **Removing the scan and POC 5 code touches more files than listed.**
   - `UploadQueueRow` imports `ScanResults` and reads `item.scan`.
   - `GlobalUploadWidget.PAGE_LABEL` has `poc5`.
   - `types/contentLibrary.ts` has the scan types and `UploadSource = 'content-library' | 'poc5'`.
   - `private.routes.tsx` routes `/dashboard/poc-5`, and `Sidebar.tsx` links to it.
8. **Client setting not in design §8:** `VITE_MAX_UPLOAD_FILE_SIZE_MB` (a client-side size cap, unset by default; `.bin`/`.dat` bypass it).
9. **Existing defect, not in scope unless you want it:** if `initiate` itself fails (e.g. 409), the uploader has no `uploadId`, so the row's **Retry** calls `retry()`, which throws `"Nothing to retry — call start() first."`.

### 4.4 Beyond 0.2c — `schema.sql` vs design §3 and the Ingest code

You said the tables follow `schema.sql`. Design §3 was written against a different table shape, so these must be settled before Phase 1:

| # | `schema.sql` | Design / requirements | Effect |
|---|---|---|---|
| D-1 | `upload_file.status` CHECK: `staged, uploading, uploaded, processing, processed, partial, rejected, error` — **no `cancelled`** | U4.1, U4.3, §3 mapping, §6 abandoned sweep all set `cancelled` | Abort and the abandoned sweep have no state to go to |
| — | S3 multipart id column is **`upload_id`**, unique `uq_upload_id` | §3: `multipart_upload_id`, `uq_upload_file_multipart` | Naming only; I'd follow `schema.sql` and fix the design text |
| D-2 | `documents`: **no `source_staged_id`**; `(organization_id, content_hash)` is a **non-unique** index | §3 and U6.4: `UNIQUE (org, content_hash)`; §5.4 uses `ON CONFLICT (org, hash) DO NOTHING` | Without a unique constraint the conflict clause errors, and two identical files can both become documents |
| D-3 | `documents`: **`UNIQUE (organization_id, title_norm)`** | Titles = file-name stem; metadata confirmation out of scope | Two different files with the same stem → commit fails with a unique violation. Needs a rule (skip with a reason, or relax for the POC) |
| D-4 | `staged_document`: no `committed_document_id`; comment "candidates; deleted on commit"; has `resolution` (`pending/create_new/replace_existing/discard`) and `duplicate_kind` | U7.2 `added`; U8.3 idempotency; §5.4 `mark_committed`, `mark_skipped` | The "added" and "skipped" markers need a home — a column, `resolution`, or deleting rows (then `added` can't be shown) |
| D-5 | `upload_file.status` **DEFAULT `'staged'`**; `size_bytes NOT NULL`; `upload_batch.created_by BIGINT NOT NULL`; `organization_id` FKs into `organizations` | Ingest code relies on default `uploading`; `files.seed_batch` inserts neither `size_bytes` nor `created_by` nor `status` | `/poc/seed` either fails (NOT NULL) or creates `staged` rows that `confirm` never moves, so every Ingest scenario breaks (U5.1, UN-6). The seeded org id matches `.env.example`'s `POC_ORGANIZATION_ID` |
| — | `documents`: `added_at`, `current_version`, `title_norm NOT NULL`, `version_uploaded_by BIGINT NOT NULL`, `status` | §3/§5.2: `created_at`, `version`; library API returns `created_at` | Mapping in the API layer; `version_uploaded_by` needs `POC_USER_ID` (not in design §8) |
| — | `audit_log` — "this merge writes: `batch_committed`" | No requirement or design step writes audit rows | Either add it to commit (§5.4) or drop the comment |
| — | `uq_entry` adds `video_script_seq` | Ingest: `(source_file_id, source_entry_name)` | Equivalent while `video_script_seq` is always NULL (NULLS NOT DISTINCT) |

---

## 5. Does `UploadQueueContext` call `initiate` up front or one at a time? (0.2d)

**Up front, all at once — but on the Upload button, not on drop.**

1. A drop only enqueues and validates: `addFiles()` → `processNewItem()` (`check-duplicate`, then the zip check). No `initiate` yet.
2. Clicking **Upload** runs `handleUpload()`: `for (const item of items.filter(canStartCloudUpload)) startCloudUpload(item);`.
3. `startCloudUpload()` builds a new `S3MultipartUploader` and calls `uploader.start()` **without awaiting it**. `start()`'s first action is `POST /api/uploads/initiate`.
4. So every ready file's `initiate` is sent in the same tick, in parallel. Each file then presigns and uploads its parts on its own (4 parts at a time **per file** — 3 files means up to 12 PUTs in flight).

(The POC 5 page, being removed, starts each file straight from the drop.)

**Why it matters (D-6).** When the second `initiate` is sent, the first hasn't returned, so there is no "first file's `batchId`" to pass. The options:

| Option | Change | Keeps the contract? |
|---|---|---|
| **A. Client-generated batch id** — `handleUpload` makes one `crypto.randomUUID()` per click and every `initiate` sends it; the API creates the batch on first sight, joins it after (`INSERT … ON CONFLICT DO NOTHING`) | U1.2 wording ("with a `batchId` the API SHALL add the file to that batch, creating it if new"); a few lines in `ContentLibraryPage`/context and `s3ChunkedUpload.ts` | Yes — `batchId` stays optional and additive. **My recommendation** |
| B. Serialise the first `initiate` | Await the first file's `initiate` before starting the rest — needs `start()` split so the context can await the `initiate` step | Yes, but reshapes the uploader more |
| C. A "create batch" call | New endpoint before the uploads | Adds an API call; conflicts with D3's spirit |

---

## 6. Where the Client depends on a status value (0.2e)

The five upload statuses: `initiated`, `stored`, `extracting`, `extracted`, `extract_failed`.

| Location | Values | Used for | Rendered today? |
|---|---|---|---|
| `components/ui/uploadFile/StoredUploadsTable.tsx:46-52` | `extracted` → "ZIP (extracted)"; `extracting` → "ZIP (extracting…)"; `extract_failed` → "ZIP (extract failed)"; anything else → "Direct upload" (also reads `parent_id`, `source_path`) | The "Source" column | **No** — nothing renders the table |
| `utils/uploadsApi.ts` — `StoredUpload.status: string` (+ `parent_id`, `source_path`) | any | Type for `GET /api/uploads` | **No** — `fetchStoredUploads()` is never called |

That is the complete list.
- `initiated` and `stored` appear nowhere in `Client/src`.
- The `status` field of the `complete` response is not read (`S3UploadResult` omits it).
- Every status the UI actually shows is client-side: `UploadStatus`, `CloudUploadStatus`, the `ScanStatus` being removed, used in `UploadQueueContext`, `UploadQueueRow`, `GlobalUploadWidget` and `ContentLibraryPage`.

**So the new LLD statuses break nothing in the existing client.** They matter only to new code: `useBatchStatus` and the row labels (§4.3 item 3). `StoredUploadsTable` loses its status logic when it becomes the Library table (library documents have `active`/`archived`, not upload statuses).

On the server side the old values are used only in `upload_duplicates.ACTIVE_PARENT_STATUSES` (`stored`, `extracted`, `validated` — `validated` is never set anywhere) and the `initiated` filter in `GET /api/uploads`; both are replaced.

---

## 7. Your two adjustments — what they touch

| Adjustment | Spec text that changes | Code consequence |
|---|---|---|
| Library table on the existing **content-library** page, no `pages/library/` | U9.3 ("Library screen"); design §7 row "New `pages/library/`"; task 6.4 | `ContentLibraryPage` hosts the upload queue, the review panel and the Library table; U9.3's "refresh after a commit" becomes a local refetch |
| **Keep `StoredUploadsTable`**, adapted to `GET /api/v1/library/documents` | U10.6 ("SHALL NOT contain … the stored-uploads table"); design §7 "Removed" row; task 6.1 | Props move from `StoredUpload` (`id, filename, size_bytes, s3_key, status, parent_id, source_path, created_at`) to the library shape (`document_id, title, file_name, file_ext, size_bytes, created_at`) plus a download action. A `fetchLibraryDocuments()` is needed — in `uploadsApi.ts` (design §7 calls it "unchanged"), or in a new file. `fetchStoredUploads()` then has no caller |

---

## 8. Proposed resolutions, for your decision

| # | Proposal |
|---|---|
| D-1 | Add `cancelled` to the `upload_file` status CHECK in `schema.sql`, marked `[POC]` |
| D-2 | Make the `(organization_id, content_hash)` index unique; add `source_staged_id UUID UNIQUE` or derive `document_id` = uuid5(staged_id) without storing it |
| D-3 | Commit skips a candidate whose `title_norm` already exists, reason "A document with this title is already in the library" — or drop `uq_org_title` for the POC |
| D-4 | Keep candidate rows; add `committed_document_id` (`[POC]`), use `resolution` = `create_new` / `discard` for added / skipped |
| D-5 | `seed_batch` and `initiate` set `status='uploading'`, `size_bytes` (seed: `head_object`), `created_by=POC_USER_ID`; add `POC_USER_ID` to settings |
| D-6 | Option A — client-generated `batchId` per Upload click |
| D-7 | Upload API routes keep FastAPI's `{"detail": …}` bodies and 422; duplicate texts keep the word "Duplicate" |
| D-8 | `abort` is a no-op 200 unless the file is `uploading` |
| D-9 | Client allows the D2 entry types and uploads zips with bad entries; the worker rejects entries with reasons |
| D-10 | Accept that 6.2 edits `s3ChunkedUpload.ts` (pass `batchId` into `initiate`, return it) and adds a status join; update U10.2 / §7 / UN-7 wording |
