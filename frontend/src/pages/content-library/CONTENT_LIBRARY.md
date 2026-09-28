# Content Library — Upload Feature Documentation

> **Route:** `/dashboard/content-library`  
> **Page:** `src/pages/content-library/ContentLibraryPage.tsx`  
> **Purpose:** Upload clinical documents and videos for governance verification.

---

## Overview

The Content Library page lets users drag-and-drop or browse files, validates them, uploads them one-by-one with progress feedback, and shows per-file status in a queue. Upload starts **automatically** when files are added — no separate "Upload Files" button.

---

## Architecture

```
src/pages/content-library/
└── ContentLibraryPage.tsx          ← Page logic (state, upload flow, validation orchestration)

src/components/ui/uploadFile/       ← Reusable upload UI components
├── UploadDropzone.tsx              ← Drag & drop / file picker
├── UploadQueue.tsx                 ← File list container
└── UploadQueueRow.tsx              ← Single file row (status, progress, remove)

src/components/ui/                  ← Other shared UI used by upload
├── Button/                         ← Validate Files button
├── ProgressBar/                    ← Upload progress bar
└── icons/                          ← UploadIcon, DocumentIcon, VideoIcon, etc.

src/types/contentLibrary.ts         ← TypeScript types
src/utils/formatBytes.ts            ← Human-readable file sizes
src/utils/contentValidation.ts      ← File format & content validation
src/utils/simulateUpload.ts         ← Mock upload with progress (dev)
```

### Separation of concerns

| Layer | Responsibility |
|-------|----------------|
| **Reusable UI** (`uploadFile/`) | How upload looks — dropzone, queue list, row states |
| **Page** (`ContentLibraryPage`) | What happens — add files, validate, upload, clear queue |
| **Utils** | Shared logic — bytes formatting, validation rules, upload simulation |
| **Types** | Data shapes for queue items and validation results |

---

## User flow

```
1. User opens /dashboard/content-library
2. User drops files or clicks to browse
3. Each file is added to the queue
4. Quick validation runs (extension, empty, max size)
5. Upload starts automatically (one file at a time)
6. Deep validation runs before each upload (if not already validated)
7. Progress bar shows during upload
8. "Uploaded successfully" shown when done
```

Optional: user can click **Validate Files** to run deep validation without waiting for upload.

---

## Page features (`ContentLibraryPage.tsx`)

### State

| State | Type | Purpose |
|-------|------|---------|
| `items` | `UploadQueueItem[]` | All files in the upload queue |
| `isValidating` | `boolean` | Validate Files button loading state |
| `isUploading` | `boolean` | Upload in progress |
| `uploadChainRef` | `Promise<void>` | Serializes upload batches when files are added quickly |

### Handlers

| Handler | Description |
|---------|-------------|
| `addFiles(files)` | Creates queue items, runs quick validation, appends to queue, starts auto-upload |
| `uploadQueuedItems(targetItems)` | Validates (if needed) then uploads files sequentially |
| `enqueueUpload(targetItems)` | Chains uploads so concurrent adds don't conflict |
| `handleRemove(id)` | Removes a file from the queue |
| `handleClearQueue()` | Clears all items and resets upload chain |
| `handleValidate()` | Manually runs deep validation on unvalidated queued files |

### Environment

| Variable | Purpose |
|----------|---------|
| `VITE_MAX_UPLOAD_FILE_SIZE_MB` | Optional max file size in MB (used in quick validation and dropzone label) |

---

## Reusable components

### `UploadDropzone`

**Path:** `src/components/ui/uploadFile/UploadDropzone.tsx`

| Prop | Type | Description |
|------|------|-------------|
| `onFilesSelected` | `(files: FileList) => void` | Called when files are dropped or selected |
| `accept` | `string` | HTML `accept` attribute for file input |
| `formatsLabel` | `string` | Helper text below dropzone (formats + max size) |

**Features:**
- Drag and drop with active state styling
- Click to browse (hidden `<input type="file" multiple>`)
- Keyboard accessible (`Enter` / `Space` to open picker)
- Uses `UploadIcon` from `icons`

---

### `UploadQueue`

**Path:** `src/components/ui/uploadFile/UploadQueue.tsx`

| Prop | Type | Description |
|------|------|-------------|
| `title` | `string` | Section heading (e.g. "Uploaded Video & Documents") |
| `items` | `UploadQueueItem[]` | Files to display |
| `onRemove` | `(id: string) => void` | Remove file callback |

Renders a list of `UploadQueueRow` components.

---

### `UploadQueueRow`

**Path:** `src/components/ui/uploadFile/UploadQueueRow.tsx`

| Prop | Type | Description |
|------|------|-------------|
| `item` | `UploadQueueItem` | File data and status |
| `onRemove` | `(id: string) => void` | Remove file callback |

**Per-status UI:**

| Status | Display |
|--------|---------|
| `queued` | Grey filename, "Queued for upload", file size |
| `uploading` | Progress bar, percentage, file size |
| `success` | Green checkmark, "Uploaded successfully" |
| `validation-failed` | Red alert, error message |
| `upload-failed` | Red alert, "Upload failed" |

**Icons:**
- `DocumentIcon` — default file
- `VideoIcon` — when validation found video sections (DOCX)
- `TrashIcon` — remove button (disabled while uploading)

**Uses:** `formatBytes`, `ProgressBar`, icons from `@/components/ui/icons`

---

## Types (`src/types/contentLibrary.ts`)

