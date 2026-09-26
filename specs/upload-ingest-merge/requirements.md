# Requirements — Upload + Ingest Merge

| | |
|---|---|
| **Feature** | `upload-ingest-merge` |
| **Revision** | 1.3 — review decisions on batches, error bodies, abort, the client zip check and what "unchanged" means; the Library table on the Content Library page (§6). 1.2: tables follow the LLD data model (`schema.sql`). 1.1: Anugrah's upload API kept as it is |
| **Status** | Ready to build, pending decision D2 |
| **Spec files** | `requirements.md` (this) · `design.md` · `tasks.md` · **`schema.sql`** |
| **Inputs** | File Upload POC (Anugrah, `Client/` + `Server/`) · Ingest Worker POC (`poc-complete`, plus `refactor/workers-tidy`) · ClinSync LLD Module 2 |
| **Notation** | EARS: *WHEN / IF / WHILE / WHERE … THE [component] SHALL …* |

---

## 1. Introduction

Two POCs each prove half of one pipeline:

- **File Upload POC** — a browser uploads files of up to 5 GB straight to S3 in parallel, resumable chunks. Its processing after the upload is weak: the worker reads the database directly, has no crash recovery, reads whole zips into memory, and has no zip guards.
- **Ingest Worker POC** — a confirmed file in `incoming/` is claimed, verified, extracted and staged, and survives crashes, duplicates and lost messages. It starts from a test script, not a browser.

This feature **merges** them into one flow:

> **A user drops files in the browser → they upload straight to S3 → the Celery worker checks and stages them → the user adds them to the library → they appear in the Library.**

### 1.1 The merge principle

**Anugrah's upload API and browser upload stay as they are.** Same paths, same request bodies, same chunked uploader. What changes is only:

1. **What happens behind `complete`** — it hands the file to the Ingest worker instead of hashing it and calling `extract_zip`
2. **What's stored** — the Ingest tables replace the single `uploads` table; every file lands in `incoming/`
3. **The status values** the client shows — the Ingest states replace `stored` / `extracting`
4. **New endpoints alongside** — batch review, **Add to library**, and the Library

The Upload POC's `complete` plays the role of the Ingest POC's `confirm`, and the Ingest worker replaces `extract_zip`.

### 1.2 Scope

**In scope**
- Anugrah's upload API and client, kept, with the internal changes above
- Everything from `complete` onward: the Ingest worker, its guarantees unchanged
- Grouping a drop into a batch, reviewing the results, adding to the library
- A Library table, on the Content Library page, with download
- Abandoned-upload cleanup

**Out of scope**
- **Authentication and organizations** — a fixed POC organization, as in the Ingest POC
- **Scanning** — the Upload POC's `/api/scan` and all its LLM code
- Metadata confirmation — titles default to the file name
- Replace and versioning — every library document is version 1
- The Upload POC's offline disk-mode S3 and SQLite
- AWS deployment

### 1.3 Glossary

| Term | Meaning |
|---|---|
| **Client** | The React app, ported from the Upload POC's `Client/` |
| **Upload API** | Anugrah's `/api/uploads/*` endpoints, kept |
| **API** | The FastAPI app — still the only component that touches PostgreSQL |
| **Worker** | The Ingest POC's Celery worker |
| **Batch** | The files from one drop |
| **Candidate** | A `staged_document` row — one possible library document |
| **Library document** | A `documents` row, with its file in `ClinSync/processed/` |
| **Commit** | Adding a batch's ready candidates to the library |

---

## 2. Decisions

