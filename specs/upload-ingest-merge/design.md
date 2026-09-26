# Design — Upload + Ingest Merge

| | |
|---|---|
| **Feature** | `upload-ingest-merge` · revision 1.3 |
| **Implements** | `requirements.md` U1–U11, UN-1–7 |
| **Builds on** | Ingest Worker POC after `refactor/workers-tidy` — `engine/file_checks.py`, `workers/tasks.py`, `workers/clients.py`, `poc/main.py`, `poc/worker.py` |

---

## 1. The merged flow

Anugrah's calls are **unchanged**. The Ingest pipeline takes over behind `complete`. Three new calls come after.

```
Browser (client/)                        API (app/)                          S3                     Worker
─────────────────                        ──────────                          ──                     ──────
KEPT ─ Anugrah's Upload API ─────────────────────────────────────────────────────────────────────────────────
  POST /api/uploads/check-duplicate ───► name+size vs library
  POST /api/uploads/initiate ──────────► batch (new, or batchId) + file row,
        (+ optional batchId)              create_multipart_upload ─────────► incoming/{org}/{batch}/{file_id}_{name}
  POST /api/uploads/parts/presign ─────► row check, presign (public host)
  PUT 8 MB parts ×4 ─────────────────────────────────────────────────────────► parts + ETags
  POST /api/uploads/complete ──────────► complete_multipart_upload ─────────► object assembled
                                         ══ Ingest confirm: staged|uploading → uploaded, commit, enqueue ► claim,
                                                                                                         verify,
UNCHANGED ─ Ingest pipeline ─────────────────────────────────────────────────────────────────────────── hash, stage
  GET /api/v1/uploads/batches/{id}  ◄── statuses, progress (every 2 s)                                ─► staging/…
  GET /api/v1/uploads/batches/{id}/staged ◄── ready / rejected / duplicate
NEW ──────────────────────────────────────────────────────────────────────────────────────────────────────────
  POST /api/v1/uploads/batches/{id}/commit ► documents rows, copy ────────► processed/{org}/{doc}/v1_{name}
                                            delete staging ──────────────► staging object removed
  GET /api/v1/library/documents ◄── the Library
  GET /api/v1/library/documents/{id}/download ◄── presigned GET
```

**Two URL prefixes, deliberately.** Anugrah's endpoints stay at `/api/uploads/…` so the Client doesn't change. The Ingest endpoints and the new ones are at `/api/v1/…`. Unifying them is for the real ClinSync build, not this merge.

---

## 2. Layout after the merge

```
client/                          NEW — ported from Upload POC Client/, trimmed (§7)
app/
├── main.py                      + upload_api and library routers, CORS for CORS_ORIGINS
├── routes/errors.py             envelope only for /api/v1/* and /internal/* (§5.1, D14)
├── routes/upload_api.py         NEW — Anugrah's /api/uploads/* endpoints, same contract, new internals
├── routes/uploads.py            existing Ingest routes + commit
├── routes/library.py            NEW — list, download
├── services/uploads.py          + initiate, complete, abort, commit
├── repositories/files.py        + initiate, find by uploadId/key, abort (→ error), stale-upload sweep
├── repositories/candidates.py   + content_hash, duplicate marking
├── repositories/documents.py    NEW — library documents
└── storage.py                   NEW — the API's own S3 calls (§4)
engine/file_checks.py            + hash during extraction, skip __MACOSX/dot-files, folder depth
workers/clients.py               + hash during download
workers/tasks.py                 + abandoned-upload sweep task
infra/localstack/init-s3.sh      + bucket CORS, abort-incomplete-multipart rule
infra/postgres/init.sql          replaced by schema.sql (§3)
docker-compose.yml               + client service
scripts/scenario_u*.py           NEW — U-S1 to U-S6 (§9)
scripts/check_large_upload.py    from the Upload POC's validate_large_upload.py
```

**From the Upload POC's `Server/`:**

