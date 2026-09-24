CREATE TABLE upload_batch (
  batch_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id  UUID NOT NULL,
  status           VARCHAR(16) NOT NULL DEFAULT 'in_progress'
                     CHECK (status IN ('in_progress','staged','committed','abandoned')),
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE upload_file (
  file_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  batch_id         UUID NOT NULL REFERENCES upload_batch(batch_id) ON DELETE CASCADE,
  organization_id  UUID NOT NULL,
  file_name        VARCHAR(512) NOT NULL,
  file_ext         VARCHAR(10)  NOT NULL,
  is_archive       BOOLEAN      NOT NULL,
  s3_key           VARCHAR(1024) NOT NULL,
  status           VARCHAR(16)  NOT NULL DEFAULT 'uploading'
                     CHECK (status IN ('uploading','uploaded','processing',
                                       'processed','partial','rejected','error')),
  status_message   VARCHAR(512),
  detected_type    VARCHAR(32),
  entries_total    INTEGER,
  entries_done     INTEGER NOT NULL DEFAULT 0,
  attempt_count    INTEGER NOT NULL DEFAULT 0,
  claim_token      UUID,
  heartbeat_at     TIMESTAMPTZ,
  uploaded_at      TIMESTAMPTZ,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_file_batch     ON upload_file (batch_id);
CREATE INDEX idx_file_sweep     ON upload_file (status, heartbeat_at);
CREATE INDEX idx_file_reconcile ON upload_file (status, uploaded_at);

CREATE TABLE staged_document (
  staged_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  batch_id           UUID NOT NULL REFERENCES upload_batch(batch_id) ON DELETE CASCADE,
  organization_id    UUID NOT NULL,
  source_file_id     UUID NOT NULL REFERENCES upload_file(file_id) ON DELETE CASCADE,
  source_entry_name  VARCHAR(1024),              -- NULL for a direct upload
  entry_index        INTEGER,                    -- position among file entries (dirs excluded); NULL for direct
  file_name          VARCHAR(512) NOT NULL,
  file_ext           VARCHAR(10)  NOT NULL,
  size_bytes         BIGINT,
  s3_key             VARCHAR(1024),              -- NULL when rejected
  status             VARCHAR(16) NOT NULL CHECK (status IN ('processed','rejected')),
  reject_reason      VARCHAR(512),
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_entry UNIQUE NULLS NOT DISTINCT (source_file_id, source_entry_name)
);