| # | Decision | Position | Status |
|---|---|---|---|
| D1 | Base repository | The **Ingest Worker POC repo**. The Upload POC's client and upload routes move into it | Decided |
| D2 | **Supported file types** | Default: **`.docx`, and `.zip` of `.docx`**, per the Product Brief. The two POCs disagree — the Upload POC also takes PDF, HTML and zips of PDFs. Both lists are settings, so changing them is configuration, not code | **Open — Product (OD-11)** |
| D3 | **Keep Anugrah's upload API** | Same paths, same request bodies. Two *additive* changes only: `initiate` accepts an optional `batchId`, and responses carry `batchId`. Old calls without it still work | Decided |
| D4 | Batches | One batch per **Upload click**. The Client generates a `batchId` (UUID) and sends it with every `initiate` of that click — they run in parallel. The API creates the batch on first sight and adds the rest to it (rev 1.3) | Decided |
| D5 | Getting into the library | An **Add to library** action. It's **incremental**: it adds whatever is ready now, and can be pressed again when more files finish | Decided |
| D6 | Duplicates | **As Anugrah's POC does today:** `check-duplicate` and `initiate` refuse a file whose name and size match — now matched against the **library**. **Added:** after processing, a candidate whose content hash is already in the library is marked, and commit skips it | Decided |
| D7 | Hashing | The **worker** computes each document's SHA-256 as it downloads or extracts it. `complete` no longer re-reads the object | Decided |
| D8 | Every file through the worker | All uploads land in `incoming/` — no file goes straight to `processed/` | Decided (24 Sep) |
| D9 | **Tables follow the LLD** | `schema.sql` implements the LLD's M1 organization table and M2.1 upload and library tables. The Ingest POC's operational columns — claim token, heartbeat, attempts — are kept and marked for folding back into the LLD | Decided |
| D10 | Statuses follow LLD M2.5 | `upload_file`: `staged → uploading → uploaded → processing → processed · partial · rejected`, and `error`. There is no `cancelled`: an aborted or abandoned upload ends `error` with a message, as the LLD's `/fail` and stale-upload sweeper do | Decided |
| D11 | Duplicates as the LLD defines them | `documents` is unique on `(organization_id, title_norm)`. A candidate whose title matches is `same_title`; one whose content hash matches is `same_content` — a POC addition to the LLD's `duplicate_kind`. Both are discarded at commit while replace is out of scope | Decided |
| D12 | Candidates are deleted on commit | As the LLD's ERD states. The library row and the audit row are the record | Decided |
| D13 | Ids and users | UUID keys throughout, per OD-18's recommendation. `created_by` and `version_uploaded_by` hold `POC_USER_ID = 0`, the system actor of OD-19, until auth exists | Decided for the POC — OD-18 and OD-19 still need confirming for the real build |
| D14 | Error bodies | `/api/uploads/*` keeps Anugrah's error bodies exactly: `{"detail": "…"}`, FastAPI's 422 for an invalid request body, and "Duplicate" in every duplicate refusal — the Client shows `detail` and recognises duplicates by that word. The `{"error": {"code", "message"}}` envelope applies to `/api/v1/*` and `/internal/*` only (rev 1.3) | Decided |
| D15 | Abort after the upload | `abort` acts only on `staged` or `uploading` files. For any later status it changes nothing and returns the normal `{"ok": true}` — the Client sends abort when a finished row is removed or the queue is cleared (rev 1.3) | Decided |
| D16 | Client zip check | Checks entries against the allowed-entry list; bad entries are warnings, not blocks; only a zip with no usable entry is blocked. The Worker stays the authority on entries (rev 1.3) | Decided |
| D17 | What "unchanged" means for the Client | The upload **mechanics** are unchanged — chunking, parallel parts, retries, ETags, completion. The changes allowed around them are listed in U10.2 (rev 1.3) | Decided |
| D18 | Where the Library lives | On the existing Content Library page, as Anugrah's `StoredUploadsTable` adapted to `GET /api/v1/library/documents`. No separate Library page (rev 1.3) | Decided |

---

## 3. Requirements

### U1 · Starting an upload — `initiate`, kept

**User story** — As a content operator, I want each file registered when its upload starts, and my drop kept together, so that I can review it as one.