| File | Becomes |
|---|---|
| `upload_routes.py` | `app/routes/upload_api.py` — **same endpoints and bodies**, calling the Ingest services |
| `s3_storage.py` | Its multipart and presign logic → `app/storage.py` |
| `upload_duplicates.py` | `repositories/documents.py` — now against the library |
| `upload_validation.py` | Dropped — `engine/file_checks.py` does this, in the Worker |
| `zip_extract.py`, `extract_tasks.py`, `celery_app.py`, `re_extract_zip.py` | Dropped — the Ingest worker and sweepers replace them |
| `db.py`, `models.py`, `local_s3.py` | Dropped — Postgres, the Ingest models, LocalStack |
| `validate_large_upload.py` | `scripts/check_large_upload.py` |
| `app.py`, `upload_reader.py`, `docx_sections.py`, `compare_engine.py`, `llm_client.py`, `source_tiers.py`, `references/` | Dropped — scan, out of scope |

---

## 3. Data model — the LLD's tables

The schema is **`schema.sql`**, in this folder. It implements the LLD's M1 `organizations` table and its M2.1 upload and library tables, with M2.5's state machines. It **replaces** the Ingest POC's `init.sql` outright rather than altering it — the POC has no data worth migrating.

### Which LLD tables, and why

| Table | In this merge | Why |
|---|---|---|
| `organizations` | **Used** — one POC row | Every `organization_id` is a foreign key into it |
| `upload_batch`, `upload_file`, `staged_document` | **Used** | The upload and ingest pipeline |
| `documents` | **Used** | The library |
| `audit_log` | **Used** — one `batch_committed` row per commit | The LLD's record of a governance event |
| `specialty`, `document_type`, `document_specialty` | Created, **unused** | Their foreign keys are on `documents` and `staged_document`; tagging is out of scope |
| `organization_config`, `organization_memberships` | Not created | Auth is out of scope |
| `organization_specialty`, `organization_document_type` | Not created | Tagging is out of scope |
| `document_version_log` | Not created | Replace is out of scope |
| Scan tables | Not created | Scan is out of scope |

### Where the merge departs from the LLD — to fold back in

| Column or rule | Table | Why the POC needs it |
|---|---|---|
| `attempt_count`, `claim_token`, `heartbeat_at`, `uploaded_at` | `upload_file` | The Ingest POC's claim, fencing, stale detection and reconcile — proven, and not yet in the LLD |
| `entry_index` | `staged_document` | Deterministic staging keys, which make resume safe |
| `content_type`, `uq_upload_id` | `upload_file` | Anugrah's multipart upload, and his API looks rows up by `uploadId` |
| `duplicate_kind = 'same_content'` | `staged_document` | The LLD's kinds are `replace_requested` and `same_title`; a byte-identical file is a third case |
| `document_id` derived with `uuid5` | `documents` | A re-run commit reuses the id — and so the S3 key — leaving no orphans |
| `documents.source_file_id … ON DELETE SET NULL` | `documents` | Abandoning a batch deletes its files; the library document must survive |

### The LLD rules this merge follows

- **Statuses** — `upload_file`: `staged → uploading → uploaded → processing → processed · partial · rejected`, plus `error`. `upload_batch`: `in_progress → staged → committed`, or `abandoned`. No `cancelled`: an abort or an abandoned upload is `error` with a message.
- **Duplicates** — `documents` is unique on `(organization_id, title_norm)`. Content duplicates are found by `content_hash` and checked at commit.
- **Candidates are deleted on commit.** `documents` and `audit_log` are the record.
- **`created_by` / `version_uploaded_by`** are AUTH user ids. Until auth exists they hold `POC_USER_ID = 0`, the system actor (OD-19).

### What the Client shows

| Upload POC | Now (LLD M2.5) | Client shows |
|---|---|---|
| `initiated` | `staged`, `uploading` | Uploading *n*% |
| `extracting` | `uploaded`, `processing` | Checking… — *12 of 30* for a zip |
| `stored`, `extracted` | `processed` | Ready |
| — | `partial` | Partly ready — *27 of 30* |
| `extract_failed` | `rejected` / `error` | Rejected — *reason* / Failed — *message* |
| *(row deleted on abort)* | `error`, "Upload cancelled" | Cancelled |

## 4. The API's S3 module — `app/storage.py`

The API needs S3 for multipart set-up, presigning, completion, the commit copy and downloads. The Worker keeps its own S3 code; the API must not import `workers/`.

**Two clients, because of hostnames.** Inside Docker the API reaches LocalStack as `http://localstack:4566`, which a browser can't resolve. A presigned URL's host is part of its signature, so it can't be rewritten afterwards.

