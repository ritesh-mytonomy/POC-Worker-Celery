# Tasks — Upload + Ingest Merge

| | |
|---|---|
| **Feature** | `upload-ingest-merge` · revision 1.3 |
| **Read first** | `requirements.md` · `design.md` |
| **Convention** | Same as the Ingest POC: each task names its requirements and ends with a **Done when**. When it holds: tick the box, commit only after the full suite has passed, push, stop. Branch: `feature/upload-merge`. |
| **Schema** | `schema.sql` in this folder — the LLD's tables. It replaces `infra/postgres/init.sql` in task 1.1 |
| **Golden rule** | **Anugrah's Upload API contract does not change** — same paths, same request bodies, same response fields. The only additions are the optional `batchId` in `initiate` and `batchId` in two responses. |

---

## Build order

```
Phase 0  Preconditions + record Anugrah's exact API ──► Checkpoint M0
Phase 1  Schema
Phase 2  API S3 module + bucket set-up
Phase 3  Anugrah's Upload API on the Ingest internals ──► Checkpoint M1 (upload → processed, no browser)
Phase 4  Worker: hashing, Mac files, depth, abandoned sweep
Phase 5  Duplicates, commit, Library API ──► Checkpoint M2 (upload → library, no browser)
Phase 6  Client port ──► Checkpoint M3 (manual UI walkthrough)
Phase 7  Scenarios + run_all ──► Checkpoint M4
Phase 8  Docs
```

Plan mode is worth using for **0.2, 3.4, 5.2 and 6.2**.

---

## Phase 0 · Preconditions

- [x] **0.1 Start from the tidied Ingest POC**
  - `refactor/workers-tidy` merged into `main`; create `feature/upload-merge` from it.
  - **Done when:** the refactored layout is in place and the full suite passes on the new branch.

- [x] **0.2 Read the Upload POC and record its API exactly** — *plan mode*
  - Source: `/home/ritesh/Worker-Celery POC/File Upload source code`. Read `Client/` and `Server/` in full.
  - Record, for every endpoint the Client calls: path, method, request body, response body and error codes — **exactly as the code has them**, field names and casing included. Save one real request and response per endpoint as JSON fixtures in `tests/fixtures/upload_api/`. These become the contract tests in 3.x.
  - Compare the code with `design.md` §2, §5.1 and §7, and report anything that differs or that the design missed.
  - **Report only — change no code.**
  - **Done when:** `docs/upload-poc-review.md` and the fixtures exist, and I've approved the report. **Checkpoint M0.**

---

## Phase 1 · Schema

- [x] **1.1 Adopt the LLD schema** — `schema.sql`, per `design.md` §3
  - Replace `infra/postgres/init.sql` with `schema.sql`. Update the SQLAlchemy models to match, including the renamed and added columns.
  - Adjust the Ingest POC code to the LLD shapes: the initial status is `staged`; confirm accepts `staged` or `uploading`; `/poc/seed` supplies `size_bytes` (from a `HEAD` on the object) and uses the seeded organization; `upload_batch.created_by` is `POC_USER_ID`.
  - **Done when:** `test_models_match_schema` passes against the rebuilt database; every existing test, and every Ingest scenario, still passes.
  - _Requirements: U4.1, U6.4, U8_

---

## Phase 2 · The API's S3 module

- [ ] **2.1 `app/storage.py`** — per `design.md` §4, porting the multipart and presign logic from Anugrah's `s3_storage.py`
  - Two clients: internal and public endpoint.
  - **Done when:** tests show a presigned URL's host is `localhost:4566` while the internal client uses `localstack:4566`, and parts are sorted on completion.
  - _Requirements: U2.3_

- [ ] **2.2 Bucket CORS and lifecycle** — `init-s3.sh`
  - CORS for `http://localhost:5173` exposing `ETag`; `AbortIncompleteMultipartUpload` after 1 day.
  - **Done when:** `awslocal s3api get-bucket-cors` shows `ExposeHeaders: ["ETag"]`; the lifecycle shows three rules.
  - _Requirements: U2.4, U4.3_

---