1. `POST /api/uploads/initiate` SHALL keep its current path, request body and response fields.
2. WHEN called without a `batchId` THE API SHALL create a new batch. WHEN called with a `batchId` THE API SHALL create that batch if it does not exist (`INSERT … ON CONFLICT DO NOTHING`) and add the file to it, so that parallel `initiate` calls with the same id land in one batch (D4). IF that batch is `committed` or `abandoned` THEN THE API SHALL refuse with 409. The response SHALL include `batchId`.
3. THE API SHALL create an `upload_file` row in status `staged` and start an S3 multipart upload to that file's `incoming/` key. The first presign request SHALL move it to `uploading` (LLD M2.5).
4. AS TODAY, THE API SHALL refuse a file over the size limit with 413, and a duplicate with 409 — a duplicate now meaning a **library** document with the same name, case-insensitive, and size.
5. THE API SHALL also refuse, with 400, a file whose extension is not in `ALLOWED_TOP_LEVEL_EXT`.
6. THE API SHALL build every S3 key itself. The returned `key` is opaque to the Client.

### U2 · Uploading parts — kept

1. `POST /api/uploads/parts/presign` and `GET /api/uploads/{uploadId}/parts` SHALL keep their paths and bodies.
2. THE API SHALL look up the file row by `uploadId` and `key`, and SHALL refuse with 404 if no row matches both, and with 409 if the file is not `staged` or `uploading`. Today they accept any pair; the request doesn't change, only the check.
3. EVERY presigned URL SHALL be addressable from the browser, not only from inside Docker.
4. THE bucket SHALL expose the `ETag` header to the Client's origin.
5. THE API SHALL NOT receive or proxy any file bytes.

### U3 · Completing an upload — kept, and the join point

1. `POST /api/uploads/complete` SHALL keep its path and request body.
2. WHEN called THE API SHALL complete the S3 multipart upload, then move the file from `staged` or `uploading` to `uploaded` and enqueue it, **exactly as the Ingest POC's confirm does**.
3. THE API SHALL NOT download, hash or validate the object during `complete` (D7). The duplicate-by-hash and content checks it used to do there move to the Worker.
4. THE response SHALL keep its fields. Its `status` value becomes `uploaded`, and it SHALL include `batchId`.
5. IF called again for a file already past `uploading` THEN THE API SHALL return the current status and SHALL NOT enqueue again.
6. IF S3 rejects the completion THEN THE API SHALL return 400, and the file's status SHALL NOT change, so the Client can retry.

### U4 · Cancelling, listing, and what's removed

1. `POST /api/uploads/abort` SHALL keep its path, body and success response `{"ok": true}`, and SHALL refuse with 404 a pair that matches no row (U2.2). WHEN the file is `staged` or `uploading` THE API SHALL abort the multipart upload and set the file to `error` with `status_message` "Upload cancelled" — instead of deleting the row (D10). WHEN the file is in any later status THE API SHALL change nothing and return `{"ok": true}` (D15).
2. `GET /api/uploads` SHALL keep its path and response shape, now listing top-level `upload_file` rows, with the new status values.
3. THE stale-upload sweeper (LLD M2.5) SHALL abort the multipart upload of any file still `staged` or `uploading` after `UPLOAD_ABANDON_SECONDS`, and set it to `error` with "Upload was not completed". The bucket's lifecycle rule aborting incomplete multipart uploads after 1 day is the backstop.
4. `POST /api/uploads/{id}/reextract` and the CLI `re_extract_zip` SHALL be removed — the sweepers now recover stuck files. The disk-mode part route SHALL be removed with disk mode. Nothing in the Client calls either.

### U5 · Processing — the Ingest worker

1. EVERY Ingest POC requirement, R1–R15 in `specs/ingest-worker-poc/requirements.md`, SHALL continue to hold unchanged.
2. THE Worker SHALL compute a SHA-256 for each document it stages — the whole file for a single document, each entry for an archive — while downloading or extracting it, with no second read.
3. THE Worker SHALL skip archive entries under `__MACOSX/` and entries whose file name starts with `.`. They SHALL NOT become candidates and SHALL NOT count towards `entries_total`.
4. IF an archive entry sits more than `MAX_ZIP_FOLDER_DEPTH` folders deep THEN THE Worker SHALL reject that entry with a reason; the archive continues.

### U6 · Duplicates