| Client | Endpoint | Used for |
|---|---|---|
| `internal` | `AWS_ENDPOINT_URL` = `http://localstack:4566` | create / complete / abort multipart, copy, delete |
| `public` | `S3_PUBLIC_ENDPOINT_URL` = `http://localhost:4566` | presigning part uploads and downloads |

In AWS both are unset and resolve to real S3. SigV4 and path-style addressing for LocalStack, as in Anugrah's `s3_storage.py`.

**Bucket set-up** in `init-s3.sh`: CORS for `http://localhost:5173` with **`ExposeHeaders: ["ETag"]`** — without it the browser can't read part ETags — plus `AbortIncompleteMultipartUpload` after 1 day, alongside the existing `incoming/` and `staging/` rules. Anugrah's app set CORS at API startup; here it moves to the bucket's init script, like the other bucket rules.

---

## 5. Endpoints

### 5.1 Kept — Anugrah's Upload API

Paths and request bodies **exactly as in `upload_routes.py` today**. Task 0.2 records the exact shapes before anything is built.

| Endpoint | Request | What changes inside |
|---|---|---|
| `POST /api/uploads/check-duplicate` | unchanged | Matches against **library documents** instead of `uploads` rows |
| `POST /api/uploads/initiate` | unchanged, **+ optional `batchId`** | No `batchId`: a new batch. With one: `INSERT … ON CONFLICT DO NOTHING`, then join it — parallel calls with one id land in one batch; 409 if that batch is `committed` or `abandoned` (D4). `upload_file` row in `staged`; key under `incoming/`; also refuses types outside `ALLOWED_TOP_LEVEL_EXT`. Response **+ `batchId`** |
| `POST /api/uploads/parts/presign` | unchanged | Checks the `key` + `uploadId` pair against a row in `staged` or `uploading`; the first presign moves `staged → uploading` (U1.3); presigns with the public host |
| `GET /api/uploads/{uploadId}/parts` | unchanged | Same row check |
| `POST /api/uploads/complete` | unchanged | S3 completion, then **the Ingest confirm service**. No download, no hashing, no validation. Response `status` is `uploaded`, **+ `batchId`** |
| `POST /api/uploads/abort` | unchanged | Same pair check (404 if no row). A `staged` or `uploading` row: abort the multipart upload, row becomes `error` — "Upload cancelled" — instead of being deleted. Any later status: nothing changes, `{"ok": true}` (D15) |
| `GET /api/uploads` | unchanged | Lists top-level `upload_file` rows, with the new statuses |
| ~~`POST /api/uploads/{id}/reextract`~~ | — | **Removed** — the sweepers recover stuck files |
| ~~`PUT /api/uploads/parts/{uploadId}/{n}`~~ | — | **Removed** with disk mode |

**Error bodies (D14, U12).** `/api/uploads/*` answers errors exactly as Anugrah's API does — `{"detail": "<message>"}`, and FastAPI's 422 `{"detail": [...]}` for an invalid request body — because the Client shows `detail` and marks a row **Duplicate** when that text contains "duplicate". The app's error handlers choose the body by path: `/api/uploads/*` gets `detail`, everything else — `/api/v1/*`, `/internal/*` — keeps the `{"error": {"code", "message"}}` envelope. The messages recorded in `docs/upload-poc-review.md` §2 are kept word for word; the new refusals are:

| Case | Status | `detail` |
|---|---|---|
| Extension not in `ALLOWED_TOP_LEVEL_EXT` | 400 | `Unsupported file type: <filename>` |
| `batchId` of a committed or abandoned batch | 409 | `Batch <batchId> is already <status>.` |
| No row matches the `key` + `uploadId` pair | 404 | `Upload not found.` |
| Presign or complete for a file past `uploading` where U3.5 does not apply | 409 | `Upload is not awaiting completion.` |
| S3 no longer has the multipart upload at `complete` | 400 | `Upload session expired — start the upload again` |

### 5.2 New

| Endpoint | Response |
|---|---|
| `GET /api/v1/uploads/batches/{id}` | Existing Ingest route, **+** `ready_to_add`, `in_progress` counts |
| `GET /api/v1/uploads/batches/{id}/staged` | Existing, **+** `content_hash`, `duplicate_of: {document_id, title, kind}?`. Added candidates are no longer listed — deleted on commit (D12) |
| `POST /api/v1/uploads/batches/{id}/commit` | `{added, skipped: [{staged_id, file_name, reason}], still_in_progress, documents: [document_id]}` |
| `GET /api/v1/library/documents` | `{documents: [{document_id, title, file_name, file_ext, size_bytes, created_at}], total}` |
| `GET /api/v1/library/documents/{id}/download` | `{url, expires_at}` |