## Phase 3 · Anugrah's Upload API, on the Ingest internals

Every task here keeps the endpoint's contract. Its **Done when** always includes: the 0.2 fixture request is accepted unchanged, the response has every field of the 0.2 fixture response, and errors have the fixtures' `{"detail"}` shape (U12).

- [ ] **3.1 `check-duplicate` and `initiate`** — `app/routes/upload_api.py`
  - Duplicates matched against the library. `initiate` creates or joins a batch via the optional `batchId`, creates the row and the multipart upload, builds the key, keeps the 413 and 409 responses, adds a 400 for disallowed types.
  - **Done when:** contract tests pass; parallel `initiate` calls with the same new `batchId` land in one batch; a call without one creates a new batch; a `batchId` of a committed or abandoned batch gets 409; errors on `/api/uploads/*` are `{"detail"}` while a `/api/v1/*` error is still the envelope.
  - _Requirements: U1, U6.1, U12_

- [ ] **3.2 `parts/presign` and `parts`**
  - Row check on the `key` + `uploadId` pair.
  - **Done when:** contract tests pass; a pair that doesn't match a row gets 404; a file not `staged` or `uploading` gets 409; the first presign moves `staged` to `uploading`.
  - _Requirements: U2_

- [ ] **3.3 `abort` and `GET /api/uploads`**
  - `abort` sets a `staged` or `uploading` file to `error` — "Upload cancelled" — instead of deleting; for any later status it changes nothing (D15). The list comes from `upload_file`.
  - **Done when:** contract tests pass; an aborted upload's `list_parts` fails; aborting a processed file changes nothing and returns `{"ok": true}`.
  - _Requirements: U4.1, U4.2_

- [ ] **3.4 `complete` — the join point** — *plan mode*
  - Per `design.md` §5.3: S3 completion, then the **existing** Ingest confirm service.
  - **Done when:**
    - contract tests pass; the response `status` is `uploaded` and it includes `batchId`
    - a spy proves `complete` never downloads the object
    - a second call returns the current status without enqueueing
    - bad parts return 400 and leave the file's status unchanged
  - _Requirements: U3_

- [ ] **3.5 Remove what the merge replaces**
  - `reextract`, the disk-mode part route, and nothing else.
  - **Done when:** neither route exists, and nothing in `client/` referenced them — checked by search.
  - _Requirements: U4.4_

- [ ] **3.6 Checkpoint M1 — upload without a browser**
  - A throwaway script calls the endpoints as the browser does and waits for `processed`.
  - **Done when:** it ends `processed` with one candidate. It becomes U-S1 in Phase 7.

---

## Phase 4 · Worker and engine

- [ ] **4.1 Hashing** — per `design.md` §6
  - **Done when:** each candidate's hash equals `sha256` of its bytes, with no second read; the engine purity test still passes.
  - _Requirements: U5.2_

- [ ] **4.2 Mac files and folder depth**
  - **Done when:** a fixture zip with `__MACOSX/` entries and a deep folder gives the expected `entries_total` and one depth rejection; the 16 crash-window tests still pass.
  - _Requirements: U5.3, U5.4_

- [ ] **4.3 Abandoned-upload sweep**
  - **Done when:** a file left `staged` or `uploading` beyond the threshold becomes `error`, with its multipart upload aborted.
  - _Requirements: U4.3_

---

## Phase 5 · Duplicates, commit, Library

- [ ] **5.1 Titles and duplicate marking**
  - At candidate upsert, per `design.md` §6: `proposed_title`, `title_norm`, and `same_content` / `same_title` marking.
  - **Done when:** a candidate matching a library document's hash is marked `same_content`, one matching its title `same_title`, with the document's title available to `…/staged`.
  - _Requirements: U6.2_