1. `POST /api/uploads/check-duplicate` SHALL keep its path, body and response, and now match against library documents by name, case-insensitive, and size.
2. WHEN the API records a `processed` candidate THE API SHALL set its `proposed_title` to the file name without extension, its `title_norm`, and — if a library document matches — `duplicate_of_document_id` with `duplicate_kind` `same_content` (same hash) or `same_title` (same `title_norm`). Content is checked first.
3. WHEN two candidates committed together share a hash or a `title_norm` THE API SHALL add only the first.
4. THE `documents` table SHALL enforce one document per `(organization_id, title_norm)`, as the LLD's `uq_org_title` does. Content duplicates SHALL be checked at commit, under a per-organization lock held for the commit's transaction. A `uq_org_title` violation at commit SHALL be a skip with the reason "A document with this title is already in the library", not an error.

### U7 · Reviewing a batch — new

1. `GET /api/v1/uploads/batches/{id}` — already in the Ingest POC — SHALL additionally return how many candidates are ready to add, and how many files are still in progress.
2. `GET …/staged` SHALL additionally return each candidate's duplicate marker — kind, and the matching document's title. Candidates already added are no longer listed: they're deleted on commit (D12).

### U8 · Adding to the library — new

1. WHEN the Client calls `POST /api/v1/uploads/batches/{id}/commit` THE API SHALL, for each `processed` candidate of a finished file: set `confirmed_title` from `proposed_title`, set `resolution` to `create_new` — or to `discard` if marked duplicate — create a library document for `create_new` and copy its file to `processed/{org}/{document_id}/v1_{file_name}`, then **delete the candidate** (D12). Rejected candidates of finished files are deleted with it.
2. THE commit SHALL be **incremental**: it adds what's ready now, leaves files still in progress for a later commit, and SHALL return how many were added, how many skipped and why, and how many are still in progress.
3. THE commit SHALL be idempotent: a candidate already added SHALL NOT be added twice, and a crash mid-commit followed by a re-run SHALL leave no orphaned objects.
4. AFTER a commit THE API SHALL delete the staging objects of the candidates it removed, write one `audit_log` row with action `batch_committed`, and set the batch to `committed` once all its files are finished and no candidates remain.
5. IF nothing is ready to add THEN THE API SHALL return 200 with `added: 0`, not an error.

### U9 · The Library — new

1. `GET /api/v1/library/documents` SHALL list library documents, newest first, with title, file name, type, size and date added.
2. `GET /api/v1/library/documents/{id}/download` SHALL return a short-lived presigned download URL.
3. THE Client SHALL show that list as the Library table on the Content Library page (`/dashboard/content-library`) — Anugrah's `StoredUploadsTable`, adapted (D18) — refresh it after a commit, and offer download.

### U10 · The Client

1. THE Client's upload mechanics SHALL stay as they are — chunking, parallel parts, retries, ETags and completion — calling the same Upload API (D17).
2. THE only changes to the existing upload code SHALL be:
   - generating one `batchId` per Upload click and passing it to every `initiate` of that click (D4)
   - the abort behaviour, in line with D15
   - the zip check (U10.5, D16)
   - new code that feeds the batch's server statuses into the queue rows (U10.3)
3. AFTER a file's upload completes THE Client SHALL poll the batch every ~2 s and show each file's processing status, including `entries_done / entries_total` for archives.
4. THE Client SHALL show the batch's candidates — ready, rejected with reason, or duplicate — with an **Add to library** button, enabled while anything is ready to add. After a commit it SHALL show what was added and skipped, from the commit response.
5. THE Client SHALL accept only the API's allowed types, and show others as unavailable rather than hiding them. Its zip check SHALL use the allowed-entry list, show bad entries as warnings without blocking the upload, and block only a zip with no usable entry (D16).
6. THE Client SHALL NOT contain the scan code or the POC 5 page. The stored-uploads table SHALL be kept and adapted into the Library table (U9.3, D18).

### U11 · Running it

1. `docker compose up` SHALL also start the Client, at `http://localhost:5173`.
2. Every published port SHALL stay bound to `127.0.0.1`.