The Ingest POC's `confirm` also stays. The `/poc/seed` scenarios use it, and `complete` calls the same service.

### 5.3 `complete`, step by step

```python
def complete(db, req):
    """Anugrah's complete: finish the S3 upload, then hand over exactly as the Ingest confirm does."""
    f = files.find_by_upload(db, req.upload_id, req.key)         # 404 if no row matches both
    if f.status not in ("staged", "uploading"):
        return completed_response(f, enqueued=False)             # U3.5 — idempotent
    try:
        storage.complete_multipart(f.s3_key, f.upload_id, req.parts)             # parts sorted
    except storage.NoSuchUpload:
        if not storage.exists(f.s3_key):                         # an earlier call already completed it?
            raise InvalidInput("Upload session expired — start the upload again")
    except storage.InvalidParts as e:
        raise InvalidInput(str(e))                               # U3.6 — status unchanged
    result = uploads_service.confirm(db, f.file_id)              # conditional UPDATE (staged|uploading → uploaded), commit, THEN enqueue
    return completed_response(result)                            # same fields, status "uploaded", + batchId
```

No `get_object`, no hashing (U3.3). A test spies on `storage` and asserts nothing else is called.

### 5.4 `commit`, step by step — incremental

```python
def commit(db, batch_id):
    """Add whatever is ready in this batch. Safe to call again, and again later."""
    batch = batches.lock(db, batch_id)                           # SELECT … FOR UPDATE: one commit at a time
    added, skipped, removed_keys = [], [], []
    for c in candidates.of_finished_files(db, batch_id):         # both statuses; ordered by file, then entry_index
        if c.status == "rejected":
            candidates.delete(db, c); continue                   # its reason was shown in review
        c.confirmed_title = c.proposed_title                     # metadata is out of scope
        dup = c.duplicate_of_document_id or documents.find_duplicate(db, c)   # re-check under the lock
        if dup:
            c.resolution = "discard"; skipped.append((c, reason(dup)))
        else:
            c.resolution = "create_new"
            doc = documents.insert(db, c)                        # id = uuid5(ns, staged_id)
            storage.copy(c.s3_key, doc.s3_key)                   # staging → processed
            added.append(doc)
        removed_keys.append(c.s3_key)
        candidates.delete(db, c)                                 # D12 — the library row is the record
    audit.write(db, "batch_committed", "upload_batch", batch_id,
                {"added": [d.document_id for d in added], "skipped": len(skipped)})
    if batches.all_files_finished(db, batch_id) and not candidates.any_left(db, batch_id):
        batch.status = "committed"
    db.commit()
    storage.delete_many(k for k in removed_keys if k)            # U8.4 — only after the rows are safe
    return CommitResult(added, skipped, still_in_progress=batches.in_progress_count(db, batch_id))
```

`documents.find_duplicate` checks `content_hash` first, then `title_norm` — including documents added earlier in this same commit, which is how two identical files in one batch become one document. The database's `uq_org_title` is the last line of defence.

**Why the document id is derived, not random.** If the process dies after some copies but before `db.commit()`, the rows roll back but the copied objects remain. With a random id, the re-run would copy to *new* keys and orphan the first copies. `uuid5(namespace, staged_id)` gives the re-run the same id, the same key, and overwrites the same object — the same idea as the Ingest POC's deterministic staging keys.

**Why incremental.** Anugrah's client starts each file's upload on its own, so a small file can finish processing before a large one in the same drop has even started. Waiting for "the whole batch" would need the client to announce how many files are coming — an API change. Instead, **Add to library** adds what's ready, and the Client keeps the button enabled while anything else is still ready to add.

---

## 6. Worker and engine changes

Small; none of the Ingest guarantees change.