### `SupportedExtension`
`'docx' | 'pdf' | 'html' | 'zip'`

### `UploadStatus`
`'queued' | 'uploading' | 'success' | 'validation-failed' | 'upload-failed'`

### `UploadQueueItem`

```ts
{
  id: string;
  file: File;
  name: string;
  extension: SupportedExtension | null;
  size: number;
  status: UploadStatus;
  progress: number;        // 0–100 during upload
  validated: boolean;      // deep validation has run
  validation?: FileValidationResult;
}
```

### `FileValidationResult`

```ts
{
  valid: boolean;
  errors: string[];
  videoSections?: VideoSection[];   // extracted from DOCX
  zipEntries?: ZipEntryResult[];    // per-file results inside ZIP
}
```

### `VideoSection`
Parsed from DOCX text matching pattern: `Video N - Title:`

### `ZipEntryResult`
Per-file validation result inside a ZIP archive.

---

## Utilities

### `formatBytes` — `src/utils/formatBytes.ts`

Converts byte count to human-readable string (`B`, `KB`, `MB`, `GB`).

**Used in:**
- `ContentLibraryPage` — max size error messages, formats label
- `UploadQueueRow` — file size display

```ts
formatBytes(44300)  // → "43.3 KB"
formatBytes(0)      // → "0 B"
```

---

### `contentValidation` — `src/utils/contentValidation.ts`

#### Exports

| Export | Purpose |
|--------|---------|
| `SUPPORTED_EXTENSIONS` | `['docx', 'pdf', 'html', 'zip']` |
| `ACCEPTED_FILE_INPUT` | `'.docx,.pdf,.html,.htm,.zip'` for file input |
| `getSupportedExtension(fileName)` | Returns extension or `null` (`.htm` → `html`) |
| `validateUploadFile(file)` | Full async validation by file type |
| `extractVideoSections(text)` | Finds video sections in DOCX text |
| `getAnalysisItemCount(result)` | Counts analyzable items (for future summary UI) |

#### Validation by file type

| Type | Checks |
|------|--------|
| **DOCX** | Not empty (mammoth), extracts video sections from text |
| **PDF** | Valid `%PDF-` header |
| **HTML** | Not empty, parses without DOMParser error |
| **ZIP** | Valid archive, each entry validated individually |

#### ZIP rules
- Ignores `__MACOSX/` and dotfiles
- Max nesting depth: 1 subfolder
- Only supported types inside ZIP (no nested ZIP)
- Each entry validated with same rules as standalone files

#### Dependencies
- `mammoth` — DOCX text extraction
- `jszip` — ZIP parsing

---

### `simulateFileUpload` — `src/utils/simulateUpload.ts`

Mock upload for development (no real API yet).

- Increments progress every 150ms by 15–35%
- Calls `onProgress(progress)` until 100%
- Used by `ContentLibraryPage` during upload loop

**Replace with real API call when backend is ready.**

---

## Validation levels

### 1. Quick validation (on file add)

Runs immediately in `addFiles()` before upload:

- Unsupported extension
- Empty file (0 bytes)
- Exceeds `VITE_MAX_UPLOAD_FILE_SIZE_MB` (if set)

Failed files get `status: 'validation-failed'` immediately.

### 2. Deep validation (before upload or via button)

Runs via `validateUploadFile()`:

- DOCX content parsing
- PDF header check
- HTML parse check
- ZIP archive + inner file validation

Triggered automatically during upload, or manually via **Validate Files**.

---

## Upload behavior

1. Files upload **one at a time** (sequential)
2. Only one file shows progress bar at a time
3. Others stay `queued` until their turn
4. Multiple drop batches are chained via `uploadChainRef`
5. Upload is **simulated** — no network request yet

---

## Routing & navigation

| Entry | Path |
|-------|------|
| Sidebar | Content Library → `/dashboard/content-library` |
| Dashboard button | Upload Content → `/dashboard/content-library` |
| Route config | `src/routes/private.routes.tsx` |

**Route handle (navbar):**
- Title: "Upload Content"
- Subtitle: "Add new patient guidance, training videos, and regulatory materials to queue verification."

Protected by `ProtectedRoute` inside `DashboardLayout`.

---

## UI actions

| Action | Location | Behavior |
|--------|----------|----------|
| Drop / browse files | UploadDropzone | Add files + auto-upload |
| Remove file | Trash icon per row | Remove from queue |
| Clear Queue | Bottom left | Clear all files, reset state |
| Validate Files | Bottom right button | Run deep validation on pending files |

---

## File map (everything involved)

```
src/pages/content-library/ContentLibraryPage.tsx
src/components/ui/uploadFile/UploadDropzone.tsx
src/components/ui/uploadFile/UploadQueue.tsx
src/components/ui/uploadFile/UploadQueueRow.tsx
src/components/ui/Button/index.tsx
src/components/ui/ProgressBar/index.tsx
src/components/ui/icons/index.tsx
src/types/contentLibrary.ts
src/utils/formatBytes.ts
src/utils/contentValidation.ts
src/utils/simulateUpload.ts
src/routes/private.routes.tsx
src/components/layout/UploadContentButton.tsx
src/components/layout/Sidebar.tsx
```

---

## Future improvements

- [ ] Replace `simulateFileUpload` with real API upload
- [ ] Restore validation summary panel after upload
- [ ] Persist queue across page refresh
- [ ] Cancel in-progress upload
- [ ] Retry failed uploads