- [ ] **5.2 Commit — incremental** — *plan mode*
  - Per `design.md` §5.4, with the derived `document_id`.
  - **Done when:**
    - a commit adds what's ready and reports `still_in_progress`; a later commit adds the rest
    - a second commit with nothing new adds 0, and returns 200
    - two identical files → one document
    - a commit crashed after its copies, then re-run, leaves no orphan objects
    - two concurrent commits of the same batch add each document once
    - two batches committing the same content at once add one document (the organization lock)
    - a `uq_org_title` violation is a skip with the reason "A document with this title is already in the library", not an error
    - candidates are deleted, and one `batch_committed` audit row is written per commit
    - staging objects of removed candidates are gone
    - the batch becomes `committed` once all files are finished and no candidates remain
  - _Requirements: U6.3, U8_

- [ ] **5.3 Batch review and Library routes**
  - `ready_to_add` and `in_progress` on the batch; duplicate markers on candidates; `GET /library/documents`; download URL.
  - **Done when:** tests cover each. **Checkpoint M2:** the M1 script extended with commit and a Library check passes.
  - _Requirements: U7, U9.1, U9.2_

---

## Phase 6 · The Client

- [ ] **6.1 Port and trim**
  - Copy `Client/` into `client/`; remove the scan code and the POC 5 page. Keep `StoredUploadsTable` — 6.4 adapts it. Add a `client` compose service on `127.0.0.1:5173`.
  - **Done when:** the app serves at `http://localhost:5173`, and `npm test` passes.
  - _Requirements: U10.6, U11_

- [ ] **6.2 The upload-code changes U10.2 allows** — *plan mode*
  - One `batchId` per Upload click, passed to every `initiate` of it. The abort behaviour, per D15. The zip check, per D16. New code feeding the batch's statuses into the rows, labelled per `design.md` §3.
  - **The upload mechanics don't change** — chunking, parallel parts, retries, ETags, completion.
  - **Done when:** uploading two files with one click puts both in one batch and both reach `processed`; a zip with a bad entry uploads with a warning, and one with no usable entry is blocked; a diff of the upload code against the Upload POC shows the mechanics unchanged and no changes beyond U10.2's list.
  - _Requirements: U10.1, U10.2_

- [ ] **6.3 Processing status and the review panel**
  - `useBatchStatus` polling; the review panel with **Add to library**.
  - **Done when:** rows move through the states in the browser, and **Add to library** adds the ready documents — and can be pressed again when more are ready.
  - _Requirements: U10.3, U10.4_

- [ ] **6.4 The Library table** — `StoredUploadsTable` adapted, on the Content Library page (D18)
  - **Done when:** added documents appear, newest first, and download opens the file. **Checkpoint M3:** the walkthrough in `design.md` §9.3 passes.
  - _Requirements: U9.3, U10.5_

---

## Phase 7 · Scenarios

- [ ] **7.1 U-S1 to U-S6** — per `design.md` §9.1
  - **Done when:** each passes on its own from a clean stack.

- [ ] **7.2 Add them to `run_all.sh`**
  - **Done when:** the full run passes from a clean stack, laptop awake — every Ingest scenario and the six new ones. **Checkpoint M4.**
  - _Requirements: UN-1–7_

---

## Phase 8 · Docs

- [ ] **8.1 README and runbook**
  - How to open the app; the two ways in (browser, and the scenario scripts); new settings. Add the browser walkthrough to the demo runbook, before the kill-and-resume demo.
- [ ] **8.2 Findings**
  - The public presign endpoint, the ETag CORS rule, hashing moved to the worker, Mac files filtered before numbering, the derived document id, the two URL prefixes to unify in the real build — and the D2 decision.

---

## Traceability

| Requirement | Tasks |
|---|---|
| U1 Initiate | 3.1 |
| U2 Parts | 2.1, 2.2, 3.2 |
| U3 Complete | 3.4 |
| U4 Abort, list, removed, abandoned | 1.1, 2.2, 3.3, 3.5, 4.3 |
| U5 Worker | 4.1, 4.2 |
| U6 Duplicates | 1.1, 3.1, 5.1, 5.2 |
| U7 Review | 5.3 |
| U8 Commit | 1.1, 5.2 |
| U9 Library | 5.3, 6.4 |
| U10 Client | 6.1–6.4 |
| U11 Running | 6.1 |
| U12 Error bodies | 3.1–3.4 |
| UN-1–7 | 0.2, 6.2, 7.1, 7.2 |