| Change | Where | How |
|---|---|---|
| Hash a single document | `workers/clients.py` — download | Feed each 64 KB chunk to `hashlib.sha256` as it's written |
| Hash an archive entry | `engine/file_checks.py` — `extract_streaming` | Same, per entry. `hashlib` is standard library, so the engine stays pure |
| Send the hash | `workers/tasks.py` → `upsert_candidate(content_hash=…)` | New field in the candidate contract |
| Title and duplicates | `app/repositories/candidates.py` — upsert | Same transaction: set `proposed_title` and `title_norm`; look up `documents` by hash, then by `title_norm`; set `duplicate_of_document_id` and `duplicate_kind` |
| Skip Mac and hidden files | `engine/file_checks.py` — `inspect_archive` | Filter `__MACOSX/` and dot-files **before** counting, so `entries_total` and entry positions stay deterministic |
| Folder depth | `engine/file_checks.py` | Deeper than `MAX_ZIP_FOLDER_DEPTH` → entry-level rejection: *"Folders nested too deeply — at most one subfolder is supported"*. Anugrah's POC had this rule; kept |
| Stale uploads | `workers/tasks.py` + `app/services/sweeps.py` | The LLD's stale-upload sweeper, same pattern as the other two: `staged` or `uploading` older than `UPLOAD_ABANDON_SECONDS` → abort multipart → `error` |

**Why Mac files are filtered first.** A zip made on a Mac carries a hidden `__MACOSX/._name.docx` beside every file. Without the filter, every Mac zip would end `partial`, full of confusing rejections. Anugrah's POC already ignored these; the Ingest worker didn't.

---

## 7. The Client

Ported from `Client/` into `client/`. **The upload mechanics stay — chunking, parallel parts, retries, ETags, completion (D17).** The only changes to the existing upload code are the four U10.2 allows: the per-click `batchId`, the abort behaviour, the zip check, and feeding server statuses into rows.

| Area | Status |
|---|---|
| `utils/s3ChunkedUpload.ts` | **Mechanics unchanged** — same calls, same part handling. Takes a `batchId` option, adds it to the `initiate` body, and returns `batchId` with its result |
| `utils/duplicateCheck.ts` | **Unchanged** |
| `utils/uploadsApi.ts` | Gains the Library calls — list, download URL — which replace the unused `fetchStoredUploads` |
| `pages/content-library/ContentLibraryPage.tsx` | Generates one `batchId` (`crypto.randomUUID()`) per **Upload** click and passes it to every `startCloudUpload` of that click (D4); format text from the allowed types; hosts the review panel and the Library table |
| `context/UploadQueueContext.tsx` | Carries the click's `batchId` into each uploader; abort behaviour in line with D15; joins batch statuses to rows by file id (`complete`'s `id`) |
| `components/ui/uploadFile/*` | Status labels per the mapping in §3, fed from the batch poll |
| `utils/contentValidation.ts` | Allowed types from one setting matching `ALLOWED_TOP_LEVEL_EXT`; others shown disabled. **Zip check (D16):** entries against `ALLOWED_ZIP_ENTRY_EXT`; an entry is usable if its extension is allowed, it is not a Mac or hidden file, it is at most `MAX_ZIP_FOLDER_DEPTH` folders deep, and it is not empty. Unusable entries are warnings; the zip is blocked only when no entry is usable |
| Login, shell, routes, dashboard | Unchanged — auth is out of scope |
| **New** `hooks/useBatchStatus.ts` | Polls `GET /api/v1/uploads/batches/{id}` every 2 s after the first `complete` |
| **New** batch review panel | Candidates — ready, rejected with reason, already in the library — and **Add *N* to library**; afterwards, what was added and skipped |
| **Adapted** `components/ui/uploadFile/StoredUploadsTable.tsx` | Becomes the Library table on the Content Library page (D18): rows from `GET /api/v1/library/documents` (`document_id, title, file_name, file_ext, size_bytes, created_at`), newest first, with download. No separate Library page |
| **Removed** | `utils/scanApi.ts`, `ScanResults`, `pages/poc5/`, hidden `.bin` / `.dat` types |

Anugrah's `VITE_SCAN_API_URL` stays as the Client's API base URL — renaming it is cosmetic and not part of the merge.

---

## 8. Configuration — additions

