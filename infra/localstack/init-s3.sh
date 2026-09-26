#!/bin/bash
# LocalStack ready hook: create the POC bucket, its lifecycle rules (design.md §8.5a, R10.2) and, for the browser
# upload, its CORS rule (upload-ingest-merge design.md §4, U2.4, U4.3).
set -euo pipefail

BUCKET=clinsync-poc

awslocal s3 mb "s3://${BUCKET}"
awslocal s3api put-bucket-lifecycle-configuration \
  --bucket "${BUCKET}" \
  --lifecycle-configuration '{
    "Rules": [
      {
        "ID": "expire-incoming",
        "Filter": {"Prefix": "ClinSync/incoming/"},
        "Status": "Enabled",
        "Expiration": {"Days": 1}
      },
      {
        "ID": "expire-staging",
        "Filter": {"Prefix": "ClinSync/staging/"},
        "Status": "Enabled",
        "Expiration": {"Days": 7}
      },
      {
        "ID": "abort-incomplete-multipart",
        "Filter": {"Prefix": ""},
        "Status": "Enabled",
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}
      }
    ]
  }'
# The browser PUTs parts straight to S3 and must read each part's ETag to complete the upload: without ETag in
# ExposeHeaders the XHR sees no ETag and s3ChunkedUpload.ts fails the part.
awslocal s3api put-bucket-cors \
  --bucket "${BUCKET}" \
  --cors-configuration '{
    "CORSRules": [
      {
        "AllowedOrigins": ["http://localhost:5173"],
        "AllowedMethods": ["PUT", "GET", "HEAD"],
        "AllowedHeaders": ["*"],
        "ExposeHeaders": ["ETag"],
        "MaxAgeSeconds": 3000
      }
    ]
  }'