### U12 · Error bodies

1. EVERY error from `/api/uploads/*` SHALL have Anugrah's body exactly: `{"detail": "<message>"}`, and 422 with FastAPI's `{"detail": [...]}` for an invalid request body. Existing messages SHALL be kept word for word, and every duplicate refusal SHALL contain "Duplicate" (D14).
2. EVERY error from `/api/v1/*` and `/internal/*` SHALL keep the Ingest envelope `{"error": {"code", "message"}}`.
3. Contract tests SHALL assert both shapes.

---

## 4. Non-functional requirements

| ID | Requirement | Target |
|---|---|---|
| UN-1 | API memory during a 1 GB upload | Idle ± 50 MB — no bytes through the API |
| UN-2 | A 250 MB upload on the local stack | Completes; the stored object's SHA-256 equals the source's |
| UN-3 | `complete` to the file showing `processing` | < 3 s on an idle worker |
| UN-4 | Duplicate library documents after any scenario | 0 |
| UN-5 | Staging objects left for added candidates | 0 |
| UN-6 | Ingest scenarios S1–S8 and S5b | Still pass |
| UN-7 | Upload API requests from the Client | Unchanged — same paths and bodies as at the Upload POC's last commit, apart from the optional `batchId` |

---

## 5. Definition of done

1. Scenarios U-S1 to U-S6 in `design.md` §9 pass, and are added to `run_all.sh`.
2. `run_all.sh` passes in full from a clean stack, laptop awake.
3. The manual UI walkthrough in `design.md` §9.3 passes.
4. The full test suite passes with the coverage gate.
5. A diff of the Client's upload code against the Upload POC shows the upload mechanics unchanged, and no changes beyond those U10.2 allows.

---

## 6. Revision history

| Rev | Change | Where |
|---|---|---|
| 1.1 | Anugrah's upload API kept as it is; only what happens behind `complete` changes, plus new endpoints for review and the Library | — |
| 1.2 | Tables follow the LLD data model, in `schema.sql` (D9–D13): LLD M2.5 statuses with no `cancelled` — an aborted or abandoned upload ends `error`; duplicates by `uq_org_title` plus `same_content`; candidates deleted on commit; `POC_USER_ID` | D9–D13 · U1.3, U2.2, U3, U4, U6, U7, U8 · design §3 · task 1.1 |
| 1.3 | Review of the Upload POC (`docs/upload-poc-review.md`). **Batches:** one client-generated `batchId` per Upload click, sent by every parallel `initiate`; the API creates the batch on first sight, and refuses a committed or abandoned batch with 409 (D4, U1.2). **Error bodies:** `/api/uploads/*` keeps `{"detail"}` and 422 exactly, with "Duplicate" in duplicate texts; the envelope is for `/api/v1/*` and `/internal/*` only (D14, U12). **Abort** acts only on `staged`/`uploading`; later it is a no-op `{"ok": true}` (D15, U4.1). **Client zip check** uses the allowed-entry list, warns on bad entries, blocks only a zip with no usable entry (D16, U10.5). **"Unchanged"** now means the upload mechanics; U10.2 lists the allowed changes (D17, U10.1, U10.2, DoD 5). **Library** is the adapted `StoredUploadsTable` on the Content Library page, not a new page (D18, U9.3, U10.6). Consistency fixes carried over from 1.2: presign checks and moves `staged` files (design §5.1, task 3.2); failed completion leaves the status unchanged (task 3.4); no `added` field on candidates (design §1, §5.2); `init.sql` is replaced, not altered (design §2); the walkthrough clicks **Upload**. **Commit concurrency:** commit takes a per-organization `pg_advisory_xact_lock` before the batch lock, so two batches can't both add the same content; a `uq_org_title` violation is a skip with "A document with this title is already in the library" (U6.4, design §5.4, task 5.2) | D4, D14–D18 · U1.2, U4.1, U9.3, U10, U12, DoD 5 · design §1, §2, §5, §7, §9 · tasks 3.1–3.4, 6.1, 6.2, 6.4 |