| Setting | POC | Notes |
|---|---|---|
| `S3_PUBLIC_ENDPOINT_URL` | `http://localhost:4566` | Host for presigned URLs; unset in AWS |
| `CORS_ORIGINS` | `http://localhost:5173` | API and bucket |
| `UPLOAD_PART_SIZE_BYTES` | 8 MiB | Anugrah's default; ≥ 5 MiB, startup-validated |
| `MAX_UPLOAD_BYTES` | 5 GiB | Anugrah's limit |
| `PRESIGN_EXPIRES_SECONDS` | 3600 | Anugrah's default |
| `DOWNLOAD_URL_EXPIRES_SECONDS` | 300 | |
| `UPLOAD_ABANDON_SECONDS` | 3600 (prod 86400) | The LLD's stale-upload sweeper: 24 h |
| `POC_USER_ID` | 0 | The system actor (OD-19), until auth exists |
| `MAX_ZIP_FOLDER_DEPTH` | 1 | Anugrah's rule, and the MVP workflows |
| `ALLOWED_TOP_LEVEL_EXT` | `docx,zip` | **D2 — open** |
| `ALLOWED_ZIP_ENTRY_EXT` | `docx` | **D2 — open** |
| `VITE_UPLOAD_CONCURRENCY` (client) | 4 | Anugrah's default |

---

## 9. Verification

### 9.1 New scenarios — `scripts/scenario_u*.py`

Each drives **Anugrah's Upload API exactly as the browser does** — `check-duplicate` → `initiate` → `parts/presign` → PUT parts → `complete` — then the new review and commit calls. Standard library only, like the existing scenarios; each ends with cleanup and `ae.undeliver == 0`.

| # | Scenario | Assert |
|---|---|---|
| **U-S1** | One `valid.docx` → commit | `processed` · commit adds 1 · in `GET /library/documents` · download returns the same SHA-256 · staging empty · no candidates left · one `batch_committed` audit row · **presigned URLs use `localhost`** |
| **U-S2** | `mixed.zip` → commit | `partial` · commit adds the 3 `.docx` · rejected entries listed with reasons |
| **U-S3** | Upload and commit `valid.docx`, then upload it again | the second `check-duplicate` reports a duplicate, and `initiate` returns 409 · the library still has 1 |
| **U-S4** | A zip with `__MACOSX/` entries and a folder two levels deep | Mac entries absent from candidates and from `entries_total` · the deep entry rejected with the depth reason |
| **U-S5** | Two files in one batch; commit after the small one is ready, the large one still uploading; commit again after it's ready | first commit adds 1 with `still_in_progress: 1` · second adds 1 more · a third adds 0 |
| **U-S6** | A 250 MB file, 8 MB parts, 4 at a time | completes · stored SHA-256 equals the source's · API memory flat (UN-1) |

Plus abort: start a multipart upload, send half the parts, abort → `error` "Upload cancelled", multipart aborted, nothing in `incoming/`. Folded into U-S5's script as a third file.

### 9.2 Tests

| Layer | What |
|---|---|
| Upload API | Every kept endpoint accepts **its original request body** — the fixtures in `tests/fixtures/upload_api/` · presign and abort refuse a `key` / `uploadId` pair that doesn't match a row · `complete` never downloads the object · `complete` is idempotent · parallel `initiate`s with one `batchId` → one batch; a committed batch → 409 · aborting a processed file changes nothing and returns `{"ok": true}` · errors on `/api/uploads/*` are `{"detail"}` (422 for bad bodies), on `/api/v1/*` and `/internal/*` the envelope |
| `app/storage.py` | Presigned host is the public one; parts sorted on completion |
| Commit | Incremental; twice adds nothing twice; two identical files → one document; two files with the same title → one document; candidates deleted; audit row written; a crashed-then-re-run commit leaves no orphans; two concurrent commits give one result |
| Engine | Hash equals `sha256` of the bytes; `__MACOSX/` and dot-files skipped before counting; depth rule |
| Client — Vitest | One `batchId` per Upload click, sent by every `initiate` of it · status mapping · zip check warns on bad entries and blocks only when none is usable · review panel enables **Add** only when something is ready |

### 9.3 Manual UI walkthrough

1. `docker compose up`, open `http://localhost:5173`, log in with the mock login
2. Drop `valid.docx` and `mixed.zip` — `mixed.zip` shows warnings for its bad entries but isn't blocked — and click **Upload**; both upload with progress bars, as before the merge
3. Rows move to *Checking…*, then *Ready* and *Partly ready — 3 of 5*
4. The review panel lists 4 ready and 2 rejected with reasons; click **Add 4 to library**
5. The Library table on the Content Library page shows 4 documents; download one and open it
6. Drop `valid.docx` again — refused as a duplicate before it uploads
